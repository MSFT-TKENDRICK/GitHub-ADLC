"""``adlc.stages.spec`` -- the GitHub Spec Kit bridge.

Previously exercised only via the conformance pipeline's fallback path (no
`specify` CLI on PATH), leaving the entire spec-kit-available branch (script
discovery, script invocation, JSON parsing, and the error-recovery path where
a script fails and the built-in template still gets written) untested. At
66% coverage. Adds direct unit coverage of `spec_kit_available`, `_script`,
`_run_script`, `_title_and_summary`, and `run_spec`'s every combination of
spec-kit-present/absent x script-succeeds/fails x existing-files-are-left-alone.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from adlc.config import Config
from adlc.runs import RunDir, new_run_id
from adlc.stages.spec import (
    _run_script,
    _script,
    _title_and_summary,
    run_spec,
    spec_kit_available,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path)


@pytest.fixture
def rd(cfg: Config) -> RunDir:
    rdir = RunDir(cfg, new_run_id())
    rdir.create(profile="minimal", brief_text="# Dark mode\n\nAdd a dark theme.\n")
    return rdir


class TestSpecKitAvailable:
    def test_false_when_specify_not_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        available, reason = spec_kit_available()
        assert available is False
        assert "built-in minimal spec templates" in reason

    def test_true_when_specify_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/specify" if name == "specify" else None)
        available, reason = spec_kit_available()
        assert available is True
        assert "specify" in reason


class TestScriptDiscovery:
    def test_finds_bash_script_when_present(self, tmp_path: Path) -> None:
        script_dir = tmp_path / ".specify" / "scripts" / "bash"
        script_dir.mkdir(parents=True)
        (script_dir / "create-new-feature.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        found = _script(tmp_path, "create-new-feature")
        assert found is not None
        assert found.name == "create-new-feature.sh"

    def test_finds_python_script_when_bash_absent(self, tmp_path: Path) -> None:
        script_dir = tmp_path / ".specify" / "scripts" / "python"
        script_dir.mkdir(parents=True)
        (script_dir / "setup-plan.py").write_text("print('{}')\n", encoding="utf-8")
        found = _script(tmp_path, "setup-plan")
        assert found is not None
        assert found.suffix == ".py"

    def test_returns_none_when_no_script_exists(self, tmp_path: Path) -> None:
        assert _script(tmp_path, "create-new-feature") is None


class TestRunScript:
    def test_python_script_success_parses_last_json_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "setup-plan.py"
        script.write_text("", encoding="utf-8")
        fake_proc = MagicMock(returncode=0, stdout='noise\n{"branch": "001-x"}', stderr="")
        monkeypatch.setattr(
            "adlc.stages.spec.subprocess.run", lambda cmd, **kw: fake_proc
        )
        result = _run_script(script, "--json", cwd=tmp_path)
        assert result == {"branch": "001-x"}

    def test_non_json_output_is_wrapped_as_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "setup-plan.py"
        script.write_text("", encoding="utf-8")
        fake_proc = MagicMock(returncode=0, stdout="plain text output", stderr="")
        monkeypatch.setattr(
            "adlc.stages.spec.subprocess.run", lambda cmd, **kw: fake_proc
        )
        result = _run_script(script, cwd=tmp_path)
        assert result == {"raw": "plain text output"}

    def test_nonzero_exit_raises_runtime_error_with_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "setup-plan.sh"
        script.write_text("", encoding="utf-8")
        fake_proc = MagicMock(returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(
            "adlc.stages.spec.subprocess.run", lambda cmd, **kw: fake_proc
        )
        with pytest.raises(RuntimeError, match="boom"):
            _run_script(script, cwd=tmp_path)

    def test_powershell_script_invoked_with_pwsh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "setup-plan.ps1"
        script.write_text("", encoding="utf-8")
        captured: dict[str, Any] = {}

        def _fake_run(cmd: list[str], **kw: Any) -> MagicMock:
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="{}", stderr="")

        monkeypatch.setattr("adlc.stages.spec.subprocess.run", _fake_run)
        _run_script(script, cwd=tmp_path)
        assert captured["cmd"][0] == "pwsh"

    def test_bash_script_invoked_with_bash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "setup-plan.sh"
        script.write_text("", encoding="utf-8")
        captured: dict[str, Any] = {}

        def _fake_run(cmd: list[str], **kw: Any) -> MagicMock:
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="{}", stderr="")

        monkeypatch.setattr("adlc.stages.spec.subprocess.run", _fake_run)
        _run_script(script, cwd=tmp_path)
        assert captured["cmd"][0] == "bash"


class TestTitleAndSummary:
    def test_extracts_h1_heading_as_title(self) -> None:
        title, summary = _title_and_summary("# Add dark mode\n\nUsers want a dark theme.\n")
        assert title == "Add dark mode"
        assert summary == "Users want a dark theme."

    def test_falls_back_to_first_line_when_no_heading(self) -> None:
        title, _summary = _title_and_summary("Just a plain first line\nSecond line\n")
        assert title == "Just a plain first line"

    def test_empty_brief_yields_default_title(self) -> None:
        title, summary = _title_and_summary("")
        assert title == "Untitled change"
        assert summary == "Untitled change"

    def test_summary_skips_heading_and_blockquote_lines(self) -> None:
        _title, summary = _title_and_summary("# Title\n> a quoted note\nReal summary line\n")
        assert summary == "Real summary line"


class TestRunSpecFallbackPath:
    def test_writes_spec_and_tasks_when_spec_kit_unavailable(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = run_spec(cfg, rd)
        assert result["usedSpecKit"] is False
        assert (rd.spec_dir / "spec.md").is_file()
        assert (rd.spec_dir / "tasks.md").is_file()

    def test_does_not_overwrite_an_existing_spec_md(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        rd.spec_dir.mkdir(parents=True, exist_ok=True)
        (rd.spec_dir / "spec.md").write_text("hand-written spec\n", encoding="utf-8")
        run_spec(cfg, rd)
        assert (rd.spec_dir / "spec.md").read_text(encoding="utf-8") == "hand-written spec\n"


class TestRunSpecWithSpecKit:
    def _install_specify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            shutil, "which", lambda name: "/usr/bin/specify" if name == "specify" else None
        )

    def test_runs_create_feature_and_setup_plan_scripts_on_success(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_specify(monkeypatch)
        script_dir = cfg.root / ".specify" / "scripts" / "bash"
        script_dir.mkdir(parents=True)
        (script_dir / "create-new-feature.sh").write_text("", encoding="utf-8")
        (script_dir / "setup-plan.sh").write_text("", encoding="utf-8")

        def _fake_run(cmd: list[str], **kw: Any) -> MagicMock:
            if "create-new-feature.sh" in cmd[1]:
                return MagicMock(returncode=0, stdout=json.dumps({"branch": "001-dark-mode"}), stderr="")
            return MagicMock(returncode=0, stdout=json.dumps({"plan": "ok"}), stderr="")

        monkeypatch.setattr("adlc.stages.spec.subprocess.run", _fake_run)

        result = run_spec(cfg, rd)
        assert result["usedSpecKit"] is True
        assert (rd.spec_dir / "spec.md").is_file()

    def test_falls_back_to_templates_when_create_feature_script_fails(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_specify(monkeypatch)
        script_dir = cfg.root / ".specify" / "scripts" / "bash"
        script_dir.mkdir(parents=True)
        (script_dir / "create-new-feature.sh").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            "adlc.stages.spec.subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=1, stdout="", stderr="script exploded"),
        )

        result = run_spec(cfg, rd)
        assert result["usedSpecKit"] is False
        assert (rd.spec_dir / "spec.md").is_file()

    def test_no_setup_plan_script_still_records_create_feature_output(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_specify(monkeypatch)
        script_dir = cfg.root / ".specify" / "scripts" / "bash"
        script_dir.mkdir(parents=True)
        (script_dir / "create-new-feature.sh").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            "adlc.stages.spec.subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=0, stdout=json.dumps({"branch": "001-x"}), stderr=""),
        )

        result = run_spec(cfg, rd)
        assert result["usedSpecKit"] is True
