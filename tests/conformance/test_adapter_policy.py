"""Adapter selection policy -- the guard against silent, costly escalation.

The dangerous default in a plugin architecture is "first thing that detects
wins". On any GitHub Actions runner ``GITHUB_TOKEN`` is present, so a naive
policy would silently switch a plain ``adlc build`` onto a paid cloud agent that
opens pull requests. These tests pin the policy down.
"""

from __future__ import annotations

import os

import pytest

from adlc.config import EXPLICIT_ONLY_KINDS, SPINE_DEFAULTS, Config, select_adapter


def test_agents_never_auto_escalate(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cost-incurring, PR-opening runner must be a deliberate choice."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token_for_test_only_000000")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    runner = select_adapter(cfg, "agents")
    assert runner.name == SPINE_DEFAULTS["agents"] == "fake", (
        "an ambient GITHUB_TOKEN must not switch the agent runner"
    )


def test_taskstore_never_auto_escalates(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing issues into a live repo must be opted into, not inferred."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token_for_test_only_000000")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    store = select_adapter(cfg, "taskstore")
    assert store.name == SPINE_DEFAULTS["taskstore"] == "sqlite"


def test_explicit_override_is_honoured(cfg: Config) -> None:
    """Opting in works, and does so by name."""
    runner = select_adapter(cfg, "agents", "fake")
    assert runner.name == "fake"


def test_unknown_adapter_fails_loudly(cfg: Config) -> None:
    """A typo in config must not silently fall back to the default."""
    with pytest.raises(LookupError, match="not registered"):
        select_adapter(cfg, "agents", "does-not-exist")


def test_side_effecting_kinds_are_declared() -> None:
    """The guard list is the contract; keep it explicit."""
    assert EXPLICIT_ONLY_KINDS == frozenset({"agents", "taskstore"})


def test_observational_kinds_may_auto_detect(cfg: Config) -> None:
    """Read-only adapters are allowed to upgrade themselves."""
    assert "evidence" not in EXPLICIT_ONLY_KINDS
    collector = select_adapter(cfg, "evidence")
    # With playwright absent, the always-available local collector is chosen.
    assert collector.name in {"local", "playwright"}


def test_every_spine_default_is_credential_free(cfg: Config) -> None:
    """The default path must work with nothing configured and no secrets."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "LAUNCHDARKLY_SDK_KEY"):
        os.environ.pop(name, None)

    for kind, expected in SPINE_DEFAULTS.items():
        adapter = select_adapter(cfg, kind)  # type: ignore[arg-type]
        available, reason = type(adapter).detect(cfg)
        assert available, f"spine default for '{kind}' is unavailable: {reason}"
        if kind in EXPLICIT_ONLY_KINDS:
            assert adapter.name == expected
