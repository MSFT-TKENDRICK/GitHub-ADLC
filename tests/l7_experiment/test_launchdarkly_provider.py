"""LaunchDarkly provider — availability, manifest and OpenFeature evaluation.

Runs with **no credentials**: the interesting assertion is that ``detect()``
returns ``(False, reason)`` and the spine carries on with its flagd file
provider. The evaluation path is exercised with an injected fake OpenFeature
client so it can be tested without an SDK key or a network connection.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.flags.launchdarkly import (
    MANIFEST_NAME,
    REQUIRED_MODULES,
    SDK_KEY_ENV,
    LaunchDarklyProvider,
    _context_attributes,
)
from adlc.config import Config
from adlc.ports import FlagProvider
from adlc.stages.experiment import FLAG_ATTRIBUTES

RUN: dict[str, Any] = {
    "runId": "2026-08-19-a1b2",
    "variants": [
        {"key": "control", "role": "control", "commit": "3f1a9c7e", "flagKeys": []},
        {
            "key": "candidate-a",
            "role": "treatment",
            "commit": "c7e2b4d6",
            "flagKeys": ["adlc.exp.a1b2"],
        },
    ],
}


class _Details:
    def __init__(self, value: Any, variant: str | None, reason: str | None) -> None:
        self.value = value
        self.variant = variant
        self.reason = reason


class _FakeClient:
    """Minimal stand-in for an OpenFeature client."""

    def __init__(self, value: Any = "candidate-a", *, raises: bool = False) -> None:
        self.value = value
        self.raises = raises
        self.calls: list[tuple[str, Any, Any]] = []

    def _details(self, key: str, default: Any, context: Any) -> _Details:
        if self.raises:
            raise RuntimeError("LaunchDarkly is unreachable")
        self.calls.append((key, default, context))
        return _Details(self.value, "candidate-a", "TARGETING_MATCH")

    get_boolean_details = _details
    get_string_details = _details
    get_integer_details = _details
    get_float_details = _details
    get_object_details = _details


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def emit(self, span: dict[str, Any]) -> None:
        self.spans.append(span)


# -- availability -------------------------------------------------------------


def test_detect_is_false_without_a_key(cfg: Config) -> None:
    available, reason = LaunchDarklyProvider.detect(cfg)
    assert available is False
    assert SDK_KEY_ENV in reason
    assert "flagd-file" in reason, "the reason must name the fallback the spine will use"


def test_detect_is_false_for_a_blank_key(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SDK_KEY_ENV, "   ")
    available, reason = LaunchDarklyProvider.detect(cfg)
    assert available is False
    assert SDK_KEY_ENV in reason


def test_detect_names_the_missing_distribution(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SDK_KEY_ENV, "sdk-fake-key")
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    available, reason = LaunchDarklyProvider.detect(cfg)
    assert available is False
    for _module, distribution in REQUIRED_MODULES:
        assert distribution in reason
    assert "pip install" in reason


def test_detect_is_true_when_key_and_packages_are_present(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SDK_KEY_ENV, "sdk-fake-key")
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    available, reason = LaunchDarklyProvider.detect(cfg)
    assert available is True
    assert "never gates a run" in reason


def test_detect_never_raises(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SDK_KEY_ENV, "sdk-fake-key")

    def _boom(_name: str) -> None:
        raise RuntimeError("import system is unhappy")

    monkeypatch.setattr("importlib.util.find_spec", _boom)
    available, reason = LaunchDarklyProvider.detect(cfg)
    assert available is False
    assert "probe failed" in reason


def test_detect_makes_no_network_call(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _no_sockets(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("detect() must not open a socket")

    monkeypatch.setenv(SDK_KEY_ENV, "sdk-fake-key")
    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    LaunchDarklyProvider.detect(cfg)


def test_provider_satisfies_the_frozen_protocol() -> None:
    assert isinstance(LaunchDarklyProvider(), FlagProvider)
    assert LaunchDarklyProvider.name == "launchdarkly"
    assert LaunchDarklyProvider.kind == "flags"


# -- materialize --------------------------------------------------------------


def test_materialize_writes_the_variant_to_flag_mapping(tmp_path: Path) -> None:
    path = LaunchDarklyProvider(tmp_path / MANIFEST_NAME).materialize(RUN)
    assert path == tmp_path / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["provider"] == "launchdarkly"
    assert manifest["runId"] == "2026-08-19-a1b2"
    assert manifest["flagSetId"] == "adlc/2026-08-19-a1b2"
    flag = manifest["flags"][0]
    assert flag["key"] == "adlc.exp.a1b2"
    assert flag["variations"] == ["candidate-a"]
    assert flag["servedTo"]["candidate-a"]["role"] == "treatment"
    assert "cannot create them" in manifest["note"]


def test_materialize_never_writes_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SDK_KEY_ENV, "sdk-super-secret-value")
    path = LaunchDarklyProvider(tmp_path / MANIFEST_NAME).materialize(RUN)
    body = path.read_text(encoding="utf-8")
    assert "sdk-super-secret-value" not in body
    assert SDK_KEY_ENV in body, "the manifest names the env var, never its value"


def test_materialize_defaults_to_the_run_directory(tmp_path: Path) -> None:
    """Same default layout as the spine's flagd file provider."""
    provider = LaunchDarklyProvider()
    path = provider.materialize(RUN)
    assert path == Path(".adlc") / "runs" / "2026-08-19-a1b2" / MANIFEST_NAME
    assert path.is_file()
    # The resolved location is remembered, exactly like FlagdFileProvider.path.
    assert provider.path == path


def test_materialize_handles_a_run_with_no_flags(tmp_path: Path) -> None:
    path = LaunchDarklyProvider(tmp_path / MANIFEST_NAME).materialize(
        {"runId": "r1", "variants": []}
    )
    assert json.loads(path.read_text(encoding="utf-8"))["flags"] == []


def test_provider_surface_matches_the_spine_default() -> None:
    """A caller must be able to swap flagd-file for LaunchDarkly with no changes."""
    from adlc.adapters.flags.flagd_file import FlagdFileProvider

    for method in ("detect", "materialize", "evaluate", "span_attributes"):
        assert hasattr(LaunchDarklyProvider, method)
    assert inspect.signature(LaunchDarklyProvider.span_attributes) == inspect.signature(
        FlagdFileProvider.span_attributes
    )
    assert inspect.signature(LaunchDarklyProvider.evaluate) == inspect.signature(
        FlagdFileProvider.evaluate
    )
    assert inspect.signature(LaunchDarklyProvider.materialize) == inspect.signature(
        FlagdFileProvider.materialize
    )


# -- evaluate -----------------------------------------------------------------


def test_evaluate_returns_a_flag_result() -> None:
    provider = LaunchDarklyProvider(client=_FakeClient())
    result = provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci", "default": "control"})
    assert result == {
        "key": "adlc.exp.a1b2",
        "value": "candidate-a",
        "variant": "candidate-a",
        "reason": "TARGETING_MATCH",
    }


def test_evaluate_selects_the_typed_accessor_from_the_default() -> None:
    client = _FakeClient(value=True)
    provider = LaunchDarklyProvider(client=client)
    provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci", "default": False})
    key, default, _context = client.calls[0]
    assert key == "adlc.exp.a1b2"
    assert default is False


def test_evaluate_does_not_raise_when_the_backend_is_down() -> None:
    """A flag outage degrades to a defaulted result; it never fails a build."""
    provider = LaunchDarklyProvider(client=_FakeClient(raises=True))
    result = provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci", "default": "control"})
    assert result["reason"] == "ERROR"
    assert result["value"] == "control"
    assert result["variant"] is None


def test_evaluate_without_a_key_is_an_error_result_not_an_exception() -> None:
    result = LaunchDarklyProvider().evaluate("adlc.exp.a1b2", {"targetingKey": "ci"})
    assert result["reason"] == "ERROR"
    assert result["value"] is False


# -- telemetry ----------------------------------------------------------------


def test_telemetry_uses_current_semconv_attribute_names() -> None:
    telemetry = _RecordingTelemetry()
    provider = LaunchDarklyProvider(client=_FakeClient(), telemetry=telemetry)
    provider.evaluate(
        "adlc.exp.a1b2",
        {"targetingKey": "ci-runner-7", "default": "control", "flagSetId": "exp-a1b2"},
    )

    (span,) = telemetry.spans
    assert span == {
        "name": "feature_flag.evaluation",
        "feature_flag.key": "adlc.exp.a1b2",
        "feature_flag.provider.name": "launchdarkly",
        "feature_flag.result.value": "candidate-a",
        "feature_flag.result.variant": "candidate-a",
        "feature_flag.result.reason": "targeting_match",
        "feature_flag.context.id": "ci-runner-7",
        "feature_flag.set.id": "exp-a1b2",
    }
    assert set(span) - {"name"} <= set(FLAG_ATTRIBUTES)


def test_span_attributes_match_the_spine_shape() -> None:
    """Identical keys to ``FlagdFileProvider.span_attributes`` for the same input."""
    from adlc.adapters.flags.flagd_file import FlagdFileProvider

    result = {
        "key": "adlc.exp.a1b2",
        "value": "candidate-a",
        "variant": "candidate-a",
        "reason": "TARGETING_MATCH",
    }
    ctx = {"targetingKey": "ci-runner-7"}
    ours = LaunchDarklyProvider().span_attributes(result, ctx)
    theirs = FlagdFileProvider().span_attributes(result, ctx)

    assert set(ours) == set(theirs)
    assert ours["feature_flag.provider.name"] == "launchdarkly"
    assert ours["feature_flag.result.reason"] == "targeting_match"
    assert ours["feature_flag.context.id"] == "ci-runner-7"


def test_flag_set_id_falls_back_to_the_materialized_manifest(tmp_path: Path) -> None:
    provider = LaunchDarklyProvider(tmp_path / "flags.json", client=_FakeClient())
    provider.materialize(
        {"runId": "2026-08-19-a1b2", "variants": [{"key": "a", "flagKeys": ["f"]}]}
    )
    attributes = provider.span_attributes({"key": "f", "reason": "DEFAULT"}, {})
    assert attributes["feature_flag.set.id"] == "adlc/2026-08-19-a1b2"


def test_adlc_only_context_keys_are_not_sent_to_launchdarkly() -> None:
    """``default`` and ``flagSetId`` are ADLC conveniences, not LD context attributes."""
    attributes = _context_attributes(
        {"targetingKey": "ci", "default": "control", "flagSetId": "exp-a1b2", "tier": "gold"}
    )
    assert attributes == {"tier": "gold"}


def test_obsolete_semconv_spellings_are_never_emitted() -> None:
    telemetry = _RecordingTelemetry()
    provider = LaunchDarklyProvider(client=_FakeClient(), telemetry=telemetry)
    provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci", "default": "control"})
    span = telemetry.spans[0]
    for obsolete in (
        "feature_flag.provider_name",
        "feature_flag.variant",
        "feature_flag.evaluation.reason",
        "feature_flag.context.key",
    ):
        assert obsolete not in span


def test_telemetry_prefers_the_spine_builder(tmp_path: Path) -> None:
    from adlc.adapters.telemetry.otel_file import OtelFileTelemetry

    telemetry = OtelFileTelemetry(tmp_path / "otel.jsonl")
    provider = LaunchDarklyProvider(client=_FakeClient(), telemetry=telemetry)
    provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci", "default": "control"})

    span = json.loads((tmp_path / "otel.jsonl").read_text(encoding="utf-8").strip())
    assert span["name"] == "feature_flag.evaluation"
    assert span["feature_flag.provider.name"] == "launchdarkly"
    assert span["feature_flag.result.reason"] == "targeting_match"


def test_telemetry_failure_does_not_break_evaluation() -> None:
    class _BrokenTelemetry:
        def emit(self, span: dict[str, Any]) -> None:
            raise RuntimeError("collector is down")

    provider = LaunchDarklyProvider(client=_FakeClient(), telemetry=_BrokenTelemetry())
    assert provider.evaluate("adlc.exp.a1b2", {"default": "control"})["value"] == "candidate-a"
