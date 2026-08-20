"""Shared fixtures for the L9 enrichment leaf.

These tests must pass with **no credentials and no network**, so everything here
is local files plus a hand-built :class:`~adlc.config.Config`.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# The spine owns packaging; make ``src`` importable without requiring an install
# so this leaf's suite is runnable on its own.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def spec_text() -> str:
    return (FIXTURES / "spec.md").read_text(encoding="utf-8")


@pytest.fixture()
def cfg(tmp_path: Path):
    from adlc.config import Config

    return Config(root=tmp_path, profile="minimal", raw={"version": 1, "profile": "minimal"})


@pytest.fixture()
def skip_cfg(tmp_path: Path):
    """A config that switches every enrichment facet off."""
    from adlc.config import Config

    return Config(
        root=tmp_path,
        profile="minimal",
        raw={"enrich": {"skip": ["diagrams", "personas", "wireframe"]}},
    )


@pytest.fixture()
def run_dir(tmp_path: Path, spec_text: str) -> Path:
    """A run directory shaped like ``.adlc/runs/<id>/`` after the spec stage."""
    run = tmp_path / "runs" / "2026-08-19-l9t"
    spec = run / "spec"
    (spec / "contracts").mkdir(parents=True)
    (spec / "spec.md").write_text(spec_text, encoding="utf-8")
    shutil.copy(FIXTURES / "plan.md", spec / "plan.md")
    shutil.copy(FIXTURES / "data-model.md", spec / "data-model.md")
    shutil.copy(
        FIXTURES / "contracts" / "preferences.yaml", spec / "contracts" / "preferences.yaml"
    )
    return run


@pytest.fixture()
def bare_run_dir(tmp_path: Path) -> Path:
    """A run directory with no spec at all — the degenerate input."""
    run = tmp_path / "runs" / "empty"
    run.mkdir(parents=True)
    return run
