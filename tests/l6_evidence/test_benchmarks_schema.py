"""``benchmarks.yaml`` loading and the schema L6 owns."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from adlc.adapters.evidence.lighthouse import (
    aggregate_values,
    collector_options,
    evaluate_budget,
    json_pointer,
    load_benchmarks,
    metrics_for,
    resolve_run_dir,
    target_urls,
    timeout_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "benchmarks.schema.json"


def _validator():
    pytest.importorskip("jsonschema", reason="jsonschema is a spine dependency")
    import jsonschema as js

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return js.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# schemas/benchmarks.schema.json
# ---------------------------------------------------------------------------


def test_schema_file_is_valid_json_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["required"] == ["metrics"]
    item = schema["properties"]["metrics"]["items"]
    assert set(item["required"]) == {"id", "collector", "budget", "direction"}
    assert item["additionalProperties"] is False
    # A pattern, not an enum: a new evidence adapter must not invalidate an
    # existing benchmarks.yaml. The spine's own seed uses `playwright`.
    assert item["properties"]["collector"]["pattern"] == "^[a-z][a-z0-9_-]*$"
    assert "enum" not in item["properties"]["collector"]
    assert set(item["properties"]["direction"]["enum"]) == {
        "lower_is_better",
        "higher_is_better",
    }


def test_schema_accepts_the_spine_seeded_benchmarks() -> None:
    """`adlc enrich` writes this file; my schema must not reject it."""
    from adlc.stages.enrich import DEFAULT_BENCHMARKS

    _validator().validate(DEFAULT_BENCHMARKS)
    assert {m["collector"] for m in DEFAULT_BENCHMARKS["metrics"]} >= {
        "lighthouse",
        "axe",
        "playwright",
    }


def test_spine_seeded_metric_ids_are_in_the_built_in_catalogues() -> None:
    """`lcp_ms` and `a11y_critical_violations` must need no `source` pointer."""
    from adlc.adapters.evidence.axe import AXE_CATALOGUE
    from adlc.adapters.evidence.lighthouse import LIGHTHOUSE_CATALOGUE
    from adlc.stages.enrich import DEFAULT_BENCHMARKS

    catalogues = {"lighthouse": LIGHTHOUSE_CATALOGUE, "axe": AXE_CATALOGUE}
    for metric in DEFAULT_BENCHMARKS["metrics"]:
        catalogue = catalogues.get(metric["collector"])
        if catalogue is not None:
            assert metric["id"] in catalogue, metric["id"]


def test_fixture_benchmarks_validates(benchmarks_doc) -> None:
    _validator().validate(benchmarks_doc)


@pytest.mark.parametrize(
    "metric",
    [
        {"collector": "k6", "budget": 1, "direction": "lower_is_better"},  # no id
        {"id": "x", "budget": 1, "direction": "lower_is_better"},  # no collector
        {"id": "x", "collector": "k6", "direction": "lower_is_better"},  # no budget
        {"id": "x", "collector": "k6", "budget": 1},  # no direction
        {"id": "x", "collector": "Not A Collector", "budget": 1, "direction": "lower_is_better"},
        {"id": "x", "collector": "k6", "budget": 1, "direction": "smaller"},
        {"id": "X", "collector": "k6", "budget": 1, "direction": "lower_is_better"},
        {"id": "x", "collector": "k6", "budget": 1, "direction": "lower_is_better", "oops": 1},
    ],
)
def test_schema_rejects_malformed_metrics(metric) -> None:
    validator = _validator()
    assert not validator.is_valid({"metrics": [metric]}), metric


def test_schema_requires_at_least_one_metric() -> None:
    assert not _validator().is_valid({"metrics": []})


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def test_load_benchmarks_reads_the_run_directory(run_dir: Path) -> None:
    doc = load_benchmarks(run_dir)
    assert doc["version"] == 1
    assert len(metrics_for(doc, "lighthouse")) == 10
    assert len(metrics_for(doc, "k6")) == 6
    assert len(metrics_for(doc, "axe")) == 5


def test_load_benchmarks_never_raises(tmp_path: Path) -> None:
    assert load_benchmarks(None) == {"metrics": []}
    assert load_benchmarks(tmp_path) == {"metrics": []}

    broken = tmp_path / "broken"
    (broken / "enrichment").mkdir(parents=True)
    (broken / "enrichment" / "benchmarks.yaml").write_text("{[not: yaml", encoding="utf-8")
    assert load_benchmarks(broken) == {"metrics": []}

    wrong_type = tmp_path / "wrong"
    (wrong_type / "enrichment").mkdir(parents=True)
    (wrong_type / "enrichment" / "benchmarks.yaml").write_text("just a string", encoding="utf-8")
    assert load_benchmarks(wrong_type) == {"metrics": []}


def test_metrics_for_drops_unusable_entries(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    (directory / "enrichment").mkdir(parents=True)
    (directory / "enrichment" / "benchmarks.yaml").write_text(
        yaml.safe_dump(
            {
                "metrics": [
                    {"id": "ok", "collector": "k6", "budget": 1, "direction": "lower_is_better"},
                    {"id": "", "collector": "k6", "budget": 1, "direction": "lower_is_better"},
                    {"id": "no_budget", "collector": "k6", "direction": "lower_is_better"},
                    {"id": "bad_dir", "collector": "k6", "budget": 1, "direction": "sideways"},
                    {"id": "other", "collector": "axe", "budget": 1, "direction": "lower_is_better"},
                ]
            }
        ),
        encoding="utf-8",
    )
    specs = metrics_for(load_benchmarks(directory), "k6")
    assert [s["id"] for s in specs] == ["ok"]


def test_resolve_run_dir_from_the_evidence_directory(run_dir: Path, evidence_out: Path) -> None:
    assert resolve_run_dir({"runId": "2026-08-19-a1b2"}, evidence_out) == run_dir


def test_resolve_run_dir_returns_none_when_unrelated(tmp_path: Path) -> None:
    assert resolve_run_dir({"runId": "nope"}, tmp_path / "somewhere" / "else") is None


def test_target_urls_precedence(run_dir: Path, monkeypatch) -> None:
    doc = load_benchmarks(run_dir)
    monkeypatch.delenv("ADLC_TARGET_URL", raising=False)
    assert target_urls(doc, "lighthouse") == [
        "http://localhost:3000/",
        "http://localhost:3000/checkout",
    ]
    assert target_urls(doc, "k6") == ["http://localhost:3000/api/health"]

    monkeypatch.setenv("ADLC_TARGET_URL", "https://preview.example.test/")
    assert target_urls(doc, "lighthouse") == ["https://preview.example.test/"]


def test_target_urls_falls_back_to_target_url() -> None:
    doc = {"target": {"url": "http://app.test/"}, "metrics": []}
    assert target_urls(doc, "axe") == ["http://app.test/"]
    assert target_urls({"metrics": []}, "axe") == []


def test_target_urls_falls_back_to_the_run_capability(monkeypatch) -> None:
    """The spine passes the live candidate URL as run.capabilities.targetUrl."""
    monkeypatch.delenv("ADLC_TARGET_URL", raising=False)
    run = {"capabilities": {"targetUrl": "http://candidate.test:8080/"}}
    assert target_urls({"metrics": []}, "axe", run) == ["http://candidate.test:8080/"]
    # An explicit benchmarks target still wins.
    doc = {"target": {"url": "http://app.test/"}, "metrics": []}
    assert target_urls(doc, "axe", run) == ["http://app.test/"]
    # `about:blank` is the spine's placeholder, not a real target.
    blank = {"capabilities": {"targetUrl": "about:blank"}}
    assert target_urls({"metrics": []}, "axe", blank) == []


def test_collector_options_and_timeout(run_dir: Path) -> None:
    doc = load_benchmarks(run_dir)
    assert collector_options(doc, "k6")["vus"] == 5
    assert collector_options(doc, "nope") == {}
    assert timeout_for(doc, "k6") == 300
    assert timeout_for({"metrics": []}, "k6", default=42) == 42


# ---------------------------------------------------------------------------
# comparison semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "budget", "direction", "expected"),
    [
        (1820.0, 2500, "lower_is_better", True),
        (2500.0, 2500, "lower_is_better", True),
        (2500.1, 2500, "lower_is_better", False),
        (94.0, 90, "higher_is_better", True),
        (90.0, 90, "higher_is_better", True),
        (89.9, 90, "higher_is_better", False),
        (0.0, 0, "lower_is_better", True),
        (1.0, 0, "lower_is_better", False),
    ],
)
def test_evaluate_budget(value, budget, direction, expected) -> None:
    assert evaluate_budget(value, budget, direction) is expected


def test_aggregate_values_defaults_to_worst_case() -> None:
    lower = {"id": "x", "direction": "lower_is_better"}
    higher = {"id": "y", "direction": "higher_is_better"}
    assert aggregate_values([1.0, 9.0, 4.0], lower) == 9.0
    assert aggregate_values([1.0, 9.0, 4.0], higher) == 1.0
    assert aggregate_values([1.0, 9.0, 4.0], {**lower, "aggregate": "best"}) == 1.0
    assert aggregate_values([1.0, 9.0, 4.0], {**higher, "aggregate": "best"}) == 9.0
    assert aggregate_values([1.0, 9.0, 4.0], {**lower, "aggregate": "first"}) == 1.0
    assert aggregate_values([1.0, 3.0], {**lower, "aggregate": "mean"}) == 2.0
    assert aggregate_values([1.0, 3.0], {**lower, "aggregate": "sum"}) == 4.0
    assert aggregate_values([], lower) is None


def test_json_pointer() -> None:
    doc = {"metrics": {"http_req_duration": {"p(95)": 331.7}}, "list": [{"a": 1}]}
    assert json_pointer(doc, "/metrics/http_req_duration/p(95)") == 331.7
    assert json_pointer(doc, "/list/0/a") == 1
    assert json_pointer(doc, "/list/9/a") is None
    assert json_pointer(doc, "/nope") is None
    assert json_pointer(doc, "not-a-pointer") is None
    assert json_pointer(doc, "") is None
