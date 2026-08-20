"""Integration with the landed spine.

Two things are proved here:

1. Every ``GateResult`` these adapters can emit validates against the frozen
   ``schemas/adlc-run.schema.json`` — whose ``gateResult`` definition is
   ``additionalProperties: false``, so an extra key is a hard error.
2. Run through the real ``adlc.stages.gates.run_gates`` executor, the adapters
   behave the way the spine expects: unavailable ⇒ ``not_run``, and
   required + ``not_run`` ⇒ the aggregate fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.gate.code_quality import CodeQualityGate
from adlc.adapters.gate.codeql import CodeQlGate
from adlc.adapters.gate.dependency import DependencyReviewGate
from adlc.config import Config
from adlc.reduce import aggregate_passed, load_run, write_gate
from adlc.runs import RunDir
from adlc.schemas import is_valid
from adlc.stages.gates import run_gates

from .conftest import HEAD_SHA, FakeClient, load_fixture

ALL_GATES = (CodeQlGate, CodeQualityGate, DependencyReviewGate)


def assert_schema_valid(result: dict[str, Any]) -> None:
    """A GateResult is only valid in situ, so wrap it in a minimal valid run."""
    run = {
        "schemaVersion": "adlc-run/v1",
        "runId": "2026-08-19-a1b2",
        "createdAt": "2026-08-19T10:00:00Z",
        "repo": "acme/widget",
        "status": "gated",
        "profile": "full",
        "stages": [],
        "gates": [result],
    }
    ok, errors = is_valid("adlc-run", run)
    assert ok, f"GateResult failed schema validation: {errors}"


def test_the_schema_helper_actually_rejects_bad_results() -> None:
    """Guard the guard: gateResult is additionalProperties:false, so prove it bites."""
    good = {
        "id": "security", "required": True, "status": "pass", "severity": "low",
        "observed": {}, "expected": {}, "message": "ok", "evidence": [],
    }
    assert_schema_valid(good)
    with pytest.raises(AssertionError):
        assert_schema_valid({**good, "unexpectedKey": 1})
    with pytest.raises(AssertionError):
        assert_schema_valid({**good, "status": "green"})


# ---------------------------------------------------------------------------
# Schema conformance of every reachable result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_no_credential_result_is_schema_valid(gate_cls: Any, cfg: Config, run: Any) -> None:
    assert_schema_valid(gate_cls().evaluate(run, cfg))


def test_every_codeql_outcome_is_schema_valid(
    cfg: Config, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]

    # pass, fail and not_run in turn
    cases = [
        (FakeClient(analyses=analyses, alerts=alerts["clean"]), cfg, "pass"),
        (FakeClient(analyses=analyses, alerts=alerts["breaching"]), cfg, "fail"),
        (FakeClient(analyses=[], alerts=[]), _zero_timeout(cfg), "not_run"),
    ]
    for client, use_cfg, expected_status in cases:
        monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)
        result = CodeQlGate().evaluate(run, use_cfg)
        assert result["status"] == expected_status
        assert_schema_valid(result)


def test_every_code_quality_outcome_is_schema_valid(
    cfg: Config, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cq = load_fixture("code_quality.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    routes_clean = {
        "code-quality/setup": cq["setup_configured"],
        "code-quality/findings": cq["findings_clean"],
    }
    routes_bad = {
        "code-quality/setup": cq["setup_configured"],
        "code-quality/findings": cq["findings_breaching"],
    }
    routes_off = {
        "code-quality/setup": cq["setup_not_configured"],
        "code-quality/findings": [],
    }
    cases = [
        (FakeClient(analyses=analyses, routes=routes_clean), "pass"),
        (FakeClient(analyses=analyses, routes=routes_bad), "fail"),
        (FakeClient(analyses=analyses, routes=routes_off), "not_run"),
    ]
    for client, expected_status in cases:
        monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)
        result = CodeQualityGate().evaluate(run, cfg)
        assert result["status"] == expected_status
        assert_schema_valid(result)


def test_every_dependency_outcome_is_schema_valid(
    cfg: Config, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep = load_fixture("dependency.json")
    cases = [
        (FakeClient(routes={"dependency-graph/compare": []}), "pass"),
        (FakeClient(routes={"dependency-graph/compare": dep["dependency_review"]}), "fail"),
    ]
    for client, expected_status in cases:
        monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)
        result = DependencyReviewGate().evaluate(run, cfg)
        assert result["status"] == expected_status
        assert_schema_valid(result)


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_results_carry_every_contract_field(gate_cls: Any, cfg: Config, run: Any) -> None:
    result = gate_cls().evaluate(run, cfg)
    for field in ("id", "required", "status", "severity", "observed", "expected", "message"):
        assert field in result, f"{gate_cls.__name__} result is missing '{field}'"
    assert isinstance(result["observed"], dict)
    assert isinstance(result["expected"], dict)
    assert result["severity"] in ("low", "medium", "high", "critical")


# ---------------------------------------------------------------------------
# End-to-end through the spine's executor
# ---------------------------------------------------------------------------


def _zero_timeout(cfg: Config) -> Config:
    cfg.gates = {"security": {"timeoutSeconds": 0}, "code_quality": {"timeoutSeconds": 0}}
    return cfg


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDir:
    cfg = Config(root=tmp_path, profile="full", gates={})
    rd = RunDir(cfg, "2026-08-19-a1b2")
    rd.create(profile="full", brief_text="L4 integration test")
    return rd


def test_gates_run_through_the_spine_executor_without_credentials(run_dir: RunDir) -> None:
    """The whole point of an optional adapter: no creds must not crash the spine."""
    cfg = run_dir.cfg
    outcome = run_gates(cfg, run_dir, ["security", "code_quality", "dependency"])

    by_id = {g["id"]: g for g in outcome["gates"]}
    for gate_id in ("security", "code_quality", "dependency"):
        assert by_id[gate_id]["status"] == "not_run"
        assert by_id[gate_id]["status"] != "pass"


def test_required_not_run_fails_the_aggregate(run_dir: RunDir) -> None:
    """required + not_run ⇒ the build goes red. This is the contract."""
    cfg = run_dir.cfg
    outcome = run_gates(cfg, run_dir, ["security"])

    assert outcome["passed"] is False
    assert any("security" in failure for failure in outcome["failures"])


def test_optional_dependency_gate_alone_does_not_fail_the_aggregate(tmp_path: Path) -> None:
    """`dependency` is advisory, so its not_run must not go red on its own."""
    cfg = Config(root=tmp_path, profile="minimal", gates={"required": ["dependency"]})
    assert cfg.is_required("dependency") is True  # sanity: override works

    cfg_advisory = Config(root=tmp_path, profile="minimal", gates={})
    assert cfg_advisory.is_required("dependency") is False
    result = DependencyReviewGate().evaluate({"repo": "acme/widget"}, cfg_advisory)
    passed, failures = aggregate_passed([result])
    assert result["status"] == "not_run"
    assert passed is True
    assert failures == []


def test_gate_results_survive_a_write_read_round_trip(
    run_dir: RunDir, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the gate returns must be exactly what `adlc reduce` later reads."""
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    alerts = load_fixture("code_scanning_alerts.json")["clean"]
    monkeypatch.setattr(
        "adlc.adapters.gate.codeql.GitHubRestClient", FakeClient(analyses=analyses, alerts=alerts)
    )
    result = CodeQlGate().evaluate(run, run_dir.cfg)
    assert result["status"] == "pass"

    path = write_gate(run_dir, result)
    assert path.is_file()

    from adlc.reduce import collect_gates

    stored = {g["id"]: g for g in collect_gates(run_dir, run_dir.cfg)}
    assert stored["security"]["status"] == "pass"
    assert stored["security"]["observed"]["headSha"] == HEAD_SHA
    assert_schema_valid(stored["security"])


def test_spine_never_writes_run_json_from_a_gate(run_dir: RunDir) -> None:
    """Gates must not author run.json; only `adlc reduce` may."""
    run_gates(run_dir.cfg, run_dir, ["security", "code_quality", "dependency"])
    stored = load_run(run_dir)
    # run.json exists only because RunDir.create() seeded it; the gate stage must
    # not have written gate results into it.
    assert stored.get("gates", []) == []
    assert (run_dir.gates_dir / "security.json").is_file()
