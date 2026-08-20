"""Test package for workstream L1 — Copilot agent runners.

Makes ``tests/l1_copilot`` a package (so sibling modules can share helpers) and
puts ``src/`` on ``sys.path``. The spine is not necessarily pip-installed while
leaves land in parallel, and this module is imported before ``conftest``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
