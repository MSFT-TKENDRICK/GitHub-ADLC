"""Build a governed Microsoft Agent Framework agent.

Deliberately tiny. MAF is not the ADLC orchestrator, so there is no scheduler,
no workflow, and no state machine here -- just "make me a chat agent whose tool
calls pass through Agent Governance Toolkit enforcement first".

MAF is public preview and its Python agent class has been spelled ``Agent``,
``ChatAgent`` and (in .NET) ``ChatClientAgent`` across releases, with the client
keyword alternating between ``client`` and ``chat_client``. Rather than pin a
spelling that breaks on the next preview, we introspect. See
``docs/governance.md`` for the versions this was verified against.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Self

from adlc.maf.middleware import (
    MAF_INSTALL_HINT,
    GovernanceDecision,
    GovernanceMiddleware,
    GovernanceUnavailable,
    PolicyEngine,
    detect_maf,
)

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = [
    "AGENT_CLASS_CANDIDATES",
    "GovernedAgent",
    "build_governed_agent",
    "resolve_agent_class",
]

#: Checked in order. The first that exists in ``agent_framework`` wins.
AGENT_CLASS_CANDIDATES: tuple[str, ...] = ("ChatClientAgent", "ChatAgent", "Agent")

#: Client keyword, checked against the resolved class's signature.
_CLIENT_KWARGS: tuple[str, ...] = ("chat_client", "client")


class GovernedAgent:
    """A MAF agent plus the policy engine that guards it.

    Holding both together means the caller can hand the engine's decision log
    to the ``governance`` gate as evidence after the run.
    """

    def __init__(self, agent: Any, engine: PolicyEngine, middleware: GovernanceMiddleware) -> None:
        self.agent = agent
        self.engine = engine
        self.middleware = middleware

    async def run(self, prompt: str, **kwargs: Any) -> Any:
        return await self.agent.run(prompt, **kwargs)

    @property
    def decisions(self) -> tuple[Any, ...]:
        return self.engine.records

    def evidence(self) -> dict[str, Any]:
        return self.engine.evidence()

    def close(self) -> None:
        self.engine.close()

    async def __aenter__(self) -> Self:
        enter = getattr(self.agent, "__aenter__", None)
        if callable(enter):
            await enter()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        exit_ = getattr(self.agent, "__aexit__", None)
        if callable(exit_):
            await exit_(*exc_info)
        self.close()


def resolve_agent_class() -> tuple[type, str]:
    """Return the installed MAF agent class and the client keyword it wants."""
    try:
        module = importlib.import_module("agent_framework")
    except ImportError as exc:  # pragma: no cover - covered by detect()
        raise GovernanceUnavailable(MAF_INSTALL_HINT) from exc

    for name in AGENT_CLASS_CANDIDATES:
        cls = getattr(module, name, None)
        if isinstance(cls, type):
            return cls, _client_kwarg(cls)

    raise GovernanceUnavailable(
        "agent_framework exposes none of "
        f"{', '.join(AGENT_CLASS_CANDIDATES)} — unsupported MAF build"
    )


def _client_kwarg(cls: type) -> str:
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):  # pragma: no cover - C-level constructors
        return _CLIENT_KWARGS[0]
    for candidate in _CLIENT_KWARGS:
        if candidate in params:
            return candidate
    return _CLIENT_KWARGS[0]


def build_governed_agent(
    *,
    chat_client: Any,
    instructions: str,
    tools: Sequence[Callable[..., Any]] = (),
    name: str = "adlc-governed-agent",
    cfg: Config | None = None,
    engine: PolicyEngine | None = None,
    policy_path: Any = None,
    agent_id: str = "adlc-agent",
    session_id: str = "adlc-session",
    extra_middleware: Iterable[Any] = (),
    on_decision: Callable[[GovernanceDecision], None] | None = None,
) -> GovernedAgent:
    """Create a MAF agent with AGT enforcement on every tool call.

    Raises :class:`GovernanceUnavailable` when MAF or AGT is missing. Callers on
    the optional path must gate this behind ``detect()`` -- never let an absent
    preview dependency degrade into an *ungoverned* agent.
    """
    available, reason = detect_maf(cfg)
    if not available:
        raise GovernanceUnavailable(reason)

    engine = engine or PolicyEngine.load(
        cfg, policy_path=policy_path, agent_id=agent_id, session_id=session_id, strict=True
    )
    if engine is None:  # pragma: no cover - strict=True raises instead
        raise GovernanceUnavailable("no AGT policy engine could be constructed")

    middleware = GovernanceMiddleware(engine, on_decision=on_decision)
    agent_cls, client_kwarg = resolve_agent_class()

    kwargs: dict[str, Any] = {
        client_kwarg: chat_client,
        "name": name,
        "instructions": instructions,
        # Governance runs first so nothing downstream can observe a call that
        # policy would have blocked.
        "middleware": [middleware, *extra_middleware],
    }
    if tools:
        kwargs["tools"] = list(tools)

    return GovernedAgent(agent_cls(**kwargs), engine, middleware)
