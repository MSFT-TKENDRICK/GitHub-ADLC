"""Shared fixtures for the L1 Copilot runner tests.

Every test here runs with **no credentials** and touches no network: the
adapters' unavailable paths are asserted directly, and the available paths are
exercised with mocks plus a real, local, throwaway git repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adlc.config import Config
from adlc.ports import TaskNode

#: Every credential the L1 adapters look at. Cleared for the whole package so a
#: developer's real environment can never make a test pass or fail by accident.
CREDENTIAL_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "COPILOT_CLI_TOKEN",
    "GITHUB_REPOSITORY",
    "ADLC_REPO",
    "ADLC_PATCH_DIR",
    "ADLC_BASE_REF",
    "ADLC_GHAW_WORKFLOW",
    "ADLC_GHAW_MODE",
    "ADLC_GHAW_REF",
    "ADLC_COPILOT_MODEL",
    "ADLC_AGENT_TASK_MODEL",
    "ADLC_GIT_REMOTE",
)


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny real git repository standing in for an isolated task worktree."""
    root = tmp_path / "worktree"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    # Written directly rather than via three `git config` calls: process spawn
    # dominates this fixture's cost and it runs for every patch test.
    config = root / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "[user]\n\temail = adlc@example.invalid\n\tname = ADLC Test\n"
        + "[commit]\n\tgpgsign = false\n"
        + "[gc]\n\tauto = 0\n",
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("export const mount = () => {};\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def base_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A config rooted at a plain directory — no git repo, so it is cheap.

    Use :func:`repo_cfg` when the test needs the config to point at the real
    throwaway repository.
    """
    root = tmp_path / "plain-root"
    root.mkdir(exist_ok=True)
    return Config(root=root, limits={"taskTimeoutSeconds": 30, "pollSeconds": 1})


@pytest.fixture
def repo_cfg(repo: Path) -> Config:
    return Config(root=repo, limits={"taskTimeoutSeconds": 30, "pollSeconds": 1})


@pytest.fixture
def node() -> TaskNode:
    return {
        "id": "T001",
        "title": "Add a theme toggle",
        "kind": "implement",
        "level": 0,
        "writeSet": ["src/app.ts", "src/theme.ts"],
        "acceptance": ["US1-AC1"],
        "context": {
            "refs": [{"path": "src/app.ts", "symbols": ["mount"], "excerpt": "export const"}],
            "commands": {"test": "npm test"},
            "doNotTouch": [".github/**"],
        },
    }
