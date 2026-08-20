"""``detect()`` must be cheap, non-raising, network-free and specific.

This is the contract from ``CONTRIBUTING.md`` rules 4 and 5: with no
credentials every L10 adapter reports ``(False, <specific reason>)`` and the
spine carries on with its own defaults.
"""

from __future__ import annotations

import socket

import pytest

from adlc.adapters.daytwo.foundry import FoundryHotfixAgent
from adlc.adapters.daytwo.sre_agent import SreAgentReceiver
from adlc.adapters.telemetry.appinsights import CONNECTION_STRING_ENV, AppInsightsTelemetry
from adlc.config import Config

ADAPTERS = [SreAgentReceiver, FoundryHotfixAgent, AppInsightsTelemetry]
ADAPTER_IDS = [a.__name__ for a in ADAPTERS]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=ADAPTER_IDS)
def test_detect_is_false_without_credentials(adapter, cfg: Config) -> None:
    available, reason = adapter.detect(cfg)
    assert available is False
    assert isinstance(reason, str)
    # "specific" means it names what was missing, not just "unavailable".
    assert len(reason) > 30, f"reason is not specific enough: {reason!r}"
    assert "unavailable" != reason.strip().lower()


@pytest.mark.parametrize("adapter", ADAPTERS, ids=ADAPTER_IDS)
def test_detect_names_the_thing_that_is_missing(adapter, cfg: Config) -> None:
    _, reason = adapter.detect(cfg)
    expected = {
        "SreAgentReceiver": "ADLC_INCIDENT_PAYLOAD",
        "FoundryHotfixAgent": "FOUNDRY_PROJECT_ENDPOINT",
        "AppInsightsTelemetry": CONNECTION_STRING_ENV,
    }[adapter.__name__]
    assert expected in reason


@pytest.mark.parametrize("adapter", ADAPTERS, ids=ADAPTER_IDS)
def test_detect_declares_adapter_protocol_fields(adapter) -> None:
    assert isinstance(adapter.name, str) and adapter.name
    assert adapter.kind in {"daytwo", "telemetry"}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=ADAPTER_IDS)
def test_detect_makes_no_network_call(adapter, cfg: Config, monkeypatch) -> None:
    """Rule 5: no network. Detonate on any socket use."""

    def explode(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError(f"{adapter.__name__}.detect() attempted a network call")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    assert adapter.detect(cfg)[0] is False


@pytest.mark.parametrize("adapter", ADAPTERS, ids=ADAPTER_IDS)
def test_detect_reason_is_console_safe(adapter, cfg: Config, monkeypatch) -> None:
    """Reasons are printed by `adlc doctor` and stored in capabilities.json.

    A non-ASCII character renders as a replacement glyph on a default Windows
    console, which makes a diagnostic message look like corruption at exactly
    the moment someone is debugging.
    """
    monkeypatch.setenv(CONNECTION_STRING_ENV, "InstrumentationKey=x")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/p")
    monkeypatch.setenv("ADLC_INCIDENT_FILE", "/nope/missing.json")
    for reason in (adapter.detect(cfg)[1], adapter.detect(None)[1]):  # type: ignore[arg-type]
        assert reason.isascii(), f"non-ASCII in {adapter.__name__} reason: {reason!r}"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=ADAPTER_IDS)
def test_detect_does_not_raise_on_a_hostile_config(adapter) -> None:
    """Rule 5: must not raise. A garbage config is still not an exception."""
    assert adapter.detect(None)[0] is False  # type: ignore[arg-type]
    assert adapter.detect(object())[0] is False  # type: ignore[arg-type]


def test_sre_receiver_available_with_inline_payload(cfg: Config, monkeypatch) -> None:
    monkeypatch.setenv("ADLC_INCIDENT_PAYLOAD", '{"title": "boom"}')
    available, reason = SreAgentReceiver.detect(cfg)
    assert available is True
    assert "ADLC_INCIDENT_PAYLOAD" in reason


def test_sre_receiver_reports_a_missing_file_specifically(cfg: Config, monkeypatch, tmp_path) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("ADLC_INCIDENT_FILE", str(missing))
    available, reason = SreAgentReceiver.detect(cfg)
    assert available is False
    assert str(missing) in reason


def test_appinsights_distinguishes_missing_sdk_from_missing_credential(
    cfg: Config, monkeypatch
) -> None:
    """With the connection string set, the reason must move on to the SDK."""
    monkeypatch.setenv(CONNECTION_STRING_ENV, "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    available, reason = AppInsightsTelemetry.detect(cfg)
    if available:
        assert "azure-monitor-opentelemetry" in reason
    else:
        assert "azure.monitor.opentelemetry" in reason
        assert "not importable" in reason


def test_foundry_requires_a_credential_not_just_an_endpoint(cfg: Config, monkeypatch) -> None:
    """An endpoint alone is not enough - say so, and say what is missing."""
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/p")
    available, reason = FoundryHotfixAgent.detect(cfg)
    assert available is False
    assert "no Azure credential source" in reason

    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s")
    available, reason = FoundryHotfixAgent.detect(cfg)
    assert available is True
    # Availability must still surface the honesty caveat.
    assert "shim" in reason
