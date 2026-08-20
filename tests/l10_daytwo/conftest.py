"""Shared fixtures for the L10 day-2 tests.

Every test in this package must pass with **no credentials**. The
``no_azure_env`` autouse fixture strips every environment variable the three
adapters look at, so an engineer who happens to have Azure vars exported
locally gets the same result CI does.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from adlc.adapters.daytwo.foundry import CREDENTIAL_ENV_GROUPS, PROJECT_ENDPOINT_ENVS
from adlc.adapters.daytwo.sre_agent import PAYLOAD_ENV_VARS
from adlc.adapters.telemetry.appinsights import CONNECTION_STRING_ENV
from adlc.config import DEFAULT_CONFIG, Config

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples" / "azure"

#: Everything that could make an adapter believe it is available.
_ALL_ENV_VARS: tuple[str, ...] = (
    *PAYLOAD_ENV_VARS,
    *PROJECT_ENDPOINT_ENVS,
    *{var for group in CREDENTIAL_ENV_GROUPS for var in group},
    CONNECTION_STRING_ENV,
    "GITHUB_EVENT_NAME",
    "GITHUB_SHA",
    "ADLC_PROFILE",
)


@pytest.fixture(autouse=True)
def no_azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee a credential-free environment for every test in this package."""
    for var in _ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A Config rooted in a throwaway directory, so nothing touches the repo."""
    return Config(root=tmp_path, profile="minimal", limits={"maxParallel": 4})


@pytest.fixture
def repo_cfg(tmp_path: Path) -> Config:
    """A Config rooted in a real (empty) git repo.

    ``RunDir.create`` records ``baseSha`` via git, so hotfix tests need an
    actual repository rather than a bare directory. Kept local and disposable:
    no network, no credentials, and never the ADLC repo itself.
    """
    root = tmp_path / "repo"
    root.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
           "GIT_CONFIG_SYSTEM": str(tmp_path / "gitconfig-system")}
    (root / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "adlc@example.invalid"],
        ["config", "user.name", "ADLC Test"],
        ["add", "-A"],
        ["commit", "-qm", "initial"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, env=env,
                       capture_output=True, text=True)

    return Config(root=root, profile="minimal", limits={"maxParallel": 2},
                  raw=copy.deepcopy(DEFAULT_CONFIG))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
