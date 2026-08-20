"""Verdict normalization and the MAF middleware seam, with mocks only.

Two things are under test:

1. **Policy mapping** — every AGT verdict shape we have seen in the public
   preview maps onto the right ``permits`` boolean, and an unrecognized shape
   fails *closed*.
2. **The seam** — on a blocking verdict, MAF's continuation is never awaited, so
   the tool call does not happen. That is the entire security property.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import pytest

from adlc.maf.middleware import (
    BLOCKING_DECISIONS,
    PERMISSIVE_DECISIONS,
    DecisionRecord,
    GovernanceBlocked,
    GovernanceDecision,
    GovernanceMiddleware,
    PolicyEngine,
    normalize_verdict,
)

from .conftest import FakeFunctionInvocationContext, context_arg_next, zero_arg_next

# ---------------------------------------------------------------------------
# Fake AGT verdict shapes
# ---------------------------------------------------------------------------


class _DecisionEnum(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"
    TRANSFORM = "transform"
    WARN = "warn"


@dataclass
class _AcsDecision:
    """`result.verdict.decision` in the Agent Control Specification SDK."""

    permits: bool
    value: str
    reason: str = ""


@dataclass
class _AcsVerdict:
    decision: _AcsDecision
    rule: str = ""


@dataclass
class _AcsResult:
    verdict: _AcsVerdict


def acs(decision: str, permits: bool, *, rule: str = "", reason: str = "") -> _AcsResult:
    return _AcsResult(_AcsVerdict(_AcsDecision(permits, decision, reason), rule))


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestVerdictMapping:
    @pytest.mark.parametrize(
        ("decision", "permits"),
        [
            ("allow", True),
            ("warn", True),
            ("transform", True),
            ("deny", False),
            ("escalate", False),
        ],
    )
    def test_acs_permits_flag_is_authoritative(self, decision: str, permits: bool) -> None:
        result = normalize_verdict(
            acs(decision, permits), tool="write_file", arguments={}, source="acs"
        )
        assert result.decision == decision
        assert result.permits is permits

    @pytest.mark.parametrize("decision", sorted(PERMISSIVE_DECISIONS))
    def test_bare_permissive_names_permit(self, decision: str) -> None:
        result = normalize_verdict(
            {"verdict": {"decision": decision}}, tool="t", arguments={}, source="acs"
        )
        assert result.permits is True

    @pytest.mark.parametrize("decision", sorted(BLOCKING_DECISIONS))
    def test_bare_blocking_names_block(self, decision: str) -> None:
        result = normalize_verdict(
            {"verdict": {"decision": decision}}, tool="t", arguments={}, source="acs"
        )
        assert result.permits is False

    def test_escalate_blocks_now(self) -> None:
        """`escalate` means 'a human has not approved this yet', i.e. not now."""
        result = normalize_verdict(
            {"verdict": {"decision": "escalate"}}, tool="t", arguments={}, source="acs"
        )
        assert result.permits is False

    def test_enum_decisions_are_flattened(self) -> None:
        result = normalize_verdict(
            {"verdict": {"decision": _DecisionEnum.DENY}},
            tool="t",
            arguments={},
            source="acs",
        )
        assert result.decision == "deny"
        assert result.permits is False

    def test_boolean_only_shape(self) -> None:
        """The Rust/Go client shape: ``result.allowed``."""
        allowed = normalize_verdict({"allowed": True}, tool="t", arguments={}, source="x")
        blocked = normalize_verdict({"allowed": False}, tool="t", arguments={}, source="x")
        assert allowed.permits is True
        assert blocked.permits is False
        assert blocked.decision == "deny"

    def test_permits_flag_wins_over_an_unknown_name(self) -> None:
        """Vocabulary drift must not flip a deny into an allow."""
        result = normalize_verdict(
            acs("quarantine_pending_review", False), tool="t", arguments={}, source="acs"
        )
        assert result.permits is False

    def test_unrecognized_shape_fails_closed(self) -> None:
        for raw in (None, object(), {"something": "else"}, "banana", 42):
            result = normalize_verdict(raw, tool="t", arguments={}, source="acs")
            assert result.permits is False, raw
            assert result.decision == "deny"
            assert "unrecognized" in result.reason

    def test_rule_and_reason_are_carried_through(self) -> None:
        result = normalize_verdict(
            acs("deny", False, rule="block-destructive", reason="drop is not allowed"),
            tool="drop_table",
            arguments={"table": "users"},
            source="acs",
        )
        assert result.rule == "block-destructive"
        assert "drop is not allowed" in result.reason
        assert result.arguments == {"table": "users"}

    def test_summary_is_human_readable(self) -> None:
        decision = normalize_verdict(
            acs("deny", False, rule="block-destructive", reason="nope"),
            tool="drop_table",
            arguments={},
            source="acs",
        )
        summary = decision.summary()
        assert "deny" in summary
        assert "drop_table" in summary
        assert "block-destructive" in summary


# ---------------------------------------------------------------------------
# A stub engine that exercises the middleware without AGT installed
# ---------------------------------------------------------------------------


class StubEngine(PolicyEngine):
    """PolicyEngine with a scripted verdict, so no AGT install is needed."""

    def __init__(self, verdicts: dict[str, Any], *, default: Any = None) -> None:
        super().__init__(policy_path=__import__("pathlib").Path("policy.yaml"), source="stub")
        self._verdicts = verdicts
        self._default = default if default is not None else acs("allow", True)
        self.evaluated: list[tuple[str, dict[str, Any]]] = []

    def _evaluate(self, tool: str, args: dict[str, Any]) -> GovernanceDecision:
        self.evaluated.append((tool, dict(args)))
        raw = self._verdicts.get(tool, self._default)
        return normalize_verdict(raw, tool=tool, arguments=args, source=self.source)


class TestPolicyEngine:
    def test_check_records_every_decision(self) -> None:
        engine = StubEngine({"drop_table": acs("deny", False)})
        engine.check("read_file", {"path": "a.py"})
        engine.check("drop_table", {"table": "users"})
        assert [record.tool for record in engine.records] == ["read_file", "drop_table"]
        assert [record.permits for record in engine.records] == [True, False]

    def test_engine_failure_is_a_deny_not_an_allow(self) -> None:
        class Exploding(StubEngine):
            def _evaluate(self, tool: str, args: dict[str, Any]) -> GovernanceDecision:
                raise RuntimeError("policy engine exploded")

        decision = Exploding({}).check("write_file", {})
        assert decision.permits is False
        assert "policy engine exploded" in decision.reason

    def test_enforce_raises_on_deny(self) -> None:
        engine = StubEngine({"drop_table": acs("deny", False, reason="no")})
        with pytest.raises(GovernanceBlocked) as excinfo:
            engine.enforce("drop_table", {})
        assert excinfo.value.decision.permits is False

    def test_evidence_counts_denials(self) -> None:
        engine = StubEngine({"drop_table": acs("deny", False)})
        engine.check("read_file", {})
        engine.check("drop_table", {})
        evidence = engine.evidence()
        assert evidence["total"] == 2
        assert evidence["denied"] == 1
        assert evidence["engine"] == "stub"
        assert len(evidence["decisions"]) == 2

    def test_decision_record_is_json_safe(self) -> None:
        import json

        record = DecisionRecord.of(
            GovernanceDecision(tool="t", decision="allow", permits=True)
        )
        assert json.loads(json.dumps(record.as_dict()))["tool"] == "t"


# ---------------------------------------------------------------------------
# The MAF seam
# ---------------------------------------------------------------------------


class TestMiddlewareSeam:
    @pytest.mark.asyncio
    async def test_allowed_call_reaches_the_tool(self) -> None:
        engine = StubEngine({})
        middleware = GovernanceMiddleware(engine)
        context = FakeFunctionInvocationContext("read_file", {"path": "a.py"})
        calls: list[int] = []

        await middleware(context, zero_arg_next(calls))

        assert calls == [1]
        assert context.terminate is False

    @pytest.mark.asyncio
    async def test_denied_call_never_reaches_the_tool(self) -> None:
        engine = StubEngine({"drop_table": acs("deny", False, rule="block-destructive")})
        middleware = GovernanceMiddleware(engine)
        context = FakeFunctionInvocationContext("drop_table", {"table": "users"})
        calls: list[int] = []

        await middleware(context, zero_arg_next(calls))

        assert calls == [], "the continuation must not be awaited on a deny"
        assert context.terminate is True
        assert "Blocked by agent governance policy" in str(context.result)
        assert "block-destructive" in str(context.result)

    @pytest.mark.asyncio
    async def test_policy_is_checked_before_execution_not_after(self) -> None:
        order: list[str] = []

        class Ordering(StubEngine):
            def _evaluate(self, tool: str, args: dict[str, Any]) -> GovernanceDecision:
                order.append("policy")
                return super()._evaluate(tool, args)

        async def _next() -> None:
            order.append("tool")

        await GovernanceMiddleware(Ordering({}))(
            FakeFunctionInvocationContext("read_file"), _next
        )
        assert order == ["policy", "tool"]

    @pytest.mark.asyncio
    async def test_escalate_blocks(self) -> None:
        engine = StubEngine({"write_file": acs("escalate", False, rule="protected-paths")})
        context = FakeFunctionInvocationContext("write_file", {"path": ".github/x.yml"})
        calls: list[int] = []
        await GovernanceMiddleware(engine)(context, zero_arg_next(calls))
        assert calls == []
        assert context.terminate is True

    @pytest.mark.asyncio
    async def test_unknown_verdict_blocks_the_tool(self) -> None:
        engine = StubEngine({"write_file": {"totally": "unexpected"}})
        context = FakeFunctionInvocationContext("write_file")
        calls: list[int] = []
        await GovernanceMiddleware(engine)(context, zero_arg_next(calls))
        assert calls == [], "an unparseable verdict must not authorize a tool call"

    @pytest.mark.asyncio
    async def test_supports_zero_arg_continuation(self) -> None:
        calls: list[int] = []
        await GovernanceMiddleware(StubEngine({}))(
            FakeFunctionInvocationContext("read_file"), zero_arg_next(calls)
        )
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_supports_context_arg_continuation(self) -> None:
        """Earlier MAF previews passed the context to ``next``."""
        seen: list[Any] = []
        context = FakeFunctionInvocationContext("read_file")
        await GovernanceMiddleware(StubEngine({}))(context, context_arg_next(seen))
        assert seen == [context]

    @pytest.mark.asyncio
    async def test_supports_a_bound_method_continuation(self) -> None:
        class Continuation:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self) -> None:
                self.calls += 1

        cont = Continuation()
        await GovernanceMiddleware(StubEngine({}))(
            FakeFunctionInvocationContext("read_file"), cont
        )
        assert cont.calls == 1

    @pytest.mark.asyncio
    async def test_supports_a_partial_continuation(self) -> None:
        import functools

        seen: list[Any] = []

        async def _next(tag: str, context: Any) -> None:
            seen.append((tag, context))

        context = FakeFunctionInvocationContext("read_file")
        await GovernanceMiddleware(StubEngine({}))(
            context, functools.partial(_next, "tagged")
        )
        assert seen == [("tagged", context)]

    @pytest.mark.asyncio
    async def test_supports_a_varargs_continuation(self) -> None:
        seen: list[Any] = []

        async def _next(*args: Any) -> None:
            seen.append(args)

        await GovernanceMiddleware(StubEngine({}))(
            FakeFunctionInvocationContext("read_file"), _next
        )
        assert seen and len(seen[0]) == 0

    @pytest.mark.asyncio
    async def test_unsupported_continuation_aborts_rather_than_running_ungoverned(
        self,
    ) -> None:
        async def _next(a: Any, b: Any) -> None:  # pragma: no cover - must not run
            raise AssertionError("should not be reachable")

        with pytest.raises(TypeError):
            await GovernanceMiddleware(StubEngine({}))(
                FakeFunctionInvocationContext("read_file"), _next
            )

    @pytest.mark.asyncio
    async def test_process_alias_for_class_based_middleware(self) -> None:
        calls: list[int] = []
        await GovernanceMiddleware(StubEngine({})).process(
            FakeFunctionInvocationContext("read_file"), zero_arg_next(calls)
        )
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_tool_name_and_arguments_are_forwarded_to_policy(self) -> None:
        engine = StubEngine({})
        context = FakeFunctionInvocationContext("write_file", {"path": "src/a.py"})
        await GovernanceMiddleware(engine)(context, zero_arg_next([]))
        assert engine.evaluated == [("write_file", {"path": "src/a.py"})]

    @pytest.mark.asyncio
    async def test_transform_rewrites_arguments_then_proceeds(self) -> None:
        raw = {
            "verdict": {
                "decision": {"permits": True, "value": "transform"},
                "transformed_args": {"path": "src/redacted.py"},
            }
        }
        engine = StubEngine({"write_file": raw})
        context = FakeFunctionInvocationContext("write_file", {"path": "src/secret.py"})
        calls: list[int] = []
        await GovernanceMiddleware(engine)(context, zero_arg_next(calls))
        assert calls == [1]
        assert context.arguments == {"path": "src/redacted.py"}

    @pytest.mark.asyncio
    async def test_transform_on_the_outer_result_is_also_found(self) -> None:
        """Newer ACS builds put the rewrite beside the verdict, not inside it."""
        raw = {
            "verdict": {"decision": {"permits": True, "value": "transform"}},
            "transformed_args": {"path": "src/redacted.py"},
        }
        context = FakeFunctionInvocationContext("write_file", {"path": "src/secret.py"})
        calls: list[int] = []
        await GovernanceMiddleware(StubEngine({"write_file": raw}))(
            context, zero_arg_next(calls)
        )
        assert calls == [1]
        assert context.arguments == {"path": "src/redacted.py"}

    @pytest.mark.asyncio
    async def test_transform_without_a_rewrite_blocks(self) -> None:
        """The original arguments are exactly what policy declined to permit."""
        raw = {"verdict": {"decision": {"permits": True, "value": "transform"}}}
        engine = StubEngine({"write_file": raw})
        context = FakeFunctionInvocationContext("write_file", {"path": "src/secret.py"})
        calls: list[int] = []

        await GovernanceMiddleware(engine)(context, zero_arg_next(calls))

        assert calls == [], "untransformed arguments must not reach the tool"
        assert context.terminate is True
        assert context.arguments == {"path": "src/secret.py"}
        assert engine.records[-1].permits is False

    @pytest.mark.asyncio
    async def test_transform_that_cannot_be_installed_blocks(self) -> None:
        raw = {
            "verdict": {
                "decision": {"permits": True, "value": "transform"},
                "transformed_args": {"path": "src/redacted.py"},
            }
        }

        class ReadOnlyContext(FakeFunctionInvocationContext):
            """A context whose ``arguments`` cannot be replaced."""

            def __setattr__(self, name: str, value: Any) -> None:
                if name == "arguments" and getattr(self, "_sealed", False):
                    raise AttributeError("arguments is read-only")
                object.__setattr__(self, name, value)

        context = ReadOnlyContext("write_file", {"path": "src/secret.py"})
        object.__setattr__(context, "arguments", "not-a-mapping")
        object.__setattr__(context, "_sealed", True)

        calls: list[int] = []
        await GovernanceMiddleware(StubEngine({"write_file": raw}))(
            context, zero_arg_next(calls)
        )
        assert calls == [], "a transform that could not be applied must block"

    @pytest.mark.asyncio
    async def test_on_decision_callback_sees_every_decision(self) -> None:
        seen: list[GovernanceDecision] = []
        engine = StubEngine({"drop_table": acs("deny", False)})
        middleware = GovernanceMiddleware(engine, on_decision=seen.append)
        await middleware(FakeFunctionInvocationContext("drop_table"), zero_arg_next([]))
        assert len(seen) == 1
        assert seen[0].permits is False

    @pytest.mark.asyncio
    async def test_raise_on_deny_is_opt_in(self) -> None:
        engine = StubEngine({"drop_table": acs("deny", False)})
        middleware = GovernanceMiddleware(engine, raise_on_deny=True)
        with pytest.raises(GovernanceBlocked):
            await middleware(FakeFunctionInvocationContext("drop_table"), zero_arg_next([]))


class TestDirectToolWrapping:
    def test_wrapped_tool_is_blocked(self) -> None:
        from adlc.maf.middleware import govern_tools

        def drop_table(table: str) -> str:  # pragma: no cover - must never run
            raise AssertionError("policy should have blocked this")

        engine = StubEngine({"drop_table": acs("deny", False)})
        (guarded,) = govern_tools(engine, [drop_table])
        with pytest.raises(GovernanceBlocked):
            guarded(table="users")

    def test_wrapped_tool_runs_when_allowed(self) -> None:
        from adlc.maf.middleware import govern_tools

        def read_file(path: str) -> str:
            return f"contents of {path}"

        (guarded,) = govern_tools(StubEngine({}), [read_file])
        assert guarded(path="a.py") == "contents of a.py"

    @pytest.mark.asyncio
    async def test_async_tool_is_wrapped_too(self) -> None:
        from adlc.maf.middleware import govern_tools

        async def read_file(path: str) -> str:
            return f"async {path}"

        (guarded,) = govern_tools(StubEngine({}), [read_file])
        assert await guarded(path="a.py") == "async a.py"
