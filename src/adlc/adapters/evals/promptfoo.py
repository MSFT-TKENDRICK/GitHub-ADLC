"""promptfoo eval backend — the simpler LLM-judge fallback (L3).

Where ASSERT derives its own test taxonomy from ``spec.md``, promptfoo is a thin judge
harness: we render one ``llm-rubric`` assertion per rubric criterion, hand promptfoo the
run's spec/artifact context as the output under test, and read the per-assertion grades
back out of ``results.json``.

```yaml
tests:
  - description: R-perf-01        # the criterion id — how we map results back
    threshold: 0.7                # promptfoo puts threshold on the *test*, not the assert
    assert:
      - type: llm-rubric
        value: |
          <criterion statement>
```

One promptfoo *test* per criterion keeps the mapping unambiguous — the criterion id
travels in the test's ``description``, ``metadata`` and ``vars``, and we match on any of
them when reading results back.

Everything normalises onto the frozen :class:`~adlc.ports.RubricScore` (see ``assert_.py``,
which owns the shared normalisation core).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from adlc.adapters.evals.assert_ import (
    CriterionOutcome,
    CriterionSpec,
    EvalBackendError,
    EvalBackendUnavailable,
    backend_settings,
    build_rubric_score,
    coerce_score,
    invoke_tool,
    iter_criteria,
    judge_credential_reason,
    resolve_threshold,
    resolve_timeout,
    run_dir_for,
    write_score,
)
from adlc.config import Config
from adlc.ports import Rubric, RubricScore, Run

__all__ = ["PromptfooEvalRunner", "map_promptfoo_results"]

#: Default provider. ``echo`` returns the prompt verbatim as the output, which is what we
#: want: the thing being judged is the run's own context, not a fresh model completion.
#: Override with ``eval.promptfoo.providers`` to grade a real endpoint instead.
DEFAULT_PROVIDERS: tuple[str, ...] = ("echo",)

#: Hard cap on the context we hand the judge, so a large spec cannot blow the token
#: budget (and the bill) open.
MAX_CONTEXT_CHARS = 12_000

#: Subprocess env that keeps promptfoo non-interactive in CI.
NONINTERACTIVE_ENV: dict[str, str] = {
    "CI": "true",
    "PROMPTFOO_DISABLE_TELEMETRY": "1",
    "PROMPTFOO_DISABLE_UPDATE": "1",
    "PROMPTFOO_DISABLE_SHARE_WARNING": "1",
}

#: Judge keys promptfoo auto-detects, in its own priority order.
PROMPTFOO_CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("OPENAI_API_KEY",),
    ("ANTHROPIC_API_KEY",),
    ("GEMINI_API_KEY",),
    ("GOOGLE_API_KEY",),
    ("PALM_API_KEY",),
    ("MISTRAL_API_KEY",),
    ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
    ("AZURE_API_KEY", "AZURE_API_BASE"),
)

#: ``promptfoo eval`` exits with this code when the run completed but some assertions
#: failed. That is a legitimate outcome — a criterion is *allowed* to fail — so the
#: results file, never the exit code, is our source of truth.
EXIT_ASSERTION_FAILURES = 100

# Where a per-result record can hide the criterion id, and the grade.
_RESULT_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("results",),
    ("results", "results"),
    ("evalResults",),
    ("data", "results"),
)


class PromptfooEvalRunner:
    """Grade a rubric with promptfoo's ``llm-rubric`` assertion."""

    name = "promptfoo"
    kind = "evals"

    def __init__(self, cfg: Config | None = None, run_dir: Path | None = None) -> None:
        self._cfg = cfg
        self._run_dir = run_dir

    def bind(self, cfg: Config, run_dir: Path) -> None:
        """Called by ``adlc.stages.evals.run_eval`` before :meth:`run`."""
        self._cfg = cfg
        self._run_dir = run_dir

    # -- detection --------------------------------------------------------
    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        settings = backend_settings(cfg, "promptfoo")
        command = _resolve_command(settings)
        if command is None:
            return False, (
                "promptfoo not on PATH (install with `npm install -g promptfoo`); the "
                "`npx promptfoo` fallback is opt-in via eval.promptfoo.useNpx or "
                "ADLC_PROMPTFOO_NPX=1 because it downloads on first use"
            )
        reason = judge_credential_reason(PROMPTFOO_CREDENTIAL_GROUPS)
        if reason:
            return False, f"promptfoo found ({shlex.join(command)}) but {reason}"
        return True, f"promptfoo available via {shlex.join(command)} with a configured judge"

    # -- execution --------------------------------------------------------
    def run(self, run: Run, rubric: Rubric) -> RubricScore:
        cfg = self._cfg or Config.load()
        available, reason = self.detect(cfg)
        if not available:
            raise EvalBackendUnavailable(reason)

        settings = backend_settings(cfg, "promptfoo")
        command = _resolve_command(settings)
        if command is None:  # pragma: no cover - detect() already guarantees this
            raise EvalBackendUnavailable("promptfoo disappeared between detect() and run()")

        rdir = self._run_dir or run_dir_for(run, cfg)
        evals_dir = rdir / "evals"
        workdir = evals_dir / "promptfoo"
        workdir.mkdir(parents=True, exist_ok=True)

        specs = iter_criteria(rubric)
        if not specs:
            raise EvalBackendError("rubric declares no criteria; nothing for promptfoo to judge")

        threshold = resolve_threshold(rubric, cfg)
        config_path = workdir / "promptfoo.yaml"
        config_path.write_text(
            yaml.safe_dump(
                build_promptfoo_config(specs, _context_document(rdir), threshold, settings),
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        output_path = workdir / "results.json"
        output_path.unlink(missing_ok=True)
        argv = [
            *command, "eval",
            "--config", str(config_path),
            "--output", str(output_path),
            "--no-progress-bar",
            "--no-table",
        ]
        env = {**os.environ, **NONINTERACTIVE_ENV}
        completed = invoke_tool(argv, cwd=workdir, timeout=resolve_timeout(settings), env=env)
        (workdir / "promptfoo.log").write_text(
            f"$ {shlex.join(argv)}\nexit={completed.returncode}\n\n"
            f"{completed.stdout}\n{completed.stderr}",
            encoding="utf-8",
        )

        # Exit code 100 means "ran fine, some assertions failed" — a legitimate outcome for
        # a rubric. Exit 1 means the tool itself failed. Either way the results file, not
        # the exit code, is the source of truth; its absence is what we treat as fatal.
        if not output_path.is_file():
            raise EvalBackendError(
                f"promptfoo exited {completed.returncode} without writing {output_path.name}; "
                "see evals/promptfoo/promptfoo.log"
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EvalBackendError(
                f"promptfoo wrote unparseable JSON to {output_path}: {exc}"
            ) from exc

        outcomes = map_promptfoo_results(payload, specs)
        if not any(o.score is not None for o in outcomes.values()):
            raise EvalBackendError(
                "promptfoo produced results.json but no graded result mapped onto a rubric "
                "criterion id; refusing to report a score for criteria nothing judged"
            )

        score = build_rubric_score(
            rubric,
            outcomes,
            threshold=threshold,
            backend="promptfoo",
            shared_evidence=["evals/promptfoo/results.json"],
        )
        write_score(evals_dir / "promptfoo-score.json", score)
        return score


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


def build_promptfoo_config(
    specs: Sequence[CriterionSpec],
    context: str,
    threshold: float,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a promptfoo config: one test, one ``llm-rubric`` assertion, per criterion.

    ``threshold`` sits on the **test**, not on the assertion — verified against
    promptfoo's docs, where ``threshold`` is a test-case property and ``llm-rubric``
    itself takes only ``type``, ``value`` and an optional ``provider``.
    """
    settings = settings or {}
    providers = list(settings.get("providers") or DEFAULT_PROVIDERS)
    config: dict[str, Any] = {
        "description": "adlc rubric evaluation (generated — do not edit by hand)",
        "prompts": ["{{context}}"],
        "providers": providers,
        "tests": [
            {
                "description": spec.id,
                "threshold": threshold,
                "metadata": {"criterionId": spec.id, "weight": spec.weight},
                "vars": {"criterionId": spec.id, "context": context},
                "assert": [
                    {
                        "type": "llm-rubric",
                        "value": spec.statement or f"Criterion {spec.id} is satisfied.",
                    }
                ],
            }
            for spec in specs
        ],
    }
    if grader := settings.get("grader"):
        config["defaultTest"] = {"options": {"provider": grader}}
    return config


def _context_document(rdir: Path) -> str:
    """Assemble what the judge actually reads: the spec plus a run artifact index."""
    parts: list[str] = []
    spec = rdir / "spec" / "spec.md"
    if spec.is_file():
        parts.append(f"# Specification\n\n{spec.read_text(encoding='utf-8', errors='replace')}")
    plan = rdir / "spec" / "plan.md"
    if plan.is_file():
        parts.append(f"# Plan\n\n{plan.read_text(encoding='utf-8', errors='replace')}")

    index: list[str] = []
    for sub in ("patches", "evidence"):
        target = rdir / sub
        if target.is_dir():
            names = sorted(p.relative_to(rdir).as_posix() for p in target.rglob("*") if p.is_file())
            if names:
                index.append(f"## {sub}\n" + "\n".join(f"- {n}" for n in names[:200]))
    if index:
        parts.append("# Run artifacts\n\n" + "\n\n".join(index))

    if not parts:
        parts.append("# Run artifacts\n\n(no spec or artifacts were found for this run)")
    document = "\n\n".join(parts)
    if len(document) > MAX_CONTEXT_CHARS:
        document = document[:MAX_CONTEXT_CHARS] + "\n\n…[truncated by adlc]"
    return document


# ---------------------------------------------------------------------------
# results.json → outcomes
# ---------------------------------------------------------------------------


def _result_records(payload: Any) -> list[Mapping[str, Any]]:
    """Find the per-test result list. promptfoo has moved it between releases, so we
    probe the known locations rather than pinning one schema version."""
    for path in _RESULT_LIST_PATHS:
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, Mapping) else None
            if node is None:
                break
        if isinstance(node, list) and all(isinstance(item, Mapping) for item in node):
            return list(node)
    return []


def _record_criterion_id(record: Mapping[str, Any]) -> str | None:
    test_case = record.get("testCase")
    sources: list[Any] = [record]
    if isinstance(test_case, Mapping):
        sources.append(test_case)
        for key in ("metadata", "vars"):
            nested = test_case.get(key)
            if isinstance(nested, Mapping):
                sources.append(nested)
    for key in ("metadata", "vars"):
        nested = record.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key in ("criterionId", "criterion_id", "description"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _record_grade(record: Mapping[str, Any]) -> tuple[float | None, str, list[str]]:
    grading = record.get("gradingResult")
    grading = grading if isinstance(grading, Mapping) else {}
    components = grading.get("componentResults")
    components = [c for c in components if isinstance(c, Mapping)] if isinstance(
        components, list
    ) else []

    # A record carrying a provider/tool error and no grading was never judged. Scoring it
    # 0.0 would read as "failed the criterion on merit"; it must stay unevaluated.
    error = record.get("error")
    if not components and not grading and isinstance(error, str) and error.strip():
        return None, f"promptfoo did not grade this criterion: {error.strip()}", []

    rubric_components = [
        c
        for c in components
        if str((c.get("assertion") or {}).get("type", "")).lower() == "llm-rubric"
    ] or components

    scores: list[float] = []
    reasons: list[str] = []
    for component in rubric_components:
        value = component.get("score")
        score = coerce_score(value) if value is not None else coerce_score(component.get("pass"))
        if score is not None:
            scores.append(score)
        reason = component.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons.append(reason.strip())

    if not scores:
        for source in (grading, record):
            value = source.get("score")
            score = coerce_score(value) if value is not None else None
            if score is None:
                flag = source.get("pass")
                if flag is None:
                    flag = source.get("success")
                score = coerce_score(flag) if flag is not None else None
            if score is not None:
                scores.append(score)
                reason = source.get("reason")
                if isinstance(reason, str) and reason.strip():
                    reasons.append(reason.strip())
                break

    if not scores:
        return None, "", []
    evidence: list[str] = []
    for key in ("id", "testIdx", "promptId"):
        value = record.get(key)
        if isinstance(value, (str, int)):
            evidence.append(f"promptfoo:{key}={value}")
    return sum(scores) / len(scores), " | ".join(reasons)[:2000], evidence


def map_promptfoo_results(
    payload: Any, specs: Sequence[CriterionSpec]
) -> dict[str, CriterionOutcome]:
    """Map a promptfoo ``results.json`` payload onto criterion outcomes.

    Falls back to positional matching only when *no* record carries a usable id and the
    record count matches the criterion count exactly — anything looser would risk
    attributing a pass to a criterion that was never judged.
    """
    records = _result_records(payload)
    by_id = {spec.id.strip().lower(): spec.id for spec in specs}
    for spec in specs:
        by_id.setdefault(spec.slug, spec.id)
    outcomes: dict[str, CriterionOutcome] = {}
    scores: dict[str, list[float]] = {}
    reasons: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    matched_any = False

    for record in records:
        raw_id = _record_criterion_id(record)
        cid = by_id.get(raw_id.strip().lower()) if raw_id else None
        if cid is None:
            continue
        matched_any = True
        score, reason, refs = _record_grade(record)
        if score is None:
            outcomes.setdefault(
                cid,
                CriterionOutcome(score=None, rationale=reason or "promptfoo returned no grade"),
            )
            continue
        scores.setdefault(cid, []).append(score)
        if reason:
            reasons.setdefault(cid, []).append(reason)
        evidence.setdefault(cid, []).extend(refs)

    if not matched_any and len(records) == len(specs) and records:
        for spec, record in zip(specs, records, strict=True):
            score, reason, refs = _record_grade(record)
            if score is None:
                continue
            scores.setdefault(spec.id, []).append(score)
            if reason:
                reasons.setdefault(spec.id, []).append(reason)
            evidence.setdefault(spec.id, []).extend(refs)

    for cid, values in scores.items():
        outcomes[cid] = CriterionOutcome(
            score=sum(values) / len(values),
            rationale=" | ".join(reasons.get(cid, []))[:2000],
            evidence=evidence.get(cid, []),
        )
    return outcomes


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_command(settings: Mapping[str, Any]) -> list[str] | None:
    """Resolve how to invoke promptfoo, without executing anything."""
    override = settings.get("command")
    if isinstance(override, str) and override.strip():
        return shlex.split(override)
    if isinstance(override, (list, tuple)) and override:
        return [str(token) for token in override]
    if path := shutil.which("promptfoo"):
        return [path]
    use_npx = str(settings.get("useNpx", os.environ.get("ADLC_PROMPTFOO_NPX", ""))).lower()
    if use_npx in {"1", "true", "yes", "on"}:
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes", "promptfoo@latest"]
    return None
