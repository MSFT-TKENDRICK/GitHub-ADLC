"""Shared fixtures for the L6 evidence-collector tests.

Everything here must work with **no tools installed and no credentials**:
the collectors are optional adapters, and the spine's Playwright collector
alone satisfies the credential-free conformance suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
REPO_ROOT = TESTS_DIR.parents[1]
SRC = REPO_ROOT / "src"

if str(SRC) not in sys.path:  # the package is importable without an editable install
    sys.path.insert(0, str(SRC))

from adlc.config import Config


def load_fixture(name: str) -> Any:
    """Read a checked-in raw tool output fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def lhr() -> dict[str, Any]:
    return load_fixture("lighthouse-lhr.json")


@pytest.fixture
def k6_summary() -> dict[str, Any]:
    return load_fixture("k6-summary.json")


@pytest.fixture
def axe_results() -> dict[str, Any]:
    return load_fixture("axe-results.json")


@pytest.fixture
def benchmarks_doc() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "benchmarks.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A plan §4.1 run directory carrying the fixture budgets."""
    directory = tmp_path / ".adlc" / "runs" / "2026-08-19-a1b2"
    (directory / "enrichment").mkdir(parents=True)
    (directory / "enrichment" / "benchmarks.yaml").write_text(
        (FIXTURES / "benchmarks.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return directory


@pytest.fixture
def evidence_out(run_dir: Path) -> Path:
    out = run_dir / "evidence" / "candidate-a"
    out.mkdir(parents=True)
    return out


@pytest.fixture
def run() -> dict[str, Any]:
    return {"schemaVersion": "adlc-run/v1", "runId": "2026-08-19-a1b2", "profile": "minimal"}


@pytest.fixture
def no_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """An environment where lhci, k6 and node are provably absent.

    Makes the ``detect() -> (False, reason)`` assertions deterministic on
    developer machines that happen to have the real tools installed.
    """
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    isolated = tmp_path / "isolated-cwd"
    isolated.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.delenv("NODE_PATH", raising=False)
    monkeypatch.delenv("ADLC_TARGET_URL", raising=False)
    monkeypatch.chdir(isolated)
    return Config(root=isolated)
