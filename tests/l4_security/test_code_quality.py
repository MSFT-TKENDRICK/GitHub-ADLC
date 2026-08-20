"""GitHub Code Quality gate — settings preflight and findings evaluation.

The product is enabled in repository Settings, not by a workflow, so the gate's
first job is to find out whether it is actually on and say so precisely when it
is not.
"""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.gate.code_quality import (
    NOT_ENABLED_REASON,
    CodeQualityGate,
    classify_setup,
    setup_failure_reason,
    summarize_findings,
)
from adlc.adapters.gate.codeql import GitHubApiError

from .conftest import FakeClient, load_fixture


@pytest.fixture
def cq() -> dict[str, Any]:
    return load_fixture("code_quality.json")


def _client(cq: dict[str, Any], *, setup: Any, findings: Any, analyses: Any = None) -> FakeClient:
    if analyses is None:
        analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    return FakeClient(
        analyses=analyses,
        routes={"code-quality/setup": setup, "code-quality/findings": findings},
    )


# ---------------------------------------------------------------------------
# Pure preflight logic
# ---------------------------------------------------------------------------


def test_classify_setup_configured(cq: dict[str, Any]) -> None:
    configured, reason = classify_setup(cq["setup_configured"])
    assert configured is True
    assert "python" in reason


def test_classify_setup_not_configured_uses_the_operator_remedy(cq: dict[str, Any]) -> None:
    configured, reason = classify_setup(cq["setup_not_configured"])
    assert configured is False
    assert reason == NOT_ENABLED_REASON
    assert "Settings → Security → Code quality" in reason


def test_classify_setup_rejects_garbage() -> None:
    for junk in (None, {}, {"state": "who-knows"}, "nonsense"):
        configured, reason = classify_setup(junk)  # type: ignore[arg-type]
        assert configured is False
        assert reason


def test_setup_failure_reasons_do_not_overclaim() -> None:
    """403 conflates 'unlicensed' and 'bad token' -- the reason must admit that."""
    forbidden = setup_failure_reason(403)
    assert "not distinguish" in forbidden
    assert "not licensed" in forbidden

    missing = setup_failure_reason(404)
    assert "does not exist" in missing or "cannot see it" in missing

    assert "unavailable" in setup_failure_reason(503)
    assert "500" in setup_failure_reason(500)


# ---------------------------------------------------------------------------
# Findings summarisation
# ---------------------------------------------------------------------------


def test_summarize_findings_by_severity_and_category(cq: dict[str, Any]) -> None:
    summary = summarize_findings(cq["findings_breaching"])
    assert summary["total"] == 2
    assert summary["bySeverity"]["error"] == 1
    assert summary["bySeverity"]["warning"] == 1
    assert summary["byCategory"]["reliability"] == 1
    assert summary["byCategory"]["maintainability"] == 1


def test_summarize_findings_ignores_junk() -> None:
    summary = summarize_findings([None, 3, "x", {}])  # type: ignore[list-item]
    assert summary["total"] == 1
    assert summary["bySeverity"]["unknown"] == 1


# ---------------------------------------------------------------------------
# Gate level
# ---------------------------------------------------------------------------


def test_gate_not_run_when_code_quality_is_disabled(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline requirement: never pretend to enable it, just say it's off."""
    client = _client(cq, setup=cq["setup_not_configured"], findings=cq["findings_clean"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "not_run"
    assert result["message"] == NOT_ENABLED_REASON
    assert result["observed"]["setup"]["state"] == "not-configured"
    assert "code-quality/findings" not in [c[0] for c in client.calls]


def test_gate_not_run_on_403(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(errors={"code-quality/setup": GitHubApiError("forbidden", status=403)})
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "not_run"
    assert "Not authorized" in result["message"]


def test_gate_passes_on_clean_findings(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_clean"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "pass"
    assert result["observed"]["analysisId"] == 900299
    assert result["observed"]["truncated"] is False
    note = result["observed"]["provenanceNote"]
    assert "no commit" in note and "snapshot" in note


def test_gate_fails_on_error_severity_findings(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_breaching"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "fail"
    assert result["observed"]["bySeverity"]["error"] == 1


def test_gate_preflights_before_polling(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup is cheap; the analysis poll is slow. Check enablement first."""
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_clean"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    CodeQualityGate().evaluate(run, cfg)

    order = [c[0] for c in client.calls]
    assert "code-quality/setup" in order[0]
    assert order.index("code-scanning/analyses") < order.index(
        next(c for c in order if "code-quality/findings" in c)
    )


def test_truncated_result_set_is_not_run_never_pass(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full page may hide findings, so a clean sample cannot prove a pass."""
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_clean"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)
    cfg.gates = {"code_quality": {"maxFindings": 2}}  # fixture has exactly 2 clean findings

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "not_run"
    assert result["status"] != "pass"
    assert result["observed"]["truncated"] is True
    assert "truncated" in result["message"]


def test_maxfindings_is_clamped_to_the_endpoint_limit(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-range maxFindings must not break the truncation logic."""
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_clean"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)
    cfg.gates = {"code_quality": {"maxFindings": 5000}}  # above the API's per_page max

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "pass"
    assert result["observed"]["truncated"] is False
    findings_call = next(c for c in client.calls if "code-quality/findings" in c[0])
    assert findings_call[1]["per_page"] == 100


def test_unpinned_snapshot_can_be_opted_into(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corroboration is on by default but can be waived explicitly."""
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_clean"], analyses=[])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)
    cfg.gates = {"code_quality": {"requireAnalysisAtHeadSha": False, "timeoutSeconds": 0}}

    result = CodeQualityGate().evaluate(run, cfg)

    assert result["status"] == "pass"
    assert result["observed"]["analysisId"] is None
    assert "code-scanning/analyses" not in [c[0] for c in client.calls]


def test_missing_head_sha_is_not_run_by_default(
    cfg: Any, run: Any, cq: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(cq, setup=cq["setup_configured"], findings=cq["findings_clean"])
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    result = CodeQualityGate().evaluate({**run, "headSha": ""}, cfg)

    assert result["status"] == "not_run"
    assert "headSha" in result["message"]
