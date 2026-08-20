"""Agent Governance Toolkit enforcement wired into Microsoft Agent Framework.

This module is the whole point of workstream L2. Microsoft Agent Framework
(MAF) is **not** the ADLC orchestrator -- the spine's topological asyncio
executor runs the DAG. MAF is here for exactly one job: **governed agent
invocation**. Its function-calling middleware is the documented seam where an
Agent Governance Toolkit (AGT) policy decision runs *before* a tool call ever
reaches the wire. See ``docs/governance.md``.

Everything here is import-lazy. Both MAF and AGT are public preview, so
:func:`detect_maf` / :func:`detect_agt` only ever consult the module finder --
never the network, never a subprocess, never an actual import of the heavy
package.

Preview-API drift is expected, so the AGT bridge probes for whichever surface
is installed rather than hard-coding one call shape:

``agentmesh.governance.govern``
    The two-line wrapper API. ``govern(tool, policy="policy.yaml")`` returns a
    callable that raises ``GovernanceDenied`` when the policy blocks the call.

``agent_control_specification.AgentControl``
    The richer Agent Control Specification (ACS) runtime, which yields a
    structured verdict (``allow | warn | deny | escalate | transform``).

The ACS runtime is preferred when present because it produces a decision record
we can hand to the ``governance`` gate as evidence; the ``govern()`` wrapper is
the fallback.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

#: Preview integrations fail in surprising ways. Swallowing an exception is
#: sometimes the only safe response, but it is never the silent one.
_log = logging.getLogger("adlc.maf.governance")

__all__ = [
    "AGT_INSTALL_HINT",
    "BLOCKING_DECISIONS",
    "MAF_INSTALL_HINT",
    "PERMISSIVE_DECISIONS",
    "DecisionRecord",
    "GovernanceBlocked",
    "GovernanceDecision",
    "GovernanceMiddleware",
    "GovernanceUnavailable",
    "PolicyEngine",
    "detect_agt",
    "detect_governance",
    "detect_maf",
    "governance_function_middleware",
    "resolve_policy_path",
]

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: ACS verdict vocabulary. ``transform`` permits a rewritten call; ``escalate``
#: withholds permission pending a human approver, so it blocks *now*.
Decision = Literal["allow", "warn", "deny", "escalate", "transform"]

PERMISSIVE_DECISIONS: frozenset[str] = frozenset({"allow", "warn", "transform", "permit"})
BLOCKING_DECISIONS: frozenset[str] = frozenset(
    {"deny", "escalate", "block", "require_approval", "denied"}
)

MAF_INSTALL_HINT = 'agent_framework not installed — pip install "adlc[governance]"'
AGT_INSTALL_HINT = (
    'agent-governance-toolkit not installed — pip install "adlc[governance]"'
)

#: Top-level module names probed by ``detect``. Deliberately top-level only:
#: :func:`importlib.util.find_spec` on a dotted name imports the parent package,
#: which would violate the "detect() must be cheap" rule.
_MAF_MODULE = "agent_framework"
_AGT_MODULES = ("agentmesh", "agent_control_specification", "agent_os")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GovernanceUnavailable(RuntimeError):
    """AGT (or MAF) is not installed, so nothing can be governed.

    Raised only when governance was *explicitly requested*. Detection paths
    return ``(False, reason)`` instead, so an absent optional dependency
    degrades rather than crashes.
    """


class GovernanceBlocked(PermissionError):
    """A tool call was blocked by policy before it executed."""

    def __init__(self, decision: GovernanceDecision) -> None:
        self.decision = decision
        super().__init__(decision.summary())


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernanceDecision:
    """One normalized policy decision about one tool call."""

    tool: str
    decision: str
    permits: bool
    rule: str = ""
    reason: str = ""
    source: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    transformed_arguments: Mapping[str, Any] | None = None

    def summary(self) -> str:
        rule = f" rule={self.rule!r}" if self.rule else ""
        reason = f": {self.reason}" if self.reason else ""
        return f"[{self.source or 'agt'}] {self.decision} for tool {self.tool!r}{rule}{reason}"


@dataclass(frozen=True)
class DecisionRecord:
    """An append-only audit entry, suitable for the ``governance`` gate."""

    at: str
    tool: str
    decision: str
    permits: bool
    rule: str
    reason: str
    source: str

    @classmethod
    def of(cls, decision: GovernanceDecision) -> DecisionRecord:
        return cls(
            at=datetime.now(UTC).isoformat(timespec="seconds"),
            tool=decision.tool,
            decision=decision.decision,
            permits=decision.permits,
            rule=decision.rule,
            reason=decision.reason,
            source=decision.source,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Detection -- cheap, non-raising, no network
# ---------------------------------------------------------------------------


def _module_present(name: str) -> bool:
    """``True`` when ``name`` is importable, without importing it."""
    # An already-imported module is present by definition, and this also keeps
    # find_spec() from raising on modules with no __spec__.
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):  # pragma: no cover - defensive
        return False


def detect_maf(cfg: Config | None = None) -> tuple[bool, str]:
    """Is Microsoft Agent Framework importable?"""
    if not _module_present(_MAF_MODULE):
        return False, MAF_INSTALL_HINT
    return True, "agent_framework available (public preview)"


def detect_agt(cfg: Config | None = None) -> tuple[bool, str]:
    """Is the Agent Governance Toolkit importable?"""
    present = [name for name in _AGT_MODULES if _module_present(name)]
    if not present:
        return False, AGT_INSTALL_HINT
    return True, f"agent-governance-toolkit available ({', '.join(present)})"


def detect_governance(cfg: Config | None = None) -> tuple[bool, str]:
    """Both halves of the seam, plus a policy file, must be present."""
    ok, reason = detect_maf(cfg)
    if not ok:
        return False, reason
    ok, reason = detect_agt(cfg)
    if not ok:
        return False, reason
    policy = resolve_policy_path(cfg)
    if policy is None:
        return False, "no AGT policy found — expected .adlc/policy.yaml (see docs/governance.md)"
    return True, f"MAF + AGT available; policy {policy.name}"


# ---------------------------------------------------------------------------
# Policy location
# ---------------------------------------------------------------------------

#: Shipped default, vendored into a consumer repo by ``adlc init``.
TEMPLATE_POLICY = Path(__file__).resolve().parents[3] / "templates" / ".adlc" / "policy.yaml"


def resolve_policy_path(cfg: Config | None = None) -> Path | None:
    """Locate the active AGT policy.

    Order: ``ADLC_POLICY`` env var, ``<repo>/.adlc/policy.yaml``, then the
    template shipped with this package. Returns ``None`` when nothing exists --
    callers must treat that as "cannot govern", never as "allow".
    """
    candidates: list[Path] = []
    if env := os.environ.get("ADLC_POLICY"):
        candidates.append(Path(env))
    if cfg is not None:
        candidates.append(cfg.adlc_dir / "policy.yaml")
    candidates.append(TEMPLATE_POLICY)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - defensive
            continue
    return None


# ---------------------------------------------------------------------------
# Verdict normalization
# ---------------------------------------------------------------------------


def _first_attr(obj: Any, names: Sequence[str]) -> Any:
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
            continue
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _as_text(value: Any) -> str:
    """Flatten an enum/str/obj into a lowercase token."""
    if value is None:
        return ""
    for attr in ("value", "name"):
        inner = getattr(value, attr, None)
        if isinstance(inner, str):
            return inner.strip().lower()
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                dumped = fn()
            except Exception:  # noqa: BLE001 - preview API, never trust it
                _log.debug("%s.%s() raised while normalizing a verdict", value, attr, exc_info=True)
                continue
            if isinstance(dumped, Mapping):
                return dict(dumped)
    return None


def normalize_verdict(raw: Any, *, tool: str, arguments: Mapping[str, Any], source: str) -> GovernanceDecision:
    """Map any AGT result shape onto a :class:`GovernanceDecision`.

    AGT is public preview and has already renamed things between releases, so
    this walks the documented shapes in order of specificity. **An unrecognized
    shape is a block, not an allow** -- fail closed is the whole contract.
    """
    verdict = _first_attr(raw, ("verdict",))
    node = verdict if verdict is not None else raw
    decision_obj = _first_attr(node, ("decision", "effect", "action", "outcome"))

    # `permits` is authoritative when the runtime provides it: it survives
    # vocabulary drift that a name-based mapping would not.
    permits = _first_attr(decision_obj, ("permits", "allowed", "permitted", "is_allowed"))
    if permits is None:
        permits = _first_attr(node, ("permits", "allowed", "permitted", "is_allowed"))
    if permits is None:
        permits = _first_attr(raw, ("permits", "allowed", "permitted", "is_allowed"))

    name = _as_text(decision_obj) or _as_text(node) or _as_text(raw)

    if isinstance(permits, bool):
        resolved_permits = permits
        resolved_name = name or ("allow" if permits else "deny")
    elif name in PERMISSIVE_DECISIONS:
        resolved_permits, resolved_name = True, name
    elif name in BLOCKING_DECISIONS:
        resolved_permits, resolved_name = False, name
    else:
        # Fail closed: we genuinely do not know what the runtime said.
        return GovernanceDecision(
            tool=tool,
            decision="deny",
            permits=False,
            rule="",
            reason=(
                "unrecognized AGT verdict shape "
                f"({type(raw).__name__}); refusing to run the tool ungoverned"
            ),
            source=source,
            arguments=dict(arguments),
        )

    rule = _as_text(_first_attr(node, ("rule", "rule_name", "policy", "policy_id"))) or ""
    reason = ""
    for candidate in (decision_obj, node, raw):
        found = _first_attr(candidate, ("reason", "message", "description", "explanation"))
        if isinstance(found, str) and found.strip():
            reason = found.strip()
            break

    transformed = _as_mapping(
        _first_attr(node, ("transformed_args", "transformed_arguments", "transformed_input"))
    )

    return GovernanceDecision(
        tool=tool,
        decision=resolved_name,
        permits=bool(resolved_permits),
        rule=rule,
        reason=reason,
        source=source,
        arguments=dict(arguments),
        transformed_arguments=transformed,
    )


# ---------------------------------------------------------------------------
# The AGT bridge
# ---------------------------------------------------------------------------


class PolicyEngine:
    """A thin, drift-tolerant façade over whichever AGT surface is installed.

    Construct with :meth:`load`, which returns ``None`` when AGT is absent, or
    raises :class:`GovernanceUnavailable` when ``strict=True``.
    """

    def __init__(
        self,
        *,
        policy_path: Path,
        agent_id: str = "adlc-agent",
        session_id: str = "adlc-session",
        _runtime: Any = None,
        _govern: Any = None,
        _denied_exc: type[BaseException] | None = None,
        source: str = "agt",
    ) -> None:
        self.policy_path = policy_path
        self.agent_id = agent_id
        self.session_id = session_id
        self.source = source
        self._runtime = _runtime
        self._govern = _govern
        self._denied_exc = _denied_exc
        self._records: list[DecisionRecord] = []

    # -- construction ----------------------------------------------------
    @classmethod
    def load(
        cls,
        cfg: Config | None = None,
        *,
        policy_path: Path | None = None,
        agent_id: str = "adlc-agent",
        session_id: str = "adlc-session",
        strict: bool = True,
    ) -> PolicyEngine | None:
        policy = policy_path or resolve_policy_path(cfg)
        if policy is None:
            if strict:
                raise GovernanceUnavailable(
                    "no AGT policy found — expected .adlc/policy.yaml"
                )
            return None

        runtime, source = _load_acs_runtime(policy)
        if runtime is not None:
            return cls(
                policy_path=policy,
                agent_id=agent_id,
                session_id=session_id,
                _runtime=runtime,
                source=source,
            )

        govern, denied = _load_govern_wrapper()
        if govern is not None:
            return cls(
                policy_path=policy,
                agent_id=agent_id,
                session_id=session_id,
                _govern=govern,
                _denied_exc=denied,
                source="agentmesh.governance.govern",
            )

        if strict:
            raise GovernanceUnavailable(AGT_INSTALL_HINT)
        return None

    # -- enforcement -----------------------------------------------------
    def check(self, tool: str, arguments: Mapping[str, Any] | None = None) -> GovernanceDecision:
        """Evaluate one prospective tool call. Never raises on policy content.

        Any *runtime* failure is normalized into a deny, because a policy engine
        that errored has not authorized anything.
        """
        args = dict(arguments or {})
        try:
            decision = self._evaluate(tool, args)
        except GovernanceBlocked as exc:
            decision = exc.decision
        except Exception as exc:  # noqa: BLE001 - preview API; fail closed
            decision = GovernanceDecision(
                tool=tool,
                decision="deny",
                permits=False,
                reason=f"AGT policy evaluation failed: {type(exc).__name__}: {exc}",
                source=self.source,
                arguments=args,
            )
        self._records.append(DecisionRecord.of(decision))
        return decision

    def enforce(self, tool: str, arguments: Mapping[str, Any] | None = None) -> GovernanceDecision:
        """:meth:`check`, but raise :class:`GovernanceBlocked` when denied."""
        decision = self.check(tool, arguments)
        if not decision.permits:
            raise GovernanceBlocked(decision)
        return decision

    def _evaluate(self, tool: str, args: dict[str, Any]) -> GovernanceDecision:
        if self._runtime is not None:
            raw = _acs_evaluate(
                self._runtime,
                tool=tool,
                arguments=args,
                agent_id=self.agent_id,
                session_id=self.session_id,
            )
            return normalize_verdict(raw, tool=tool, arguments=args, source=self.source)

        if self._govern is not None:
            return _govern_probe(
                self._govern,
                self._denied_exc,
                policy_path=self.policy_path,
                tool=tool,
                arguments=args,
                source=self.source,
            )

        raise GovernanceUnavailable(AGT_INSTALL_HINT)

    # -- audit -----------------------------------------------------------
    @property
    def records(self) -> tuple[DecisionRecord, ...]:
        return tuple(self._records)

    def evidence(self) -> dict[str, Any]:
        """The decision log, shaped for ``runs/<run>/gates/`` evidence."""
        return {
            "policy": str(self.policy_path),
            "engine": self.source,
            "agentId": self.agent_id,
            "sessionId": self.session_id,
            "decisions": [record.as_dict() for record in self._records],
            "denied": sum(1 for record in self._records if not record.permits),
            "total": len(self._records),
        }

    def close(self) -> None:
        closer = getattr(self._runtime, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best effort teardown
                _log.debug("closing the AGT runtime raised", exc_info=True)


def _load_acs_runtime(policy: Path) -> tuple[Any, str]:
    """Build an ``AgentControl`` runtime if the ACS SDK is installed.

    The constructor has been spelled ``from_path`` (README) and ``from_manifest``
    (repo examples) across preview releases, so both are attempted.
    """
    try:
        module = importlib.import_module("agent_control_specification")
    except Exception:  # noqa: BLE001 - optional dependency
        return None, ""
    control = getattr(module, "AgentControl", None)
    if control is None:
        return None, ""
    for ctor in ("from_path", "from_manifest", "from_file"):
        factory = getattr(control, ctor, None)
        if not callable(factory):
            continue
        for arg in (str(policy), policy):
            try:
                return factory(arg), f"agent_control_specification.AgentControl.{ctor}"
            except Exception:  # noqa: BLE001 - try the next spelling
                _log.debug("AgentControl.%s(%r) did not work", ctor, arg, exc_info=True)
                continue
    return None, ""


def _load_govern_wrapper() -> tuple[Any, type[BaseException] | None]:
    try:
        module = importlib.import_module("agentmesh.governance")
    except Exception:  # noqa: BLE001 - optional dependency
        return None, None
    govern = getattr(module, "govern", None)
    denied = getattr(module, "GovernanceDenied", None)
    if not callable(govern):
        return None, None
    if not (isinstance(denied, type) and issubclass(denied, BaseException)):
        denied = None
    return govern, denied


def _acs_evaluate(
    runtime: Any,
    *,
    tool: str,
    arguments: Mapping[str, Any],
    agent_id: str,
    session_id: str,
) -> Any:
    """Call whichever pre-tool-call entry point this ACS build exposes."""
    payload = {
        "envelope": {"agent_id": agent_id, "session_id": session_id},
        "input": {"body": {"action": tool, "params": dict(arguments)}},
    }

    # Preferred: a session with an explicit pre-tool-call hook.
    session_factory = _load_host_session()
    if session_factory is not None:
        try:
            session = session_factory(runtime, agent_id=agent_id, session_id=session_id)
        except Exception:  # noqa: BLE001 - fall through to the runtime API
            session = None
        if session is not None:
            hook = getattr(session, "pre_tool_call", None)
            if callable(hook):
                return hook(tool_name=tool, args=dict(arguments))

    # Fallback: the stateless runtime evaluate() shown in the AGT README.
    evaluate = getattr(runtime, "evaluate", None)
    if callable(evaluate):
        return evaluate("input", payload)

    check = getattr(runtime, "check", None) or getattr(runtime, "pre_tool_call", None)
    if callable(check):
        return check(tool_name=tool, args=dict(arguments))

    raise GovernanceUnavailable(
        "AgentControl exposes no evaluate()/pre_tool_call() — unsupported AGT build"
    )


def _load_host_session() -> Any:
    try:
        module = importlib.import_module("agent_control_specification")
    except Exception:  # noqa: BLE001
        return None
    return getattr(module, "HostSession", None)


def _govern_probe(
    govern: Any,
    denied_exc: type[BaseException] | None,
    *,
    policy_path: Path,
    tool: str,
    arguments: Mapping[str, Any],
    source: str,
) -> GovernanceDecision:
    """Use ``govern()`` as a *probe* rather than as the executor.

    ``govern()`` wraps a callable and raises ``GovernanceDenied`` at call time.
    To keep the check strictly before execution we wrap a no-op sentinel that
    records the arguments it was reached with: if the sentinel runs, policy
    permitted the call; if ``GovernanceDenied`` is raised, it did not. The real
    tool is then executed by MAF, exactly once.
    """
    reached: dict[str, Any] = {}

    def _sentinel(**kwargs: Any) -> dict[str, Any]:
        reached.update(kwargs)
        return kwargs

    _sentinel.__name__ = tool

    guarded = govern(_sentinel, policy=str(policy_path))
    try:
        guarded(**dict(arguments))
    except Exception as exc:
        if denied_exc is not None and isinstance(exc, denied_exc):
            return GovernanceDecision(
                tool=tool,
                decision="deny",
                permits=False,
                reason=str(exc),
                source=source,
                arguments=dict(arguments),
            )
        if type(exc).__name__ == "GovernanceDenied":
            return GovernanceDecision(
                tool=tool,
                decision="deny",
                permits=False,
                reason=str(exc),
                source=source,
                arguments=dict(arguments),
            )
        raise

    if not reached:
        # The wrapper short-circuited without denying and without running.
        return GovernanceDecision(
            tool=tool,
            decision="deny",
            permits=False,
            reason="govern() neither permitted nor denied the call — failing closed",
            source=source,
            arguments=dict(arguments),
        )
    return GovernanceDecision(
        tool=tool,
        decision="allow",
        permits=True,
        source=source,
        arguments=dict(arguments),
    )


# ---------------------------------------------------------------------------
# The MAF seam
# ---------------------------------------------------------------------------


async def _call_next(call_next: Callable[..., Awaitable[None]], context: Any) -> None:
    """Invoke MAF's continuation across preview signature changes.

    Current MAF passes a zero-argument ``call_next``; earlier previews passed
    ``next(context)``. Inspecting is cheaper than guessing wrong.
    """
    try:
        signature = inspect.signature(call_next)
    except (TypeError, ValueError):  # pragma: no cover - builtins/partials
        signature = None

    takes_context = False
    if signature is not None:
        positional = [
            p
            for p in signature.parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        takes_context = bool(positional) or any(
            p.kind is inspect.Parameter.VAR_POSITIONAL for p in signature.parameters.values()
        )

    result = call_next(context) if takes_context else call_next()
    if inspect.isawaitable(result):
        await result


def _context_tool_name(context: Any) -> str:
    function = getattr(context, "function", None)
    for candidate in (function, context):
        name = _first_attr(candidate, ("name", "tool_name", "function_name"))
        if isinstance(name, str) and name:
            return name
    return "<unknown>"


def _context_arguments(context: Any) -> dict[str, Any]:
    raw = _first_attr(context, ("arguments", "args", "parameters"))
    mapped = _as_mapping(raw)
    return dict(mapped) if mapped is not None else {}


class GovernanceMiddleware:
    """MAF function middleware that enforces AGT policy before execution.

    Attach via ``Agent(..., middleware=[GovernanceMiddleware(engine)])``. It is
    callable, so it satisfies MAF's function-style middleware contract, and it
    also exposes :meth:`process` for the class-based ``FunctionMiddleware``
    contract used by older previews.

    On a blocking verdict the continuation is **never awaited** -- the tool call
    does not happen. ``context.terminate`` is set and ``context.result`` carries
    the denial so the model sees a normal tool error rather than a crash.
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        on_decision: Callable[[GovernanceDecision], None] | None = None,
        raise_on_deny: bool = False,
    ) -> None:
        self.engine = engine
        self.on_decision = on_decision
        self.raise_on_deny = raise_on_deny

    async def __call__(
        self, context: Any, call_next: Callable[..., Awaitable[None]]
    ) -> None:
        tool = _context_tool_name(context)
        arguments = _context_arguments(context)
        decision = self.engine.check(tool, arguments)

        if self.on_decision is not None:
            self.on_decision(decision)

        if not decision.permits:
            self._block(context, decision)
            if self.raise_on_deny:
                raise GovernanceBlocked(decision)
            return

        if decision.transformed_arguments is not None:
            self._apply_transform(context, decision)

        await _call_next(call_next, context)

    # MAF's class-based middleware contract.
    async def process(
        self, context: Any, next: Callable[..., Awaitable[None]]
    ) -> None:
        await self(context, next)

    @staticmethod
    def _block(context: Any, decision: GovernanceDecision) -> None:
        message = f"Blocked by agent governance policy — {decision.summary()}"
        for attr, value in (("result", message), ("terminate", True)):
            try:
                setattr(context, attr, value)
            except Exception:  # noqa: BLE001 - frozen or read-only contexts
                _log.debug("could not set %s on %r", attr, type(context), exc_info=True)
                continue

    @staticmethod
    def _apply_transform(context: Any, decision: GovernanceDecision) -> None:
        target = getattr(context, "arguments", None)
        transformed = dict(decision.transformed_arguments or {})
        if isinstance(target, dict):
            target.clear()
            target.update(transformed)
            return
        try:
            context.arguments = transformed
        except Exception:  # noqa: BLE001 - best effort on preview contexts
            _log.debug("could not apply a transformed-argument verdict", exc_info=True)


def governance_function_middleware(
    engine: PolicyEngine,
    *,
    on_decision: Callable[[GovernanceDecision], None] | None = None,
) -> GovernanceMiddleware:
    """Convenience constructor for the MAF middleware list."""
    return GovernanceMiddleware(engine, on_decision=on_decision)


def govern_tools(
    engine: PolicyEngine, tools: Iterable[Callable[..., Any]]
) -> list[Callable[..., Any]]:
    """Belt-and-braces: also gate each tool at its own call boundary.

    MAF middleware is the primary seam. This exists for tools that a host might
    invoke outside the agent loop, so a direct call is still policy-checked.
    """
    governed: list[Callable[..., Any]] = []
    for tool in tools:
        governed.append(_wrap_tool(engine, tool))
    return governed


def _wrap_tool(engine: PolicyEngine, tool: Callable[..., Any]) -> Callable[..., Any]:
    import functools

    name = getattr(tool, "__name__", repr(tool))

    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def _async_guarded(**kwargs: Any) -> Any:
            engine.enforce(name, kwargs)
            return await tool(**kwargs)

        return _async_guarded

    @functools.wraps(tool)
    def _guarded(**kwargs: Any) -> Any:
        engine.enforce(name, kwargs)
        return tool(**kwargs)

    return _guarded
