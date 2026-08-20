"""``collect()`` end-to-end, with and without the tools present.

The unavailable path is the one the credential-free conformance suite exercises:
no tool, no fabricated numbers, a hash-verified record of *why* nothing was
measured. The stubbed path replaces only the subprocess boundary, so harvesting,
redaction, mapping and artifact hashing are all really executed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from adlc.adapters.evidence.axe import AxeCollector
from adlc.adapters.evidence.k6 import K6Collector
from adlc.adapters.evidence.lighthouse import LighthouseCollector

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COLLECTORS = (LighthouseCollector, K6Collector, AxeCollector)


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _verify(artifacts, run_dir: Path) -> None:
    """Every artifacts[] entry carries a verified sha256 (plan §4.2 invariant)."""
    assert artifacts, "collector returned no artifacts"
    for ref in artifacts:
        target = run_dir / ref["path"]
        assert target.is_file(), ref["path"]
        assert not Path(ref["path"]).is_absolute()
        assert ref["path"].startswith("evidence/")
        assert ref["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        assert ref["bytes"] == target.stat().st_size
        assert ref["kind"] and ref["mimeType"]


def _measurements(out: Path, collector: str) -> list:
    """The bare array the spine reads from ``*-measurements.json``."""
    return json.loads((out / f"{collector}-measurements.json").read_text(encoding="utf-8"))


def _unmeasured(out: Path, collector: str) -> dict:
    """The sidecar recording budgets that could not be measured."""
    return json.loads((out / f"{collector}-unmeasured.json").read_text(encoding="utf-8"))


def _by_id(measurements) -> dict:
    return {m["metricId"]: m for m in measurements}


# ---------------------------------------------------------------------------
# No tools installed -- the credential-free path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_without_tools_measures_nothing(
    collector_cls, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    artifacts = collector_cls().collect(run, "candidate-a", evidence_out)
    _verify(artifacts, run_dir)

    assert _measurements(evidence_out, collector_cls.name) == [], (
        "a tool that did not run must measure nothing"
    )

    doc = _unmeasured(evidence_out, collector_cls.name)
    assert doc["schemaVersion"] == "adlc-unmeasured/v1"
    assert doc["collector"] == collector_cls.name
    assert doc["runId"] == "2026-08-19-a1b2"
    assert doc["variant"] == "candidate-a"
    assert doc["unmeasured"], "every declared budget must be accounted for"
    assert all(entry["status"] == "not_run" for entry in doc["unmeasured"])
    assert all(entry["cause"] == "tool_unavailable" for entry in doc["unmeasured"])
    assert all(entry["reason"] for entry in doc["unmeasured"])
    assert doc["tool"]["ran"] is False


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_without_tools_covers_every_declared_budget(
    collector_cls, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    from adlc.adapters.evidence.lighthouse import load_benchmarks, metrics_for

    collector_cls().collect(run, "candidate-a", evidence_out)
    declared = {s["id"] for s in metrics_for(load_benchmarks(run_dir), collector_cls.name)}
    doc = _unmeasured(evidence_out, collector_cls.name)
    assert {entry["metricId"] for entry in doc["unmeasured"]} == declared


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_writes_no_raw_output_when_the_tool_is_absent(
    collector_cls, run, evidence_out: Path, no_tools
) -> None:
    collector_cls().collect(run, "candidate-a", evidence_out)
    for name in ("lighthouse.json", "k6.json", "axe.json"):
        assert not (evidence_out / name).exists()


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_is_a_no_op_when_no_budgets_are_declared(
    collector_cls, run, tmp_path: Path, no_tools
) -> None:
    """No benchmarks.yaml, no tool: nothing to say, so say nothing."""
    out = tmp_path / "bare" / "evidence" / "candidate-a"
    out.mkdir(parents=True)
    assert collector_cls().collect(run, "candidate-a", out) == []
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_never_raises_on_a_hostile_run(collector_cls, tmp_path: Path, no_tools) -> None:
    out = tmp_path / "hostile" / "evidence" / "variant"
    assert collector_cls().collect({}, "", out) == []


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_clears_stale_output_from_a_previous_attempt(
    collector_cls, run, evidence_out: Path, no_tools
) -> None:
    """Evidence must describe *this* attempt, not a previous green one."""
    stale = evidence_out / f"{collector_cls.name}.json"
    stale.write_text('{"stale": true}', encoding="utf-8")
    stale_measurements = evidence_out / f"{collector_cls.name}-measurements.json"
    stale_measurements.write_text(
        json.dumps([{"metricId": "lcp_ms", "value": 1, "passed": True}]), encoding="utf-8"
    )

    collector_cls().collect(run, "candidate-a", evidence_out)

    assert not stale.exists(), "a stale raw report must not survive into a new attempt"
    assert _measurements(evidence_out, collector_cls.name) == []


# ---------------------------------------------------------------------------
# Tools stubbed at the subprocess boundary
# ---------------------------------------------------------------------------


def _ok_tool(**extra):
    return {"ran": True, "exitCode": 0, "cause": "", "reason": "", "stdout": "", **extra}


def test_lighthouse_collect_with_a_stubbed_lhci(
    monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/lhci",
    )

    def fake_run(command, cwd, timeout, env=None):
        workdir = Path(cwd) / ".lighthouseci"
        workdir.mkdir(parents=True, exist_ok=True)
        report = workdir / "lhr-1.json"
        report.write_text(json.dumps(_load("lighthouse-lhr.json")), encoding="utf-8")
        (workdir / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "url": "http://localhost:3000/",
                        "isRepresentativeRun": True,
                        "jsonPath": str(report),
                    }
                ]
            ),
            encoding="utf-8",
        )
        return _ok_tool(command=command)

    monkeypatch.setattr("adlc.adapters.evidence.lighthouse.run_tool", fake_run)

    artifacts = LighthouseCollector().collect(run, "candidate-a", evidence_out)
    _verify(artifacts, run_dir)

    assert {ref["kind"] for ref in artifacts} == {
        "lighthouse_config",
        "lighthouse",
        "evidence_measurements",
    }
    # The lhci working directory is removed: its HTML reports embed page source.
    assert not (evidence_out / ".lighthouseci").exists()

    report = json.loads((evidence_out / "lighthouse.json").read_text(encoding="utf-8"))
    assert "super-secret-token-value" not in json.dumps(report)

    measured = _by_id(_measurements(evidence_out, "lighthouse"))
    assert measured["lcp_ms"]["value"] == pytest.approx(1820.4)
    assert measured["lcp_ms"]["passed"] is True
    assert measured["best_practices_score"]["passed"] is False
    lighthouse_sha = next(r["sha256"] for r in artifacts if r["kind"] == "lighthouse")
    assert all(m["artifactSha256"] == lighthouse_sha for m in measured.values())
    doc = _unmeasured(evidence_out, "lighthouse")
    assert {e["metricId"] for e in doc["unmeasured"]} == {
        "pwa_score",
        "made_up_lighthouse_metric",
    }


def test_lighthouse_collect_aggregates_worst_case_across_urls(
    monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/lhci",
    )

    def fake_run(command, cwd, timeout, env=None):
        workdir = Path(cwd) / ".lighthouseci"
        workdir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for index, (url, lcp) in enumerate(
            (("http://localhost:3000/", 1820.4), ("http://localhost:3000/checkout", 3100.0))
        ):
            report = _load("lighthouse-lhr.json")
            report["audits"]["largest-contentful-paint"]["numericValue"] = lcp
            path = workdir / f"lhr-{index}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            manifest.append(
                {"url": url, "isRepresentativeRun": True, "jsonPath": str(path)}
            )
        (workdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return _ok_tool(command=command)

    monkeypatch.setattr("adlc.adapters.evidence.lighthouse.run_tool", fake_run)

    artifacts = LighthouseCollector().collect(run, "candidate-a", evidence_out)
    _verify(artifacts, run_dir)
    assert (evidence_out / "lighthouse.json").is_file()
    assert (evidence_out / "lighthouse-1.json").is_file()

    doc = _measurements(evidence_out, "lighthouse")
    lcp = next(m for m in doc if m["metricId"] == "lcp_ms")
    assert lcp["value"] == pytest.approx(3100.0), "worst case wins"
    assert lcp["passed"] is False


def test_lighthouse_config_carries_budget_assertions(
    monkeypatch, run, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/lhci",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.run_tool",
        lambda command, cwd, timeout, env=None: _ok_tool(command=command),
    )

    LighthouseCollector().collect(run, "candidate-a", evidence_out)
    rc = json.loads((evidence_out / "lighthouserc.json").read_text(encoding="utf-8"))
    assertions = rc["ci"]["assert"]["assertions"]
    assert assertions["largest-contentful-paint"][1]["maxNumericValue"] == 2500
    assert assertions["categories:performance"][1]["minScore"] == pytest.approx(0.9)
    assert rc["ci"]["collect"]["url"] == [
        "http://localhost:3000/",
        "http://localhost:3000/checkout",
    ]


def test_lighthouse_collect_reports_a_failed_tool(
    monkeypatch, run, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/lhci",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.run_tool",
        lambda command, cwd, timeout, env=None: {
            "ran": False,
            "exitCode": None,
            "cause": "tool_timeout",
            "reason": "lhci exceeded the 300s budget and was killed",
        },
    )

    LighthouseCollector().collect(run, "candidate-a", evidence_out)
    doc = _measurements(evidence_out, "lighthouse")
    assert doc == []
    assert {e["cause"] for e in _unmeasured(evidence_out, "lighthouse")["unmeasured"]} == {
        "tool_timeout"
    }
    assert not (evidence_out / "lighthouse.json").exists()


def test_k6_collect_with_a_stubbed_k6(
    monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.k6.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/k6",
    )

    def fake_run(command, cwd, timeout, env=None):
        export_path = Path(command[command.index("--summary-export") + 1])
        export_path.write_text(json.dumps(_load("k6-summary.json")), encoding="utf-8")
        return _ok_tool(command=command)

    monkeypatch.setattr("adlc.adapters.evidence.k6.run_tool", fake_run)

    artifacts = K6Collector().collect(run, "candidate-a", evidence_out)
    _verify(artifacts, run_dir)
    assert {ref["kind"] for ref in artifacts} == {"k6_script", "k6", "evidence_measurements"}

    script = (evidence_out / "k6-script.js").read_text(encoding="utf-8")
    assert "http://localhost:3000/api/health" in script
    assert "vus: 5" in script
    assert "duration: '30s'" in script

    measured = _by_id(_measurements(evidence_out, "k6"))
    assert measured["p95_latency_ms"]["value"] == pytest.approx(331.7)
    assert measured["p95_latency_ms"]["passed"] is True
    assert measured["p99_latency_ms"]["passed"] is False
    assert measured["checks_failed"]["passed"] is False
    unmeasured = _unmeasured(evidence_out, "k6")["unmeasured"]
    assert [e["metricId"] for e in unmeasured] == ["made_up_k6_metric"]


def test_k6_collect_uses_a_declared_script(
    monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    script = run_dir / "enrichment" / "k6" / "load.js"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("export default function () {}\n", encoding="utf-8")
    benchmarks = run_dir / "enrichment" / "benchmarks.yaml"
    benchmarks.write_text(
        benchmarks.read_text(encoding="utf-8").replace(
            "    url: http://localhost:3000/api/health",
            "    script: enrichment/k6/load.js",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.k6.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/k6",
    )
    captured: dict = {}

    def fake_run(command, cwd, timeout, env=None):
        captured["command"] = command
        Path(command[command.index("--summary-export") + 1]).write_text(
            json.dumps(_load("k6-summary.json")), encoding="utf-8"
        )
        return _ok_tool(command=command)

    monkeypatch.setattr("adlc.adapters.evidence.k6.run_tool", fake_run)

    K6Collector().collect(run, "candidate-a", evidence_out)
    assert captured["command"][-1].endswith("load.js")
    assert not (evidence_out / "k6-script.js").exists()


def test_k6_collect_reports_a_missing_script(
    monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    benchmarks = run_dir / "enrichment" / "benchmarks.yaml"
    benchmarks.write_text(
        benchmarks.read_text(encoding="utf-8").replace(
            "    url: http://localhost:3000/api/health",
            "    script: enrichment/k6/absent.js",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.k6.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/k6",
    )

    K6Collector().collect(run, "candidate-a", evidence_out)
    doc = _measurements(evidence_out, "k6")
    assert doc == []
    assert all(
        "absent.js" in e["reason"] for e in _unmeasured(evidence_out, "k6")["unmeasured"]
    )


def test_k6_collect_reports_an_unreadable_summary(
    monkeypatch, run, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.k6.find_executable",
        lambda name, cfg=None, start=None: "/fake/bin/k6",
    )

    def fake_run(command, cwd, timeout, env=None):
        Path(command[command.index("--summary-export") + 1]).write_text(
            "not json at all", encoding="utf-8"
        )
        return _ok_tool(command=command)

    monkeypatch.setattr("adlc.adapters.evidence.k6.run_tool", fake_run)

    K6Collector().collect(run, "candidate-a", evidence_out)
    doc = _measurements(evidence_out, "k6")
    assert doc == []
    assert {e["cause"] for e in _unmeasured(evidence_out, "k6")["unmeasured"]} == {
        "output_unreadable"
    }
    assert not (evidence_out / "k6.json").exists(), "unparseable output is not evidence"


def test_axe_collect_with_a_stubbed_node(
    monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_executable",
        lambda name, cfg=None, start=None: f"/fake/bin/{name}",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_node_package",
        lambda pkg, cfg=None, start=None: Path("/fake/node_modules"),
    )

    def fake_run(command, cwd, timeout, env=None):
        Path(command[3]).write_text(
            json.dumps({"results": [_load("axe-results.json")], "errors": []}), encoding="utf-8"
        )
        return _ok_tool(command=command)

    monkeypatch.setattr("adlc.adapters.evidence.axe.run_tool", fake_run)

    artifacts = AxeCollector().collect(run, "candidate-a", evidence_out)
    _verify(artifacts, run_dir)
    assert {ref["kind"] for ref in artifacts} == {
        "axe_script",
        "axe_config",
        "axe",
        "evidence_measurements",
    }
    # The intermediate scan payload is not left behind unredacted.
    assert not (evidence_out / "axe-scan.raw.json").exists()

    report = json.loads((evidence_out / "axe.json").read_text(encoding="utf-8"))
    serialised = json.dumps(report)
    assert "s3cr3t-session-value" not in serialised
    assert "9f8e7d6c5b4a3f2e1d0c" not in serialised

    measured = _by_id(_measurements(evidence_out, "axe"))
    assert measured["a11y_critical_violations"]["value"] == 1.0
    assert measured["a11y_critical_violations"]["passed"] is False
    assert measured["a11y_total_violations"]["passed"] is True
    unmeasured = _unmeasured(evidence_out, "axe")["unmeasured"]
    assert [e["metricId"] for e in unmeasured] == ["made_up_axe_metric"]


def test_axe_collect_surfaces_a_navigation_failure(
    monkeypatch, run, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_executable",
        lambda name, cfg=None, start=None: f"/fake/bin/{name}",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_node_package",
        lambda pkg, cfg=None, start=None: Path("/fake/node_modules"),
    )

    def fake_run(command, cwd, timeout, env=None):
        Path(command[3]).write_text(
            json.dumps(
                {
                    "results": [],
                    "errors": [{"url": "http://localhost:3000/checkout", "message": "ERR_REFUSED"}],
                }
            ),
            encoding="utf-8",
        )
        return {"ran": True, "exitCode": 1, "cause": "tool_failed", "reason": "node exited 1"}

    monkeypatch.setattr("adlc.adapters.evidence.axe.run_tool", fake_run)

    AxeCollector().collect(run, "candidate-a", evidence_out)
    doc = _measurements(evidence_out, "axe")
    assert doc == []
    unmeasured = _unmeasured(evidence_out, "axe")["unmeasured"]
    assert all(e["status"] == "not_run" for e in unmeasured)
    assert any("ERR_REFUSED" in e["reason"] for e in unmeasured)
    assert not (evidence_out / "axe.json").exists()


def test_axe_scan_script_is_commonjs_and_self_contained(
    monkeypatch, run, evidence_out: Path, no_tools
) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_executable",
        lambda name, cfg=None, start=None: f"/fake/bin/{name}",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_node_package",
        lambda pkg, cfg=None, start=None: Path("/fake/node_modules"),
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.run_tool",
        lambda command, cwd, timeout, env=None: _ok_tool(command=command),
    )

    AxeCollector().collect(run, "candidate-a", evidence_out)
    script = (evidence_out / "axe-scan.cjs").read_text(encoding="utf-8")
    assert "require('@axe-core/playwright')" in script
    assert "require('playwright')" in script
    assert "import " not in script

    config = json.loads((evidence_out / "axe-scan.config.json").read_text(encoding="utf-8"))
    assert config["urls"] == ["http://localhost:3000/checkout"]
    assert config["tags"] == ["wcag2a", "wcag2aa"]
    assert config["browser"] == "chromium"


@pytest.mark.parametrize("collector_cls", COLLECTORS, ids=lambda c: c.name)
def test_collect_reports_a_missing_target_url(
    collector_cls, monkeypatch, run, run_dir: Path, evidence_out: Path, no_tools
) -> None:
    module = f"adlc.adapters.evidence.{collector_cls.name}"
    monkeypatch.setattr(
        f"{module}.find_executable", lambda name, cfg=None, start=None: f"/fake/bin/{name}"
    )
    if collector_cls is AxeCollector:
        monkeypatch.setattr(
            f"{module}.find_node_package", lambda pkg, cfg=None, start=None: Path("/fake")
        )
    monkeypatch.setattr(f"{module}.target_urls", lambda doc, collector, run=None: [])

    collector_cls().collect(run, "candidate-a", evidence_out)
    doc = _measurements(evidence_out, collector_cls.name)
    assert doc == []
    assert all(
        "ADLC_TARGET_URL" in e["reason"]
        for e in _unmeasured(evidence_out, collector_cls.name)["unmeasured"]
    )
