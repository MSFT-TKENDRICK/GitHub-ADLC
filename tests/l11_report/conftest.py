"""Shared fixtures for the L11 report leaf.

No credentials, no network, no subprocess: everything here is local files plus a
hand-built :class:`~adlc.config.Config`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The spine owns packaging; make ``src`` importable without requiring an install
# so this leaf's suite is runnable on its own.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from adlc.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path, profile="full")
