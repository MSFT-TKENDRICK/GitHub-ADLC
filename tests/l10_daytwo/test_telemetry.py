"""``AppInsightsTelemetry`` — attribute handling and safe degradation.

The export itself needs Azure, so these tests target the parts that must be
correct *before* anything leaves the process: attribute names are preserved
byte-for-byte, values are coerced to OTel-legal types, and a broken exporter
degrades instead of failing the run.
"""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.telemetry.appinsights import (
    MAX_ATTRIBUTE_CHARS,
    MAX_ATTRIBUTES,
    AppInsightsTelemetry,
)

SEMCONV_FLAG_ATTRIBUTES = (
    "feature_flag.key",
    "feature_flag.provider.name",
    "feature_flag.result.variant",
    "feature_flag.result.reason",
    "feature_flag.context.id",
    "feature_flag.set.id",
)

SEMCONV_GENAI_ATTRIBUTES = (
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
)


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.events: list[tuple[str, dict]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append((name, attributes or {}))

    def set_status(self, status: Any) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, FakeSpan]] = []

    def start_as_current_span(self, name: str, kind: Any = None) -> FakeSpan:
        span = FakeSpan()
        self.spans.append((name, span))
        return span


@pytest.fixture
def wired() -> tuple[AppInsightsTelemetry, FakeTracer]:
    telemetry = AppInsightsTelemetry()
    tracer = FakeTracer()
    telemetry._tracer = tracer
    telemetry._configured = True
    return telemetry, tracer


# -- attribute names are sacred ---------------------------------------------


def test_semconv_attribute_names_are_passed_through_verbatim(wired) -> None:
    telemetry, tracer = wired
    attributes = {
        **{name: f"value-of-{name}" for name in SEMCONV_FLAG_ATTRIBUTES},
        **{name: f"value-of-{name}" for name in SEMCONV_GENAI_ATTRIBUTES},
    }
    telemetry.emit({"name": "flag.evaluate", "attributes": attributes})

    _, span = tracer.spans[0]
    for name in (*SEMCONV_FLAG_ATTRIBUTES, *SEMCONV_GENAI_ATTRIBUTES):
        assert name in span.attributes, f"{name} was dropped or renamed"
        assert span.attributes[name] == f"value-of-{name}"


def test_flat_spine_shaped_spans_keep_their_semconv_names(wired) -> None:
    """The spine's otel-file default emits FLAT spans, not nested attributes.

    Regression guard: an earlier version namespaced every top-level key under
    ``adlc.``, which silently turned ``feature_flag.key`` into
    ``adlc.feature_flag.key`` and broke the convention it claims to preserve.
    """
    telemetry, tracer = wired
    telemetry.emit({
        "name": "feature_flag.evaluation",
        "feature_flag.key": "adlc.exp.a1b2",
        "feature_flag.provider.name": "flagd-file",
        "feature_flag.result.variant": "treatment",
        "feature_flag.result.reason": "targeting_match",
        "feature_flag.context.id": "ctx-1",
        "feature_flag.set.id": "adlc",
        "runId": "2026-08-19-a1b2",
    })

    _, span = tracer.spans[0]
    for name in SEMCONV_FLAG_ATTRIBUTES:
        assert name in span.attributes, f"{name} was renamed or dropped"
        assert f"adlc.{name}" not in span.attributes
    # Non-dotted ADLC metadata is still namespaced, so it cannot collide.
    assert span.attributes["adlc.runId"] == "2026-08-19-a1b2"


def test_adapter_is_signature_compatible_with_the_spine_default(wired) -> None:
    """It must be a drop-in for OtelFileTelemetry's convenience builders."""
    import inspect

    from adlc.adapters.telemetry.otel_file import OtelFileTelemetry

    for method in ("emit", "emit_flag_evaluation", "emit_agent_invocation"):
        ours = inspect.signature(getattr(AppInsightsTelemetry, method))
        theirs = inspect.signature(getattr(OtelFileTelemetry, method))
        assert ours == theirs, f"{method} signature drifted from the spine default"


def test_emit_flag_evaluation_matches_the_spine_wire_shape(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit_flag_evaluation(
        key="adlc.exp.a1b2", variant="treatment", value=True, reason="TARGETING_MATCH",
        provider="flagd-file", context_id="ctx-1", flag_set_id="adlc",
    )

    name, span = tracer.spans[0]
    assert name == "feature_flag.evaluation"          # the spec says MUST
    assert span.attributes["feature_flag.key"] == "adlc.exp.a1b2"
    assert span.attributes["feature_flag.result.variant"] == "treatment"
    assert span.attributes["feature_flag.result.reason"] == "targeting_match"
    assert span.attributes["feature_flag.set.id"] == "adlc"


def test_emit_agent_invocation_uses_gen_ai_names(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit_agent_invocation(agent="adversarial-1", model="gpt-4o",
                                    tokens_in=120, tokens_out=45)

    _, span = tracer.spans[0]
    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["gen_ai.agent.name"] == "adversarial-1"
    assert span.attributes["gen_ai.usage.input_tokens"] == 120
    assert span.attributes["gen_ai.usage.output_tokens"] == 45


def test_superseded_names_are_not_silently_rewritten(wired) -> None:
    """``gen_ai.system`` is superseded, but rewriting it would desync the evidence."""
    telemetry, tracer = wired
    telemetry.emit({"name": "chat", "attributes": {"gen_ai.system": "azure.ai.openai"}})

    _, span = tracer.spans[0]
    assert span.attributes["gen_ai.system"] == "azure.ai.openai"
    assert "gen_ai.provider.name" not in span.attributes


def test_feature_flag_evaluation_event_keeps_its_name_and_attributes(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit({
        "name": "checkout",
        "events": [{"name": "feature_flag.evaluation",
                    "attributes": {"feature_flag.key": "adlc.exp.a1b2",
                                   "feature_flag.result.variant": "treatment"}}],
    })

    _, span = tracer.spans[0]
    name, attributes = span.events[0]
    assert name == "feature_flag.evaluation"
    assert attributes["feature_flag.key"] == "adlc.exp.a1b2"
    assert attributes["feature_flag.result.variant"] == "treatment"


# -- value coercion ----------------------------------------------------------


def test_values_are_coerced_to_otel_legal_types(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit({"name": "s", "attributes": {
        "str": "text", "int": 7, "float": 1.5, "bool": True,
        "homogeneous": ["a", "b"],
        "nested": {"a": 1},
        "mixed": [1, "two"],
        "none": None,
    }})

    _, span = tracer.spans[0]
    assert span.attributes["str"] == "text"
    assert span.attributes["int"] == 7
    assert span.attributes["float"] == 1.5
    assert span.attributes["bool"] is True
    assert span.attributes["homogeneous"] == ["a", "b"]
    for key in ("nested", "mixed", "none"):
        assert isinstance(span.attributes[key], str), f"{key} should be stringified, not dropped"


def test_long_values_are_truncated_not_discarded(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit({"name": "s", "attributes": {"huge": "x" * (MAX_ATTRIBUTE_CHARS * 2)}})
    _, span = tracer.spans[0]
    assert len(span.attributes["huge"]) == MAX_ATTRIBUTE_CHARS


def test_attribute_flood_is_capped_and_the_cap_is_reported(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit({"name": "s",
                    "attributes": {f"k{i}": i for i in range(MAX_ATTRIBUTES + 50)}})

    _, span = tracer.spans[0]
    assert len(span.attributes) <= MAX_ATTRIBUTES + 1
    assert span.attributes["adlc.telemetry.truncated"] is True


def test_top_level_extras_are_kept_under_an_adlc_prefix(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit({"name": "s", "runId": "2026-08-19-a1b2",
                    "traceId": "abc", "startTime": "2026-08-19T00:00:00Z"})

    _, span = tracer.spans[0]
    assert span.attributes["adlc.runId"] == "2026-08-19-a1b2"
    assert span.attributes["adlc.span.trace_id"] == "abc"
    assert span.attributes["adlc.span.start_time"] == "2026-08-19T00:00:00Z"


def test_span_name_defaults_when_absent(wired) -> None:
    telemetry, tracer = wired
    telemetry.emit({"attributes": {"feature_flag.key": "k"}})
    assert tracer.spans[0][0] == "adlc.span"


# -- degradation -------------------------------------------------------------


def test_emit_never_raises_and_disables_itself(wired) -> None:
    telemetry, _ = wired

    class Exploding:
        def start_as_current_span(self, *args, **kwargs):
            raise RuntimeError("Azure Monitor is unhappy")

    telemetry._tracer = Exploding()
    telemetry.emit({"name": "s"})    # must not raise

    assert telemetry.disabled_reason is not None
    assert "Azure Monitor is unhappy" in telemetry.disabled_reason


def test_a_disabled_adapter_stops_emitting(wired) -> None:
    telemetry, tracer = wired
    telemetry._disabled_reason = "already broken"
    telemetry.emit({"name": "s"})
    assert tracer.spans == []


def test_emit_without_a_connection_string_disables_rather_than_raising() -> None:
    telemetry = AppInsightsTelemetry()
    telemetry.emit({"name": "s"})    # no env, no SDK configured
    assert telemetry.disabled_reason is not None
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" in telemetry.disabled_reason


def test_flush_is_safe_without_configuration() -> None:
    assert AppInsightsTelemetry().flush() in (True, False)
