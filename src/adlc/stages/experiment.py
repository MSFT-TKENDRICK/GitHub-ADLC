"""The ADLC experiment stage — ``plan`` → ``run`` → ``analyze``.

**A run is not an experiment.** Most ADLC runs are build/evaluation runs with a
single candidate and no live traffic. This stage is the *opt-in* path that turns
a run into something genuinely comparative, and it is the only producer of the
data ``adlc export oes`` needs (plan section 1 idea 2).

Three phases, each appended as a new immutable attempt:

``plan``
    Writes the **pre-registration** — variants, metrics and design declared
    *before* anything is measured. The file is hashed and the digest is recorded
    in the stage result, so a later edit is detectable; because stage results are
    committed, the ordering is also timestamp-verifiable via git. This is a
    genuine trust check, not paperwork.

``run``
    Records *exposure*: which flag keys back which variant, which
    :class:`~adlc.ports.FlagProvider` materialized them, and any flag
    evaluations actually performed (as OpenTelemetry ``feature_flag.*`` spans
    using the **current** semantic-convention names).

``analyze``
    Loads measured outcomes, re-verifies the pre-registration digest and computes
    per-metric comparisons against the baseline variant.

Every phase returns a :class:`~adlc.ports.StageResult` and writes **only** to
``runs/<run>/stages/experiment.<attempt>.json`` plus its own outputs under
``runs/<run>/experiment/``. It never writes ``run.json`` — only ``adlc reduce``
may do that, which is what keeps parallel Actions jobs race-free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config
    from adlc.ports import StageResult

_LOG = logging.getLogger(__name__)

#: Stage name, as it appears in ``stages/<stage>.<attempt>.json``.
STAGE = "experiment"

#: Ordered phases of the stage. All three share the ``experiment`` stage name and
#: append attempts, so history stays append-only.
PHASES: tuple[str, ...] = ("plan", "run", "analyze")

PLAN_SCHEMA_VERSION = "adlc-experiment-plan/v1"
ANALYSIS_SCHEMA_VERSION = "adlc-experiment-analysis/v1"

#: Relative output paths this stage owns. Nothing else is ever written.
PLAN_PATH = "experiment/plan.json"
EXPOSURE_PATH = "experiment/exposure.json"
ANALYSIS_PATH = "experiment/analysis.json"
MEASUREMENTS_PATH = "experiment/measurements.json"

#: Candidate locations for an operator-authored pre-registration, in order.
PREREGISTRATION_INPUTS = ("experiment.yaml", "experiment.yml", "experiment.json")

# ---------------------------------------------------------------------------
# Vocabulary mirrored from the published OES 0.1.0 schema.
#
# Kept here (not in the exporter) because these are *experiment* concepts: the
# stage refuses to record a value the standard cannot express, rather than
# letting the exporter discover it far downstream.
# ---------------------------------------------------------------------------

VARIANT_ROLES = frozenset({"control", "treatment", "holdout", "baseline"})
METRIC_ROLES = frozenset(
    {"primary", "secondary", "guardrail", "diagnostic", "data_quality", "invariant"}
)
METRIC_DIRECTIONS = frozenset(
    {"increase_is_good", "decrease_is_good", "no_change_expected", "two_sided"}
)
METRIC_TYPES = frozenset(
    {"conversion", "revenue", "count", "duration", "ratio", "retention", "percentile", "custom"}
)
DESIGN_TYPES = frozenset(
    {"ab", "abn", "multivariate", "factorial", "holdout", "switchback", "bandit", "quasi_experiment"}
)
ANALYSIS_METHODS = frozenset(
    {"frequentist", "bayesian", "sequential", "cuped", "diff_in_diff", "custom"}
)
RESULT_STATUSES = frozenset({"positive", "negative", "neutral", "inconclusive", "invalid"})
DECISION_IMPACTS = frozenset(
    {"supports_ship", "blocks_ship", "needs_followup", "informational"}
)

#: An ADLC candidate is a build artifact at a commit, compared to another build
#: artifact at another commit. There is no randomization and no live traffic, so
#: the only honest OES design type is ``quasi_experiment``. A real online A/B
#: test must say so explicitly in its pre-registration.
DEFAULT_DESIGN_TYPE = "quasi_experiment"

#: Likewise: without randomization there is no valid frequentist or Bayesian
#: inference to report, so the default analysis method is ``custom`` and the
#: exporter emits an ``adlc:statistical_inference`` quality check of ``not_run``.
DEFAULT_ANALYSIS_METHOD = "custom"
DEFAULT_ANALYSIS_MODEL = "adlc-deterministic-comparison"

#: Recorded on every comparison this stage computes itself, so a consumer can
#: never mistake a single deterministic measurement for a statistical estimate.
DETERMINISTIC_BASIS = "deterministic_single_measurement"


# ---------------------------------------------------------------------------
# OpenTelemetry feature-flag semantic conventions (current names).
#
# The 2024-era `feature_flag.provider_name` spelling is **obsolete**; the current
# convention is dotted `feature_flag.provider.name`, and evaluation results moved
# under `feature_flag.result.*`. `adlc.ports.Telemetry` mandates these.
# ---------------------------------------------------------------------------

ATTR_FLAG_KEY = "feature_flag.key"
ATTR_FLAG_PROVIDER_NAME = "feature_flag.provider.name"
ATTR_FLAG_RESULT_VARIANT = "feature_flag.result.variant"
ATTR_FLAG_RESULT_VALUE = "feature_flag.result.value"
ATTR_FLAG_RESULT_REASON = "feature_flag.result.reason"
ATTR_FLAG_CONTEXT_ID = "feature_flag.context.id"
ATTR_FLAG_SET_ID = "feature_flag.set.id"

#: Every attribute name this framework is allowed to emit for a flag evaluation.
FLAG_ATTRIBUTES: tuple[str, ...] = (
    ATTR_FLAG_KEY,
    ATTR_FLAG_PROVIDER_NAME,
    ATTR_FLAG_RESULT_VARIANT,
    ATTR_FLAG_RESULT_VALUE,
    ATTR_FLAG_RESULT_REASON,
    ATTR_FLAG_CONTEXT_ID,
    ATTR_FLAG_SET_ID,
)


def flag_evaluation_attributes(
    key: str,
    *,
    provider_name: str,
    value: Any = None,
    variant: str | None = None,
    reason: str | None = None,
    context_id: str | None = None,
    flag_set_id: str | None = None,
) -> dict[str, Any]:
    """Build OTel span attributes for one flag evaluation.

    Vendor-neutral on purpose: the LaunchDarkly adapter, the spine's flagd file
    provider and this stage all funnel through here so the attribute names can
    never drift apart. Keys with no value are omitted rather than emitted null.
    """
    attrs: dict[str, Any] = {ATTR_FLAG_KEY: key, ATTR_FLAG_PROVIDER_NAME: provider_name}
    if value is not None:
        attrs[ATTR_FLAG_RESULT_VALUE] = value
    if variant is not None:
        attrs[ATTR_FLAG_RESULT_VARIANT] = variant
    if reason is not None:
        attrs[ATTR_FLAG_RESULT_REASON] = reason
    if context_id is not None:
        attrs[ATTR_FLAG_CONTEXT_ID] = context_id
    if flag_set_id is not None:
        attrs[ATTR_FLAG_SET_ID] = flag_set_id
    return attrs


# ---------------------------------------------------------------------------
# Comparison math (pure — no I/O, no config)
# ---------------------------------------------------------------------------


def baseline_variant_key(variants: Sequence[Mapping[str, Any]]) -> str | None:
    """Pick the variant everything else is compared against.

    ``control`` wins, then ``baseline``, then the first declared variant.
    """
    for role in ("control", "baseline"):
        for variant in variants:
            if variant.get("role") == role:
                key = variant.get("key") or variant.get("id")
                if key:
                    return str(key)
    for variant in variants:
        key = variant.get("key") or variant.get("id")
        if key:
            return str(key)
    return None


def _result_status(direction: str | None, baseline: float, observed: float) -> str:
    if observed == baseline:
        return "neutral"
    if direction == "increase_is_good":
        return "positive" if observed > baseline else "negative"
    if direction == "decrease_is_good":
        return "positive" if observed < baseline else "negative"
    if direction == "no_change_expected":
        return "negative"
    # ``two_sided`` or undeclared: we genuinely do not know which way is better.
    return "inconclusive"


def _budget_passed(metric: Mapping[str, Any], observed: float) -> bool | None:
    budget = metric.get("budget")
    if budget is None:
        return None
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        return None
    direction = metric.get("direction")
    if direction == "increase_is_good":
        return observed >= budget
    if direction == "decrease_is_good":
        return observed <= budget
    return None


def _decision_impact(role: str | None, status: str, budget_passed: bool | None) -> str:
    if budget_passed is False and role in ("guardrail", "primary"):
        return "blocks_ship"
    if role == "guardrail" and status == "negative":
        return "blocks_ship"
    if role == "primary" and status == "positive":
        return "supports_ship"
    if status == "negative":
        return "needs_followup"
    return "informational"


def compare_measurements(
    metrics: Sequence[Mapping[str, Any]],
    measurements: Sequence[Mapping[str, Any]],
    baseline_key: str,
) -> list[dict[str, Any]]:
    """Compare every measured variant against ``baseline_key``, per metric.

    Returns OES ``results.metricResults[]`` entries. Deliberately emits **no**
    ``pValue``, ``standardError``, ``confidenceInterval`` or
    ``statisticalPowerObserved``: with one deterministic measurement per variant
    and no randomization, none of those quantities exist. Each entry instead
    carries ``adlc:measurementBasis`` so the absence is explicit, not an
    oversight.
    """
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    order: list[str] = []
    for measurement in measurements:
        metric_id = measurement.get("metricId")
        variant_key = measurement.get("variantKey") or measurement.get("variantId")
        if not metric_id or not variant_key or measurement.get("value") is None:
            continue
        indexed[(str(metric_id), str(variant_key))] = measurement
        if variant_key not in order:
            order.append(str(variant_key))

    results: list[dict[str, Any]] = []
    for metric in metrics:
        metric_id = metric.get("id")
        if not metric_id:
            continue
        base = indexed.get((str(metric_id), baseline_key))
        if base is None:
            continue
        try:
            baseline_value = float(base["value"])
        except (TypeError, ValueError, KeyError):
            continue

        for variant_key in order:
            if variant_key == baseline_key:
                continue
            observed_row = indexed.get((str(metric_id), variant_key))
            if observed_row is None:
                continue
            try:
                observed = float(observed_row["value"])
            except (TypeError, ValueError, KeyError):
                continue

            role = metric.get("role") if metric.get("role") in METRIC_ROLES else None
            direction = metric.get("direction")
            if direction not in METRIC_DIRECTIONS:
                direction = None
            status = _result_status(direction, baseline_value, observed)
            budget_passed = _budget_passed({**metric, "direction": direction}, observed)

            entry: dict[str, Any] = {
                "metricId": str(metric_id),
                "comparison": {
                    "baselineVariantId": baseline_key,
                    "variantId": variant_key,
                },
                "baselineValue": baseline_value,
                "variantValue": observed,
                "absoluteDifference": observed - baseline_value,
                "resultStatus": status,
                "decisionImpact": _decision_impact(role, status, budget_passed),
                "adlc:measurementBasis": DETERMINISTIC_BASIS,
            }
            if role:
                entry["role"] = role
            if baseline_value:
                entry["relativeDifference"] = (observed - baseline_value) / abs(baseline_value)
            if metric.get("budget") is not None:
                entry["adlc:budget"] = metric["budget"]
            if budget_passed is not None:
                entry["adlc:budgetPassed"] = budget_passed
            for field in ("collector", "artifactSha256"):
                if observed_row.get(field):
                    entry[f"adlc:{field}"] = observed_row[field]
            results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _enum_or_none(value: Any, allowed: frozenset[str]) -> str | None:
    return str(value) if isinstance(value, str) and value in allowed else None


def normalize_variant(variant: Mapping[str, Any], repo: str | None = None) -> dict[str, Any]:
    """Normalize an ``adlc-run/v1`` variant into experiment/OES shape.

    ``adlc-run/v1`` only allows roles ``control`` and ``treatment``; OES also
    permits ``holdout`` and ``baseline``, so both vocabularies pass through.
    """
    key = str(variant.get("key") or variant.get("id") or "")
    out: dict[str, Any] = {"id": key, "key": key}
    role = _enum_or_none(variant.get("role"), VARIANT_ROLES)
    if role:
        out["role"] = role
    flag_keys = [str(k) for k in (variant.get("flagKeys") or variant.get("featureFlagKeys") or [])]
    if flag_keys:
        out["featureFlagKeys"] = flag_keys
    commit = variant.get("commit")
    if commit:
        reference: dict[str, Any] = {"type": "git_commit", "value": str(commit)}
        if repo:
            reference["repo"] = repo
        out["codeReferences"] = [reference]
    if variant.get("description"):
        out["description"] = str(variant["description"])
    return out


def normalize_metric(metric: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize one metric definition; drop values the standard cannot express."""
    metric_id = metric.get("id") or metric.get("metricId")
    if not metric_id:
        return None
    out: dict[str, Any] = {
        "id": str(metric_id),
        "name": str(metric.get("name") or metric.get("title") or metric_id),
    }
    for field, allowed in (
        ("role", METRIC_ROLES),
        ("direction", METRIC_DIRECTIONS),
        ("type", METRIC_TYPES),
    ):
        value = _enum_or_none(metric.get(field), allowed)
        if value:
            out[field] = value
    for field in ("description", "unit"):
        if metric.get(field):
            out[field] = str(metric[field])
    # ``budget`` and ``source`` are ADLC concepts with no OES equivalent; they are
    # carried here for the comparison math and namespaced by the exporter.
    for field in ("budget", "source"):
        if metric.get(field) is not None:
            out[field] = metric[field]
    if metric.get("budget") is None and metric.get("threshold") is not None:
        out["budget"] = metric["threshold"]
    return out


def metrics_from_enrichment(run_dir: Path) -> list[dict[str, Any]]:
    """Collect metric definitions from ``enrichment/benchmarks.yaml`` + rubric.

    Tolerant by design: enrichment is produced by other workstreams and a shape
    we do not recognise must degrade to "no metrics", never to an exception.
    """
    metrics: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(raw: Mapping[str, Any], *, source: str, defaults: Mapping[str, Any] | None = None):
        merged = {**(defaults or {}), **raw, "source": raw.get("source", source)}
        normalized = normalize_metric(merged)
        if normalized and normalized["id"] not in seen:
            seen.add(normalized["id"])
            metrics.append(normalized)

    benchmarks = _load_structured(run_dir / "enrichment" / "benchmarks.yaml")
    for raw in _iter_definitions(benchmarks, ("metrics", "benchmarks", "budgets")):
        _add(raw, source="benchmarks.yaml")

    rubric = _load_structured(run_dir / "enrichment" / "rubric.yaml")
    for raw in _iter_definitions(rubric, ("criteria",)):
        _add(
            raw,
            source="rubric.yaml",
            defaults={"role": "secondary", "type": "custom", "direction": "increase_is_good"},
        )
    return metrics


def _iter_definitions(
    document: Any, collection_keys: Iterable[str]
) -> list[dict[str, Any]]:
    """Yield ``{id: ..., ...}`` dicts from either a list or an id-keyed mapping."""
    if not isinstance(document, Mapping):
        return []
    for key in collection_keys:
        block = document.get(key)
        if isinstance(block, list):
            return [dict(item) for item in block if isinstance(item, Mapping)]
        if isinstance(block, Mapping):
            out = []
            for item_id, body in block.items():
                if isinstance(body, Mapping):
                    out.append({"id": item_id, **body})
                else:
                    out.append({"id": item_id, "budget": body})
            return out
    return []


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _load_structured(path: Path) -> Any:
    """Load YAML or JSON, returning ``None`` for anything unreadable."""
    try:
        if not path.is_file():
            return None
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - malformed enrichment must not break the stage
        return None


def _write_json(path: Path, payload: Any) -> bytes:
    if path.name == "run.json":
        raise RuntimeError(
            "the experiment stage must never write run.json; only `adlc reduce` may"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def _run_dir(cfg: Config, run_id: str) -> Path:
    return Path(cfg.run_dir(run_id))


def _load_run(cfg: Config, run_id: str) -> dict[str, Any]:
    """Read the reduced ``run.json`` if one exists yet. Read-only, always."""
    document = _load_structured(_run_dir(cfg, run_id) / "run.json")
    return dict(document) if isinstance(document, Mapping) else {}


def _stage_files(run_dir: Path) -> list[Path]:
    stages = run_dir / "stages"
    if not stages.is_dir():
        return []
    files = []
    for path in stages.glob(f"{STAGE}.*.json"):
        middle = path.name[len(STAGE) + 1 : -len(".json")]
        if middle.isdigit():
            files.append((int(middle), path))
    return [path for _, path in sorted(files)]


def _next_attempt(run_dir: Path) -> int:
    attempts = []
    for path in _stage_files(run_dir):
        middle = path.name[len(STAGE) + 1 : -len(".json")]
        attempts.append(int(middle))
    return max(attempts, default=0) + 1


def _prior_results(run_dir: Path) -> list[dict[str, Any]]:
    """Previously recorded attempts of this stage, oldest first."""
    out = []
    for path in _stage_files(run_dir):
        document = _load_structured(path)
        if isinstance(document, Mapping):
            out.append(dict(document))
    return out


def _phase_result(run_dir: Path, phase: str) -> dict[str, Any] | None:
    """Most recent attempt of a given phase."""
    for document in reversed(_prior_results(run_dir)):
        if (document.get("data") or {}).get("phase") == phase:
            return document
    return None


def _git_sha(root: Path) -> str | None:
    """Best-effort HEAD sha, used to make the pre-registration git-anchored.

    Only consults git when ``root`` is itself a checkout: otherwise ``git
    rev-parse`` walks up the directory tree and would anchor the plan to some
    unrelated ancestor repository.
    """
    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 - git may be absent; never fail the stage
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _emit(telemetry: Any, span: Mapping[str, Any]) -> None:
    if telemetry is None:
        return
    try:
        telemetry.emit(dict(span))
    except Exception:  # noqa: BLE001 - telemetry is never load-bearing
        _LOG.debug("telemetry emit failed for span %s", span.get("name"), exc_info=True)


def _stage_result(
    *,
    attempt: int,
    status: str,
    started_at: str,
    outputs: Sequence[str] = (),
    digest: str = "",
    message: str = "",
    data: Mapping[str, Any] | None = None,
) -> StageResult:
    result: dict[str, Any] = {
        "stage": STAGE,
        "attempt": attempt,
        "status": status,
        "startedAt": started_at,
        "endedAt": _utcnow(),
        "outputs": list(outputs),
        "message": message,
        "data": dict(data or {}),
    }
    if digest:
        result["digest"] = digest
    return result  # type: ignore[return-value]


def _record(run_dir: Path, result: Mapping[str, Any]) -> Path:
    path = run_dir / "stages" / f"{STAGE}.{result['attempt']}.json"
    _write_json(path, dict(result))
    return path


# ---------------------------------------------------------------------------
# Phase 1 — plan (pre-registration)
# ---------------------------------------------------------------------------


def plan(
    cfg: Config,
    run_id: str,
    spec: Mapping[str, Any] | None = None,
    *,
    telemetry: Any = None,
) -> StageResult:
    """Write the pre-registration for ``run_id`` **before** anything is measured.

    ``spec`` may be supplied directly; otherwise an operator-authored
    ``experiment.yaml`` in the run directory is used, and failing that the plan
    is derived from the run's declared variants plus ``enrichment/``.

    Returns a ``skipped`` result — never a failure — when the run declares fewer
    than two variants, because a single-candidate build run is simply not an
    experiment.
    """
    started_at = _utcnow()
    run_dir = _run_dir(cfg, run_id)
    attempt = _next_attempt(run_dir)
    run_document = _load_run(cfg, run_id)

    if spec is None:
        for candidate in PREREGISTRATION_INPUTS:
            loaded = _load_structured(run_dir / candidate)
            if isinstance(loaded, Mapping):
                spec = loaded
                break
    spec = dict(spec or {})

    repo = spec.get("repo") or run_document.get("repo")
    raw_variants = spec.get("variants") or run_document.get("variants") or []
    variants = [
        normalized
        for normalized in (
            normalize_variant(v, repo) for v in raw_variants if isinstance(v, Mapping)
        )
        if normalized["key"]
    ]

    if len(variants) < 2:
        result = _stage_result(
            attempt=attempt,
            status="skipped",
            started_at=started_at,
            message=(
                f"run '{run_id}' declares {len(variants)} variant(s); a run with fewer than 2 "
                "is not an experiment, so no pre-registration was written"
            ),
            data={"phase": "plan", "variants": variants},
        )
        _record(run_dir, result)
        return result

    metrics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in spec.get("metrics") or []:
        if isinstance(raw, Mapping):
            normalized = normalize_metric(raw)
            if normalized and normalized["id"] not in seen:
                seen.add(normalized["id"])
                metrics.append(normalized)
    for normalized in metrics_from_enrichment(run_dir):
        if normalized["id"] not in seen:
            seen.add(normalized["id"])
            metrics.append(normalized)

    raw_experiment = spec.get("experiment") if isinstance(spec.get("experiment"), Mapping) else {}
    experiment: dict[str, Any] = {
        "id": str(raw_experiment.get("id") or spec.get("id") or run_id),
        "title": str(raw_experiment.get("title") or spec.get("title") or f"ADLC run {run_id}"),
        "status": "planned",
    }
    for field in ("hypothesis", "summary", "description", "learningGoal", "businessGoal"):
        value = raw_experiment.get(field) or spec.get(field)
        if value:
            experiment[field] = str(value)
    tags = raw_experiment.get("tags") or spec.get("tags")
    if isinstance(tags, list):
        experiment["tags"] = [str(t) for t in tags]

    raw_design = spec.get("design") if isinstance(spec.get("design"), Mapping) else {}
    design: dict[str, Any] = {
        "type": _enum_or_none(raw_design.get("type"), DESIGN_TYPES) or DEFAULT_DESIGN_TYPE,
        "analysisUnit": str(raw_design.get("analysisUnit") or "build_run"),
        "assignmentMethod": str(
            raw_design.get("assignmentMethod") or "deterministic_build_variant"
        ),
        "exposureDefinition": str(
            raw_design.get("exposureDefinition")
            or "each variant is a build artifact at a commit; exposure is a CI evaluation, "
            "not live user traffic"
        ),
    }
    # Traffic/randomization/power fields are copied only when the operator has
    # genuinely declared them. They are never synthesized.
    for field in (
        "randomizationUnit",
        "hashAttribute",
        "trafficAllocation",
        "variantAllocation",
        "population",
        "power",
        "alpha",
        "minimumDetectableEffect",
        "startDate",
        "endDate",
    ):
        if raw_design.get(field) is not None:
            design[field] = raw_design[field]

    raw_analysis = spec.get("analysis") if isinstance(spec.get("analysis"), Mapping) else {}
    analysis: dict[str, Any] = {
        "method": _enum_or_none(raw_analysis.get("method"), ANALYSIS_METHODS)
        or DEFAULT_ANALYSIS_METHOD,
        "model": str(raw_analysis.get("model") or DEFAULT_ANALYSIS_MODEL),
    }

    plan_document = {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "runId": run_id,
        "preRegisteredAt": started_at,
        "gitSha": _git_sha(Path(cfg.root)),
        "repo": repo,
        "experiment": experiment,
        "design": design,
        "variants": variants,
        "metrics": metrics,
        "analysis": analysis,
    }
    encoded = _write_json(run_dir / PLAN_PATH, plan_document)
    digest = _sha256_bytes(encoded)

    result = _stage_result(
        attempt=attempt,
        status="ok",
        started_at=started_at,
        outputs=[PLAN_PATH],
        digest=digest,
        message=(
            f"pre-registered {len(variants)} variants and {len(metrics)} metrics "
            f"for experiment '{experiment['id']}'"
        ),
        data={
            "phase": "plan",
            "experiment": experiment,
            "design": design,
            "variants": variants,
            "metrics": metrics,
            "analysis": analysis,
            "preRegistration": {
                "digest": digest,
                "plannedAt": started_at,
                "gitSha": plan_document["gitSha"],
                "path": PLAN_PATH,
            },
        },
    )
    _record(run_dir, result)
    _emit(telemetry, {"name": "adlc.experiment.plan", "attributes": {"adlc.run.id": run_id}})
    return result


# ---------------------------------------------------------------------------
# Phase 2 — run (exposure)
# ---------------------------------------------------------------------------


def run(
    cfg: Config,
    run_id: str,
    *,
    provider: Any = None,
    telemetry: Any = None,
    evaluate: bool = False,
    context: Mapping[str, Any] | None = None,
) -> StageResult:
    """Record which flags back which variant, and any evaluations performed.

    Flag wiring is **opt-in**: a candidate is a build artifact at a commit, not
    automatically a flag variant, and OpenFeature is only meaningful when the
    application genuinely exposes both code paths in one binary. When no variant
    declares a flag key this phase records that fact and returns ``skipped``.

    Live evaluation happens only when ``evaluate=True``; otherwise the phase
    records the *intended* exposure without contacting any flag backend.
    """
    started_at = _utcnow()
    run_dir = _run_dir(cfg, run_id)
    attempt = _next_attempt(run_dir)

    plan_result = _phase_result(run_dir, "plan")
    plan_data = (plan_result or {}).get("data") or {}
    variants = plan_data.get("variants") or []
    if not variants:
        run_document = _load_run(cfg, run_id)
        variants = [
            normalize_variant(v, run_document.get("repo"))
            for v in run_document.get("variants") or []
            if isinstance(v, Mapping)
        ]

    flag_map = {
        variant["key"]: list(variant.get("featureFlagKeys") or [])
        for variant in variants
        if variant.get("key")
    }
    declared_flags = sorted({key for keys in flag_map.values() for key in keys})

    if not declared_flags:
        result = _stage_result(
            attempt=attempt,
            status="skipped",
            started_at=started_at,
            message=(
                "no variant declares a feature flag key; candidates are compared as build "
                "artifacts at commits and no flag backend was contacted"
            ),
            data={"phase": "run", "exposure": {"variants": flag_map, "flagKeys": []}},
        )
        _record(run_dir, result)
        return result

    provider_name, manifest, provider_note = _resolve_provider(
        cfg, provider, run_id, run_dir, variants
    )

    evaluations: list[dict[str, Any]] = []
    if evaluate and provider is not None:
        flag_set_id = str(plan_data.get("experiment", {}).get("id") or run_id)
        for key in declared_flags:
            evaluations.append(
                _evaluate_flag(provider, provider_name, key, context, flag_set_id, telemetry)
            )

    exposure = {
        "provider": provider_name,
        "providerNote": provider_note,
        "manifest": manifest,
        "variants": flag_map,
        "flagKeys": declared_flags,
        "evaluations": evaluations,
    }
    outputs = [EXPOSURE_PATH]
    if manifest:
        outputs.append(manifest)
    encoded = _write_json(run_dir / EXPOSURE_PATH, exposure)

    result = _stage_result(
        attempt=attempt,
        status="ok",
        started_at=started_at,
        outputs=outputs,
        digest=_sha256_bytes(encoded),
        message=(
            f"recorded exposure for {len(declared_flags)} flag key(s) via provider "
            f"'{provider_name}'"
        ),
        data={"phase": "run", "exposure": exposure},
    )
    _record(run_dir, result)
    return result


def _resolve_provider(
    cfg: Config,
    provider: Any,
    run_id: str,
    run_dir: Path,
    variants: Sequence[Mapping[str, Any]],
) -> tuple[str, str | None, str]:
    """Return ``(provider_name, manifest_relpath, note)``, degrading gracefully.

    A missing or broken flag adapter is recorded, never raised: with no
    LaunchDarkly key the spine's flagd file provider takes over, and with no
    provider at all the run is still a perfectly valid build comparison.
    """
    if provider is None:
        try:
            from adlc.config import select_adapter

            provider = select_adapter(cfg, "flags")
        except Exception as exc:  # noqa: BLE001 - a missing flag adapter must not fail the stage
            return "none", None, f"no flag provider available ({exc})"
        note = _availability_note(cfg, provider)
    else:
        note = "flag definitions materialized"

    provider_name = str(getattr(provider, "name", type(provider).__name__))
    materialize = getattr(provider, "materialize", None)
    if materialize is None:
        return provider_name, None, "provider does not implement materialize()"
    run_view = {
        "runId": run_id,
        "variants": [
            {
                "key": variant.get("key"),
                "role": variant.get("role"),
                "flagKeys": list(variant.get("featureFlagKeys") or []),
            }
            for variant in variants
        ],
    }
    try:
        path = Path(materialize(run_view))
    except Exception as exc:  # noqa: BLE001
        return provider_name, None, f"materialize() failed: {exc}"
    try:
        relative = path.relative_to(run_dir).as_posix()
    except ValueError:
        relative = path.as_posix()
    return provider_name, relative, note


def _availability_note(cfg: Config, provider: Any) -> str:
    """Record whether an auto-selected provider actually reported itself available.

    ``select_adapter`` falls back to *some* registered adapter when no spine default
    is installed, so the exposure record must say when the one it handed back is
    not really usable rather than implying the flags were served.
    """
    detect = getattr(provider, "detect", None)
    if detect is None:
        return "flag definitions materialized"
    try:
        available, reason = detect(cfg)
    except Exception as exc:  # noqa: BLE001 - detect() is contractually non-raising
        return f"flag definitions materialized, but detect() raised: {exc}"
    if available:
        return "flag definitions materialized"
    return f"flag definitions materialized, but the provider is unavailable: {reason}"


def _evaluate_flag(
    provider: Any,
    provider_name: str,
    key: str,
    context: Mapping[str, Any] | None,
    flag_set_id: str,
    telemetry: Any,
) -> dict[str, Any]:
    context = dict(context or {})
    try:
        evaluated = provider.evaluate(key, context) or {}
    except Exception as exc:  # noqa: BLE001 - a flag backend outage is not a stage failure
        evaluated = {"key": key, "reason": "ERROR", "variant": None, "value": None}
        telemetry_error: str | None = str(exc)
    else:
        telemetry_error = None

    attributes = flag_evaluation_attributes(
        key,
        provider_name=provider_name,
        value=evaluated.get("value"),
        variant=evaluated.get("variant"),
        reason=evaluated.get("reason"),
        context_id=context.get("targetingKey") or context.get("key"),
        flag_set_id=flag_set_id,
    )
    span: dict[str, Any] = {"name": "feature_flag.evaluation", "attributes": attributes}
    if telemetry_error:
        span["error"] = telemetry_error
    _emit(telemetry, span)
    return {"attributes": attributes, "error": telemetry_error}


# ---------------------------------------------------------------------------
# Phase 3 — analyze
# ---------------------------------------------------------------------------


def analyze(
    cfg: Config,
    run_id: str,
    measurements: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    *,
    telemetry: Any = None,
) -> StageResult:
    """Populate measured results and re-verify the pre-registration.

    ``measurements`` may be a list of ``{metricId, variantKey, value, ...}`` rows
    or a mapping with a ``measurements`` key (optionally alongside ``sampleSizes``
    and ``exposures``, which are passed through verbatim and never synthesized).
    When omitted, ``experiment/measurements.json`` is read.
    """
    started_at = _utcnow()
    run_dir = _run_dir(cfg, run_id)
    attempt = _next_attempt(run_dir)

    plan_result = _phase_result(run_dir, "plan")
    plan_document = _load_structured(run_dir / PLAN_PATH)
    if plan_result is None or not isinstance(plan_document, Mapping):
        result = _stage_result(
            attempt=attempt,
            status="skipped",
            started_at=started_at,
            message=(
                f"no pre-registration at {PLAN_PATH}; run the `plan` phase before `analyze` "
                "so that metrics are declared before they are measured"
            ),
            data={"phase": "analyze", "preRegistration": {"present": False}},
        )
        _record(run_dir, result)
        return result

    recorded = (plan_result.get("data") or {}).get("preRegistration", {})
    recorded_digest = str(recorded.get("digest") or "")
    current_digest = _sha256_bytes((run_dir / PLAN_PATH).read_bytes())
    unchanged = bool(recorded_digest) and recorded_digest == current_digest

    payload = measurements
    if payload is None:
        payload = _load_structured(run_dir / MEASUREMENTS_PATH)
    rows, sample_sizes, exposures = _split_measurements(payload)
    if not rows:
        rows = _measurements_from_evidence(run_dir, plan_document)

    metrics = [dict(m) for m in plan_document.get("metrics") or []]
    variants = [dict(v) for v in plan_document.get("variants") or []]
    baseline = baseline_variant_key(variants) or ""
    metric_results = compare_measurements(metrics, rows, baseline) if baseline else []

    results: dict[str, Any] = {"metricResults": metric_results}
    if sample_sizes:
        results["sampleSizes"] = sample_sizes
    if exposures:
        results["exposures"] = exposures

    analysis = dict(plan_document.get("analysis") or {})
    analysis.setdefault("method", DEFAULT_ANALYSIS_METHOD)
    analysis["generatedAt"] = started_at

    experiment = dict(plan_document.get("experiment") or {})
    experiment["status"] = "analyzed" if metric_results else "stopped"

    pre_registration = {
        "present": True,
        "path": PLAN_PATH,
        "digest": current_digest,
        "recordedDigest": recorded_digest or None,
        "unchanged": unchanged,
        "plannedAt": recorded.get("plannedAt"),
        "analyzedAt": started_at,
        "gitSha": plan_document.get("gitSha"),
    }

    analysis_document = {
        "schemaVersion": ANALYSIS_SCHEMA_VERSION,
        "runId": run_id,
        "analyzedAt": started_at,
        "baselineVariantKey": baseline,
        "preRegistration": pre_registration,
        "experiment": experiment,
        "design": plan_document.get("design") or {},
        "variants": variants,
        "metrics": metrics,
        "measurements": rows,
        "results": results,
        "analysis": analysis,
    }
    encoded = _write_json(run_dir / ANALYSIS_PATH, analysis_document)

    if not unchanged:
        status = "fail"
        message = (
            f"the pre-registration at {PLAN_PATH} changed after it was planned; "
            f"{len(metric_results)} comparison(s) computed but they are not trustworthy"
        )
    elif metric_results:
        status = "ok"
        message = (
            f"{len(metric_results)} comparison(s) computed against baseline '{baseline}' "
            f"from {len(rows)} measurement(s)"
        )
    else:
        status = "ok"
        message = (
            f"no comparable measurements found for baseline '{baseline}'; "
            f"OES export will refuse this run"
        )

    result = _stage_result(
        attempt=attempt,
        status=status,
        started_at=started_at,
        outputs=[ANALYSIS_PATH],
        digest=_sha256_bytes(encoded),
        message=message,
        data={
            "phase": "analyze",
            "experiment": experiment,
            "design": plan_document.get("design") or {},
            "variants": variants,
            "metrics": metrics,
            "measurements": rows,
            "results": results,
            "analysis": analysis,
            "preRegistration": pre_registration,
            "baselineVariantKey": baseline,
        },
    )
    _record(run_dir, result)
    _emit(telemetry, {"name": "adlc.experiment.analyze", "attributes": {"adlc.run.id": run_id}})
    return result


def _split_measurements(
    payload: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Accept either a bare list of rows or a mapping wrapping them."""
    rows: list[dict[str, Any]] = []
    sample_sizes: dict[str, Any] = {}
    exposures: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        raw_rows = payload.get("measurements") or []
        sample_sizes = dict(payload.get("sampleSizes") or {})
        exposures = dict(payload.get("exposures") or {})
    elif isinstance(payload, list):
        raw_rows = payload
    else:
        raw_rows = []
    for row in raw_rows:
        if isinstance(row, Mapping) and row.get("metricId"):
            rows.append(dict(row))
    return rows, sample_sizes, exposures


def _measurements_from_evidence(
    run_dir: Path, plan_document: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Fall back to ``evidence/<variant>/metrics.json`` maps of ``{metricId: value}``."""
    rows: list[dict[str, Any]] = []
    for variant in plan_document.get("variants") or []:
        key = variant.get("key")
        if not key:
            continue
        document = _load_structured(run_dir / "evidence" / str(key) / "metrics.json")
        if not isinstance(document, Mapping):
            continue
        for metric_id, value in document.items():
            if isinstance(value, int | float):
                rows.append(
                    {
                        "metricId": str(metric_id),
                        "variantKey": str(key),
                        "value": value,
                        "collector": "evidence",
                    }
                )
    return rows


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def execute(cfg: Config, run_id: str, phase: str, **kwargs: Any) -> StageResult:
    """Run one phase by name — the seam a generic CLI can call."""
    if phase not in PHASES:
        raise ValueError(f"unknown experiment phase '{phase}'; expected one of {', '.join(PHASES)}")
    return {"plan": plan, "run": run, "analyze": analyze}[phase](cfg, run_id, **kwargs)
