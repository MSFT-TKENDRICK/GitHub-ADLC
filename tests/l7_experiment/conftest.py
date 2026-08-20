"""Shared fixtures for the L7 (flags / experiment / OES) test suite.

Everything here runs with **no credentials**: no LaunchDarkly key, no network.
The OES schema used as the validation oracle is the vendored copy in ``data/``,
downloaded from ``openexperiment.org``, deliberately loaded from disk rather than
from the exporter module so the tests are an independent check on it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.runs import RunDir

DATA = Path(__file__).parent / "data"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The published OES schema, vendored for offline testing.
OES_SCHEMA_FILE = DATA / "openexperiment-0.1.0.schema.json"

#: The frozen ``adlc-run/v1`` schema owned by the spine.
ADLC_RUN_SCHEMA_FILE = REPO_ROOT / "schemas" / "adlc-run.schema.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def oes_schema() -> dict[str, Any]:
    """The real published OES v0.1.0 schema, read from the vendored copy."""
    return _read_json(OES_SCHEMA_FILE)


@pytest.fixture(scope="session")
def adlc_run_schema() -> dict[str, Any]:
    return _read_json(ADLC_RUN_SCHEMA_FILE)


@pytest.fixture
def comparative_run() -> dict[str, Any]:
    """A golden ``adlc-run/v1`` document for a genuinely comparative run."""
    return _read_json(DATA / "comparative-run.json")


@pytest.fixture
def single_variant_run(comparative_run: dict[str, Any]) -> dict[str, Any]:
    """The same run reduced to one candidate — the common, non-experiment case."""
    run = copy.deepcopy(comparative_run)
    run["variants"] = [v for v in run["variants"] if v["key"] == "candidate-a"]
    run["stages"] = [s for s in run["stages"] if s["stage"] != "experiment"]
    run["experimentRef"] = None
    return run


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A ``Config`` rooted at an empty temporary repository."""
    (tmp_path / ".adlc" / "runs").mkdir(parents=True)
    return Config.load(tmp_path)


@pytest.fixture
def rd(cfg: Config) -> RunDir:
    """A ``RunDir`` for the golden run id, with the standard sub-directories."""
    run_dir = RunDir(cfg, "2026-08-19-a1b2")
    for directory in (run_dir.stages_dir, run_dir.enrichment_dir, run_dir.evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return run_dir


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Guarantee the whole suite runs credential-free, reproducibly and in a sandbox.

    The working directory is moved into ``tmp_path`` because both flag providers
    fall back to a **cwd-relative** ``.adlc/runs/<id>/`` when no explicit path is
    given; without this a test would write into the checkout.
    """
    for name in (
        "LAUNCHDARKLY_SDK_KEY",
        "LAUNCHDARKLY_PROJECT",
        "LAUNCHDARKLY_ENVIRONMENT",
        "ADLC_OES_SCHEMA",
        "ADLC_RUN_DIR",
        "SOURCE_DATE_EPOCH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
