"""Shared fixtures for the L3 eval-adapter tests.

Everything here is designed so the suite passes on a machine with **no credentials** —
and, just as importantly, on a developer machine that happens to have some. Judge keys and
tool binaries are explicitly removed from the environment rather than assumed absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.evals import assert_ as assert_mod
from adlc.adapters.evals.azure import AZURE_CREDENTIAL_GROUPS
from adlc.adapters.evals.promptfoo import PROMPTFOO_CREDENTIAL_GROUPS
from adlc.config import Config

FIXTURES = Path(__file__).parent / "fixtures"

#: Every credential name any L3 backend looks at, plus the adapter's own overrides.
CREDENTIAL_ENV_NAMES: tuple[str, ...] = tuple(
    sorted(
        {
            name
            for groups in (
                assert_mod.JUDGE_CREDENTIAL_GROUPS,
                assert_mod.ASSERT_CREDENTIAL_GROUPS,
                PROMPTFOO_CREDENTIAL_GROUPS,
                AZURE_CREDENTIAL_GROUPS,
            )
            for group in groups
            for name in group
        }
        | {
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
            "ADLC_ASSERT_CMD",
            "ADLC_ASSERT_ARGS",
            "ADLC_ASSERT_MODEL",
            "ADLC_ASSERT_TIMEOUT",
            "ADLC_PROMPTFOO_CMD",
            "ADLC_PROMPTFOO_ARGS",
            "ADLC_PROMPTFOO_NPX",
            "ADLC_AZURE_CMD",
            "ADLC_PROFILE",
        }
    )
)


@pytest.fixture
def credential_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee a credential-free environment, whatever the host machine has."""
    for name in CREDENTIAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_tools(credential_free: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Also guarantee no eval binary on PATH and no eval SDK importable."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(assert_mod, "find_spec", lambda _name: None)


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any subprocess launch an immediate test failure.

    ``detect()`` is contractually cheap: no network, no subprocess that can hang.
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"detect() must not spawn a subprocess (called with {args!r})")

    monkeypatch.setattr(assert_mod.subprocess, "run", _boom)
    monkeypatch.setattr(assert_mod.subprocess, "Popen", _boom)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A Config rooted at an empty temp repo, so run dirs land under tmp_path."""
    (tmp_path / ".adlc").mkdir(parents=True, exist_ok=True)
    return Config.load(tmp_path)


@pytest.fixture
def rubric() -> dict[str, Any]:
    """A rubric matching ``schemas/rubric.schema.json``, weights 2 / 1 / 1."""
    return {
        "id": "dark-mode",
        "threshold": 0.7,
        "criteria": [
            {
                "id": "R-contrast-01",
                "statement": "Dark mode keeps text contrast at or above 4.5:1.",
                "weight": 2,
                "kind": "llm-rubric",
            },
            {
                "id": "R-perf-01",
                "statement": "Switching theme completes within 250ms.",
                "weight": 1,
                "kind": "measurement",
            },
            {
                "id": "R-a11y-01",
                "statement": "Every interactive control keeps a visible focus ring.",
                "weight": 1,
                "kind": "llm-rubric",
            },
        ],
    }


@pytest.fixture
def run_doc() -> dict[str, Any]:
    return {
        "schemaVersion": "adlc-run/v1",
        "runId": "2026-08-19-a1b2",
        "profile": "minimal",
        "status": "built",
        "stages": [],
    }


@pytest.fixture
def run_dir(cfg: Config, run_doc: dict[str, Any]) -> Path:
    """A materialised run directory with a spec, as `adlc spec` would leave it."""
    rdir = cfg.run_dir(run_doc["runId"])
    (rdir / "spec").mkdir(parents=True, exist_ok=True)
    (rdir / "spec" / "spec.md").write_text(
        "# Dark mode\n\nUsers can switch to a dark theme without losing legibility.\n",
        encoding="utf-8",
    )
    (rdir / "evals").mkdir(parents=True, exist_ok=True)
    return rdir


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
