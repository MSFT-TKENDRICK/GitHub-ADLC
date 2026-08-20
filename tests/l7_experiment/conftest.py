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


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Guarantee the whole suite runs credential-free and reproducibly.

    ``ADLC_RUN_DIR`` is pointed at a temporary directory rather than unset, so a
    provider that resolves its own output path can never write into the checkout.
    """
    for name in (
        "LAUNCHDARKLY_SDK_KEY",
        "LAUNCHDARKLY_PROJECT",
        "LAUNCHDARKLY_ENVIRONMENT",
        "ADLC_OES_SCHEMA",
        "SOURCE_DATE_EPOCH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADLC_RUN_DIR", str(tmp_path / "adlc-run-dir"))
