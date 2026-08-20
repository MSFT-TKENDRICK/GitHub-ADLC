"""ASSERT eval backend — the primary *real* rubric runner (L3).

Microsoft **ASSERT** (*Adaptive Spec-driven Scoring for Evaluation and Regression
Testing*, ``github.com/responsibleai/ASSERT``, PyPI ``assert-ai``, Python >= 3.11) turns a
natural-language spec into a graded eval suite in four stages:

``systematize`` (spec/policy -> a taxonomy of behavioural requirements) -> ``test_set``
(stratified test generation) -> ``inference`` (run the target, optionally capturing OTel
spans) -> ``judge`` (an LLM judge scores each conversation against the original policy and
cites evidence).

That maps onto ADLC almost exactly: ``runs/<run>/spec/spec.md`` -- written by spec-kit --
*is* the natural-language spec ASSERT systematizes, and ``enrichment/rubric.yaml`` is the
behavioural taxonomy we want scored.

**One ASSERT suite per rubric criterion.** ASSERT's config models exactly one ``behavior``
per suite, and its ``scores.jsonl`` rows carry that ``behavior`` name rather than an
arbitrary criterion id. So this adapter renders one ``eval_config.yaml`` per criterion
(``behavior.name`` = the criterion's slug, ``behavior.description`` = its statement,
``context`` = ``spec.md``), runs ``assert-ai run --config ...`` as a **subprocess** for
each, and concatenates every ``scores.jsonl`` into ``evals/assert-results.jsonl``.

**Scoring.** An ASSERT verdict is not a float -- it is
``verdict.dimensions.{policy_violation,overrefusal}`` booleans where ``True`` means the
behaviour was violated. A criterion's score is therefore the share of *successfully
judged* test cases with no violation, and rows whose ``judge_status`` is not ``"ok"`` are
excluded rather than counted as passes.

This module is also the home of the shared normalisation core used by *every* L3 eval
backend (``promptfoo.py``, ``azure.py``). Normalising onto the frozen
:class:`~adlc.ports.RubricScore` is the whole point of the seam: the gate and the report
only ever see a ``RubricScore``.

Availability
------------
``detect()`` is cheap and never raises: it checks for the ``assert-ai`` CLI, for judge
credentials, and for a configured system under test. With none of those present it
reports ``(False, reason)`` and the spine's credential-free deterministic runner stays in
charge. See ``docs/evals.md``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import Rubric, RubricCriterion, RubricScore, Run

__all__ = [
    "NOT_EVALUATED",
    "AssertEvalRunner",
    "CriterionOutcome",
    "CriterionSpec",
    "EvalBackendError",
    "EvalBackendUnavailable",
    "backend_settings",
    "build_rubric_score",
    "coerce_score",
    "invoke_tool",
    "iter_criteria",
    "iter_jsonl",
    "judge_credential_reason",
    "map_records_to_outcomes",
    "render_eval_config",
    "resolve_threshold",
    "resolve_timeout",
    "run_dir_for",
    "slugify",
    "verdict_score",
    "write_score",
]

#: Prefix stamped on the rationale of any criterion the backend did **not** actually
#: evaluate. Such a criterion scores 0.0 and never passes -- we fail closed rather than
#: claim a pass we did not earn. :class:`adlc.adapters.gate.evals.EvalsGate` counts these.
NOT_EVALUATED = "not evaluated"

#: Credential groups that indicate a usable LLM judge, in general. Any *one* complete
#: group is enough. Checked by name only -- never read for their value, never transmitted.
JUDGE_CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("OPENAI_API_KEY",),
    ("ANTHROPIC_API_KEY",),
    ("GEMINI_API_KEY",),
    ("GOOGLE_API_KEY",),
    ("MISTRAL_API_KEY",),
    ("AZURE_API_KEY", "AZURE_API_BASE"),
    ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
)

#: What ASSERT itself reads. It is LiteLLM-backed and uses ``AZURE_API_KEY`` /
#: ``AZURE_API_BASE`` -- deliberately *not* the ``AZURE_OPENAI_*`` names the Azure SDK
#: uses (verified against the repo's ``.env.example`` and ``assert_ai/core/azure_auth.py``).
ASSERT_CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("AZURE_API_KEY", "AZURE_API_BASE"),
    ("ASSERT_AZURE_USE_AAD", "AZURE_API_BASE"),
    ("AZURE_AI_API_KEY", "AZURE_AI_API_BASE"),
    ("OPENAI_API_KEY",),
    ("ANTHROPIC_API_KEY",),
)

#: ``assert-ai run --config <path>`` executes all four stages. ``{config}`` is
#: substituted. Overridable via ``eval.assert.args`` / ``ADLC_ASSERT_ARGS`` so an upstream
#: CLI change is a config edit, not a code change.
DEFAULT_ASSERT_ARGS: tuple[str, ...] = ("run", "--config", "{config}")

#: Console script declared by ``assert-ai``'s ``[project.scripts]``.
ASSERT_BINARIES: tuple[str, ...] = ("assert-ai",)

#: Importable module name, used for the ``python -m`` fallback.
ASSERT_MODULES: tuple[str, ...] = ("assert_ai",)

#: Judged artifact written by the ``judge`` stage, under
#: ``artifacts/results/<suite>/<run>/``.
ASSERT_SCORES_FILE = "scores.jsonl"

#: ``judge_status`` values other than this mean the row was never actually judged.
JUDGE_STATUS_OK = "ok"

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MODEL = "azure/gpt-4o-mini"
MAX_CONTEXT_CHARS = 12_000

# Field aliases used when reading a judged record. Backends differ in spelling, so we read
# tolerantly and record exactly what we found rather than assuming one schema.
_ID_KEYS = (
    "behavior", "behaviour", "criterion_id", "criterionId", "requirement_id",
    "requirementId", "rubric_id", "rubricId", "behavior_id", "taxonomy_id",
    "suite", "test_id", "testId", "id", "name",
)
_SCORE_KEYS = ("score", "judge_score", "judgeScore", "rating", "value")
_BOOL_KEYS = ("passed", "pass", "success", "verdict_pass", "label", "outcome")
_RATIONALE_KEYS = (
    "narrative", "rationale", "reasoning", "reason", "explanation", "justification",
    "judge_rationale", "critique", "comment",
)
_EVIDENCE_KEYS = (
    "highlights", "evidence", "citations", "spans", "span_ids", "spanIds",
    "span_id", "spanId", "trace_id", "traceId", "test_case_id", "source",
)
_NESTED_KEYS = ("verdict", "metadata", "meta", "judgement", "judgment", "result", "grading")


class EvalBackendError(RuntimeError):
    """The backend was selected but could not produce a trustworthy score.

    Raised -- never swallowed into a fabricated pass. The spine records the ``eval`` stage
    as failed and writes no score, so the ``evals`` gate reports ``not_run``, which a
    required gate turns into a build failure.
    """


class EvalBackendUnavailable(EvalBackendError):
    """The backend was explicitly selected but ``detect()`` says it cannot run."""


# ---------------------------------------------------------------------------
# Shared normalisation core (used by every L3 eval backend)
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    """Lowercase ``snake_case`` slug; stable and collision-free for sane criterion ids."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", str(value)).strip("_").lower()
    return slug or "criterion"


@dataclass(frozen=True)
class CriterionSpec:
    """One criterion as declared in ``rubric.yaml`` (``schemas/rubric.schema.json``)."""

    id: str
    statement: str
    weight: float
    kind: str = "deterministic"
    acceptance_refs: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        """Filesystem/YAML-safe form of the id, used as ASSERT's ``behavior.name``."""
        return slugify(self.id)


@dataclass
class CriterionOutcome:
    """What a backend actually observed for one criterion.

    ``score is None`` means *not evaluated* -- emphatically not the same thing as "scored
    zero on merit" -- and is rendered as a failing criterion with a :data:`NOT_EVALUATED`
    rationale.
    """

    score: float | None = None
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)


def iter_criteria(rubric: Rubric) -> list[CriterionSpec]:
    """Normalise ``rubric['criteria']`` into :class:`CriterionSpec` records."""
    specs: list[CriterionSpec] = []
    for index, raw in enumerate(rubric.get("criteria") or []):
        if not isinstance(raw, Mapping):
            continue
        cid = str(raw.get("id") or f"C{index + 1:03d}")
        try:
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        if weight <= 0:
            weight = 1.0
        refs = raw.get("acceptanceRefs") or []
        specs.append(
            CriterionSpec(
                id=cid,
                statement=str(raw.get("statement") or "").strip(),
                weight=weight,
                kind=str(raw.get("kind") or "deterministic"),
                acceptance_refs=tuple(str(r) for r in refs if r),
            )
        )
    return specs


def resolve_threshold(rubric: Rubric, cfg: Config | None = None) -> float:
    """Threshold precedence: the rubric's own value, then ``eval.threshold``, then 0.7."""
    raw: Any = rubric.get("threshold")
    if raw is None and cfg is not None:
        raw = (cfg.raw.get("eval") or {}).get("threshold")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.7
    return min(max(value, 0.0), 1.0)


def coerce_score(value: Any, scale: float = 1.0) -> float | None:
    """Coerce a backend's raw verdict to a 0.0-1.0 score, or ``None`` if unreadable.

    Handles booleans, pass/fail strings, 0-1 floats and Likert scales (``scale`` > 1).
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        token = value.strip().lower()
        if token in {"pass", "passed", "true", "yes", "y", "success", "compliant"}:
            return 1.0
        if token in {"fail", "failed", "false", "no", "n", "failure", "violation"}:
            return 0.0
        try:
            score = float(token)
        except ValueError:
            return None
    else:
        return None
    if scale > 1:
        score = (score - 1.0) / (scale - 1.0)  # Likert 1..scale -> 0..1
    return min(max(score, 0.0), 1.0)


def build_rubric_score(
    rubric: Rubric,
    outcomes: Mapping[str, CriterionOutcome],
    *,
    threshold: float,
    backend: str,
    shared_evidence: Sequence[str] = (),
) -> RubricScore:
    """Fold per-criterion outcomes into the frozen :class:`~adlc.ports.RubricScore`.

    Every criterion in the rubric appears in the output. A criterion with no outcome -- or
    an outcome the backend could not score -- is emitted as ``score: 0.0, passed: False``
    with a :data:`NOT_EVALUATED` rationale. We never invent a pass.
    """
    specs = iter_criteria(rubric)
    criteria: list[RubricCriterion] = []
    weighted = 0.0
    total_weight = 0.0

    for spec in specs:
        outcome = outcomes.get(spec.id)
        if outcome is None or outcome.score is None:
            detail = (
                outcome.rationale
                if outcome and outcome.rationale
                else f"no {backend} record matched criterion '{spec.id}'"
            )
            score = 0.0
            rationale = f"{NOT_EVALUATED} by {backend}: {detail}"
            evidence = list(outcome.evidence) if outcome else []
        else:
            score = min(max(float(outcome.score), 0.0), 1.0)
            rationale = outcome.rationale or f"scored {score:.2f} by {backend}"
            evidence = list(outcome.evidence)

        for ref in shared_evidence:
            if ref not in evidence:
                evidence.append(ref)

        criteria.append(
            {
                "id": spec.id,
                "score": round(score, 4),
                "weight": spec.weight,
                "passed": score >= threshold,
                "rationale": rationale,
                "evidence": evidence,
            }
        )
        weighted += score * spec.weight
        total_weight += spec.weight

    overall = round(weighted / total_weight, 4) if total_weight else 0.0
    return {
        "overall": overall,
        "threshold": threshold,
        "passed": overall >= threshold,
        "criteria": criteria,
    }


def run_dir_for(run: Run, cfg: Config | None = None) -> Path:
    """Locate ``.adlc/runs/<runId>/`` for a run document."""
    run_id = str(run.get("runId") or "").strip()
    if not run_id:
        raise EvalBackendError("run document has no 'runId'; cannot locate the run directory")
    cfg = cfg or Config.load()
    return cfg.run_dir(run_id)


def judge_credential_reason(
    groups: Sequence[Sequence[str]] = JUDGE_CREDENTIAL_GROUPS,
) -> str | None:
    """Return ``None`` when an LLM judge looks configured, else a specific reason."""
    for group in groups:
        if all(os.environ.get(name) for name in group):
            return None
    names = " or ".join("+".join(group) for group in groups)
    return f"no LLM judge credentials in the environment (need {names})"


# ---------------------------------------------------------------------------
# JSONL -> RubricScore mapping
# ---------------------------------------------------------------------------


def iter_jsonl(text: str) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects in a JSONL document, skipping blank/unparseable lines.

    Also tolerates a whole-file JSON array or a ``{"results": [...]}`` wrapper.
    """
    stripped = text.strip()
    if not stripped:
        return
    try:
        blob = json.loads(stripped)
    except json.JSONDecodeError:
        blob = None
    if isinstance(blob, list):
        for item in blob:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(blob, dict):
        for key in ("results", "records", "scores", "judgements", "judgments"):
            inner = blob.get(key)
            if isinstance(inner, list):
                for item in inner:
                    if isinstance(item, dict):
                        yield item
                return
        yield blob
        return
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            yield item


def _lookup(record: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """First present, non-empty value for ``keys``, searching one nesting level deep."""
    for key in keys:
        if key in record and record[key] not in (None, "", [], {}):
            return record[key]
    for nested_key in _NESTED_KEYS:
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            for key in keys:
                if key in nested and nested[key] not in (None, "", [], {}):
                    return nested[key]
    return None


def _as_evidence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value[:512]] if value.strip() else []
    if isinstance(value, Mapping):
        return [json.dumps(value, sort_keys=True)[:512]]
    if isinstance(value, Iterable):
        out: list[str] = []
        for item in value:
            out.extend(_as_evidence(item))
        return out
    return [str(value)]


def verdict_score(record: Mapping[str, Any]) -> float | None:
    """Score one ASSERT ``scores.jsonl`` row, or ``None`` if it was never judged.

    ASSERT reports *violations*, not merit: ``verdict.dimensions`` maps behavioural
    dimensions (``policy_violation``, ``overrefusal``, plus any custom ones) to booleans
    where ``True`` means the behaviour was violated. A clean row scores 1.0; any violation
    scores 0.0. A row whose ``judge_status`` is not ``"ok"`` was never judged and returns
    ``None`` so it can be excluded rather than silently counted as a pass.
    """
    status = record.get("judge_status")
    if isinstance(status, str) and status.strip().lower() != JUDGE_STATUS_OK:
        return None
    verdict = record.get("verdict")
    if not isinstance(verdict, Mapping):
        return None
    dimensions = verdict.get("dimensions")
    if isinstance(dimensions, Mapping):
        flags = [value for value in dimensions.values() if isinstance(value, bool)]
        if flags:
            return 0.0 if any(flags) else 1.0
    nodes = verdict.get("node_judgments")
    if isinstance(nodes, list):
        node_flags = [
            bool(node.get("violated"))
            for node in nodes
            if isinstance(node, Mapping) and isinstance(node.get("violated"), bool)
        ]
        if node_flags:
            return 0.0 if any(node_flags) else 1.0
    return None


def _verdict_rationale(record: Mapping[str, Any]) -> str:
    verdict = record.get("verdict")
    if not isinstance(verdict, Mapping):
        return ""
    parts: list[str] = []
    justifications = verdict.get("dimension_justifications")
    if isinstance(justifications, Mapping):
        parts.extend(
            f"{name}: {text}"
            for name, text in justifications.items()
            if isinstance(text, str) and text.strip()
        )
    narrative = verdict.get("narrative")
    if isinstance(narrative, str) and narrative.strip():
        parts.append(narrative.strip())
    return " | ".join(parts)


def _record_evidence(record: Mapping[str, Any]) -> list[str]:
    """Collect citable evidence: the judge's highlighted spans first, then the test id.

    ASSERT nests its span citations in ``verdict.highlights``, so we read that explicitly
    rather than letting a shallower alias (``test_case_id``) win the generic lookup.
    """
    refs: list[str] = []
    verdict = record.get("verdict")
    if isinstance(verdict, Mapping):
        refs.extend(_as_evidence(verdict.get("highlights")))
    refs.extend(_as_evidence(_lookup(record, _EVIDENCE_KEYS)))
    seen: list[str] = []
    for ref in refs:
        if ref and ref not in seen:
            seen.append(ref)
    return seen


def _iter_ids(record: Mapping[str, Any]) -> Iterator[str]:
    for key in _ID_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            yield value
    for nested_key in _NESTED_KEYS:
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            for key in _ID_KEYS:
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    yield value


def map_records_to_outcomes(
    records: Iterable[Mapping[str, Any]],
    specs: Sequence[CriterionSpec],
    *,
    scale: float = 1.0,
) -> dict[str, CriterionOutcome]:
    """Map judged records onto criterion ids, averaging when several rows match.

    Matching is by criterion id or slug under any known alias, case-insensitively. For
    ASSERT every row of a criterion's suite carries ``behavior`` = the criterion slug, so
    the criterion score is the **share of successfully judged test cases with no
    violation**. Rows whose ``judge_status`` is not ``"ok"`` count as unjudged, never as
    passes. Rows matching no criterion are ignored; criteria matching no row stay
    unevaluated, which :func:`build_rubric_score` fails closed.
    """
    by_id: dict[str, str] = {}
    for spec in specs:
        by_id[spec.id.strip().lower()] = spec.id
        by_id.setdefault(spec.slug, spec.id)
    scores: dict[str, list[float]] = {}
    rationales: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    unreadable: dict[str, int] = {}

    for record in records:
        cid = None
        for raw_id in _iter_ids(record):
            cid = by_id.get(slugify(raw_id)) or by_id.get(str(raw_id).strip().lower())
            if cid:
                break
        if cid is None:
            continue

        score = verdict_score(record)
        if score is None and "verdict" not in record and "judge_status" not in record:
            raw_score = _lookup(record, _SCORE_KEYS)
            score = coerce_score(raw_score, scale=scale) if raw_score is not None else None
            if score is None:
                raw_bool = _lookup(record, _BOOL_KEYS)
                score = coerce_score(raw_bool, scale=1.0) if raw_bool is not None else None
        if score is None:
            unreadable[cid] = unreadable.get(cid, 0) + 1
            continue

        scores.setdefault(cid, []).append(score)
        rationale = _verdict_rationale(record) or _lookup(record, _RATIONALE_KEYS)
        if isinstance(rationale, str) and rationale.strip():
            rationales.setdefault(cid, []).append(rationale.strip())
        for ref in _record_evidence(record):
            bucket = evidence.setdefault(cid, [])
            if ref not in bucket:
                bucket.append(ref)

    outcomes: dict[str, CriterionOutcome] = {}
    for cid, values in scores.items():
        joined = " | ".join(rationales.get(cid, []))
        if len(values) > 1:
            clean = sum(1 for value in values if value >= 1.0)
            prefix = f"{clean}/{len(values)} judged test cases passed without violation"
            joined = f"{prefix}: {joined}" if joined else prefix
        skipped = unreadable.get(cid)
        if skipped:
            joined += f" ({skipped} row(s) were not judged and were excluded)"
        outcomes[cid] = CriterionOutcome(
            score=sum(values) / len(values),
            rationale=joined[:2000],
            evidence=evidence.get(cid, [])[:20],
        )
    for cid, count in unreadable.items():
        if cid not in outcomes:
            outcomes[cid] = CriterionOutcome(
                score=None,
                rationale=f"{count} record(s) matched but none carried a usable judge verdict",
            )
    return outcomes


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class AssertEvalRunner:
    """Drive the ASSERT pipeline over ``spec/spec.md`` + ``enrichment/rubric.yaml``."""

    name = "assert-ai"
    kind = "evals"

    def __init__(self, cfg: Config | None = None) -> None:
        # Adapters are constructed as ``cls()`` by ``adlc.config.select_adapter``; the
        # optional argument exists so tests (and callers that already hold a Config) can
        # avoid a second filesystem probe.
        self._cfg = cfg

    # -- detection --------------------------------------------------------
    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        settings = backend_settings(cfg)
        command = _resolve_command(settings)
        if command is None:
            return False, (
                "ASSERT not installed: no 'assert-ai' console script on PATH and no "
                "importable assert_ai module (pip install assert-ai, or from source "
                "pip install -e '.[otel,langgraph]'; set ADLC_ASSERT_CMD to override)"
            )
        reason = judge_credential_reason(ASSERT_CREDENTIAL_GROUPS)
        if reason:
            return False, f"assert-ai found ({shlex.join(command)}) but {reason}"
        if not settings.get("target"):
            return False, (
                "assert-ai is installed and credentialed but eval.assert.target is not "
                "configured; ASSERT needs a system under test, e.g. "
                "eval.assert.target: {callable: 'mypkg.app:chat'}"
            )
        return True, f"assert-ai available via {shlex.join(command)} with a configured judge"

    # -- execution --------------------------------------------------------
    def run(self, run: Run, rubric: Rubric) -> RubricScore:
        cfg = self._cfg or Config.load()
        available, reason = self.detect(cfg)
        if not available:
            raise EvalBackendUnavailable(reason)

        settings = backend_settings(cfg)
        command = _resolve_command(settings)
        if command is None:  # pragma: no cover - detect() already guarantees this
            raise EvalBackendUnavailable("assert-ai disappeared between detect() and run()")

        rdir = run_dir_for(run, cfg)
        context = spec_context(rdir)
        specs = iter_criteria(rubric)
        if not specs:
            raise EvalBackendError("rubric declares no criteria; nothing for ASSERT to judge")

        evals_dir = rdir / "evals"
        workdir = evals_dir / "assert"
        workdir.mkdir(parents=True, exist_ok=True)
        run_id = str(run.get("runId") or "run")

        # One ASSERT suite per criterion: its config models exactly one `behavior`.
        collected: list[str] = []
        log: list[str] = []
        for spec in specs:
            suite = f"adlc-{slugify(run_id)}-{spec.slug}"
            config_path = workdir / f"{spec.slug}.eval_config.yaml"
            config_path.write_text(
                render_eval_config(spec, suite, run_id, context, settings), encoding="utf-8"
            )
            argv = [*command, *_args(settings, config_path)]
            completed = invoke_tool(argv, cwd=workdir, timeout=resolve_timeout(settings))
            log.append(
                f"$ {shlex.join(argv)}\nexit={completed.returncode}\n"
                f"{completed.stdout}\n{completed.stderr}\n{'-' * 72}"
            )
            scores = collect_scores(workdir, suite)
            if scores is not None:
                collected.append(scores.read_text(encoding="utf-8").strip())

        (workdir / "assert.log").write_text("\n".join(log), encoding="utf-8")
        if not any(collected):
            raise EvalBackendError(
                f"assert-ai produced no {ASSERT_SCORES_FILE} under {workdir}; "
                "see evals/assert/assert.log"
            )

        canonical = evals_dir / "assert-results.jsonl"
        canonical.write_text("\n".join(part for part in collected if part) + "\n", encoding="utf-8")

        outcomes = map_records_to_outcomes(
            iter_jsonl(canonical.read_text(encoding="utf-8")),
            specs,
            scale=float(settings.get("scoreScale", 1.0) or 1.0),
        )
        if not any(outcome.score is not None for outcome in outcomes.values()):
            raise EvalBackendError(
                "assert-ai wrote assert-results.jsonl but no judged row mapped onto a rubric "
                "criterion; refusing to report a score for criteria nothing judged"
            )

        score = build_rubric_score(
            rubric,
            outcomes,
            threshold=resolve_threshold(rubric, cfg),
            backend="ASSERT",
            shared_evidence=["evals/assert-results.jsonl"],
        )
        write_score(evals_dir / "assert-score.json", score)
        return score


# ---------------------------------------------------------------------------
# Config rendering
# ---------------------------------------------------------------------------


def render_eval_config(
    spec: CriterionSpec,
    suite: str,
    run_id: str,
    context: str,
    settings: Mapping[str, Any] | None = None,
) -> str:
    """Render ASSERT's ``eval_config.yaml`` for one rubric criterion.

    Schema verified against ``responsibleai/ASSERT`` ``examples/**/evals/*.yaml``:
    ``suite`` / ``run`` / ``behavior{name,description}`` / ``context`` /
    ``default_model{name}`` / ``pipeline{systematize,test_set,inference,judge}``.
    ``pipeline.inference.target`` is passed through from ``eval.assert.target`` verbatim,
    because only the consuming repo knows what its system under test is.
    """
    settings = settings or {}
    model = str(settings.get("model") or default_model())
    judge_model = str(settings.get("judgeModel") or model)
    target = settings.get("target") or {}
    sample_size = _int(settings.get("sampleSize"), 10)
    categories = _int(settings.get("behaviorCategoryCount"), 8)

    payload: dict[str, Any] = {
        "suite": suite,
        "run": run_id,
        "behavior": {
            "name": spec.slug,
            "description": spec.statement or f"Criterion {spec.id} is satisfied.",
        },
        "context": context,
        "default_model": {"name": model},
        "pipeline": {
            "systematize": {"behavior_category_count": categories},
            "test_set": {
                "prompt": {"sample_size": sample_size},
                "scenario": {"sample_size": sample_size},
            },
            "inference": {
                "target": dict(target) if isinstance(target, Mapping) else target,
                "max_turns": _int(settings.get("maxTurns"), 6),
            },
            "judge": {"model": {"name": judge_model}, "n": _int(settings.get("judgeN"), 1)},
        },
    }
    header = (
        f"# Generated by adlc (L3 assert-ai adapter) for rubric criterion {spec.id!r}.\n"
        "# Do not edit by hand -- regenerate with `adlc eval <run>`.\n"
    )
    return header + dump_yaml(payload)


def dump_yaml(value: Any, indent: int = 0) -> str:
    """Minimal YAML emitter.

    JSON is a subset of YAML 1.2, so every scalar is emitted as JSON. That makes escaping
    exactly correct for the multi-line spec text we embed, at a seam that must never fail
    to import.
    """
    pad = "  " * indent
    if isinstance(value, Mapping):
        if not value:
            return f"{pad}{{}}\n"
        out = ""
        for key, item in value.items():
            if isinstance(item, (Mapping, list)) and item:
                out += f"{pad}{key}:\n{dump_yaml(item, indent + 1)}"
            else:
                out += f"{pad}{key}: {_scalar(item)}\n"
        return out
    if isinstance(value, list):
        if not value:
            return f"{pad}[]\n"
        out = ""
        for item in value:
            if isinstance(item, (Mapping, list)) and item:
                out += f"{pad}-\n{dump_yaml(item, indent + 1)}"
            else:
                out += f"{pad}- {_scalar(item)}\n"
        return out
    return f"{pad}{_scalar(value)}\n"


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def spec_context(rdir: Path) -> str:
    """``spec.md`` is what ASSERT systematizes. Without it there is nothing to score."""
    spec = rdir / "spec" / "spec.md"
    if not spec.is_file():
        raise EvalBackendError(
            f"ASSERT systematizes a natural-language spec but {spec} does not exist; "
            "run `adlc spec <run>` first"
        )
    text = spec.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n\n...[truncated by adlc]"
    return text


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def backend_settings(cfg: Config, key: str = "assert") -> dict[str, Any]:
    """Backend settings from ``.adlc/config.yaml`` (``eval.<key>``), env-overridable."""
    section = (cfg.raw.get("eval") or {}).get(key)
    settings: dict[str, Any] = dict(section) if isinstance(section, Mapping) else {}
    env_prefix = f"ADLC_{key.upper()}_"
    if command := os.environ.get(f"{env_prefix}CMD"):
        settings["command"] = command
    if args := os.environ.get(f"{env_prefix}ARGS"):
        settings["args"] = shlex.split(args)
    if timeout := os.environ.get(f"{env_prefix}TIMEOUT"):
        settings["timeoutSeconds"] = timeout
    if model := os.environ.get(f"{env_prefix}MODEL"):
        settings["model"] = model
    return settings


def _args(settings: Mapping[str, Any], config_path: Path) -> list[str]:
    args = settings.get("args") or DEFAULT_ASSERT_ARGS
    return [str(token).format(config=str(config_path)) for token in args]


def default_model() -> str:
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    return f"azure/{deployment}" if deployment else DEFAULT_MODEL


def resolve_timeout(settings: Mapping[str, Any]) -> int:
    return max(_int(settings.get("timeoutSeconds"), DEFAULT_TIMEOUT_SECONDS), 1)


def _resolve_command(settings: Mapping[str, Any]) -> list[str] | None:
    """Resolve how to invoke ASSERT, without executing anything."""
    override = settings.get("command")
    if isinstance(override, str) and override.strip():
        return shlex.split(override)
    if isinstance(override, (list, tuple)) and override:
        return [str(token) for token in override]
    for binary in ASSERT_BINARIES:
        path = shutil.which(binary)
        if path:
            return [path]
    for module in ASSERT_MODULES:
        if has_module(module):
            return [sys.executable, "-m", module]
    return None


def has_module(name: str) -> bool:
    """Is ``name`` importable? Uses find_spec, so the module is never executed."""
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def invoke_tool(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with no shell, a hard timeout, and captured output."""
    try:
        # argv is always a list and shell is never used, so there is no injection surface.
        return subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvalBackendError(
            f"'{shlex.join(argv)}' exceeded the {timeout}s eval timeout"
        ) from exc
    except OSError as exc:
        raise EvalBackendError(f"could not execute '{shlex.join(argv)}': {exc}") from exc


def collect_scores(workdir: Path, suite: str) -> Path | None:
    """Find ``artifacts/results/<suite>/<run>/scores.jsonl`` for one suite."""
    root = workdir / "artifacts" / "results" / suite
    candidates = sorted(root.rglob(ASSERT_SCORES_FILE)) if root.is_dir() else []
    if not candidates:
        candidates = [
            path for path in sorted(workdir.rglob(ASSERT_SCORES_FILE)) if suite in path.as_posix()
        ]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size:
            return candidate
    return None


def write_score(path: Path, score: RubricScore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(score, indent=2, sort_keys=True) + "\n", encoding="utf-8")
