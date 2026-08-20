"""Shared fixtures for the credential-free conformance suite.

Every test here must pass with **no credentials, no network and no optional
tooling installed**. That is the whole point: a green conformance run proves the
*framework*, not an LLM.

Scoping note: the full pipeline takes ~20s, so the read-only assertions share a
single module-scoped run. Tests that mutate state (re-runs, negative cases) get
a fresh function-scoped repo.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from adlc.config import Config

BRIEF = """# Add dark mode to the settings page

## Problem

Users in low-light environments report eye strain because the settings page only
supports a light theme.

## Desired outcome

A user can switch the settings page between light and dark themes, so that the
interface is comfortable in any lighting condition.

## Acceptance criteria

- **US1-AC1**: A theme toggle is reachable from the settings page header.
- **US1-AC2**: Selecting a theme applies it immediately without a page reload.
- **US1-AC3**: The chosen theme persists and is covered by an automated test.

## Constraints and scope

- Largest Contentful Paint must stay under 2500 ms.
- Out of scope: theming any other page.

## Audience

Existing end users and administrators.
"""

CONFIG_YAML = """version: 1
profile: minimal
commands:
  test: "python -c \\"print(1)\\""
limits:
  maxParallel: 4
qualify:
  minScore: 50
"""


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


def make_consumer_repo(root: Path) -> Path:
    """Build a clean git repository standing in for a real consumer."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "adlc@example.invalid", cwd=root)
    _git("config", "user.name", "ADLC Conformance", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)

    (root / "README.md").write_text("# Consumer\n\nA repository under ADLC.\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "# Conventions\n\n- Prefer small, focused modules.\n- Every change ships a test.\n",
        encoding="utf-8",
    )
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_text(
        '"""Entry point."""\n\n\ndef mount() -> str:\n    return "app"\n', encoding="utf-8"
    )
    (root / "brief.md").write_text(BRIEF, encoding="utf-8")

    adlc_dir = root / ".adlc"
    adlc_dir.mkdir(exist_ok=True)
    (adlc_dir / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")

    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "Initial commit", cwd=root)
    return root


def bind_env(root: Path) -> Config:
    """Point the process at ``root`` with a credential-free environment."""
    os.chdir(root)
    os.environ["ADLC_ROOT"] = str(root)
    os.environ["ADLC_TEST_COMMAND"] = 'python -c "print(1)"'
    for name in (
        "GITHUB_TOKEN", "GH_TOKEN", "LAUNCHDARKLY_SDK_KEY",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
    ):
        os.environ.pop(name, None)
    return Config.load(root)


@pytest.fixture
def consumer_repo(tmp_path: Path) -> Path:
    """A fresh consumer repo for tests that mutate state."""
    return make_consumer_repo(tmp_path / "consumer")


@pytest.fixture
def cfg(consumer_repo: Path) -> Iterator[Config]:
    previous = Path.cwd()
    try:
        yield bind_env(consumer_repo)
    finally:
        os.chdir(previous)


@pytest.fixture
def brief_file(consumer_repo: Path) -> Path:
    return consumer_repo / "brief.md"


@pytest.fixture(scope="module")
def completed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Config, object]]:
    """One fully-driven pipeline shared by the read-only assertions."""
    from tests.conformance.driver import drive

    previous = Path.cwd()
    root = make_consumer_repo(tmp_path_factory.mktemp("shared") / "consumer")
    try:
        config = bind_env(root)
        yield config, drive(config, root / "brief.md")
    finally:
        os.chdir(previous)


@pytest.fixture
def repo_factory(tmp_path: Path) -> Callable[..., Path]:
    counter = {"n": 0}

    def make(name: str = "repo") -> Path:
        counter["n"] += 1
        return make_consumer_repo(tmp_path / f"{name}-{counter['n']}")

    return make
