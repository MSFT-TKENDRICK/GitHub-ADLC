"""Raw tool JSON -> normalised measurement mapping, against checked-in fixtures.

These are the tests that keep a collector honest: every declared budget either
becomes a measurement backed by a real number from a real artifact, or lands in
``unmeasured`` with ``status: "not_run"``. Nothing in between.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adlc.adapters.evidence.axe import AXE_CATALOGUE, extract_axe
from adlc.adapters.evidence.k6 import K6_CATALOGUE, extract_k6
from adlc.adapters.evidence.lighthouse import (
    LIGHTHOUSE_CATALOGUE,
    build_measurements,
    extract_lighthouse,
    load_benchmarks,
    metrics_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACK_SCHEMA = REPO_ROOT / "schemas" / "evidence-review-pack.schema.json"
FAKE_SHA = "a" * 64


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _measure(specs, values, collector, catalogue, sha=FAKE_SHA):
    """Mirror what a collector does once it has extracted values."""
    reasons = {
        spec["id"]: (
            "metric_unmapped",
            (
                f"'{spec['id']}' is not a known {collector} metric — declare a "
                "`source` JSON Pointer in benchmarks.yaml"
            ),
        )
        for spec in specs
        if not spec.get("source") and spec["id"] not in catalogue
    }
    return build_measurements(specs, values, collector, sha, reasons)


def _by_id(entries):
    return {entry["metricId"]: entry for entry in entries}


# ---------------------------------------------------------------------------
# Lighthouse
# ---------------------------------------------------------------------------


def test_lighthouse_extraction(lhr, run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "lighthouse")
    values = extract_lighthouse(lhr, specs)

    assert values["lcp_ms"] == pytest.approx(1820.4)
    assert values["cls"] == pytest.approx(0.042)
    assert values["tbt_ms"] == pytest.approx(137.0)
    # Category scores are reported on a 0-100 scale.
    assert values["performance_score"] == pytest.approx(94.0)
    assert values["accessibility_score"] == pytest.approx(98.0)
    assert values["best_practices_score"] == pytest.approx(83.0)
    assert values["total_byte_weight_kb"] == pytest.approx(2048.0)
    # Declared via an explicit `source` JSON Pointer.
    assert values["dom_nodes"] == pytest.approx(1210.0)
    # Absent from the report, and unknown to the catalogue: no value, ever.
    assert values["pwa_score"] is None
    assert values["made_up_lighthouse_metric"] is None


def test_lighthouse_measurements(lhr, run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "lighthouse")
    values = extract_lighthouse(lhr, specs)
    measured, unmeasured = _measure(specs, values, "lighthouse", LIGHTHOUSE_CATALOGUE)

    passed = _by_id(measured)
    assert passed["lcp_ms"]["passed"] is True
    assert passed["lcp_ms"]["budget"] == 2500
    assert passed["lcp_ms"]["collector"] == "lighthouse"
    assert passed["lcp_ms"]["artifactSha256"] == FAKE_SHA
    assert passed["performance_score"]["passed"] is True
    # 83 < 90 with higher_is_better, and 2048 KiB > 1536 KiB: both fail.
    assert passed["best_practices_score"]["passed"] is False
    assert passed["total_byte_weight_kb"]["passed"] is False

    missing = _by_id(unmeasured)
    assert set(missing) == {"pwa_score", "made_up_lighthouse_metric"}
    assert missing["pwa_score"]["status"] == "not_run"
    assert missing["pwa_score"]["cause"] == "metric_absent"
    assert missing["made_up_lighthouse_metric"]["cause"] == "metric_unmapped"
    assert "benchmarks.yaml" in missing["made_up_lighthouse_metric"]["reason"]


# ---------------------------------------------------------------------------
# k6
# ---------------------------------------------------------------------------


def test_k6_extraction(k6_summary, run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "k6")
    values = extract_k6(k6_summary, specs)

    assert values["p95_latency_ms"] == pytest.approx(331.7)
    assert values["p99_latency_ms"] == pytest.approx(702.4)
    assert values["rps"] == pytest.approx(49.86)
    assert values["error_rate"] == pytest.approx(0.004666)
    assert values["checks_failed"] == pytest.approx(7.0)
    assert values["made_up_k6_metric"] is None


def test_k6_catalogue_covers_the_documented_metrics(k6_summary) -> None:
    specs = [
        {"id": key, "collector": "k6", "budget": 1, "direction": "lower_is_better"}
        for key in K6_CATALOGUE
    ]
    values = extract_k6(k6_summary, specs)
    assert values["p50_latency_ms"] == pytest.approx(128.4)
    assert values["avg_latency_ms"] == pytest.approx(142.8)
    assert values["error_rate_pct"] == pytest.approx(0.4666)
    assert values["data_received_kb"] == pytest.approx(10240.0)
    assert values["requests_total"] == pytest.approx(1500.0)
    assert values["iterations"] == pytest.approx(1500.0)
    assert values["vus_max"] == pytest.approx(5.0)


def test_k6_measurements(k6_summary, run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "k6")
    values = extract_k6(k6_summary, specs)
    measured, unmeasured = _measure(specs, values, "k6", K6_CATALOGUE)

    results = _by_id(measured)
    assert results["p95_latency_ms"]["passed"] is True
    assert results["p99_latency_ms"]["passed"] is False  # 702.4 > 600
    assert results["rps"]["passed"] is True  # higher_is_better
    assert results["error_rate"]["passed"] is True
    assert results["checks_failed"]["passed"] is False  # 7 > 0

    missing = _by_id(unmeasured)
    assert set(missing) == {"made_up_k6_metric"}
    assert missing["made_up_k6_metric"]["status"] == "not_run"


def test_k6_summary_without_metrics_measures_nothing(run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "k6")
    measured, unmeasured = _measure(specs, extract_k6({}, specs), "k6", K6_CATALOGUE)
    assert measured == []
    assert len(unmeasured) == len(specs)
    assert all(entry["status"] == "not_run" for entry in unmeasured)


# ---------------------------------------------------------------------------
# axe
# ---------------------------------------------------------------------------


def test_axe_extraction(axe_results, run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "axe")
    values = extract_axe(axe_results, specs)

    assert values["a11y_critical_violations"] == 1.0
    assert values["a11y_serious_violations"] == 1.0
    assert values["a11y_total_violations"] == 5.0
    assert values["a11y_incomplete"] == 1.0
    assert values["made_up_axe_metric"] is None


def test_axe_catalogue_counts(axe_results) -> None:
    specs = [
        {"id": key, "collector": "axe", "budget": 1, "direction": "lower_is_better"}
        for key in AXE_CATALOGUE
    ]
    values = extract_axe(axe_results, specs)
    assert values["a11y_moderate_violations"] == 1.0
    assert values["a11y_minor_violations"] == 1.0
    assert values["a11y_violation_nodes"] == 6.0
    assert values["a11y_critical_nodes"] == 1.0
    assert values["a11y_serious_nodes"] == 2.0
    assert values["a11y_blocking_violations"] == 2.0
    assert values["a11y_passes"] == 2.0
    assert values["a11y_inapplicable"] == 1.0


def test_axe_null_impact_counts_in_the_total_only(axe_results) -> None:
    """axe reports impact: null. Assigning it a severity would be invented data."""
    specs = [
        {"id": key, "collector": "axe", "budget": 1, "direction": "lower_is_better"}
        for key in (
            "a11y_critical_violations",
            "a11y_serious_violations",
            "a11y_moderate_violations",
            "a11y_minor_violations",
            "a11y_total_violations",
        )
    ]
    values = extract_axe(axe_results, specs)
    by_impact = sum(
        values[key]
        for key in (
            "a11y_critical_violations",
            "a11y_serious_violations",
            "a11y_moderate_violations",
            "a11y_minor_violations",
        )
    )
    assert values["a11y_total_violations"] == 5.0
    assert by_impact == 4.0


def test_axe_measurements(axe_results, run_dir: Path) -> None:
    specs = metrics_for(load_benchmarks(run_dir), "axe")
    values = extract_axe(axe_results, specs)
    measured, unmeasured = _measure(specs, values, "axe", AXE_CATALOGUE)

    results = _by_id(measured)
    assert results["a11y_critical_violations"]["passed"] is False  # 1 > 0
    assert results["a11y_serious_violations"]["passed"] is False
    assert results["a11y_total_violations"]["passed"] is True  # 5 <= 10
    assert results["a11y_incomplete"]["passed"] is True

    assert [entry["metricId"] for entry in unmeasured] == ["made_up_axe_metric"]


def test_clean_axe_run_yields_a_real_zero() -> None:
    """A measured zero is legitimate -- it is an *absent* metric that must not be zero."""
    specs = [
        {
            "id": "a11y_critical_violations",
            "collector": "axe",
            "budget": 0,
            "direction": "lower_is_better",
        }
    ]
    clean = {"violations": [], "incomplete": [], "passes": [], "inapplicable": []}
    measured, unmeasured = _measure(specs, extract_axe(clean, specs), "axe", AXE_CATALOGUE)
    assert unmeasured == []
    assert measured[0]["value"] == 0.0
    assert measured[0]["passed"] is True


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("collector", "fixture_name", "extractor", "catalogue"),
    [
        ("lighthouse", "lighthouse-lhr.json", extract_lighthouse, LIGHTHOUSE_CATALOGUE),
        ("k6", "k6-summary.json", extract_k6, K6_CATALOGUE),
        ("axe", "axe-results.json", extract_axe, AXE_CATALOGUE),
    ],
)
def test_every_declared_budget_is_either_measured_or_not_run(
    collector, fixture_name, extractor, catalogue, run_dir: Path
) -> None:
    specs = metrics_for(load_benchmarks(run_dir), collector)
    measured, unmeasured = _measure(
        specs, extractor(load_fixture(fixture_name), specs), collector, catalogue
    )

    declared = {spec["id"] for spec in specs}
    measured_ids = {entry["metricId"] for entry in measured}
    unmeasured_ids = {entry["metricId"] for entry in unmeasured}
    assert measured_ids | unmeasured_ids == declared
    assert measured_ids & unmeasured_ids == set()
    assert all("value" not in entry for entry in unmeasured)
    assert all("passed" not in entry for entry in unmeasured)


@pytest.mark.parametrize(
    ("collector", "fixture_name", "extractor", "catalogue"),
    [
        ("lighthouse", "lighthouse-lhr.json", extract_lighthouse, LIGHTHOUSE_CATALOGUE),
        ("k6", "k6-summary.json", extract_k6, K6_CATALOGUE),
        ("axe", "axe-results.json", extract_axe, AXE_CATALOGUE),
    ],
)
def test_measurements_match_the_evidence_review_pack_schema(
    collector, fixture_name, extractor, catalogue, run_dir: Path
) -> None:
    """The spine must be able to copy these into the sanitised pack verbatim."""
    js = pytest.importorskip("jsonschema", reason="jsonschema is a spine dependency")

    pack = json.loads(PACK_SCHEMA.read_text(encoding="utf-8"))
    subschema = {
        "$schema": pack["$schema"],
        **pack["properties"]["measurements"],
    }
    specs = metrics_for(load_benchmarks(run_dir), collector)
    measured, _ = _measure(specs, extractor(load_fixture(fixture_name), specs), collector, catalogue)

    assert measured, "fixture should produce at least one measurement"
    js.Draft202012Validator(subschema).validate(measured)


def test_a_missing_artifact_measures_nothing(lhr, run_dir: Path) -> None:
    """No artifact hash means no measurement -- a measurement must be traceable."""
    specs = metrics_for(load_benchmarks(run_dir), "lighthouse")
    measured, unmeasured = _measure(
        specs, extract_lighthouse(lhr, specs), "lighthouse", LIGHTHOUSE_CATALOGUE, sha=""
    )
    assert measured == []
    assert len(unmeasured) == len(specs)
