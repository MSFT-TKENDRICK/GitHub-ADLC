"""Export a golden comparative run and validate it against the real OES schema.

The oracle is the vendored copy of the published schema loaded from ``data/``,
not the copy embedded in the exporter, so these assertions cannot be satisfied by
a mistake that lives in both places.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from adlc.adapters.export.oes import (
    EXTENSION_PREFIX,
    OesExporter,
    build_oes_document,
    experiment_record,
    is_comparative,
)

#: OES fields that only exist for a randomized online experiment. If any of these
#: ever appear in ADLC output, something fabricated them.
FABRICATION_MARKERS = (
    "pValue",
    "qValue",
    "standardError",
    "confidenceInterval",
    "credibleInterval",
    "statisticalPowerObserved",
    "probabilityOfImprovement",
    "expectedLoss",
    "power",
    "minimumDetectableEffect",
    "trafficAllocation",
    "variantAllocation",
    "randomizationUnit",
    "sampleSizes",
    "exposures",
    "hashSalt",
)


def _validate(document: Any, schema: dict[str, Any]) -> None:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema, format_checker=validator_cls.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )


def _keys(node: Any) -> set[str]:
    """Every key appearing anywhere in a nested structure."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= _keys(item)
    return found


@pytest.fixture
def document(comparative_run: dict[str, Any]) -> dict[str, Any]:
    return build_oes_document(comparative_run, exported_at="2026-08-19T12:00:00Z")


# -- the fixture itself -------------------------------------------------------


def test_golden_fixture_is_a_valid_adlc_run(
    comparative_run: dict[str, Any], adlc_run_schema: dict[str, Any]
) -> None:
    """The input has to be a real ``adlc-run/v1`` document or the test proves nothing."""
    _validate(comparative_run, adlc_run_schema)


# -- the export ---------------------------------------------------------------


def test_export_validates_against_the_published_schema(
    comparative_run: dict[str, Any], oes_schema: dict[str, Any], tmp_path: Path
) -> None:
    out = OesExporter().export(comparative_run, tmp_path / "oes.json")
    assert out.is_file()
    _validate(json.loads(out.read_text(encoding="utf-8")), oes_schema)


def test_export_to_a_directory_writes_oes_json(
    comparative_run: dict[str, Any], tmp_path: Path
) -> None:
    out = OesExporter().export(comparative_run, tmp_path)
    assert out.name == "oes.json"


def test_export_is_reproducible_under_source_date_epoch(
    comparative_run: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1789000000")
    first = OesExporter().export(comparative_run, tmp_path / "a.json").read_bytes()
    second = OesExporter().export(comparative_run, tmp_path / "b.json").read_bytes()
    assert first == second
    assert json.loads(first)["exportedAt"] == "2026-09-10T00:26:40Z"


# -- header, identity and provenance -----------------------------------------


def test_header_identifies_adlc_as_the_source(document: dict[str, Any]) -> None:
    assert document["schemaVersion"] == "0.1.0"
    assert document["objectType"] == "experiment"
    assert document["sourceSystem"] == "adlc"
    assert document["sourceSystemVersion"] == "0.1.0"


def test_canonical_url_points_at_the_pull_request(document: dict[str, Any]) -> None:
    assert document["canonicalUrl"] == "https://github.com/octo-org/octo-app/pull/42"


def test_external_ids_are_strings(document: dict[str, Any]) -> None:
    assert document["externalIds"]["github_pr"] == "42"
    assert document["externalIds"]["github_issue"] == "118"
    assert document["externalIds"]["github_repo"] == "octo-org/octo-app"
    assert document["externalIds"]["adlc_run"] == "2026-08-19-a1b2"
    assert all(isinstance(v, str) for v in document["externalIds"].values())


def test_provenance_binds_the_document_to_a_commit(document: dict[str, Any]) -> None:
    provenance = document["provenance"]
    assert provenance["codeVersion"] == "c7e2b4d6081a5c3e7f902b4d6081a5c3e7f90213"
    assert provenance["createdBy"] == {"system": "adlc"}
    assert provenance["analysisGeneratedBy"] == "adlc/0.1.0"
    assert provenance["resultHash"].startswith("sha256:")


# -- experiment, variants, metrics --------------------------------------------


def test_experiment_carries_the_pre_registered_hypothesis(document: dict[str, Any]) -> None:
    experiment = document["experiment"]
    assert experiment["id"] == "exp-dark-mode-a1b2"
    assert experiment["title"].startswith("Dark mode")
    assert "design tokens" in experiment["hypothesis"]
    # The run carries a decision, so the experiment is decided rather than analyzed.
    assert experiment["status"] == "decided"


def test_variants_carry_flag_keys_and_code_references(document: dict[str, Any]) -> None:
    variants = {v["key"]: v for v in document["variants"]}
    assert set(variants) == {"control", "candidate-a"}
    assert variants["control"]["role"] == "control"
    assert variants["candidate-a"]["role"] == "treatment"
    assert variants["candidate-a"]["featureFlagKeys"] == ["adlc.exp.a1b2"]
    reference = variants["candidate-a"]["codeReferences"][0]
    assert reference["type"] == "git_commit"
    assert reference["value"] == "c7e2b4d6081a5c3e7f902b4d6081a5c3e7f90213"
    assert reference["repo"] == "octo-org/octo-app"
    assert all(v["id"] == v["key"] for v in document["variants"])


def test_metrics_namespace_the_adlc_only_budget(document: dict[str, Any]) -> None:
    metrics = {m["id"]: m for m in document["metrics"]}
    assert set(metrics) == {"lcp_ms", "a11y_violations", "rubric_overall", "bundle_kb"}
    assert metrics["lcp_ms"]["role"] == "primary"
    assert metrics["lcp_ms"]["direction"] == "decrease_is_good"
    assert metrics["lcp_ms"]["unit"] == "ms"
    # OES metrics have no budget field, so ours is namespaced rather than faked.
    assert metrics["lcp_ms"][f"{EXTENSION_PREFIX}budget"] == 2500
    assert "budget" not in metrics["lcp_ms"]
    assert metrics["rubric_overall"][f"{EXTENSION_PREFIX}source"] == "rubric.yaml"


# -- results ------------------------------------------------------------------


def test_results_compare_every_metric_against_the_control(document: dict[str, Any]) -> None:
    results = {r["metricId"]: r for r in document["results"]["metricResults"]}
    assert set(results) == {"lcp_ms", "a11y_violations", "rubric_overall", "bundle_kb"}
    lcp = results["lcp_ms"]
    assert lcp["comparison"] == {"baselineVariantId": "control", "variantId": "candidate-a"}
    assert lcp["baselineValue"] == 2000
    assert lcp["variantValue"] == 1750
    assert lcp["absoluteDifference"] == -250
    assert lcp["relativeDifference"] == -0.125
    assert lcp["resultStatus"] == "positive"
    assert lcp["decisionImpact"] == "supports_ship"
    # A regression against a declared budget is reported as such, not smoothed over.
    assert results["bundle_kb"]["resultStatus"] == "negative"
    assert results["bundle_kb"]["decisionImpact"] == "needs_followup"
    assert results["bundle_kb"][f"{EXTENSION_PREFIX}budgetPassed"] is False


def test_results_are_recomputed_when_the_stage_did_not_record_them(
    comparative_run: dict[str, Any]
) -> None:
    """A run whose analyze phase stored only measurements still exports fully."""
    stripped = copy.deepcopy(comparative_run)
    for stage in stripped["stages"]:
        if stage["stage"] == "experiment":
            stage["data"].pop("results", None)
    recomputed = build_oes_document(stripped, exported_at="2026-08-19T12:00:00Z")
    original = build_oes_document(comparative_run, exported_at="2026-08-19T12:00:00Z")
    assert recomputed["results"] == original["results"]


def test_no_statistical_quantity_is_fabricated(document: dict[str, Any]) -> None:
    present = _keys(
        {
            key: document[key]
            for key in ("design", "results", "analysis", "scorecard")
            if key in document
        }
    )
    assert not present & set(FABRICATION_MARKERS)


def test_measurement_basis_is_recorded_on_every_comparison(document: dict[str, Any]) -> None:
    for result in document["results"]["metricResults"]:
        assert result[f"{EXTENSION_PREFIX}measurementBasis"] == "deterministic_single_measurement"


def test_design_is_a_quasi_experiment(document: dict[str, Any]) -> None:
    design = document["design"]
    assert design["type"] == "quasi_experiment"
    assert design["analysisUnit"] == "build_run"
    assert "not live user traffic" in design["exposureDefinition"]


# -- quality checks -----------------------------------------------------------


def test_every_gate_becomes_a_namespaced_quality_check(
    document: dict[str, Any], comparative_run: dict[str, Any]
) -> None:
    checks = {c["checkType"]: c for c in document["qualityChecks"]}
    for gate in comparative_run["gates"]:
        assert f"{EXTENSION_PREFIX}{gate['id']}" in checks
    security = checks[f"{EXTENSION_PREFIX}security"]
    assert security["status"] == "pass"
    assert security["severity"] == "critical"
    assert security["observed"]["alerts"] == 0
    assert security[f"{EXTENSION_PREFIX}required"] is True
    assert security[f"{EXTENSION_PREFIX}evidence"] == ["gates/security.json"]


def test_a_not_run_gate_stays_not_run(document: dict[str, Any]) -> None:
    governance = next(
        c for c in document["qualityChecks"] if c["checkType"] == f"{EXTENSION_PREFIX}governance"
    )
    assert governance["status"] == "not_run"
    assert governance[f"{EXTENSION_PREFIX}required"] is False
    assert "agent-governance-toolkit" in governance["message"]


def test_aggregate_check_reflects_the_fail_closed_verdict(document: dict[str, Any]) -> None:
    aggregate = next(
        c for c in document["qualityChecks"] if c["checkType"] == f"{EXTENSION_PREFIX}aggregate"
    )
    assert aggregate["status"] == "pass"
    assert aggregate["observed"]["failingRequiredGates"] == []


def test_a_required_not_run_gate_fails_the_aggregate(comparative_run: dict[str, Any]) -> None:
    run = copy.deepcopy(comparative_run)
    for gate in run["gates"]:
        if gate["id"] == "security":
            gate["status"] = "not_run"
    document = build_oes_document(run, exported_at="2026-08-19T12:00:00Z")
    aggregate = next(
        c for c in document["qualityChecks"] if c["checkType"] == f"{EXTENSION_PREFIX}aggregate"
    )
    assert aggregate["status"] == "fail"
    assert aggregate["observed"]["failingRequiredGates"] == ["security"]
    assert document["scorecard"]["qualityStatus"] == "invalid"


def test_pre_registration_check_passes_for_an_unedited_plan(document: dict[str, Any]) -> None:
    check = next(
        c
        for c in document["qualityChecks"]
        if c["checkType"] == f"{EXTENSION_PREFIX}pre_registration"
    )
    assert check["status"] == "pass"
    assert check["observed"]["unchanged"] is True
    assert check["observed"]["plannedAt"] < check["observed"]["analyzedAt"]


def test_pre_registration_check_fails_when_the_plan_was_edited(
    comparative_run: dict[str, Any]
) -> None:
    run = copy.deepcopy(comparative_run)
    for stage in run["stages"]:
        if stage["stage"] == "experiment" and stage["data"].get("phase") == "analyze":
            stage["data"]["preRegistration"]["unchanged"] = False
    document = build_oes_document(run, exported_at="2026-08-19T12:00:00Z")
    check = next(
        c
        for c in document["qualityChecks"]
        if c["checkType"] == f"{EXTENSION_PREFIX}pre_registration"
    )
    assert check["status"] == "fail"
    assert check["severity"] == "high"


def test_statistical_inference_is_declared_not_run(document: dict[str, Any]) -> None:
    check = next(
        c
        for c in document["qualityChecks"]
        if c["checkType"] == f"{EXTENSION_PREFIX}statistical_inference"
    )
    assert check["status"] == "not_run"
    assert "were not fabricated" in check["message"]


# -- artifacts ----------------------------------------------------------------


def test_only_enum_legal_artifacts_are_promoted(
    document: dict[str, Any], oes_schema: dict[str, Any]
) -> None:
    allowed = set(oes_schema["properties"]["artifacts"]["items"]["properties"]["type"]["enum"])
    types = {a["type"] for a in document["artifacts"]}
    assert types <= allowed
    assert types == {"screenshot", "html_report", "csv"}


def test_traces_har_and_jsonl_go_to_extensions(document: dict[str, Any]) -> None:
    """The artifact-enum limitation, handled by reference rather than mislabelling."""
    promoted = {a["uri"] for a in document["artifacts"]}
    referenced = {a["uri"] for a in document["extensions"][f"{EXTENSION_PREFIX}artifacts"]}
    assert "evidence/candidate-a/trace.zip" in referenced
    assert "evidence/candidate-a/network.har" in referenced
    assert "evidence/candidate-a/console.jsonl" in referenced
    assert not promoted & referenced
    trace = next(
        a
        for a in document["extensions"][f"{EXTENSION_PREFIX}artifacts"]
        if a["uri"].endswith("trace.zip")
    )
    assert trace[f"{EXTENSION_PREFIX}kind"] == "playwright_trace"
    assert trace["hash"].startswith("sha256:")
    assert "no member in the OES" in trace[f"{EXTENSION_PREFIX}reason"]


def test_promoted_artifacts_keep_their_adlc_kind_and_hash(document: dict[str, Any]) -> None:
    report = next(a for a in document["artifacts"] if a["type"] == "html_report")
    assert report["uri"] == "report.html"
    assert report[f"{EXTENSION_PREFIX}kind"] == "report_html"
    assert report["hash"] == (
        "sha256:e7f6c011776e8db7cd330b54174fd76f7d0216b612387a5ffcfb81e6f0919683"
    )


# -- decision, scorecard, extensions ------------------------------------------


def test_decision_maps_to_the_oes_object_shape(document: dict[str, Any]) -> None:
    decision = document["decision"]
    assert decision["outcome"] == "ship"
    assert decision["status"] == "decided"
    assert decision["decidedBy"] == {"name": "octocat", "role": "reviewer"}
    assert decision["decidedAt"] == "2026-08-19T11:30:00Z"
    assert decision[f"{EXTENSION_PREFIX}adr"] == "0004"
    assert decision[f"{EXTENSION_PREFIX}reviewSha"].startswith("c7e2b4d")


def test_scorecard_summarizes_a_mixed_result(document: dict[str, Any]) -> None:
    scorecard = document["scorecard"]
    assert scorecard["overallResult"] == "mixed"
    assert scorecard["qualityStatus"] == "valid"
    assert scorecard["recommendedAction"] == "ship"
    assert len(scorecard["keyFindings"]) == 4
    assert "bundle-size regression" in scorecard["summary"]


def test_extensions_carry_the_adlc_record_losslessly(document: dict[str, Any]) -> None:
    extensions = document["extensions"]
    assert all(key.startswith(EXTENSION_PREFIX) for key in extensions)
    assert extensions[f"{EXTENSION_PREFIX}run"]["runId"] == "2026-08-19-a1b2"
    assert extensions[f"{EXTENSION_PREFIX}run"]["headSha"].startswith("c7e2b4d")
    assert len(extensions[f"{EXTENSION_PREFIX}gates"]) == 10
    assert len(extensions[f"{EXTENSION_PREFIX}measurements"]) == 8
    assert extensions[f"{EXTENSION_PREFIX}statistics"]["inference"] == "none"
    assert "adlc-run/v1" in extensions[f"{EXTENSION_PREFIX}canonicalRecord"]["schemaVersion"]
    assert set(extensions[f"{EXTENSION_PREFIX}experimentStage"]) == {"plan", "run", "analyze"}
    assert extensions[f"{EXTENSION_PREFIX}exposure"]["flagKeys"] == ["adlc.exp.a1b2"]


# -- helpers ------------------------------------------------------------------


def test_experiment_record_folds_the_phases_in_order(comparative_run: dict[str, Any]) -> None:
    record = experiment_record(comparative_run)
    # The analyze phase's status wins over the plan phase's.
    assert record["experiment"]["status"] == "analyzed"
    assert record["baselineVariantKey"] == "control"
    assert record["phases"]["plan"]["attempt"] == 1
    assert record["phases"]["analyze"]["attempt"] == 3


def test_is_comparative_explains_why_it_accepted(comparative_run: dict[str, Any]) -> None:
    ok, reason = is_comparative(comparative_run)
    assert ok
    assert "2 variants" in reason
