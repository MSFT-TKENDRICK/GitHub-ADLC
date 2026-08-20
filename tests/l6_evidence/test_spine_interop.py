"""The spine must be able to read what these collectors write.

`adlc.stages.evidence.collect_measurements` and the deterministic rubric
runner's `metric_within_budget` check both parse `*-measurements.json` as a
**bare JSON list**, exactly like the spine's own `local` collector. These tests
exercise the real spine functions rather than a copy of their logic, so a spine
change to that contract fails here instead of silently producing zero
measurements.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from adlc.adapters.evals.deterministic import DeterministicRubricRunner
from adlc.adapters.evidence.axe import AxeCollector
from adlc.adapters.evidence.k6 import K6Collector
from adlc.adapters.evidence.lighthouse import LighthouseCollector
from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages.evidence import collect_measurements

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The metric ids the spine seeds into enrichment/benchmarks.yaml.
SPINE_SEEDED = {
    "lcp_ms": "lighthouse",
    "a11y_critical_violations": "axe",
    "console_errors": "playwright",
}


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def spine_run_dir(tmp_path: Path) -> RunDir:
    """A run directory laid out the way the spine builds it."""
    from adlc.stages.enrich import DEFAULT_BENCHMARKS

    cfg = Config(root=tmp_path)
    rd = RunDir(cfg, "2026-08-19-a1b2")
    rd.enrichment_dir.mkdir(parents=True)
    (rd.enrichment_dir / "benchmarks.yaml").write_text(
        yaml.safe_dump(DEFAULT_BENCHMARKS, sort_keys=False), encoding="utf-8"
    )
    (rd.evidence_dir / "candidate-a").mkdir(parents=True)
    return rd


def _stub_lighthouse(monkeypatch, lcp_ms: float = 1820.4) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/lhci",
    )

    def fake_run(command, cwd, timeout, env=None):
        workdir = Path(cwd) / ".lighthouseci"
        workdir.mkdir(parents=True, exist_ok=True)
        report = _load("lighthouse-lhr.json")
        report["audits"]["largest-contentful-paint"]["numericValue"] = lcp_ms
        path = workdir / "lhr-0.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        (workdir / "manifest.json").write_text(
            json.dumps(
                [{"url": "http://app.test/", "isRepresentativeRun": True, "jsonPath": str(path)}]
            ),
            encoding="utf-8",
        )
        return {"ran": True, "exitCode": 0, "cause": "", "reason": ""}

    monkeypatch.setattr("adlc.adapters.evidence.lighthouse.run_tool", fake_run)


def _stub_axe(monkeypatch, critical: int = 1) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_executable",
        lambda name, cfg=None, start=None: f"/fake/bin/{name}",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_node_package",
        lambda pkg, cfg=None, start=None: Path("/fake/node_modules"),
    )

    def fake_run(command, cwd, timeout, env=None):
        results = _load("axe-results.json")
        results["violations"] = [
            v for v in results["violations"] if v.get("impact") != "critical"
        ] + [
            {"id": f"synthetic-{i}", "impact": "critical", "nodes": [{"html": "<img>"}]}
            for i in range(critical)
        ]
        Path(command[3]).write_text(
            json.dumps({"results": [results], "errors": []}), encoding="utf-8"
        )
        return {"ran": True, "exitCode": 0, "cause": "", "reason": ""}

    monkeypatch.setattr("adlc.adapters.evidence.axe.run_tool", fake_run)


# ---------------------------------------------------------------------------
# adlc.stages.evidence.collect_measurements
# ---------------------------------------------------------------------------


def test_spine_reads_lighthouse_measurements(spine_run_dir, monkeypatch, no_tools) -> None:
    monkeypatch.setenv("ADLC_TARGET_URL", "http://app.test/")
    _stub_lighthouse(monkeypatch)

    out = spine_run_dir.evidence_dir / "candidate-a"
    LighthouseCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    measurements = collect_measurements(spine_run_dir, "candidate-a")
    by_id = {m["metricId"]: m for m in measurements}

    assert "lcp_ms" in by_id, "the spine parses *-measurements.json as a bare list"
    assert by_id["lcp_ms"]["value"] == pytest.approx(1820.4)
    assert by_id["lcp_ms"]["budget"] == 2500
    assert by_id["lcp_ms"]["passed"] is True
    assert by_id["lcp_ms"]["collector"] == "lighthouse"
    assert len(by_id["lcp_ms"]["artifactSha256"]) == 64


def test_spine_reads_axe_measurements(spine_run_dir, monkeypatch, no_tools) -> None:
    monkeypatch.setenv("ADLC_TARGET_URL", "http://app.test/")
    _stub_axe(monkeypatch, critical=2)

    out = spine_run_dir.evidence_dir / "candidate-a"
    AxeCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    by_id = {m["metricId"]: m for m in collect_measurements(spine_run_dir, "candidate-a")}
    assert by_id["a11y_critical_violations"]["value"] == 2.0
    assert by_id["a11y_critical_violations"]["budget"] == 0
    assert by_id["a11y_critical_violations"]["passed"] is False


def test_spine_reads_k6_measurements(spine_run_dir, monkeypatch, no_tools) -> None:
    """k6 has no seeded metric, so declare one the way a consumer would."""
    bench = spine_run_dir.enrichment_dir / "benchmarks.yaml"
    doc = yaml.safe_load(bench.read_text(encoding="utf-8"))
    doc["target"] = {"url": "http://app.test/api/health"}
    doc["metrics"].append(
        {"id": "p95_latency_ms", "collector": "k6", "budget": 400, "direction": "lower_is_better"}
    )
    bench.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(
        "adlc.adapters.evidence.k6.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/k6",
    )

    def fake_run(command, cwd, timeout, env=None):
        Path(command[command.index("--summary-export") + 1]).write_text(
            json.dumps(_load("k6-summary.json")), encoding="utf-8"
        )
        return {"ran": True, "exitCode": 0, "cause": "", "reason": ""}

    monkeypatch.setattr("adlc.adapters.evidence.k6.run_tool", fake_run)

    out = spine_run_dir.evidence_dir / "candidate-a"
    K6Collector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    by_id = {m["metricId"]: m for m in collect_measurements(spine_run_dir, "candidate-a")}
    assert by_id["p95_latency_ms"]["value"] == pytest.approx(331.7)
    assert by_id["p95_latency_ms"]["budget"] == 400
    assert by_id["p95_latency_ms"]["passed"] is True


def test_spine_measurements_satisfy_the_review_pack_schema(
    spine_run_dir, monkeypatch, no_tools
) -> None:
    js = pytest.importorskip("jsonschema", reason="jsonschema is a spine dependency")
    from adlc.schemas import load_schema

    monkeypatch.setenv("ADLC_TARGET_URL", "http://app.test/")
    _stub_lighthouse(monkeypatch)
    out = spine_run_dir.evidence_dir / "candidate-a"
    LighthouseCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    pack = load_schema("evidence-review-pack")
    subschema = {"$schema": pack["$schema"], **pack["properties"]["measurements"]}
    js.Draft202012Validator(subschema).validate(
        collect_measurements(spine_run_dir, "candidate-a")
    )


def test_unmeasured_sidecar_is_invisible_to_the_spine_measurement_glob(
    spine_run_dir, no_tools
) -> None:
    """A not-run record has no value; it must never reach the review pack."""
    out = spine_run_dir.evidence_dir / "candidate-a"
    LighthouseCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    assert (out / "lighthouse-unmeasured.json").is_file()
    assert not list(out.glob("*-unmeasured*measurements.json"))
    assert collect_measurements(spine_run_dir, "candidate-a") == []


# ---------------------------------------------------------------------------
# Registry discoverability
#
# The spine now runs *every* available evidence collector rather than selecting
# one, recording each in collectorsRan / collectorsSkipped / collectorsFailed.
# These tests assert what that model needs from L6 and are deliberately
# agnostic to whether the spine selects or enumerates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "collector_cls", [LighthouseCollector, K6Collector, AxeCollector], ids=lambda c: c.name
)
def test_each_collector_is_discoverable_through_the_registry(collector_cls, no_tools) -> None:
    """The spine enumerates `adlc.evidence`; an unloadable leaf is invisible."""
    from adlc.config import load_adapters

    registered = load_adapters("evidence")
    assert collector_cls.name in registered, (
        f"'{collector_cls.name}' must load from its pyproject entry point"
    )
    assert registered[collector_cls.name] is collector_cls


@pytest.mark.parametrize(
    "collector_cls", [LighthouseCollector, K6Collector, AxeCollector], ids=lambda c: c.name
)
def test_an_unavailable_collector_yields_a_skip_reason(collector_cls, no_tools) -> None:
    """With no tools installed each collector lands in collectorsSkipped.

    The spine records `detect()`'s reason verbatim, and its conformance test
    requires that reason to be specific rather than empty.
    """
    from adlc.config import detect_all

    available, reason = detect_all(no_tools, "evidence")[collector_cls.name]
    assert available is False
    assert reason.strip(), "an empty skip reason is not actionable"
    assert any(token in reason for token in ("not on PATH", "not installed"))


def test_evidence_is_not_an_explicit_only_kind(no_tools) -> None:
    """Evidence is observational: it needs no opt-in to run."""
    from adlc.config import EXPLICIT_ONLY_KINDS

    assert "evidence" not in EXPLICIT_ONLY_KINDS


@pytest.mark.parametrize(
    "collector_cls", [LighthouseCollector, K6Collector, AxeCollector], ids=lambda c: c.name
)
def test_an_explicit_override_selects_this_collector(collector_cls, no_tools) -> None:
    """`adapters: {evidence: k6}` still pins one collector by name."""
    from adlc.config import select_adapter

    assert select_adapter(no_tools, "evidence", collector_cls.name).name == collector_cls.name


# ---------------------------------------------------------------------------
# adlc.adapters.evals.deterministic -- metric_within_budget
# ---------------------------------------------------------------------------


def _rubric(metric_id: str) -> dict:
    return {
        "id": "interop",
        "threshold": 0.7,
        "criteria": [
            {
                "id": f"budget-{metric_id}",
                "weight": 1.0,
                "check": {"type": "metric_within_budget", "metricId": metric_id},
            }
        ],
    }


def test_deterministic_eval_reads_a_passing_measurement(
    spine_run_dir, monkeypatch, no_tools
) -> None:
    monkeypatch.setenv("ADLC_TARGET_URL", "http://app.test/")
    _stub_lighthouse(monkeypatch, lcp_ms=1200.0)
    out = spine_run_dir.evidence_dir / "candidate-a"
    LighthouseCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    runner = DeterministicRubricRunner(root=spine_run_dir.cfg.root, run_dir=spine_run_dir.path)
    score = runner.run({}, _rubric("lcp_ms"))

    assert score["passed"] is True
    assert score["criteria"][0]["score"] == 1.0
    assert "1200.0" in score["criteria"][0]["rationale"]


def test_deterministic_eval_fails_a_breached_budget(
    spine_run_dir, monkeypatch, no_tools
) -> None:
    monkeypatch.setenv("ADLC_TARGET_URL", "http://app.test/")
    _stub_lighthouse(monkeypatch, lcp_ms=4200.0)
    out = spine_run_dir.evidence_dir / "candidate-a"
    LighthouseCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)

    score = DeterministicRubricRunner(
        root=spine_run_dir.cfg.root, run_dir=spine_run_dir.path
    ).run({}, _rubric("lcp_ms"))

    assert score["passed"] is False
    assert "exceeds budget" in score["criteria"][0]["rationale"]


def test_a_tool_that_did_not_run_fails_the_budget_check(spine_run_dir, no_tools) -> None:
    """The contract: a missing measurement is a failing check, never a pass."""
    out = spine_run_dir.evidence_dir / "candidate-a"
    artifacts = LighthouseCollector().collect({"runId": spine_run_dir.run_id}, "candidate-a", out)
    assert artifacts, "the collector still records why it measured nothing"

    score = DeterministicRubricRunner(
        root=spine_run_dir.cfg.root, run_dir=spine_run_dir.path
    ).run({}, _rubric("lcp_ms"))

    assert score["passed"] is False
    assert score["criteria"][0]["score"] == 0.0
    assert "no measurement recorded" in score["criteria"][0]["rationale"]

    # ...and the reason is still on disk, hash-verified, for a human.
    sidecar = json.loads((out / "lighthouse-unmeasured.json").read_text(encoding="utf-8"))
    entry = next(e for e in sidecar["unmeasured"] if e["metricId"] == "lcp_ms")
    assert entry["status"] == "not_run"
    assert "lhci not on PATH" in entry["reason"]
