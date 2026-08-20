"""Shared fixtures for the L4 security/quality gate tests.

Every test here must pass with **no credentials**. The ``scrub_github_env``
fixture is autouse so the suite is hermetic: a developer who happens to have
``GITHUB_TOKEN`` exported cannot accidentally make (or break) a test, and no test
can reach the network by accident.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Standard src-layout bootstrap: make *this* checkout's `src/` win over any
# editable install that points at a different worktree, so the tests always
# exercise the code sitting next to them.
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir():
    if str(_SRC) not in sys.path[:1]:
        sys.path.insert(0, str(_SRC))
    _stale = [
        name
        for name, mod in sys.modules.items()
        if name == "adlc" or name.startswith("adlc.")
        if not str(getattr(mod, "__file__", "") or "").startswith(str(_SRC))
    ]
    for name in _stale:
        del sys.modules[name]

from adlc.config import Config

FIXTURES = Path(__file__).parent / "fixtures"

#: Every env var that can make a gate think it has credentials or a target repo.
GITHUB_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_REPOSITORY",
    "GITHUB_REF",
    "GITHUB_API_URL",
)

HEAD_SHA = "d3c4b5a69788e1f2a3b4c5d6e7f8091a2b3c4d5e"
BASE_SHA = "0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a"
STALE_SHA = "1111111111111111111111111111111111111111"
PR_REF = "refs/pull/42/merge"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def scrub_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all GitHub credentials/context from the environment by default."""
    for var in GITHUB_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt in to *fake* credentials. No test using these ever hits the network."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake_token_for_tests")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widget")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A ``full``-profile config, so security/code_quality are required gates."""
    return Config(root=tmp_path, profile="full", gates={})


@pytest.fixture
def run() -> dict[str, Any]:
    return {
        "schemaVersion": "adlc-run/v1",
        "runId": "2026-08-19-a1b2",
        "repo": "acme/widget",
        "baseSha": BASE_SHA,
        "headSha": HEAD_SHA,
        "prNumber": 42,
        "profile": "full",
    }


class FakeClock:
    """Deterministic monotonic clock. ``sleep`` advances it; nothing really waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class FakeClient:
    """Stand-in for ``GitHubRestClient`` that serves fixtures and records calls.

    Any endpoint not explicitly stubbed raises, so a test can never silently
    depend on an unmodelled call.
    """

    def __init__(
        self,
        *,
        analyses: list[dict[str, Any]] | None = None,
        alerts: list[dict[str, Any]] | None = None,
        routes: dict[str, Any] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.analyses = analyses if analyses is not None else []
        self.alerts = alerts if alerts is not None else []
        self.routes = routes or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, Any]] = []

    # -- constructed by the gate as GitHubRestClient(token, repo) ----------
    def __call__(self, *args: Any, **kwargs: Any) -> FakeClient:
        return self

    def _resolve(self, path: str) -> Any:
        for key, exc in self.errors.items():
            if key in path:
                raise exc
        for key, value in self.routes.items():
            if key in path:
                return value
        raise AssertionError(f"FakeClient received an unstubbed request: {path}")

    def get(self, path: str, params: Any = None) -> Any:
        self.calls.append((path, params))
        return self._resolve(path)

    def get_list(self, path: str, params: Any = None, **kwargs: Any) -> Any:
        self.calls.append((path, params))
        result = self._resolve(path)
        return result if isinstance(result, list) else []

    def list_analyses(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("code-scanning/analyses", kwargs))
        return list(self.analyses)

    def list_alerts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("code-scanning/alerts", kwargs))
        return list(self.alerts)
