"""Dependency review / Dependabot gate.

The important behavioural property here is that *removing* a vulnerable
dependency must never fail the gate — a naive implementation that counts every
advisory in the compare response punishes the exact change you want people to
make.
"""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.gate.codeql import GitHubApiError
from adlc.adapters.gate.dependency import (
    DependencyReviewGate,
    normalize_severity,
    summarize_dependabot_alerts,
    summarize_dependency_review,
)

from .conftest import FakeClient, load_fixture


@pytest.fixture
def dep() -> dict[str, Any]:
    return load_fixture("dependency.json")


# ---------------------------------------------------------------------------
# Severity vocabulary
# ---------------------------------------------------------------------------


def test_moderate_and_medium_are_the_same_band() -> None:
    """Dependency review says 'moderate'; Dependabot says 'medium'."""
    assert normalize_severity("moderate") == "medium"
    assert normalize_severity("medium") == "medium"


def test_severity_normalisation_is_case_insensitive_and_safe() -> None:
    assert normalize_severity("CRITICAL") == "critical"
    assert normalize_severity(" High ") == "high"
    for junk in (None, "", "banana", 5, object()):
        assert normalize_severity(junk) == "unknown"


# ---------------------------------------------------------------------------
# Dependency review summarisation
# ---------------------------------------------------------------------------


def test_only_added_dependencies_count(dep: dict[str, Any]) -> None:
    """The fixture removes a *critical* advisory; that must not be a violation."""
    summary = summarize_dependency_review(dep["dependency_review"])
    assert summary["bySeverity"]["high"] == 1
    assert summary["bySeverity"]["medium"] == 1  # 'moderate' folded in
    assert summary["bySeverity"]["critical"] == 0, "removed advisories must not count"
    assert summary["total"] == 2
    assert summary["packages"] == ["requests@2.19.0"]


def test_removed_advisories_can_be_included_explicitly(dep: dict[str, Any]) -> None:
    summary = summarize_dependency_review(dep["dependency_review"], added_only=False)
    assert summary["bySeverity"]["critical"] == 1


def test_clean_dependency_added_contributes_nothing(dep: dict[str, Any]) -> None:
    added_clean = [c for c in dep["dependency_review"] if c["name"] == "attrs"]
    summary = summarize_dependency_review(added_clean)
    assert summary["total"] == 0


def test_advisory_detail_is_captured(dep: dict[str, Any]) -> None:
    summary = summarize_dependency_review(dep["dependency_review"])
    ghsa = {a["ghsaId"] for a in summary["advisories"]}
    assert "GHSA-j8r2-6x86-q33q" in ghsa


def test_dependabot_summary(dep: dict[str, Any]) -> None:
    summary = summarize_dependabot_alerts(dep["dependabot_alerts"])
    assert summary["source"] == "dependabot-alerts"
    assert summary["bySeverity"]["high"] == 1
    assert summary["bySeverity"]["medium"] == 1
    assert summary["packages"] == ["requests"]


def test_summaries_ignore_junk() -> None:
    assert summarize_dependency_review([None, 1, "x", {}])["total"] == 0  # type: ignore[list-item]
    assert summarize_dependabot_alerts([None, 1, "x"])["total"] == 0  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Gate level
# ---------------------------------------------------------------------------


def test_gate_fails_on_newly_added_high_advisory(
    cfg: Any, run: Any, dep: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(routes={"dependency-graph/compare": dep["dependency_review"]})
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    result = DependencyReviewGate().evaluate(run, cfg)

    assert result["status"] == "fail"
    assert result["severity"] == "high"
    assert result["observed"]["source"] == "dependency-review"
    assert "requests@2.19.0" in result["message"]


def test_gate_passes_when_no_vulnerable_dependency_is_added(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(routes={"dependency-graph/compare": []})
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    result = DependencyReviewGate().evaluate(run, cfg)

    assert result["status"] == "pass"
    assert result["observed"]["total"] == 0


def test_gate_uses_the_base_head_range(
    cfg: Any, run: Any, dep: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(routes={"dependency-graph/compare": dep["dependency_review"]})
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    DependencyReviewGate().evaluate(run, cfg)

    path = next(c[0] for c in client.calls if "dependency-graph/compare" in c[0])
    assert run["baseSha"] in path
    assert run["headSha"] in path


def test_gate_falls_back_to_dependabot_and_says_so(
    cfg: Any, run: Any, dep: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback is repo-scoped, so the result must disclose the wider scope."""
    client = FakeClient(
        routes={"dependabot/alerts": dep["dependabot_alerts"]},
        errors={"dependency-graph/compare": GitHubApiError("HTTP 403", status=403)},
    )
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    result = DependencyReviewGate().evaluate(run, cfg)

    assert result["status"] == "fail"  # one 'high' advisory
    assert result["observed"]["source"] == "dependabot-alerts"
    assert any("pre-existing" in note for note in result["observed"]["notes"])


def test_gate_not_run_when_both_apis_fail(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(
        errors={
            "dependency-graph/compare": GitHubApiError("HTTP 403", status=403),
            "dependabot/alerts": GitHubApiError("HTTP 404", status=404),
        }
    )
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    result = DependencyReviewGate().evaluate(run, cfg)

    assert result["status"] == "not_run"
    assert result["status"] != "pass"
    assert "deps_local" in result["message"]


def test_not_run_does_not_fail_the_build_for_an_optional_gate(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dependency` is not in either profile's required set."""
    client = FakeClient(errors={"dependency-graph/compare": GitHubApiError("nope", status=403),
                                "dependabot/alerts": GitHubApiError("nope", status=403)})
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    result = DependencyReviewGate().evaluate(run, cfg)

    assert result["status"] == "not_run"
    assert result["required"] is False


def test_missing_shas_fall_back_with_a_note(
    cfg: Any, run: Any, dep: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(routes={"dependabot/alerts": dep["dependabot_alerts"]})
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)

    result = DependencyReviewGate().evaluate({**run, "baseSha": "", "headSha": ""}, cfg)

    assert result["observed"]["source"] == "dependabot-alerts"
    assert any("baseSha" in note for note in result["observed"]["notes"])


def test_configurable_threshold(
    cfg: Any, run: Any, dep: dict[str, Any], with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(routes={"dependency-graph/compare": dep["dependency_review"]})
    monkeypatch.setattr("adlc.adapters.gate.dependency.GitHubRestClient", client)
    cfg.gates = {"dependency": {"maxBySeverity": {"critical": 0, "high": 3}}}

    result = DependencyReviewGate().evaluate(run, cfg)

    assert result["status"] == "pass"
