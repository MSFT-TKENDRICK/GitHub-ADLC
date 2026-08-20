"""Shared fixtures for the L10 day-2 tests.

Every test in this package must pass with **no credentials**. The
``no_azure_env`` autouse fixture strips every environment variable the three
adapters look at, so an engineer who happens to have Azure vars exported
locally gets the same result CI does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adlc.adapters.daytwo.foundry import CREDENTIAL_ENV_GROUPS, PROJECT_ENDPOINT_ENVS
from adlc.adapters.daytwo.sre_agent import PAYLOAD_ENV_VARS
from adlc.adapters.telemetry.appinsights import CONNECTION_STRING_ENV
from adlc.config import Config

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


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
