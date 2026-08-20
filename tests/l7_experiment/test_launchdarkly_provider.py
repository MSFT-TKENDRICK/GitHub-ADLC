"""LaunchDarkly provider — availability, manifest and OpenFeature evaluation.

Runs with **no credentials**: the interesting assertion is that ``detect()``
returns ``(False, reason)`` and the spine carries on with its flagd file
provider. The evaluation path is exercised with an injected fake OpenFeature
client so it can be tested without an SDK key or a network connection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.flags.launchdarkly import (
    MANIFEST_NAME,
    REQUIRED_MODULES,
    SDK_KEY_ENV,
    LaunchDarklyProvider,
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
    path = LaunchDarklyProvider(run_dir=tmp_path).materialize(RUN)
    assert path == tmp_path / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["provider"] == "launchdarkly"
    assert manifest["runId"] == "2026-08-19-a1b2"
    assert manifest["flagSetId"] == "2026-08-19-a1b2"
    flag = manifest["flags"][0]
    assert flag["key"] == "adlc.exp.a1b2"
    assert flag["variations"] == ["candidate-a"]
    assert flag["servedTo"]["candidate-a"]["role"] == "treatment"
    assert "cannot create them" in manifest["note"]


def test_materialize_never_writes_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SDK_KEY_ENV, "sdk-super-secret-value")
    path = LaunchDarklyProvider(run_dir=tmp_path).materialize(RUN)
    body = path.read_text(encoding="utf-8")
    assert "sdk-super-secret-value" not in body
    assert SDK_KEY_ENV in body, "the manifest names the env var, never its value"


def test_materialize_honours_the_run_dir_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADLC_RUN_DIR", str(tmp_path / "elsewhere"))
    path = LaunchDarklyProvider().materialize(RUN)
    assert path == tmp_path / "elsewhere" / MANIFEST_NAME
    assert path.is_file()


def test_materialize_handles_a_run_with_no_flags(tmp_path: Path) -> None:
    path = LaunchDarklyProvider(run_dir=tmp_path).materialize({"runId": "r1", "variants": []})
    assert json.loads(path.read_text(encoding="utf-8"))["flags"] == []


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
    assert span["name"] == "feature_flag.evaluation"
    attributes = span["attributes"]
    assert attributes == {
        "feature_flag.key": "adlc.exp.a1b2",
        "feature_flag.provider.name": "launchdarkly",
        "feature_flag.result.value": "candidate-a",
        "feature_flag.result.variant": "candidate-a",
        "feature_flag.result.reason": "TARGETING_MATCH",
        "feature_flag.context.id": "ci-runner-7",
        "feature_flag.set.id": "exp-a1b2",
    }
    assert set(attributes) <= set(FLAG_ATTRIBUTES)


def test_obsolete_semconv_spellings_are_never_emitted() -> None:
    telemetry = _RecordingTelemetry()
    provider = LaunchDarklyProvider(client=_FakeClient(), telemetry=telemetry)
    provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci", "default": "control"})
    attributes = telemetry.spans[0]["attributes"]
    for obsolete in (
        "feature_flag.provider_name",
        "feature_flag.variant",
        "feature_flag.evaluation.reason",
        "feature_flag.context.key",
    ):
        assert obsolete not in attributes


def test_telemetry_failure_does_not_break_evaluation() -> None:
    class _BrokenTelemetry:
        def emit(self, span: dict[str, Any]) -> None:
            raise RuntimeError("collector is down")

    provider = LaunchDarklyProvider(client=_FakeClient(), telemetry=_BrokenTelemetry())
    assert provider.evaluate("adlc.exp.a1b2", {"default": "control"})["value"] == "candidate-a"
