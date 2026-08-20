"""``adlc export oes`` must refuse any run that is not genuinely comparative.

This is the whole point of demoting OES to an exporter: most ADLC runs are build
or evaluation runs with a single candidate and no live traffic, and emitting an
"experiment" for one would manufacture meaningless nulls. Refusal is the correct
behaviour, not a failure mode — ``adlc-run/v1`` remains the canonical record
either way.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.export.oes import (
    NotComparativeError,
    OesExporter,
    OesExportError,
    is_comparative,
)
from adlc.config import Config
from adlc.ports import Exporter


def _experiment_stage(run: dict[str, Any]) -> dict[str, Any]:
    return next(
        s for s in run["stages"] if s["stage"] == "experiment" and s["data"]["phase"] == "analyze"
    )


def test_single_variant_run_is_refused(
    single_variant_run: dict[str, Any], tmp_path: Path
) -> None:
    out = tmp_path / "oes.json"
    with pytest.raises(NotComparativeError) as excinfo:
        OesExporter().export(single_variant_run, out)
    message = str(excinfo.value)
    assert "1 variant" in message
    assert "at least" in message
    assert "adlc-run/v1 remains the canonical record" in message
    assert not out.exists(), "a refused export must not leave a partial document behind"


def test_zero_variant_run_is_refused(comparative_run: dict[str, Any], tmp_path: Path) -> None:
    run = copy.deepcopy(comparative_run)
    run["variants"] = []
    run["stages"] = [s for s in run["stages"] if s["stage"] != "experiment"]
    with pytest.raises(NotComparativeError):
        OesExporter().export(run, tmp_path / "oes.json")


def test_two_variants_without_measurements_are_refused(
    comparative_run: dict[str, Any], tmp_path: Path
) -> None:
    """Two candidates that were never measured are not an experiment either."""
    run = copy.deepcopy(comparative_run)
    stage = _experiment_stage(run)
    stage["data"].pop("results")
    stage["data"]["measurements"] = []
    ok, reason = is_comparative(run)
    assert not ok
    assert "no measured outcomes" in reason
    with pytest.raises(NotComparativeError):
        OesExporter().export(run, tmp_path / "oes.json")


def test_measurements_on_only_one_variant_are_refused(
    comparative_run: dict[str, Any], tmp_path: Path
) -> None:
    """A metric measured on the candidate but not the control cannot be compared."""
    run = copy.deepcopy(comparative_run)
    stage = _experiment_stage(run)
    stage["data"].pop("results")
    stage["data"]["measurements"] = [
        m for m in stage["data"]["measurements"] if m["variantKey"] == "candidate-a"
    ]
    ok, reason = is_comparative(run)
    assert not ok
    assert "2 or more variants" in reason
    with pytest.raises(NotComparativeError):
        OesExporter().export(run, tmp_path / "oes.json")


def test_measurements_with_null_values_are_refused(
    comparative_run: dict[str, Any], tmp_path: Path
) -> None:
    run = copy.deepcopy(comparative_run)
    stage = _experiment_stage(run)
    stage["data"].pop("results")
    for measurement in stage["data"]["measurements"]:
        measurement["value"] = None
    with pytest.raises(NotComparativeError):
        OesExporter().export(run, tmp_path / "oes.json")


def test_refusal_is_catchable_as_a_value_error(
    single_variant_run: dict[str, Any], tmp_path: Path
) -> None:
    """A CLI catching ``ValueError`` still handles the refusal cleanly."""
    assert issubclass(NotComparativeError, OesExportError)
    assert issubclass(OesExportError, ValueError)
    with pytest.raises(ValueError, match="refusing to export OES"):
        OesExporter().export(single_variant_run, tmp_path / "oes.json")


# -- adapter contract ---------------------------------------------------------


def test_exporter_satisfies_the_frozen_protocol() -> None:
    assert isinstance(OesExporter(), Exporter)
    assert OesExporter.name == "oes"
    assert OesExporter.kind == "export"


def test_detect_is_available_offline_and_explains_the_condition(cfg: Config) -> None:
    available, reason = OesExporter.detect(cfg)
    assert available
    assert "0.1.0" in reason
    assert "comparative" in reason
    assert "openexperiment.org" in reason


def test_detect_rejects_a_missing_schema_override(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADLC_OES_SCHEMA", str(tmp_path / "nope.json"))
    available, reason = OesExporter.detect(cfg)
    assert not available
    assert "does not exist" in reason
    assert "ADLC_OES_SCHEMA" in reason


def test_detect_never_raises(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str) -> None:
        raise RuntimeError("import system is unhappy")

    monkeypatch.setattr("importlib.util.find_spec", _boom)
    available, reason = OesExporter.detect(cfg)
    assert not available
    assert "import system is unhappy" in reason


def test_detect_makes_no_network_call(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    def _no_sockets(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("detect() must not open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    assert OesExporter.detect(cfg)[0]
