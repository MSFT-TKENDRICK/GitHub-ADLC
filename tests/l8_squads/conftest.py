"""L8 squad-gate pytest fixtures.

Two path fixes happen here, both before any test module is imported:

* ``src/`` of *this* worktree is prepended to ``sys.path``, so the suite
  exercises the code in this checkout rather than whatever editable install
  happens to be on the interpreter.
* this directory is prepended too, so ``import l8_fixtures`` is unambiguous
  regardless of what other test packages exist.

No credentials, no network, no subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SRC = REPO_ROOT / "src"

for entry in (str(HERE), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from l8_fixtures import SQUADS_YAML

from adlc.config import Config


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A bare repo root carrying a squads config at the vendored location."""
    adlc_dir = tmp_path / ".adlc"
    adlc_dir.mkdir(parents=True)
    (adlc_dir / "squads.yaml").write_text(SQUADS_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def cfg(repo: Path) -> Config:
    """`full` profile, so both squad gates are `required` and fail closed."""
    return Config(root=repo, profile="full")


@pytest.fixture
def run_dir(repo: Path) -> Path:
    d = repo / ".adlc" / "runs" / "2026-08-19-t3st"
    (d / "reviews").mkdir(parents=True)
    return d
