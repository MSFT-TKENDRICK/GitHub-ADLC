"""``detect()`` must be cheap, non-raising and specific -- with no tools installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from adlc.adapters.evidence.axe import AxeCollector
from adlc.adapters.evidence.k6 import K6Collector
from adlc.adapters.evidence.lighthouse import LighthouseCollector

COLLECTORS = (LighthouseCollector, K6Collector, AxeCollector)


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.name)
def test_detect_reports_unavailable_with_a_specific_reason(collector, no_tools) -> None:
    available, reason = collector.detect(no_tools)

    assert available is False
    assert isinstance(reason, str)
    assert reason.strip(), "reason is surfaced verbatim in capabilities.json"
    assert collector.name in reason.lower() or "node" in reason.lower()
    # Specific enough to act on: names the missing thing and how to get it.
    assert any(token in reason for token in ("not on PATH", "not installed"))
    assert any(token in reason for token in ("install", "http"))


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.name)
def test_detect_is_a_bool_string_pair(collector, no_tools) -> None:
    result = collector.detect(no_tools)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.name)
def test_detect_spawns_no_subprocess(collector, no_tools, monkeypatch) -> None:
    """No subprocess that can hang -- CONTRIBUTING.md rule 5."""

    def explode(*args, **kwargs):  # pragma: no cover - only runs on a violation
        raise AssertionError("detect() must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)

    assert collector.detect(no_tools)[0] is False


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.name)
def test_detect_does_not_raise_on_a_broken_config(collector, monkeypatch) -> None:
    class Hostile:
        @property
        def root(self):
            raise RuntimeError("config is broken")

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    available, reason = collector.detect(Hostile())
    assert available is False
    assert reason


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.name)
def test_detect_survives_a_nonexistent_root(collector, tmp_path, monkeypatch) -> None:
    from adlc.config import Config

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    available, reason = collector.detect(Config(root=tmp_path / "does" / "not" / "exist"))
    assert available is False
    assert reason


def test_lighthouse_detect_available_when_lhci_is_on_path(monkeypatch, no_tools) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.lighthouse.find_executable",
        lambda name, cfg=None, start=None: "/usr/local/bin/lhci",
    )
    available, reason = LighthouseCollector.detect(no_tools)
    assert available is True
    assert "lhci" in reason


def test_k6_detect_available_when_k6_is_on_path(monkeypatch, no_tools) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.k6.find_executable",
        lambda name, cfg=None, start=None: "/usr/local/bin/k6",
    )
    available, reason = K6Collector.detect(no_tools)
    assert available is True
    assert "k6" in reason


def test_axe_detect_needs_node_and_the_package(monkeypatch, no_tools, tmp_path) -> None:
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_executable",
        lambda name, cfg=None, start=None: f"/usr/local/bin/{name}",
    )
    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_node_package",
        lambda pkg, cfg=None, start=None: None,
    )
    available, reason = AxeCollector.detect(no_tools)
    assert available is False
    assert "@axe-core/playwright" in reason
    assert "playwright" in reason

    monkeypatch.setattr(
        "adlc.adapters.evidence.axe.find_node_package",
        lambda pkg, cfg=None, start=None: tmp_path / "node_modules",
    )
    available, reason = AxeCollector.detect(no_tools)
    assert available is True


def test_axe_detect_names_node_when_node_is_missing(no_tools) -> None:
    available, reason = AxeCollector.detect(no_tools)
    assert available is False
    assert "node" in reason.lower()


def test_find_executable_finds_a_local_node_modules_bin(tmp_path, monkeypatch) -> None:
    from adlc.adapters.evidence.lighthouse import find_executable
    from adlc.config import Config

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    bin_dir = tmp_path / "repo" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "lhci").write_text("#!/bin/sh\n", encoding="utf-8")

    found = find_executable("lhci", Config(root=tmp_path / "repo"))
    assert found is not None
    assert Path(found).name == "lhci"


def test_find_node_package_uses_node_path(tmp_path, monkeypatch) -> None:
    from adlc.adapters.evidence.lighthouse import find_node_package

    modules = tmp_path / "global" / "node_modules"
    (modules / "@axe-core" / "playwright").mkdir(parents=True)
    (modules / "@axe-core" / "playwright" / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NODE_PATH", str(modules))

    assert find_node_package("@axe-core/playwright") == modules
    assert find_node_package("@axe-core/nonexistent") is None
