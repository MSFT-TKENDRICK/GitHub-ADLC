"""The MAF agent factory, against a stubbed ``agent_framework``.

MAF is public preview and has renamed its agent class (``Agent`` /
``ChatAgent`` / ``ChatClientAgent``) and its client keyword (``client`` /
``chat_client``) between releases. The factory introspects instead of pinning,
and these tests hold that behaviour still.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Self

import pytest

from adlc.maf.agents import (
    AGENT_CLASS_CANDIDATES,
    GovernedAgent,
    build_governed_agent,
    resolve_agent_class,
)
from adlc.maf.middleware import GovernanceMiddleware, GovernanceUnavailable

from .test_policy_mapping import StubEngine, acs


class _RecordingAgent:
    """Captures the kwargs the factory passed, so we can assert the wiring."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.entered = False

    async def run(self, prompt: str, **kwargs: Any) -> Any:
        return types.SimpleNamespace(text=f"ran: {prompt}")

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.entered = False


def fake_maf(monkeypatch: pytest.MonkeyPatch, class_name: str, client_kwarg: str) -> type:
    """Install a stub ``agent_framework`` exposing exactly one agent class."""

    namespace: dict[str, Any] = {}
    exec(  # noqa: S102 - building a class with a dynamic keyword name
        f"class Agent(_Base):\n"
        f"    def __init__(self, *, {client_kwarg}, name=None, instructions=None,"
        f" tools=None, middleware=None):\n"
        f"        super().__init__({client_kwarg}={client_kwarg}, name=name,"
        f" instructions=instructions, tools=tools, middleware=middleware)\n",
        {"_Base": _RecordingAgent},
        namespace,
    )
    agent_cls = namespace["Agent"]

    module = types.ModuleType("agent_framework")
    setattr(module, class_name, agent_cls)
    monkeypatch.setitem(sys.modules, "agent_framework", module)
    return agent_cls


class TestAgentClassResolution:
    @pytest.mark.parametrize("class_name", AGENT_CLASS_CANDIDATES)
    def test_finds_each_supported_spelling(self, monkeypatch, class_name: str) -> None:
        fake_maf(monkeypatch, class_name, "chat_client")
        resolved, client_kwarg = resolve_agent_class()
        assert resolved.__name__ == "Agent"
        assert client_kwarg == "chat_client"

    def test_prefers_the_first_candidate(self, monkeypatch) -> None:
        module = types.ModuleType("agent_framework")
        for name in AGENT_CLASS_CANDIDATES:
            setattr(module, name, type(name, (), {"__init__": lambda self, **kw: None}))
        monkeypatch.setitem(sys.modules, "agent_framework", module)
        resolved, _ = resolve_agent_class()
        assert resolved.__name__ == AGENT_CLASS_CANDIDATES[0]

    @pytest.mark.parametrize("client_kwarg", ["client", "chat_client"])
    def test_detects_the_client_keyword(self, monkeypatch, client_kwarg: str) -> None:
        fake_maf(monkeypatch, "ChatAgent", client_kwarg)
        _, resolved = resolve_agent_class()
        assert resolved == client_kwarg

    def test_unknown_build_is_an_explicit_error(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "agent_framework", types.ModuleType("agent_framework"))
        with pytest.raises(GovernanceUnavailable, match="unsupported MAF build"):
            resolve_agent_class()


class TestBuildGovernedAgent:
    def build(self, monkeypatch, cfg, engine=None, **kwargs: Any) -> GovernedAgent:
        fake_maf(monkeypatch, "ChatAgent", "chat_client")
        return build_governed_agent(
            chat_client=object(),
            instructions="do the thing",
            cfg=cfg,
            engine=engine or StubEngine({}),
            **kwargs,
        )

    def test_governance_middleware_is_attached(self, monkeypatch, cfg) -> None:
        governed = self.build(monkeypatch, cfg)
        middleware = governed.agent.kwargs["middleware"]
        assert isinstance(middleware[0], GovernanceMiddleware)

    def test_governance_runs_before_any_other_middleware(self, monkeypatch, cfg) -> None:
        """Nothing may observe a call that policy would have blocked."""
        marker = object()
        governed = self.build(monkeypatch, cfg, extra_middleware=[marker])
        middleware = governed.agent.kwargs["middleware"]
        assert isinstance(middleware[0], GovernanceMiddleware)
        assert middleware[1] is marker

    def test_tools_are_forwarded(self, monkeypatch, cfg) -> None:
        def read_file(path: str) -> str:
            return path

        governed = self.build(monkeypatch, cfg, tools=[read_file])
        assert governed.agent.kwargs["tools"] == [read_file]

    def test_instructions_and_name_are_forwarded(self, monkeypatch, cfg) -> None:
        governed = self.build(monkeypatch, cfg, name="adlc-T003")
        assert governed.agent.kwargs["name"] == "adlc-T003"
        assert governed.agent.kwargs["instructions"] == "do the thing"

    def test_refuses_to_build_without_maf(self, monkeypatch, no_optional_deps, cfg) -> None:
        with pytest.raises(GovernanceUnavailable):
            build_governed_agent(
                chat_client=object(),
                instructions="x",
                cfg=cfg,
                engine=StubEngine({}),
            )

    @pytest.mark.asyncio
    async def test_evidence_survives_the_run(self, monkeypatch, cfg) -> None:
        engine = StubEngine({"drop_table": acs("deny", False)})
        governed = self.build(monkeypatch, cfg, engine=engine)
        async with governed:
            await governed.run("go")
        engine.check("drop_table", {})
        evidence = governed.evidence()
        assert evidence["denied"] == 1
        assert evidence["engine"] == "stub"

    @pytest.mark.asyncio
    async def test_context_manager_delegates_to_the_agent(self, monkeypatch, cfg) -> None:
        governed = self.build(monkeypatch, cfg)
        async with governed:
            assert governed.agent.entered is True
        assert governed.agent.entered is False
