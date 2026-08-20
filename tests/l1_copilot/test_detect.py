"""``detect()`` must be cheap, must never raise, and must explain *why*.

Nothing in this module needs a credential, a network call or an installed
backend — which is the whole point: with none of them present, all three L1
runners report ``(False, <specific reason>)`` and the spine falls back to its
``fake`` runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adlc.adapters.agents import agent_task, copilot_sdk, gh_aw
from adlc.adapters.agents.agent_task import AgentTaskRunner
from adlc.adapters.agents.copilot_sdk import CopilotSdkRunner
from adlc.adapters.agents.gh_aw import GhAwRunner
from adlc.config import Config

RUNNERS = (CopilotSdkRunner, AgentTaskRunner, GhAwRunner)


@pytest.mark.parametrize("runner", RUNNERS)
def test_runner_declares_the_adapter_contract(runner: type) -> None:
    assert isinstance(runner.name, str) and runner.name
    assert runner.kind == "agents"
    assert callable(runner.detect)
    assert callable(runner().run_task)


@pytest.mark.parametrize("runner", RUNNERS)
def test_detect_is_false_and_specific_without_credentials(runner: type, cfg: Config) -> None:
    available, reason = runner.detect(cfg)
    assert available is False
    assert isinstance(reason, str)
    assert len(reason) > 20, "reason is surfaced verbatim in capabilities.json"
    assert runner.name.split("-")[0] in reason.lower() or "gh" in reason.lower()


@pytest.mark.parametrize("runner", RUNNERS)
def test_detect_never_raises_on_a_hostile_config(runner: type) -> None:
    class Exploding:
        root = Path("/definitely/not/here")

        def __getattr__(self, item: str) -> object:
            raise RuntimeError(f"boom: {item}")

    available, reason = runner.detect(Exploding())  # type: ignore[arg-type]
    assert available is False
    assert isinstance(reason, str) and reason


# ---------------------------------------------------------------------------
# copilot-sdk
# ---------------------------------------------------------------------------


def test_copilot_sdk_reports_the_missing_package(cfg: Config) -> None:
    available, reason = CopilotSdkRunner.detect(cfg)
    assert not available
    assert "not installed" in reason
    assert "github-copilot-sdk" in reason


def test_copilot_sdk_reports_a_missing_token_when_the_sdk_is_present(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "copilot"
    package.mkdir()
    (package / "session.py").touch()
    monkeypatch.setattr(
        copilot_sdk, "find_spec", lambda _name: _FakeSpec([str(package)])
    )

    available, reason = CopilotSdkRunner.detect(cfg)

    assert not available
    assert "no credential" in reason
    assert "GH_TOKEN" in reason


def test_copilot_sdk_rejects_an_impostor_copilot_module(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "copilot"
    package.mkdir()  # no session.py — not the GitHub SDK
    monkeypatch.setattr(
        copilot_sdk, "find_spec", lambda _name: _FakeSpec([str(package)])
    )
    monkeypatch.setenv("GH_TOKEN", "x")

    available, reason = CopilotSdkRunner.detect(cfg)

    assert not available
    assert "not the GitHub" in reason


def test_copilot_sdk_is_available_with_sdk_and_token(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "copilot"
    package.mkdir()
    (package / "session.py").touch()
    monkeypatch.setattr(
        copilot_sdk, "find_spec", lambda _name: _FakeSpec([str(package)])
    )
    monkeypatch.setenv("COPILOT_CLI_TOKEN", "x")

    available, reason = CopilotSdkRunner.detect(cfg)

    assert available
    assert "COPILOT_CLI_TOKEN" in reason


def test_copilot_sdk_survives_a_raising_find_spec(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_name: str) -> object:
        raise ValueError("broken meta path finder")

    monkeypatch.setattr(copilot_sdk, "find_spec", boom)
    available, reason = CopilotSdkRunner.detect(cfg)
    assert not available
    assert "not installed" in reason


class _FakeSpec:
    def __init__(self, locations: list[str]) -> None:
        self.submodule_search_locations = locations


# ---------------------------------------------------------------------------
# agent-task
# ---------------------------------------------------------------------------


def test_agent_task_reports_the_missing_token(cfg: Config) -> None:
    available, reason = AgentTaskRunner.detect(cfg)
    assert not available
    assert "$GITHUB_TOKEN" in reason


def test_agent_task_reports_an_unresolvable_repository(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    available, reason = AgentTaskRunner.detect(cfg)
    assert not available
    assert "owner/repo" in reason


def test_agent_task_rejects_a_malformed_repository_override(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("ADLC_REPO", "not-a-slug")
    available, reason = AgentTaskRunner.detect(cfg)
    assert not available
    assert "owner/repo form" in reason


def test_agent_task_is_available_and_says_it_is_preview(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/demo")
    available, reason = AgentTaskRunner.detect(cfg)
    assert available
    assert "public preview" in reason
    assert "octo/demo" in reason


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/octo/demo.git", "octo/demo"),
        ("https://github.com/octo/demo", "octo/demo"),
        ("git@github.com:octo/demo.git", "octo/demo"),
        ("ssh://git@github.com/octo/demo.git", "octo/demo"),
        ("https://gitlab.com/octo/demo.git", None),
    ],
)
def test_repo_slug_is_parsed_from_a_remote_url(url: str, expected: str | None) -> None:
    assert agent_task._slug_from_url(url) == expected


def test_repo_slug_is_read_from_the_git_config(repo: Path, repo_cfg: Config) -> None:
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + '\n[remote "origin"]\n\turl = git@github.com:octo/demo.git\n',
        encoding="utf-8",
    )
    slug, source = agent_task.resolve_repo_slug(repo_cfg)
    assert slug == "octo/demo"
    assert "origin" in source


def test_repo_slug_reports_a_missing_remote(repo: Path, repo_cfg: Config) -> None:
    slug, source = agent_task.resolve_repo_slug(repo_cfg)
    assert slug is None
    assert "origin" in source


# ---------------------------------------------------------------------------
# gh-aw
# ---------------------------------------------------------------------------


@pytest.fixture
def gh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every gh discovery path at an empty throwaway directory."""
    home = tmp_path / "gh-home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("GH_CONFIG_DIR", str(home / "config"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    monkeypatch.setenv("AppData", str(home / "roaming"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def test_gh_aw_reports_a_missing_cli(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: None)
    available, reason = GhAwRunner.detect(cfg)
    assert not available
    assert "gh CLI not on PATH" in reason


def test_gh_aw_reports_a_missing_extension(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: "/usr/bin/gh")
    available, reason = GhAwRunner.detect(cfg)
    assert not available
    assert "gh-aw' is not installed" in reason
    assert "gh extension install" in reason


def test_gh_aw_reports_missing_authentication(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: "/usr/bin/gh")
    (gh_home / "config" / "extensions" / "gh-aw").mkdir(parents=True)
    available, reason = GhAwRunner.detect(cfg)
    assert not available
    assert "not authenticated" in reason
    assert "gh auth login" in reason


def test_gh_aw_reports_an_unresolvable_repository(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: "/usr/bin/gh")
    (gh_home / "config" / "extensions" / "gh-aw").mkdir(parents=True)
    monkeypatch.setenv("GH_TOKEN", "x")
    available, reason = GhAwRunner.detect(cfg)
    assert not available
    assert "owner/repo" in reason


def test_gh_aw_is_available_when_everything_is_present(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: "/usr/bin/gh")
    (gh_home / "config" / "extensions" / "gh-aw").mkdir(parents=True)
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    available, reason = GhAwRunner.detect(cfg)
    assert available
    assert "octo/demo" in reason


def test_gh_aw_accepts_hosts_yml_instead_of_a_token(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: "/usr/bin/gh")
    (gh_home / "config" / "extensions" / "gh-aw").mkdir(parents=True)
    (gh_home / "config" / "hosts.yml").write_text("github.com:\n  user: octo\n", encoding="utf-8")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    available, reason = GhAwRunner.detect(cfg)
    assert available
    assert "hosts.yml" in reason


def test_gh_aw_detect_does_not_shell_out(
    cfg: Config, gh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden rules forbid a subprocess in ``detect()`` (CONTRIBUTING.md §5)."""
    import subprocess

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("detect() must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(gh_aw.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")

    assert GhAwRunner.detect(cfg)[0] is False  # extension is absent, and nothing was executed
