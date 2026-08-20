"""L8 squad-gate pytest fixtures.

No credentials, no network, no subprocess.

**Import binding.** This repository is checked out as several parallel git
worktrees that share one interpreter, and ``pip install -e .`` in any of them
re-points the editable install for all of them. That means ``import adlc`` can
silently resolve to a *sibling worktree's* source tree, and the suite would then
be testing code that is not in this checkout.

So this conftest binds ``adlc`` to this worktree explicitly: it prepends this
worktree's ``src/`` and, if ``adlc`` was already imported from somewhere else
(because another test package was collected first), it drops those modules so
the next import resolves here. A warning is emitted when that happens, because
it means the environment is misconfigured even though this suite has recovered.

The durable fix belongs to the spine -- a repo-root ``conftest.py`` doing the
same prepend would fix every workstream at once -- so this only repairs the
binding for the L8 modules and reports the condition.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _bind_adlc_to_this_worktree() -> None:
    module = sys.modules.get("adlc")
    if module is None:
        return
    origin = getattr(module, "__file__", None)
    if origin and Path(origin).resolve().is_relative_to(SRC):
        return
    warnings.warn(
        f"`adlc` was already imported from {origin!r}, which is outside this worktree "
        f"({SRC}). Another checkout owns the editable install. Re-binding for the L8 "
        f"tests; run pytest with PYTHONPATH={SRC} to fix the whole session.",
        RuntimeWarning,
        stacklevel=2,
    )
    for name in [n for n in sys.modules if n == "adlc" or n.startswith("adlc.")]:
        del sys.modules[name]


_bind_adlc_to_this_worktree()

from adlc.config import Config

from .l8_fixtures import SQUADS_YAML


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
    """A run directory laid out the way `RunDir.create` lays one out."""
    d = repo / ".adlc" / "runs" / "2026-08-19-t3st"
    (d / "reviews").mkdir(parents=True)
    (d / "gates").mkdir(parents=True)
    (d / "evidence").mkdir(parents=True)
    return d
