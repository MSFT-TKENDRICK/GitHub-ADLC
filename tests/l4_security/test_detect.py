"""``detect()`` must be cheap, offline, non-raising, and specific.

These are the tests that guarantee the L4 adapters degrade politely on a machine
with no GitHub credentials, so the spine's credential-free conformance suite is
unaffected by anything in this workstream.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any

import pytest

import adlc.adapters.gate.code_quality as code_quality_module
import adlc.adapters.gate.codeql as codeql_module
import adlc.adapters.gate.dependency as dependency_module
from adlc.adapters.gate.code_quality import CodeQualityGate
from adlc.adapters.gate.codeql import CodeQlGate
from adlc.adapters.gate.dependency import DependencyReviewGate
from adlc.ports import GATE_IDS, GateRunner

ALL_GATES = (CodeQlGate, CodeQualityGate, DependencyReviewGate)


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_detect_is_false_without_credentials(gate_cls: Any, cfg: Any) -> None:
    available, reason = gate_cls.detect(cfg)
    assert available is False
    assert reason, "detect() must always explain itself"


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_detect_reason_names_the_missing_env_vars(gate_cls: Any, cfg: Any) -> None:
    """The reason is surfaced verbatim to users, so it must be actionable."""
    _, reason = gate_cls.detect(cfg)
    assert "GITHUB_TOKEN" in reason
    assert "GITHUB_REPOSITORY" in reason


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_detect_is_true_with_credentials(gate_cls: Any, cfg: Any, with_credentials: None) -> None:
    available, reason = gate_cls.detect(cfg)
    assert available is True
    assert "acme/widget" in reason


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_detect_makes_no_network_call(
    gate_cls: Any, cfg: Any, with_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTRIBUTING rule 5: detect() must not touch the network."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("detect() attempted a network call")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    gate_cls.detect(cfg)


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_detect_does_not_raise_on_hostile_config(gate_cls: Any) -> None:
    """A broken config must not crash capability probing."""
    for bogus in (None, object(), {"not": "a config"}):
        available, reason = gate_cls.detect(bogus)  # type: ignore[arg-type]
        assert isinstance(available, bool)
        assert isinstance(reason, str)


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_gate_satisfies_the_frozen_protocol(gate_cls: Any) -> None:
    gate = gate_cls()
    assert isinstance(gate, GateRunner)
    assert gate.kind == "gate"
    assert callable(gate.evaluate)


def test_adapters_under_test_come_from_this_checkout() -> None:
    """Guard against an editable install pointing at a different worktree."""
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    for module in (codeql_module, code_quality_module, dependency_module):
        assert str(module.__file__).startswith(repo_src), (
            f"{module.__name__} was imported from {module.__file__}, not {repo_src}"
        )


def test_gate_ids_are_registered_vocabulary() -> None:
    """Ids must match the frozen GATE_IDS and the pyproject entry-point names."""
    assert CodeQlGate.id == "security"
    assert CodeQualityGate.id == "code_quality"
    assert DependencyReviewGate.id == "dependency"
    for gate_cls in (CodeQlGate, CodeQualityGate):
        assert gate_cls.id in GATE_IDS


def test_required_by_default_matches_the_plan() -> None:
    assert CodeQlGate.required_by_default is True
    assert CodeQualityGate.required_by_default is True
    # Dependency review is advisory: the spine's credential-free deps_local gate
    # is the required one.
    assert DependencyReviewGate.required_by_default is False


@pytest.mark.parametrize("gate_cls", ALL_GATES)
def test_evaluate_without_credentials_is_not_run_never_pass(
    gate_cls: Any, cfg: Any, run: Any
) -> None:
    """No credentials must degrade to not_run — never to a pass."""
    result = gate_cls().evaluate(run, cfg)
    assert result["status"] == "not_run"
    assert result["status"] != "pass"
    assert result["id"] == gate_cls.id
    assert result["evidence"] == [f"gates/{gate_cls.id}.json"]
    assert result["message"]


def test_required_flag_reflects_profile(cfg: Any, run: Any) -> None:
    """`required: true` + `not_run` is what the aggregator turns into a failure."""
    result = CodeQlGate().evaluate(run, cfg)
    assert result["required"] is True
    assert result["status"] == "not_run"
