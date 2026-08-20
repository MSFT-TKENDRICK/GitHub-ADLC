"""Shared fixtures for the L2 governance suite.

Everything in this package must pass:
  * with **no credentials**, and
  * with **neither** Microsoft Agent Framework nor the Agent Governance Toolkit
    installed (the degradation path is the thing under test), and
  * when they *are* installed (the tests never assert absence as a precondition).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_POLICY = REPO_ROOT / "templates" / ".adlc" / "policy.yaml"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A Config rooted at an empty temp dir with a vendored policy."""
    adlc_dir = tmp_path / ".adlc"
    adlc_dir.mkdir(parents=True, exist_ok=True)
    (adlc_dir / "policy.yaml").write_text(
        TEMPLATE_POLICY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return Config(root=tmp_path, profile="full")


@pytest.fixture
def bare_cfg(tmp_path: Path) -> Config:
    """A Config with no policy file at all."""
    return Config(root=tmp_path, profile="minimal")


@pytest.fixture
def no_optional_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make MAF and AGT look uninstalled, whatever is on this machine."""
    from adlc.maf import middleware

    hidden = {"agent_framework", "agentmesh", "agent_control_specification", "agent_os"}
    monkeypatch.setattr(
        middleware,
        "_module_present",
        lambda name: name not in hidden and _real_module_present(name),
    )


@pytest.fixture
def no_agt_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAF present, AGT missing — the half-installed case."""
    from adlc.maf import middleware

    hidden = {"agentmesh", "agent_control_specification", "agent_os"}
    monkeypatch.setattr(
        middleware,
        "_module_present",
        lambda name: name not in hidden
        and (name == "agent_framework" or _real_module_present(name)),
    )


def _real_module_present(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


class FakeFunction:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeFunctionInvocationContext:
    """Stand-in for ``agent_framework.FunctionInvocationContext``.

    Mirrors the attributes the middleware touches: ``function.name``,
    ``arguments``, ``result``, ``terminate``.
    """

    def __init__(self, name: str, arguments: dict[str, Any] | None = None) -> None:
        self.function = FakeFunction(name)
        self.arguments: dict[str, Any] = dict(arguments or {})
        self.result: Any = None
        self.terminate: bool = False


class RecordingNext:
    """A MAF continuation that records whether the tool would have executed."""

    def __init__(self, *, takes_context: bool = False) -> None:
        self.calls = 0
        self.takes_context = takes_context

    async def __call__(self, *args: Any) -> None:  # pragma: no cover - trivial
        self.calls += 1


def zero_arg_next(counter: list[int]):
    """Current MAF: ``call_next()``."""

    async def _next() -> None:
        counter.append(1)

    return _next


def context_arg_next(counter: list[Any]):
    """Earlier MAF preview: ``next(context)``."""

    async def _next(context: Any) -> None:
        counter.append(context)

    return _next


__all__ = [
    "REPO_ROOT",
    "TEMPLATE_POLICY",
    "FakeFunction",
    "FakeFunctionInvocationContext",
    "RecordingNext",
    "context_arg_next",
    "zero_arg_next",
]

# Keep pytest from trying to collect the helper classes above as test cases.
FakeFunction.__test__ = False  # type: ignore[attr-defined]
FakeFunctionInvocationContext.__test__ = False  # type: ignore[attr-defined]
