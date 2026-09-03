"""Open Experiment Specification (OES) v0.1.0 **exporter**.

``adlc-run/v1`` is the canonical record. OES is an export target and nothing
more (plan section 1, idea 2). The reason is concrete, not stylistic:

* OES models an **online A/B experiment** -- traffic allocation, randomization
  units, sample-ratio mismatch, p-values, statistical power. An ADLC run is
  usually a build/evaluation run with no live traffic and often a single
  candidate. Forcing one into the other manufactures meaningless nulls.
* Its ``artifacts[].type`` enum is
  ``chart|screenshot|sql|notebook|csv|dashboard|slide|image|html_report``. It
  cannot name a Playwright trace, a HAR, or a JSONL console log -- the three
  artifacts ADLC evidence capture cares most about.

Therefore this exporter **refuses to emit a document unless the run is genuinely
comparative** (>= 2 variants with measured outcomes for at least one shared
metric), and it never fabricates power, p-values, randomization or traffic
figures. What OES cannot express is carried losslessly under
``extensions["adlc:*"]``, which the specification requires importers to ignore
safely.

The published schema is vendored verbatim at the bottom of this module so that
validation works offline and in an air-gapped CI job. ``ADLC_OES_SCHEMA`` (or
``export.oes.schemaPath`` in ``config.yaml``) points at a newer copy when the
standard moves.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.stages.experiment import (
    ANALYSIS_METHODS,
    DESIGN_TYPES,
    METRIC_DIRECTIONS,
    METRIC_ROLES,
    METRIC_TYPES,
    VARIANT_ROLES,
    baseline_variant_key,
    compare_measurements,
    metrics_from_enrichment,
    normalize_metric,
    normalize_variant,
)
from adlc.stages.experiment import STAGE as EXPERIMENT_STAGE

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config
    from adlc.ports import Run

#: The version of the standard this exporter targets.
OES_SCHEMA_VERSION = "0.1.0"

#: Where the schema vendored at the bottom of this module came from.
OES_SCHEMA_URL = "https://openexperiment.org/schema/openexperiment-0.1.0.schema.json"

#: Environment override pointing at an alternative copy of the schema.
OES_SCHEMA_ENV = "ADLC_OES_SCHEMA"

OES_OBJECT_TYPE = "experiment"
SOURCE_SYSTEM = "adlc"
EXTENSION_PREFIX = "adlc:"
DEFAULT_OUTPUT_NAME = "oes.json"

# ---------------------------------------------------------------------------
# Enumerations, mirrored from the vendored schema.
#
# The exporter drops any value it cannot prove is legal rather than emitting a
# document that fails validation deep inside a consumer's pipeline.
# ---------------------------------------------------------------------------

EXPERIMENT_STATUSES = frozenset(
    {"draft", "planned", "running", "stopped", "analyzed", "decided", "archived"}
)
DECISION_OUTCOMES = frozenset(
    {"ship", "do_not_ship", "iterate", "rerun", "rollback", "partial_rollout"}
)
DECISION_STATUSES = frozenset({"pending", "decided", "superseded"})
CHECK_STATUSES = frozenset({"pass", "warn", "fail", "not_run"})
CHECK_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
RESULT_STATUSES = frozenset({"positive", "negative", "neutral", "inconclusive", "invalid"})
DECISION_IMPACTS = frozenset(
    {"supports_ship", "blocks_ship", "needs_followup", "informational"}
)
ARTIFACT_TYPES = frozenset(
    {"chart", "screenshot", "sql", "notebook", "csv", "dashboard", "slide", "image", "html_report"}
)
SCORECARD_RESULTS = frozenset({"win", "loss", "neutral", "mixed", "inconclusive", "invalid"})
SCORECARD_ACTIONS = frozenset(
    {"ship", "do_not_ship", "iterate", "rerun", "continue_running", "roll_back"}
)
SCORECARD_QUALITY = frozenset({"valid", "warning", "invalid", "needs_review"})

#: ``adlc-run/v1`` status -> OES ``experiment.status``.
RUN_STATUS_TO_EXPERIMENT_STATUS: dict[str, str] = {
    "draft": "draft",
    "specced": "planned",
    "built": "running",
    "evaluated": "analyzed",
    "gated": "analyzed",
    "reported": "analyzed",
    "decided": "decided",
    "abandoned": "archived",
}

#: ``decision.outcome`` -> ``scorecard.recommendedAction``. Note the two enums
#: disagree on spelling (``rollback`` vs ``roll_back``) and OES has no scorecard
#: action for ``partial_rollout``, so that one is deliberately dropped rather
#: than coerced into something it does not mean.
DECISION_TO_SCORECARD_ACTION: dict[str, str] = {
    "ship": "ship",
    "do_not_ship": "do_not_ship",
    "iterate": "iterate",
    "rerun": "rerun",
    "rollback": "roll_back",
}

#: ADLC ``artifacts[].kind`` -> OES ``artifacts[].type``. Anything absent here
#: goes to ``extensions["adlc:artifacts"]`` instead of being mislabelled.
ARTIFACT_KIND_TO_OES: dict[str, str] = {
    "screenshot": "screenshot",
    "screenshots": "screenshot",
    "image": "image",
    "chart": "chart",
    "csv": "csv",
    "notebook": "notebook",
    "sql": "sql",
    "dashboard": "dashboard",
    "slide": "slide",
    "report": "html_report",
    "report_html": "html_report",
    "html_report": "html_report",
    "lighthouse_report": "html_report",
}

#: Secondary mapping, used only when ``kind`` is unrecognised.
MIME_TYPE_TO_OES: dict[str, str] = {
    "text/csv": "csv",
    "text/html": "html_report",
    "image/png": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "image/gif": "image",
    "image/svg+xml": "image",
    "application/x-ipynb+json": "notebook",
    "application/sql": "sql",
    "text/x-sql": "sql",
}


class OesExportError(ValueError):
    """Base class for every refusal this exporter can make."""


class NotComparativeError(OesExportError):
    """The run is not a comparative experiment, so no OES document is emitted.

    This is the normal outcome for most ADLC runs and is **not** a bug. OES
    describes an experiment; a single-candidate build run is not one.
    """


class OesValidationError(OesExportError):
    """The generated document failed validation against the published schema."""


# ---------------------------------------------------------------------------
# Schema access
# ---------------------------------------------------------------------------


def schema_override_path(cfg: Config | None = None) -> Path | None:
    """Resolve an alternative schema location from env or config, if any."""
    raw = os.environ.get(OES_SCHEMA_ENV)
    if not raw and cfg is not None:
        raw = ((getattr(cfg, "raw", {}) or {}).get("export", {}) or {}).get("oes", {}).get(
            "schemaPath"
        )
    return Path(raw).expanduser() if raw else None


@lru_cache(maxsize=4)
def _load_schema_file(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def _vendored_schema() -> dict[str, Any]:
    return json.loads(_VENDORED_OES_SCHEMA)


def load_oes_schema(cfg: Config | None = None) -> dict[str, Any]:
    """Return the OES JSON Schema to validate against.

    Prefers an operator-supplied copy so a newer draft can be adopted without a
    code change; otherwise uses the copy vendored in this module, which makes
    validation work with no network access at all.
    """
    override = schema_override_path(cfg)
    if override is not None:
        return copy.deepcopy(_load_schema_file(str(override)))
    return copy.deepcopy(_vendored_schema())


# ---------------------------------------------------------------------------
# Reading the ADLC run
# ---------------------------------------------------------------------------


def experiment_record(run: Run | Mapping[str, Any]) -> dict[str, Any]:
    """Fold the experiment stage's attempts into one record.

    The ``plan`` → ``run`` → ``analyze`` phases each append an attempt, and each
    contributes different keys; later attempts win. Everything the exporter needs
    is therefore reachable from a reduced ``run.json`` alone, with no second
    filesystem read.
    """
    record: dict[str, Any] = {}
    stages = [
        stage
        for stage in (run.get("stages") or [])
        if isinstance(stage, Mapping) and stage.get("stage") == EXPERIMENT_STAGE
    ]
    for stage in sorted(stages, key=lambda s: s.get("attempt") or 0):
        data = stage.get("data")
        if not isinstance(data, Mapping):
            continue
        phase = data.get("phase")
        if phase:
            record.setdefault("phases", {})[str(phase)] = {
                "attempt": stage.get("attempt"),
                "status": stage.get("status"),
                "startedAt": stage.get("startedAt"),
                "message": stage.get("message"),
            }
        for key, value in data.items():
            if key == "phase" or value in (None, [], {}):
                continue
            record[key] = value
    return record


def is_comparative(run: Run | Mapping[str, Any]) -> tuple[bool, str]:
    """Decide whether this run may be exported as an OES experiment.

    Returns ``(ok, reason)``; ``reason`` is written verbatim into the refusal so
    a user is told exactly what is missing rather than "export failed".
    """
    record = experiment_record(run)
    variants = record.get("variants") or run.get("variants") or []
    keys = [
        str(v.get("key") or v.get("id"))
        for v in variants
        if isinstance(v, Mapping) and (v.get("key") or v.get("id"))
    ]
    if len(keys) < 2:
        return False, (
            f"the run declares {len(keys)} variant(s); OES describes a comparison, so at least "
            "2 variants are required. adlc-run/v1 remains the canonical record for this run."
        )

    results = record.get("results") if isinstance(record.get("results"), Mapping) else {}
    metric_results = [r for r in (results.get("metricResults") or []) if isinstance(r, Mapping)]
    if metric_results:
        return True, (
            f"{len(keys)} variants with {len(metric_results)} measured metric comparison(s)"
        )

    measurements = [m for m in (record.get("measurements") or []) if isinstance(m, Mapping)]
    if not measurements:
        return False, (
            "the run has no measured outcomes; run the experiment stage's `analyze` phase "
            "(or provide experiment/measurements.json) before exporting"
        )

    per_metric: dict[str, set[str]] = {}
    for measurement in measurements:
        metric_id = measurement.get("metricId")
        variant_key = measurement.get("variantKey") or measurement.get("variantId")
        if metric_id and variant_key and measurement.get("value") is not None:
            per_metric.setdefault(str(metric_id), set()).add(str(variant_key))
    comparable = sorted(mid for mid, seen in per_metric.items() if len(seen) >= 2)
    if not comparable:
        return False, (
            "no metric was measured on 2 or more variants, so there is nothing to compare; "
            "OES export needs at least one metric present on both the baseline and a candidate"
        )
    return True, f"{len(keys)} variants with comparable measurements for {', '.join(comparable)}"


def _stage_data(run: Run | Mapping[str, Any], stage_name: str) -> dict[str, Any]:
    for stage in reversed(list(run.get("stages") or [])):
        if isinstance(stage, Mapping) and stage.get("stage") == stage_name:
            data = stage.get("data")
            if isinstance(data, Mapping):
                return dict(data)
    return {}


# ---------------------------------------------------------------------------
# Document sections
# ---------------------------------------------------------------------------


def _enum_or_none(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _experiment(run: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("experiment") if isinstance(record.get("experiment"), Mapping) else {}
    run_id = str(run.get("runId") or "unknown")
    experiment: dict[str, Any] = {
        "id": str(raw.get("id") or run.get("experimentRef") or run_id),
        "title": str(raw.get("title") or f"ADLC run {run_id}"),
    }
    if run.get("decision"):
        status = "decided"
    else:
        status = _enum_or_none(raw.get("status"), EXPERIMENT_STATUSES) or (
            RUN_STATUS_TO_EXPERIMENT_STATUS.get(str(run.get("status") or ""), "analyzed")
        )
    experiment["status"] = status
    for field in (
        "slug",
        "summary",
        "description",
        "hypothesis",
        "learningGoal",
        "businessGoal",
        "productArea",
    ):
        if raw.get(field):
            experiment[field] = str(raw[field])
    tags = raw.get("tags")
    if isinstance(tags, list) and tags:
        experiment["tags"] = [str(tag) for tag in tags]
    return experiment


def _design(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("design") if isinstance(record.get("design"), Mapping) else {}
    design: dict[str, Any] = {}
    design_type = _enum_or_none(raw.get("type"), DESIGN_TYPES)
    if design_type:
        design["type"] = design_type
    for field in (
        "randomizationUnit",
        "analysisUnit",
        "assignmentMethod",
        "hashAttribute",
        "namespace",
        "population",
        "exposureDefinition",
        "triggerDefinition",
        "stoppingRule",
        "startDate",
        "endDate",
        "trafficAllocation",
        "variantAllocation",
        "power",
        "alpha",
        "minimumDetectableEffect",
    ):
        if raw.get(field) is not None:
            design[field] = raw[field]
    return design


def _variants(run: Mapping[str, Any], record: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_variants = record.get("variants") or run.get("variants") or []
    repo = run.get("repo")
    out: list[dict[str, Any]] = []
    for raw in raw_variants:
        if not isinstance(raw, Mapping):
            continue
        variant = normalize_variant(raw, repo) if "id" not in raw else dict(raw)
        key = str(variant.get("key") or variant.get("id") or "")
        if not key:
            continue
        variant.setdefault("id", key)
        variant["key"] = key
        role = _enum_or_none(variant.get("role"), VARIANT_ROLES)
        if role:
            variant["role"] = role
        else:
            variant.pop("role", None)
        out.append(variant)
    return out


def _metric_definitions(
    record: Mapping[str, Any], run_dir: Path | None
) -> list[dict[str, Any]]:
    """ADLC-shaped metric catalogue: the pre-registration, plus enrichment.

    Keeps ADLC-only fields (``budget``, ``source``) under their plain names
    because the comparison math reads them; :func:`_oes_metrics` namespaces them
    on the way out.
    """
    definitions: list[Mapping[str, Any]] = [
        m for m in (record.get("metrics") or []) if isinstance(m, Mapping)
    ]
    if run_dir is not None:
        definitions = [*definitions, *metrics_from_enrichment(run_dir)]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in definitions:
        normalized = normalize_metric(raw)
        if not normalized or normalized["id"] in seen:
            continue
        seen.add(normalized["id"])
        out.append(normalized)
    return out


def _oes_metrics(definitions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project the ADLC catalogue onto OES ``metrics[]``."""
    out: list[dict[str, Any]] = []
    for normalized in definitions:
        metric: dict[str, Any] = {"id": normalized["id"], "name": normalized["name"]}
        for field, allowed in (
            ("role", METRIC_ROLES),
            ("direction", METRIC_DIRECTIONS),
            ("type", METRIC_TYPES),
        ):
            value = _enum_or_none(normalized.get(field), allowed)
            if value:
                metric[field] = value
        for field in ("description", "unit"):
            if normalized.get(field):
                metric[field] = normalized[field]
        # A budget/threshold is an ADLC concept: OES metrics have no such field,
        # so it is namespaced rather than smuggled in as a look-alike.
        if normalized.get("budget") is not None:
            metric[f"{EXTENSION_PREFIX}budget"] = normalized["budget"]
        if normalized.get("source"):
            metric[f"{EXTENSION_PREFIX}source"] = normalized["source"]
        out.append(metric)
    return out


def _sanitize_metric_result(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    metric_id = raw.get("metricId")
    comparison = raw.get("comparison")
    if not metric_id or not isinstance(comparison, Mapping):
        return None
    if not comparison.get("baselineVariantId") or not comparison.get("variantId"):
        return None
    entry = dict(raw)
    entry["metricId"] = str(metric_id)
    entry["comparison"] = {
        "baselineVariantId": str(comparison["baselineVariantId"]),
        "variantId": str(comparison["variantId"]),
    }
    for field, allowed in (
        ("resultStatus", RESULT_STATUSES),
        ("decisionImpact", DECISION_IMPACTS),
    ):
        if entry.get(field) is not None and _enum_or_none(entry.get(field), allowed) is None:
            entry.pop(field)
    return entry


def _results(
    record: Mapping[str, Any], definitions: Sequence[Mapping[str, Any]], baseline: str | None
) -> dict[str, Any]:
    raw = record.get("results") if isinstance(record.get("results"), Mapping) else {}
    metric_results = [r for r in (raw.get("metricResults") or []) if isinstance(r, Mapping)]
    if not metric_results and baseline:
        measurements = [m for m in (record.get("measurements") or []) if isinstance(m, Mapping)]
        metric_results = compare_measurements(definitions, measurements, baseline)

    results: dict[str, Any] = {
        "metricResults": [
            sanitized
            for sanitized in (_sanitize_metric_result(r) for r in metric_results)
            if sanitized is not None
        ]
    }
    # Sample sizes and exposures exist only for a real online experiment. They
    # are passed through when genuinely supplied and never invented.
    for field in ("sampleSizes", "exposures"):
        value = raw.get(field)
        if isinstance(value, Mapping) and value:
            results[field] = dict(value)
    return results


def _analysis(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("analysis") if isinstance(record.get("analysis"), Mapping) else {}
    analysis: dict[str, Any] = {}
    method = _enum_or_none(raw.get("method"), ANALYSIS_METHODS)
    if method:
        analysis["method"] = method
    for field in ("model", "adjustmentMethod", "missingDataHandling", "outlierHandling"):
        if raw.get(field):
            analysis[field] = str(raw[field])
    for field in ("confidenceLevel", "alpha"):
        if isinstance(raw.get(field), int | float):
            analysis[field] = raw[field]
    if raw.get("generatedAt"):
        analysis["generatedAt"] = str(raw["generatedAt"])
    return analysis


def _has_statistical_inference(results: Mapping[str, Any]) -> bool:
    """True only when the source data genuinely carries inferential statistics."""
    inferential = ("pValue", "qValue", "standardError", "confidenceInterval", "credibleInterval")
    return any(
        entry.get(field) is not None
        for entry in results.get("metricResults") or []
        for field in inferential
    )


def _quality_checks(
    run: Mapping[str, Any], record: Mapping[str, Any], results: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Map **every** ADLC gate to a ``qualityChecks[]`` entry.

    This is the one place where ADLC and OES line up almost exactly: both model a
    named check with a pass/warn/fail/not_run status, a severity, and observed vs
    expected values. ``checkType`` is a free string in the schema, so ours are
    namespaced ``adlc:<gate id>`` to stay unambiguous alongside a vendor's own
    checks (SRM, pre-exposure bias, and so on).
    """
    checks: list[dict[str, Any]] = []
    gates = [g for g in (run.get("gates") or []) if isinstance(g, Mapping)]

    for gate in gates:
        gate_id = str(gate.get("id") or "unknown")
        check: dict[str, Any] = {"checkType": f"{EXTENSION_PREFIX}{gate_id}"}
        status = _enum_or_none(gate.get("status"), CHECK_STATUSES) or "not_run"
        check["status"] = status
        severity = _enum_or_none(gate.get("severity"), CHECK_SEVERITIES)
        if severity:
            check["severity"] = severity
        for field in ("observed", "expected"):
            if isinstance(gate.get(field), Mapping) and gate[field]:
                check[field] = dict(gate[field])
        if gate.get("message"):
            check["message"] = str(gate["message"])
        check[f"{EXTENSION_PREFIX}required"] = bool(gate.get("required"))
        if gate.get("evidence"):
            check[f"{EXTENSION_PREFIX}evidence"] = [str(e) for e in gate["evidence"]]
        checks.append(check)

    checks.append(_aggregate_check(gates))
    checks.append(_pre_registration_check(record))
    checks.append(_inference_check(record, results))
    return checks


def _aggregate_check(gates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The single fail-closed verdict ADLC uses as its branch-protection target."""
    required = [g for g in gates if g.get("required")]
    blocking = [
        str(g.get("id")) for g in required if g.get("status") in ("fail", "not_run", None)
    ]
    if not required:
        return {
            "checkType": f"{EXTENSION_PREFIX}aggregate",
            "status": "not_run",
            "message": "no required gates were recorded for this run",
        }
    return {
        "checkType": f"{EXTENSION_PREFIX}aggregate",
        "status": "fail" if blocking else "pass",
        "severity": "critical" if blocking else "low",
        "observed": {"failingRequiredGates": blocking},
        "expected": {"failingRequiredGates": []},
        "message": (
            f"required gate(s) {', '.join(blocking)} did not pass; a required gate that is "
            "not_run fails closed"
            if blocking
            else f"all {len(required)} required gate(s) passed"
        ),
    }


def _pre_registration_check(record: Mapping[str, Any]) -> dict[str, Any]:
    """Was the design declared before the results were measured, and unedited?"""
    check_type = f"{EXTENSION_PREFIX}pre_registration"
    pre = record.get("preRegistration")
    if not isinstance(pre, Mapping) or not pre.get("present", True):
        return {
            "checkType": check_type,
            "status": "not_run",
            "message": (
                "no pre-registration was recorded; variants and metrics cannot be shown to "
                "have been declared before they were measured"
            ),
        }
    planned_at = pre.get("plannedAt")
    analyzed_at = pre.get("analyzedAt")
    ordered = bool(planned_at and analyzed_at and str(planned_at) <= str(analyzed_at))
    unchanged = bool(pre.get("unchanged"))
    passed = ordered and unchanged
    return {
        "checkType": check_type,
        "status": "pass" if passed else "fail",
        "severity": "low" if passed else "high",
        "observed": {
            "plannedAt": planned_at,
            "analyzedAt": analyzed_at,
            "digest": pre.get("digest"),
            "unchanged": unchanged,
            "gitSha": pre.get("gitSha"),
        },
        "expected": {"unchanged": True, "plannedBeforeAnalyzed": True},
        "message": (
            "the pre-registration was written before analysis and its digest is unchanged"
            if passed
            else "the pre-registration is missing, was edited after planning, or postdates "
            "the analysis"
        ),
    }


def _inference_check(
    record: Mapping[str, Any], results: Mapping[str, Any]
) -> dict[str, Any]:
    """State plainly whether any statistical inference was performed.

    Emitting this as ``not_run`` is the honest alternative to leaving OES's
    ``pValue``/``power``/``sampleSizes`` fields conspicuously empty and letting a
    reader assume they were simply lost in translation.
    """
    check_type = f"{EXTENSION_PREFIX}statistical_inference"
    if _has_statistical_inference(results):
        design = record.get("design") if isinstance(record.get("design"), Mapping) else {}
        return {
            "checkType": check_type,
            "status": "pass",
            "observed": {"randomizationUnit": design.get("randomizationUnit")},
            "message": "inferential statistics were supplied by the measurement source",
        }
    return {
        "checkType": check_type,
        "status": "not_run",
        "severity": "low",
        "observed": {"randomizationUnit": None, "inference": "none"},
        "expected": {"inference": "frequentist_or_bayesian"},
        "message": (
            "no randomization and no live traffic: variants are build artifacts at commits "
            "compared by deterministic measurement, so p-values, statistical power and "
            "sample-ratio checks do not exist for this run and were not fabricated"
        ),
    }


def _artifacts(run: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ADLC artifacts into ones OES can name and ones it cannot.

    The enum has no member for a Playwright trace, a HAR or a JSONL log, so those
    are referenced under ``extensions["adlc:artifacts"]`` with their kind and
    hash intact. Mislabelling a trace as ``image`` to satisfy the enum would make
    the document lie.
    """
    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for raw in run.get("artifacts") or []:
        if not isinstance(raw, Mapping) or not raw.get("path"):
            continue
        kind = str(raw.get("kind") or "")
        mime = str(raw.get("mimeType") or "")
        oes_type = ARTIFACT_KIND_TO_OES.get(kind) or MIME_TYPE_TO_OES.get(mime)
        entry: dict[str, Any] = {
            "uri": str(raw["path"]),
            "source": SOURCE_SYSTEM,
        }
        if kind:
            entry[f"{EXTENSION_PREFIX}kind"] = kind
        if mime:
            entry["mimeType"] = mime
        if raw.get("sha256"):
            entry["hash"] = f"sha256:{raw['sha256']}"
        if raw.get("bytes") is not None:
            entry[f"{EXTENSION_PREFIX}bytes"] = raw["bytes"]

        if oes_type in ARTIFACT_TYPES:
            mapped.append({"type": oes_type, **entry})
        else:
            unmapped.append(
                {
                    **entry,
                    f"{EXTENSION_PREFIX}reason": (
                        f"ADLC kind '{kind or mime or 'unknown'}' has no member in the OES "
                        "artifacts[].type enum"
                    ),
                }
            )
    return mapped, unmapped


def _decision(run: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = run.get("decision")
    if not isinstance(raw, Mapping) or not raw:
        return None
    decision: dict[str, Any] = {"status": "decided"}
    outcome = _enum_or_none(raw.get("outcome"), DECISION_OUTCOMES)
    if outcome:
        decision["outcome"] = outcome
    if raw.get("rationale"):
        decision["rationale"] = str(raw["rationale"])
    if raw.get("decidedAt"):
        decision["decidedAt"] = str(raw["decidedAt"])
    if raw.get("decidedBy"):
        # ADLC records a person; OES expects an object.
        decision["decidedBy"] = {"name": str(raw["decidedBy"]), "role": "reviewer"}
    for field in ("reviewSha", "adr"):
        if raw.get(field):
            decision[f"{EXTENSION_PREFIX}{field}"] = str(raw[field])
    return decision


def _scorecard(
    run: Mapping[str, Any],
    record: Mapping[str, Any],
    results: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The curated, human-facing summary."""
    metric_results = list(results.get("metricResults") or [])
    positive = [r for r in metric_results if r.get("resultStatus") == "positive"]
    negative = [r for r in metric_results if r.get("resultStatus") == "negative"]
    blocking = [r for r in metric_results if r.get("decisionImpact") == "blocks_ship"]

    if not metric_results:
        overall = "inconclusive"
    elif negative and positive:
        overall = "mixed"
    elif positive:
        overall = "win"
    elif negative:
        overall = "loss"
    else:
        overall = "neutral"

    required_failed = [
        c
        for c in checks
        if c.get(f"{EXTENSION_PREFIX}required") and c.get("status") in ("fail", "not_run")
    ]
    optional_failed = [
        c
        for c in checks
        if not c.get(f"{EXTENSION_PREFIX}required") and c.get("status") == "fail"
    ]
    if required_failed:
        quality = "invalid"
    elif optional_failed:
        quality = "warning"
    elif any(c.get("status") == "pass" for c in checks):
        quality = "valid"
    else:
        quality = "needs_review"

    experiment = record.get("experiment") if isinstance(record.get("experiment"), Mapping) else {}
    decision = run.get("decision") if isinstance(run.get("decision"), Mapping) else {}
    summary = str(
        decision.get("rationale")
        or experiment.get("summary")
        or (
            f"{len(metric_results)} metric comparison(s) across "
            f"{len(record.get('variants') or run.get('variants') or [])} variants of ADLC run "
            f"{run.get('runId')}"
        )
    )

    scorecard: dict[str, Any] = {
        "summary": summary,
        "overallResult": overall if overall in SCORECARD_RESULTS else "inconclusive",
        "qualityStatus": quality if quality in SCORECARD_QUALITY else "needs_review",
    }
    action = DECISION_TO_SCORECARD_ACTION.get(str(decision.get("outcome") or ""))
    if action in SCORECARD_ACTIONS:
        scorecard["recommendedAction"] = action

    findings = [
        f"{r['metricId']}: {r.get('baselineValue')} → {r.get('variantValue')} "
        f"({r.get('resultStatus', 'unknown')}) for variant "
        f"{r.get('comparison', {}).get('variantId')}"
        for r in metric_results
    ]
    if findings:
        scorecard["keyFindings"] = findings

    risks = [
        f"{c['checkType']}: {c.get('message') or c.get('status')}"
        for c in checks
        if c.get("status") in ("fail", "warn")
    ]
    if blocking:
        risks.append(
            f"{len(blocking)} metric comparison(s) are marked blocks_ship"
        )
    if risks:
        scorecard["risks"] = risks
    return scorecard


def _provenance(run: Mapping[str, Any], results: Mapping[str, Any]) -> dict[str, Any]:
    version = _adlc_version()
    provenance: dict[str, Any] = {
        "createdBy": {"system": SOURCE_SYSTEM},
        "exportedBy": {"system": SOURCE_SYSTEM, "version": version},
        "analysisGeneratedBy": f"{SOURCE_SYSTEM}/{version}",
        "resultHash": _canonical_hash(results),
    }
    if run.get("headSha"):
        provenance["codeVersion"] = str(run["headSha"])
    artifacts = run.get("artifacts") or []
    if artifacts:
        provenance["attachmentsHash"] = _canonical_hash(
            [a.get("sha256") for a in artifacts if isinstance(a, Mapping)]
        )
    return provenance


def _external_ids(run: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, str]:
    """OES requires every ``externalIds`` value to be a string."""
    ids: dict[str, str] = {}
    if run.get("runId"):
        ids["adlc_run"] = str(run["runId"])
    if run.get("repo"):
        ids["github_repo"] = str(run["repo"])
    if run.get("prNumber") is not None:
        ids["github_pr"] = str(run["prNumber"])
    declared = record.get("externalIds")
    intake = _stage_data(run, "intake")
    issue = (
        (declared or {}).get("github_issue")
        if isinstance(declared, Mapping)
        else None
    ) or intake.get("issue") or intake.get("issueNumber")
    if issue is not None:
        ids["github_issue"] = str(issue)
    if isinstance(declared, Mapping):
        for key, value in declared.items():
            if value is not None:
                ids.setdefault(str(key), str(value))
    return ids


def _extensions(
    run: Mapping[str, Any],
    record: Mapping[str, Any],
    unmapped_artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Everything ADLC-specific, namespaced. Importers MUST ignore what they do not know."""
    extensions: dict[str, Any] = {
        f"{EXTENSION_PREFIX}canonicalRecord": {
            "schemaVersion": run.get("schemaVersion"),
            "note": (
                "adlc-run/v1 is the canonical record for this work; this OES document is a "
                "lossy export produced for interchange only"
            ),
        },
        f"{EXTENSION_PREFIX}run": {
            key: run.get(key)
            for key in (
                "runId",
                "repo",
                "baseSha",
                "headSha",
                "prNumber",
                "status",
                "profile",
                "referencesRun",
                "experimentRef",
                "createdAt",
            )
            if run.get(key) is not None
        },
        f"{EXTENSION_PREFIX}statistics": {
            "inference": "none",
            "note": (
                "variants are build artifacts at commits; there is no randomization unit, no "
                "traffic allocation and no sampling distribution, so no p-value, confidence "
                "interval or statistical power is reported"
            ),
        },
    }
    if run.get("capabilities"):
        extensions[f"{EXTENSION_PREFIX}capabilities"] = dict(run["capabilities"])
    if run.get("gates"):
        extensions[f"{EXTENSION_PREFIX}gates"] = [dict(g) for g in run["gates"]]
    if unmapped_artifacts:
        extensions[f"{EXTENSION_PREFIX}artifacts"] = [dict(a) for a in unmapped_artifacts]
    if record.get("measurements"):
        extensions[f"{EXTENSION_PREFIX}measurements"] = [
            dict(m) for m in record["measurements"] if isinstance(m, Mapping)
        ]
    if record.get("exposure"):
        extensions[f"{EXTENSION_PREFIX}exposure"] = record["exposure"]
    if record.get("phases"):
        extensions[f"{EXTENSION_PREFIX}experimentStage"] = record["phases"]
    return extensions


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _adlc_version() -> str:
    try:
        import adlc

        return str(getattr(adlc, "__version__", "0.1.0"))
    except Exception:  # noqa: BLE001 - version reporting is never load-bearing
        return "0.1.0"


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _exported_at() -> str:
    """UTC timestamp, honouring ``SOURCE_DATE_EPOCH`` for reproducible exports."""
    epoch = (os.environ.get("SOURCE_DATE_EPOCH") or "").strip()
    moment = datetime.fromtimestamp(int(epoch), UTC) if epoch.isdigit() else datetime.now(UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def build_oes_document(
    run: Run | Mapping[str, Any],
    *,
    run_dir: Path | None = None,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build the OES document for a comparative run.

    Does not itself refuse non-comparative runs -- :meth:`OesExporter.export` does
    that -- so callers can inspect a partial mapping when debugging.
    """
    record = experiment_record(run)
    variants = _variants(run, record)
    baseline = record.get("baselineVariantKey") or baseline_variant_key(variants)
    definitions = _metric_definitions(record, run_dir)
    metrics = _oes_metrics(definitions)
    results = _results(record, definitions, str(baseline) if baseline else None)
    checks = _quality_checks(run, record, results)
    mapped_artifacts, unmapped_artifacts = _artifacts(run)

    document: dict[str, Any] = {
        "schemaVersion": OES_SCHEMA_VERSION,
        "objectType": OES_OBJECT_TYPE,
        "exportedAt": exported_at or _exported_at(),
        "sourceSystem": SOURCE_SYSTEM,
        "sourceSystemVersion": _adlc_version(),
    }
    if run.get("repo") and run.get("prNumber") is not None:
        document["canonicalUrl"] = f"https://github.com/{run['repo']}/pull/{run['prNumber']}"

    external_ids = _external_ids(run, record)
    if external_ids:
        document["externalIds"] = external_ids

    document["experiment"] = _experiment(run, record)
    design = _design(record)
    if design:
        document["design"] = design
    if variants:
        document["variants"] = variants
    if metrics:
        document["metrics"] = metrics
    analysis = _analysis(record)
    if analysis:
        document["analysis"] = analysis
    document["results"] = results
    document["scorecard"] = _scorecard(run, record, results, checks)
    decision = _decision(run)
    if decision:
        document["decision"] = decision
    document["qualityChecks"] = checks
    if mapped_artifacts:
        document["artifacts"] = mapped_artifacts
    document["provenance"] = _provenance(run, results)
    document["extensions"] = _extensions(run, record, unmapped_artifacts)
    return document


def validate_oes_document(
    document: Mapping[str, Any], *, cfg: Config | None = None, schema: Mapping[str, Any] | None = None
) -> None:
    """Validate against the published OES schema, raising on the first failure set."""
    import jsonschema

    resolved = dict(schema) if schema is not None else load_oes_schema(cfg)
    validator_cls = jsonschema.validators.validator_for(resolved)
    validator = validator_cls(resolved, format_checker=validator_cls.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    detail = "; ".join(
        f"{'/' + '/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors[:5]
    )
    raise OesValidationError(
        f"generated document is not valid against OES {OES_SCHEMA_VERSION} "
        f"({len(errors)} error(s)): {detail}"
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OesExporter:
    """Export a comparative ADLC run as an OES v0.1.0 document.

    Registered as the ``oes`` entry point in ``adlc.export``. Refuses, loudly and
    specifically, whenever the run is not genuinely comparative.
    """

    name = "oes"
    kind = "export"

    def __init__(self, run_dir: Path | str | None = None, cfg: Config | None = None) -> None:
        self._run_dir = Path(run_dir) if run_dir else None
        self._cfg = cfg

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, non-raising, offline availability probe."""
        try:
            if importlib.util.find_spec("jsonschema") is None:
                return False, "jsonschema is not installed, so OES output cannot be validated"
            override = schema_override_path(cfg)
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"OES exporter probe failed: {exc}"
        if override is not None and not override.is_file():
            return False, (
                f"OES schema override '{override}' does not exist "
                f"(unset {OES_SCHEMA_ENV} to use the vendored copy)"
            )
        source = str(override) if override is not None else f"vendored {OES_SCHEMA_URL}"
        return True, (
            f"OES {OES_SCHEMA_VERSION} exporter available (schema: {source}); emits a document "
            "only for comparative runs with >= 2 variants and measured outcomes"
        )

    def export(self, run: Run, out: Path) -> Path:
        """Write ``oes.json`` for ``run``, or refuse.

        :raises NotComparativeError: the run is not a comparative experiment.
        :raises OesValidationError: the mapping produced an invalid document.
        """
        comparative, reason = is_comparative(run)
        if not comparative:
            raise NotComparativeError(
                f"refusing to export OES for run '{run.get('runId')}': {reason}"
            )

        out = Path(out)
        if out.is_dir() or not out.suffix:
            out = out / DEFAULT_OUTPUT_NAME
        run_dir = self._run_dir or _infer_run_dir(out)

        document = build_oes_document(run, run_dir=run_dir)
        validate_oes_document(document, cfg=self._cfg)

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return out


def _infer_run_dir(out: Path) -> Path | None:
    """Locate the run directory containing ``out``, for ``enrichment/`` lookups."""
    parent = out.parent
    if (parent / "stages").is_dir() or (parent / "run.json").is_file():
        return parent
    return None


# ---------------------------------------------------------------------------
# Vendored schema
#
# Fetched verbatim from OES_SCHEMA_URL. Kept inline so that `adlc export oes`
# validates its own output with no network access, in an air-gapped runner, and
# in the credential-free conformance suite. tests/l7_experiment asserts this copy
# is byte-for-byte equivalent to the published document.
# ---------------------------------------------------------------------------

_VENDORED_OES_SCHEMA = r"""
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://openexperiment.org/schema/openexperiment-0.1.0.schema.json",
  "title": "Open Experiment Standard",
  "description": "OES v0.1.0 — a vendor-neutral, machine-readable standard for documenting, exchanging, archiving, and presenting online experiment designs, results, scorecards, decisions, and supporting artifacts.",
  "type": "object",
  "required": ["schemaVersion", "objectType", "experiment"],
  "additionalProperties": true,
  "properties": {
    "schemaVersion": {
      "type": "string",
      "description": "Version of the OES standard the document conforms to.",
      "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?$"
    },
    "objectType": {
      "type": "string",
      "enum": ["experiment"],
      "description": "Top-level object type. Reserved for future expansion."
    },
    "exportedAt": { "type": "string", "format": "date-time" },
    "sourceSystem": { "type": "string" },
    "sourceSystemVersion": { "type": "string" },
    "canonicalUrl": { "type": "string", "format": "uri" },
    "externalIds": {
      "type": "object",
      "additionalProperties": { "type": "string" }
    },

    "experiment": {
      "type": "object",
      "required": ["id", "title"],
      "additionalProperties": true,
      "properties": {
        "id": { "type": "string", "minLength": 1 },
        "slug": { "type": "string" },
        "title": { "type": "string", "minLength": 1 },
        "summary": { "type": "string" },
        "description": { "type": "string" },
        "hypothesis": { "type": "string" },
        "learningGoal": { "type": "string" },
        "businessGoal": { "type": "string" },
        "productArea": { "type": "string" },
        "tags": { "type": "array", "items": { "type": "string" } },
        "status": {
          "type": "string",
          "enum": [
            "draft",
            "planned",
            "running",
            "stopped",
            "analyzed",
            "decided",
            "archived"
          ]
        },
        "owner": { "type": "object" },
        "stakeholders": { "type": "array", "items": { "type": "object" } },
        "links": { "type": "array", "items": { "type": "object" } }
      }
    },

    "design": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "type": {
          "type": "string",
          "enum": [
            "ab",
            "abn",
            "multivariate",
            "factorial",
            "holdout",
            "switchback",
            "bandit",
            "quasi_experiment"
          ]
        },
        "randomizationUnit": { "type": "string" },
        "analysisUnit": { "type": "string" },
        "assignmentMethod": { "type": "string" },
        "hashAttribute": { "type": "string" },
        "hashSalt": { "type": "string" },
        "namespace": { "type": "string" },
        "population": { "type": "string" },
        "targetingRules": { "type": "array", "items": { "type": "object" } },
        "exclusionRules": { "type": "array", "items": { "type": "object" } },
        "trafficAllocation": { "type": "number", "minimum": 0, "maximum": 1 },
        "variantAllocation": { "type": "object", "additionalProperties": { "type": "number" } },
        "startDate": { "type": "string", "format": "date-time" },
        "endDate": { "type": "string", "format": "date-time" },
        "exposureDefinition": { "type": "string" },
        "triggerDefinition": { "type": "string" },
        "rampSchedule": { "type": "array", "items": { "type": "object" } },
        "concurrentExperiments": { "type": "array", "items": { "type": "string" } },
        "interferenceRisk": { "type": "object" },
        "power": { "type": "number", "minimum": 0, "maximum": 1 },
        "minimumDetectableEffect": { "type": "number" },
        "alpha": { "type": "number", "minimum": 0, "maximum": 1 },
        "multipleTestingPolicy": {
          "type": "string",
          "enum": ["none", "bonferroni", "fdr", "hierarchical", "metric_family", "benjamini_hochberg", "custom"]
        },
        "peekingPolicy": {
          "type": "string",
          "enum": ["fixed_horizon", "sequential", "always_valid", "bayesian_monitoring", "informal"]
        },
        "stoppingRule": { "type": "string" }
      }
    },

    "variants": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "key"],
        "additionalProperties": true,
        "properties": {
          "id": { "type": "string", "minLength": 1 },
          "key": { "type": "string", "minLength": 1 },
          "name": { "type": "string" },
          "role": { "type": "string", "enum": ["control", "treatment", "holdout", "baseline"] },
          "description": { "type": "string" },
          "allocation": { "type": "number", "minimum": 0, "maximum": 1 },
          "featureFlagKeys": { "type": "array", "items": { "type": "string" } },
          "config": { "type": "object" },
          "screenshots": { "type": "array", "items": { "type": "object" } },
          "urls": { "type": "array", "items": { "type": "object" } },
          "codeReferences": { "type": "array", "items": { "type": "object" } }
        }
      }
    },

    "metrics": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "name"],
        "additionalProperties": true,
        "properties": {
          "id": { "type": "string", "minLength": 1 },
          "name": { "type": "string", "minLength": 1 },
          "description": { "type": "string" },
          "role": {
            "type": "string",
            "enum": ["primary", "secondary", "guardrail", "diagnostic", "data_quality", "invariant"]
          },
          "direction": {
            "type": "string",
            "enum": ["increase_is_good", "decrease_is_good", "no_change_expected", "two_sided"]
          },
          "type": {
            "type": "string",
            "enum": ["conversion", "revenue", "count", "duration", "ratio", "retention", "percentile", "custom"]
          },
          "unit": { "type": "string" },
          "numerator": { "type": "object" },
          "denominator": { "type": "object" },
          "aggregation": {
            "type": "string",
            "enum": ["mean", "sum", "ratio", "percentile", "capped_mean", "winsorized_mean"]
          },
          "analysisWindow": { "type": "object" },
          "dataSource": { "type": "object" },
          "sql": { "type": "string" },
          "filters": { "type": "array", "items": { "type": "object" } },
          "capping": { "type": "object" },
          "covariates": { "type": "array", "items": { "type": "object" } },
          "owner": { "type": "object" }
        }
      }
    },

    "analysis": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "method": {
          "type": "string",
          "enum": ["frequentist", "bayesian", "sequential", "cuped", "diff_in_diff", "custom"]
        },
        "model": { "type": "string" },
        "varianceEstimator": {
          "type": "string",
          "enum": ["naive", "delta_method", "cluster_robust", "sandwich", "bootstrap"]
        },
        "confidenceLevel": { "type": "number", "minimum": 0, "maximum": 1 },
        "alpha": { "type": "number", "minimum": 0, "maximum": 1 },
        "prior": { "type": "object" },
        "adjustmentMethod": { "type": "string" },
        "multipleComparisonCorrection": { "type": "string" },
        "segmentation": { "type": "array", "items": { "type": "string" } },
        "dimensionBreakdowns": { "type": "array", "items": { "type": "string" } },
        "missingDataHandling": { "type": "string" },
        "outlierHandling": { "type": "string" },
        "queryReferences": { "type": "array", "items": { "type": "object" } },
        "generatedAt": { "type": "string", "format": "date-time" }
      }
    },

    "results": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "sampleSizes": { "type": "object", "additionalProperties": { "type": "number" } },
        "exposures": { "type": "object", "additionalProperties": { "type": "number" } },
        "metricResults": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["metricId", "comparison"],
            "additionalProperties": true,
            "properties": {
              "metricId": { "type": "string", "minLength": 1 },
              "role": { "type": "string" },
              "comparison": {
                "type": "object",
                "required": ["baselineVariantId", "variantId"],
                "properties": {
                  "baselineVariantId": { "type": "string" },
                  "variantId": { "type": "string" }
                }
              },
              "baselineValue": { "type": "number" },
              "variantValue": { "type": "number" },
              "absoluteDifference": { "type": "number" },
              "relativeDifference": { "type": "number" },
              "standardError": { "type": "number" },
              "confidenceInterval": {
                "type": "object",
                "properties": {
                  "level": { "type": "number", "minimum": 0, "maximum": 1 },
                  "lower": { "type": "number" },
                  "upper": { "type": "number" }
                }
              },
              "credibleInterval": {
                "type": "object",
                "properties": {
                  "level": { "type": "number", "minimum": 0, "maximum": 1 },
                  "lower": { "type": "number" },
                  "upper": { "type": "number" }
                }
              },
              "pValue": { "type": "number", "minimum": 0, "maximum": 1 },
              "qValue": { "type": "number", "minimum": 0, "maximum": 1 },
              "probabilityOfImprovement": { "type": "number", "minimum": 0, "maximum": 1 },
              "expectedLoss": { "type": "number" },
              "statisticalPowerObserved": { "type": "number", "minimum": 0, "maximum": 1 },
              "resultStatus": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "inconclusive", "invalid"]
              },
              "decisionImpact": {
                "type": "string",
                "enum": ["supports_ship", "blocks_ship", "needs_followup", "informational"]
              }
            }
          }
        },
        "segmentResults": { "type": "array", "items": { "type": "object" } },
        "timeSeriesResults": { "type": "array", "items": { "type": "object" } },
        "variantComparisons": { "type": "array", "items": { "type": "object" } }
      }
    },

    "scorecard": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "summary": { "type": "string" },
        "primaryOutcome": { "type": "object" },
        "guardrailOutcomes": { "type": "array", "items": { "type": "object" } },
        "secondaryOutcomes": { "type": "array", "items": { "type": "object" } },
        "qualityStatus": {
          "type": "string",
          "enum": ["valid", "warning", "invalid", "needs_review"]
        },
        "overallResult": {
          "type": "string",
          "enum": ["win", "loss", "neutral", "mixed", "inconclusive", "invalid"]
        },
        "recommendedAction": {
          "type": "string",
          "enum": ["ship", "do_not_ship", "iterate", "rerun", "continue_running", "roll_back"]
        },
        "keyFindings": { "type": "array", "items": { "type": "string" } },
        "risks": { "type": "array", "items": { "type": "string" } },
        "presentationNotes": { "type": "string" },
        "charts": { "type": "array", "items": { "type": "object" } }
      }
    },

    "decision": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "status": { "type": "string", "enum": ["pending", "decided", "superseded"] },
        "outcome": {
          "type": "string",
          "enum": ["ship", "do_not_ship", "iterate", "rerun", "rollback", "partial_rollout"]
        },
        "rationale": { "type": "string" },
        "decidedBy": { "type": "object" },
        "decidedAt": { "type": "string", "format": "date-time" },
        "followUps": { "type": "array", "items": { "type": "object" } },
        "rolloutPlan": { "type": "object" },
        "productChanges": { "type": "string" },
        "businessImpactEstimate": { "type": "object" }
      }
    },

    "qualityChecks": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["checkType"],
        "additionalProperties": true,
        "properties": {
          "checkType": { "type": "string", "minLength": 1 },
          "status": { "type": "string", "enum": ["pass", "warn", "fail", "not_run"] },
          "severity": { "type": "string", "enum": ["low", "medium", "high", "critical"] },
          "observed": {},
          "expected": {},
          "pValue": { "type": "number", "minimum": 0, "maximum": 1 },
          "message": { "type": "string" }
        }
      }
    },

    "artifacts": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["type", "uri"],
        "additionalProperties": true,
        "properties": {
          "type": {
            "type": "string",
            "enum": [
              "chart",
              "screenshot",
              "sql",
              "notebook",
              "csv",
              "dashboard",
              "slide",
              "image",
              "html_report"
            ]
          },
          "title": { "type": "string" },
          "description": { "type": "string" },
          "uri": { "type": "string" },
          "mimeType": { "type": "string" },
          "generatedAt": { "type": "string", "format": "date-time" },
          "source": { "type": "string" },
          "hash": { "type": "string" }
        }
      }
    },

    "provenance": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "createdBy": { "type": "object" },
        "exportedBy": { "type": "object" },
        "analysisGeneratedBy": { "type": "string" },
        "dataSources": { "type": "array", "items": { "type": "object" } },
        "queryIds": { "type": "array", "items": { "type": "string" } },
        "codeVersion": { "type": "string" },
        "metricDefinitionVersion": { "type": "string" },
        "assignmentSource": { "type": "object" },
        "exposureSource": { "type": "object" },
        "resultHash": { "type": "string" },
        "attachmentsHash": { "type": "string" }
      }
    },

    "extensions": {
      "type": "object",
      "description": "Namespaced vendor-specific fields. Importers MUST safely ignore unknown extensions.",
      "additionalProperties": true
    }
  }
}
"""
