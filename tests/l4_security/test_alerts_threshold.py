"""Severity summarisation and threshold comparison for code scanning alerts."""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.gate.codeql import (
    CodeQlGate,
    alert_sarif_severity,
    alert_security_severity,
    evaluate_threshold,
    summarize_alerts,
)

from .conftest import HEAD_SHA, FakeClient, load_fixture


@pytest.fixture
def alerts() -> dict[str, Any]:
    return load_fixture("code_scanning_alerts.json")


def test_security_severity_is_read_from_the_rule(alerts: dict[str, Any]) -> None:
    critical = alerts["breaching"][0]
    assert alert_security_severity(critical) == "critical"
    assert alert_sarif_severity(critical) == "error"


def test_quality_rule_has_no_security_severity(alerts: dict[str, Any]) -> None:
    """Quality rules carry `security_severity_level: null` -- never invent a band."""
    quality = alerts["clean"][0]
    assert quality["rule"]["security_severity_level"] is None
    assert alert_security_severity(quality) == "unknown"
    assert alert_sarif_severity(quality) == "note"


def test_summarize_counts_both_vocabularies(alerts: dict[str, Any]) -> None:
    summary = summarize_alerts(alerts["breaching"], sha=HEAD_SHA)
    assert summary["total"] == 3
    assert summary["bySeverity"]["critical"] == 1
    assert summary["bySeverity"]["high"] == 1
    assert summary["bySeverity"]["low"] == 1
    assert summary["bySarifSeverity"]["error"] == 2
    assert summary["bySarifSeverity"]["warning"] == 1
    assert "py/sql-injection" in summary["ruleIds"]


def test_at_sha_is_reported_but_does_not_shrink_the_blocking_set(alerts: dict[str, Any]) -> None:
    """For a PR the instance SHA is the merge commit, not the head SHA.

    Filtering the blocking set on it would silently drop every real finding.
    """
    summary = summarize_alerts(alerts["breaching"], sha=HEAD_SHA)
    assert summary["atSha"] == 0
    assert summary["total"] == 3, "total must not be reduced by the instance-SHA mismatch"


def test_threshold_violation_records(alerts: dict[str, Any]) -> None:
    summary = summarize_alerts(alerts["breaching"])
    violations = evaluate_threshold(summary["bySeverity"], {"critical": 0, "high": 0})
    assert {v["severity"] for v in violations} == {"critical", "high"}
    assert all(v["observed"] > v["max"] for v in violations)


def test_threshold_allows_configured_budget(alerts: dict[str, Any]) -> None:
    summary = summarize_alerts(alerts["breaching"])
    assert evaluate_threshold(summary["bySeverity"], {"critical": 1, "high": 1}) == []


def test_threshold_ignores_unlisted_severities(alerts: dict[str, Any]) -> None:
    """Default policy does not block on low/medium."""
    summary = summarize_alerts(alerts["clean"])
    assert evaluate_threshold(summary["bySeverity"], {"critical": 0, "high": 0}) == []


def test_malformed_threshold_values_are_treated_as_zero() -> None:
    assert evaluate_threshold({"critical": 1}, {"critical": "not-a-number"}) == [
        {"severity": "critical", "observed": 1, "max": 0}
    ]


def test_summarize_ignores_junk_entries() -> None:
    summary = summarize_alerts([None, "junk", 7, {}])  # type: ignore[list-item]
    assert summary["total"] == 1  # only the {} counts, as an unknown-severity alert
    assert summary["bySeverity"]["unknown"] == 1


# ---------------------------------------------------------------------------
# Gate level
# ---------------------------------------------------------------------------


def test_gate_passes_when_within_threshold(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    client = FakeClient(analyses=analyses, alerts=fixture["clean"])
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    result = CodeQlGate().evaluate(run, cfg)

    assert result["status"] == "pass"
    assert result["observed"]["analysisId"] == 900299
    assert result["observed"]["headSha"] == HEAD_SHA
    assert result["expected"]["maxBySeverity"] == {"critical": 0, "high": 0}


def test_gate_fails_on_critical_alerts(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    client = FakeClient(analyses=analyses, alerts=fixture["breaching"])
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    result = CodeQlGate().evaluate(run, cfg)

    assert result["status"] == "fail"
    assert result["severity"] == "critical"
    assert result["observed"]["bySeverity"]["critical"] == 1
    assert len(result["observed"]["violations"]) == 2


def test_gate_reads_alerts_scoped_to_the_matched_analysis_ref(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alerts must be queried on the ref of the analysis we actually matched."""
    fixture = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    client = FakeClient(analyses=analyses, alerts=fixture["clean"])
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    CodeQlGate().evaluate(run, cfg)

    alert_calls = [c for c in client.calls if c[0] == "code-scanning/alerts"]
    assert alert_calls
    assert alert_calls[0][1]["ref"] == "refs/pull/42/merge"
    assert alert_calls[0][1]["state"] == "open"


def test_truncated_alert_set_is_not_run_never_pass(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean sample from a partial result set proves nothing — fail closed."""
    fixture = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    client = FakeClient(analyses=analyses, alerts=fixture["clean"], truncated=True)
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    result = CodeQlGate().evaluate(run, cfg)

    assert result["status"] == "not_run"
    assert result["status"] != "pass"
    assert result["observed"]["truncated"] is True
    assert "truncated" in result["message"]


def test_truncation_still_fails_on_a_real_breach(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A breach found inside the sample is sound regardless of truncation."""
    fixture = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    client = FakeClient(analyses=analyses, alerts=fixture["breaching"], truncated=True)
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    result = CodeQlGate().evaluate(run, cfg)

    assert result["status"] == "fail"


def test_configurable_threshold_from_config(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = load_fixture("code_scanning_alerts.json")
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    client = FakeClient(analyses=analyses, alerts=fixture["breaching"])
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)
    cfg.gates = {"security": {"maxBySeverity": {"critical": 5, "high": 5}}}

    result = CodeQlGate().evaluate(run, cfg)

    assert result["status"] == "pass"
    assert result["expected"]["maxBySeverity"] == {"critical": 5, "high": 5}
