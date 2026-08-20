"""Fail-closed timeout behaviour.

This file exists to prove one property above all others:

    **a timeout yields ``not_run``, never ``pass``.**

Code scanning results are uploaded asynchronously, so "I did not see the
analysis" and "there were no findings" are indistinguishable from the outside.
Only one of those interpretations is safe.
"""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.gate.code_quality import CodeQualityGate
from adlc.adapters.gate.codeql import (
    CodeQlGate,
    GitHubApiError,
    poll_for_analysis,
)

from .conftest import HEAD_SHA, PR_REF, FakeClient, FakeClock, load_fixture


@pytest.fixture
def analyses() -> list[dict[str, Any]]:
    return load_fixture("code_scanning_analyses.json")["analyses"]


# ---------------------------------------------------------------------------
# poll_for_analysis -- unit level, deterministic fake clock
# ---------------------------------------------------------------------------


def test_timeout_reports_timed_out_and_no_analysis(clock: FakeClock) -> None:
    poll = poll_for_analysis(
        list,
        sha=HEAD_SHA,
        timeout=30.0,
        interval=10.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.timed_out is True
    assert poll.analysis is None
    assert poll.found is False
    assert poll.attempts == 4  # t=0, 10, 20, 30
    assert clock.slept == [10.0, 10.0, 10.0]


def test_poll_never_sleeps_past_the_deadline(clock: FakeClock) -> None:
    poll = poll_for_analysis(
        list,
        sha=HEAD_SHA,
        timeout=25.0,
        interval=10.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.timed_out is True
    assert sum(clock.slept) <= 25.0
    assert clock.now <= 25.0


def test_zero_timeout_still_makes_exactly_one_attempt(clock: FakeClock) -> None:
    """Even an impatient config must actually look once before giving up."""
    poll = poll_for_analysis(
        list,
        sha=HEAD_SHA,
        timeout=0.0,
        interval=10.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.attempts == 1
    assert poll.timed_out is True
    assert clock.slept == []


def test_poll_interval_is_floored_to_avoid_hammering_the_api(clock: FakeClock) -> None:
    """interval=0 must not become a tight loop against the REST API."""
    poll = poll_for_analysis(
        list,
        sha=HEAD_SHA,
        timeout=5.0,
        interval=0.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.timed_out is True
    assert clock.slept, "a zero interval must still wait between polls"
    assert min(clock.slept) >= 1.0
    assert poll.attempts <= 6


def test_analysis_appearing_late_is_found(clock: FakeClock, analyses: list[dict[str, Any]]) -> None:
    """The whole point of polling: the analysis shows up after a few tries."""
    calls = {"n": 0}

    def fetch() -> list[dict[str, Any]]:
        calls["n"] += 1
        return analyses if calls["n"] >= 3 else []

    poll = poll_for_analysis(
        fetch,
        sha=HEAD_SHA,
        timeout=300.0,
        interval=10.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.found is True
    assert poll.timed_out is False
    assert poll.attempts == 3
    assert (poll.analysis or {})["commit_sha"] == HEAD_SHA


def test_api_errors_are_retried_then_time_out(clock: FakeClock) -> None:
    """A flaky API must end in a timeout, never in a silent success."""

    def fetch() -> list[dict[str, Any]]:
        raise GitHubApiError("HTTP 502: bad gateway", status=502)

    poll = poll_for_analysis(
        fetch,
        sha=HEAD_SHA,
        timeout=20.0,
        interval=10.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.timed_out is True
    assert poll.analysis is None
    assert poll.errors and "502" in poll.errors[0]


def test_unexpected_exceptions_do_not_escape(clock: FakeClock) -> None:
    """A transport bug must not crash the run; it must fail the gate closed."""

    def fetch() -> list[dict[str, Any]]:
        raise ValueError("something unexpected")

    poll = poll_for_analysis(
        fetch,
        sha=HEAD_SHA,
        timeout=0.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.timed_out is True
    assert any("ValueError" in err for err in poll.errors)


def test_stale_analyses_never_satisfy_the_poll(
    clock: FakeClock, analyses: list[dict[str, Any]]
) -> None:
    """Serving only other commits' analyses forever must still time out."""
    stale = [a for a in analyses if a["commit_sha"] != HEAD_SHA]
    poll = poll_for_analysis(
        lambda: stale,
        sha=HEAD_SHA,
        timeout=60.0,
        interval=20.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert poll.timed_out is True
    assert poll.analysis is None


# ---------------------------------------------------------------------------
# Gate level -- the property that actually protects the pipeline
# ---------------------------------------------------------------------------


def _timeout_cfg(cfg: Any) -> Any:
    """Zero timeout => the gate polls once, finds nothing, and gives up."""
    cfg.gates = {"security": {"timeoutSeconds": 0}, "code_quality": {"timeoutSeconds": 0}}
    return cfg


def test_codeql_gate_timeout_is_not_run_never_pass(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE test: an analysis that never arrives must not become a green build."""
    client = FakeClient(analyses=[])  # the analysis for our SHA never appears
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    result = CodeQlGate().evaluate(run, _timeout_cfg(cfg))

    assert result["status"] == "not_run"
    assert result["status"] != "pass"
    assert result["required"] is True, "required + not_run is what fails the build"
    assert result["observed"]["timedOut"] is True
    assert HEAD_SHA[:12] in result["message"]
    assert "stale" in result["message"].lower()


def test_codeql_gate_timeout_when_only_stale_analyses_exist(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false-green scenario, end to end.

    The API happily returns a *newer, clean* analysis of the default branch. The
    gate must refuse it because its commit is not our head SHA.
    """
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]
    stale_only = [a for a in analyses if a["commit_sha"] != HEAD_SHA]
    assert stale_only and stale_only[0]["results_count"] == 0  # tempting green
    client = FakeClient(analyses=stale_only, alerts=[])
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)

    result = CodeQlGate().evaluate(run, _timeout_cfg(cfg))

    assert result["status"] == "not_run"
    assert "code-scanning/alerts" not in [call[0] for call in client.calls], (
        "the gate must not read alerts at all when no analysis matched the head SHA"
    )


def test_codeql_gate_without_head_sha_is_not_run(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SHA to pin to means no trustworthy verdict is possible."""
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", FakeClient())
    result = CodeQlGate().evaluate({**run, "headSha": ""}, cfg)
    assert result["status"] == "not_run"
    assert "headSha" in result["message"]


def test_codeql_gate_alert_read_failure_is_not_run(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding the analysis but failing to read alerts must not pass either."""
    analyses = load_fixture("code_scanning_analyses.json")["analyses"]

    class ExplodingClient(FakeClient):
        def list_alerts_paged(self, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
            raise GitHubApiError("HTTP 403: GHAS not enabled", status=403)

    monkeypatch.setattr(
        "adlc.adapters.gate.codeql.GitHubRestClient", ExplodingClient(analyses=analyses)
    )
    result = CodeQlGate().evaluate(run, cfg)
    assert result["status"] == "not_run"
    assert "403" in result["message"]


def test_code_quality_gate_timeout_is_not_run_never_pass(
    cfg: Any, run: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code Quality corroborates freshness the same way, and fails closed too."""
    cq = load_fixture("code_quality.json")
    client = FakeClient(
        analyses=[],  # no analysis for our head SHA
        routes={
            "code-quality/setup": cq["setup_configured"],
            "code-quality/findings": cq["findings_clean"],
        },
    )
    monkeypatch.setattr("adlc.adapters.gate.code_quality.GitHubRestClient", client)

    result = CodeQualityGate().evaluate(run, _timeout_cfg(cfg))

    assert result["status"] == "not_run"
    assert result["status"] != "pass"
    assert result["observed"]["timedOut"] is True
    assert "code-quality/findings" not in [call[0] for call in client.calls], (
        "findings must not be read when freshness could not be corroborated"
    )


def test_pr_ref_is_used_when_github_ref_is_absent(cfg: Any, run: Any, with_credentials: None,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(analyses=[])
    monkeypatch.setattr("adlc.adapters.gate.codeql.GitHubRestClient", client)
    CodeQlGate().evaluate(run, _timeout_cfg(cfg))
    analyses_calls = [c for c in client.calls if c[0] == "code-scanning/analyses"]
    assert analyses_calls
    assert analyses_calls[0][1]["ref"] == PR_REF
