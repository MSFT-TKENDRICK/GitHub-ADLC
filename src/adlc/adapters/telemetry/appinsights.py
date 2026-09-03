"""Application Insights ``Telemetry`` adapter (OpenTelemetry → Azure Monitor).

Tier: **documented + disabled example** (``docs/PLAN.md`` §7). Without
``APPLICATIONINSIGHTS_CONNECTION_STRING`` this adapter reports unavailable and
the spine's credential-free ``otel-file`` default takes over untouched.

Design rule: **pass attributes through verbatim.**
--------------------------------------------------
The framework, not this adapter, decides attribute names. We do not rename,
prefix, drop or "helpfully correct" anything, because the OpenTelemetry
semantic conventions this framework targets are still moving and a translation
layer here would silently desync the exported data from the spec the rest of
ADLC is written against.

Verified against the OpenTelemetry semantic conventions (checked 2026-08-19):

* Feature-flag attributes — status **Release Candidate**, carried on a
  ``feature_flag.evaluation`` event (the spec says the event name MUST be
  exactly that): ``feature_flag.key`` (Required),
  ``feature_flag.provider.name``, ``feature_flag.result.variant``,
  ``feature_flag.result.reason``, ``feature_flag.context.id``,
  ``feature_flag.set.id``. Also defined: ``feature_flag.result.value``,
  ``feature_flag.version``, ``feature_flag.error.message``, ``error.type``.
  The older ``feature_flag.provider_name`` and ``feature_flag.variant`` are
  **superseded** by the ``.provider.name`` / ``.result.variant`` spellings.
  Source: https://opentelemetry.io/docs/specs/semconv/feature-flags/feature-flags-events/

* GenAI attributes — status **Development**, and the conventions have moved to
  https://github.com/open-telemetry/semantic-conventions-genai . Current names
  include ``gen_ai.operation.name`` (Required), ``gen_ai.provider.name``
  (Required), ``gen_ai.request.model``, ``gen_ai.response.model``,
  ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``.

  **Note the rename:** ``gen_ai.system`` has been superseded by
  ``gen_ai.provider.name``. This adapter does **not** rewrite it — if the spine
  emits ``gen_ai.system`` that is what lands in Azure Monitor. Passing it
  through unchanged is the honest behaviour; silently rewriting would make the
  exported data disagree with the run's own JSONL evidence.

Azure Monitor specifics, verified 2026-08-19 against
https://learn.microsoft.com/en-us/azure/azure-monitor/app/opentelemetry-configuration?tabs=python :

* ``from azure.monitor.opentelemetry import configure_azure_monitor`` — confirmed.
* ``configure_azure_monitor(connection_string="…")`` — confirmed kwarg.
* ``APPLICATIONINSIGHTS_CONNECTION_STRING`` — confirmed env var name.

UNVERIFIED, stated rather than guessed:

* Azure Monitor documents no attribute-count or attribute-length cap for values
  flowing into ``customDimensions``. We searched ``opentelemetry-enable``,
  ``opentelemetry-configuration`` and ``opentelemetry-add-modify`` and found
  none. This adapter therefore applies **its own** conservative caps
  (:data:`MAX_ATTRIBUTES`, :data:`MAX_ATTRIBUTE_CHARS`) so a pathological span
  cannot blow up an export, and records that it did so via
  ``adlc.telemetry.truncated``. Those numbers are ours, not Microsoft's.
* The docs show the standard OpenTelemetry API is used for custom spans after
  ``configure_azure_monitor()``, but we could not quote a line showing
  ``get_tracer(__name__)`` verbatim. That call is the OTel-standard pattern.
"""

from __future__ import annotations

import os
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config

CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"

#: Modules that must import for this adapter to work. Probed with
#: ``importlib.util.find_spec`` so ``detect()`` never executes package code.
REQUIRED_MODULES: tuple[str, ...] = ("azure.monitor.opentelemetry", "opentelemetry.trace")

#: Our own guard rails — see the module docstring. Not an Azure documented limit.
MAX_ATTRIBUTES = 128
MAX_ATTRIBUTE_CHARS = 8_192

#: Span-dict keys that are structure, not attributes.
_RESERVED_KEYS = frozenset({
    "name", "attributes", "kind", "status", "events", "links", "resource",
    "startTime", "start_time", "endTime", "end_time",
    "traceId", "trace_id", "spanId", "span_id", "parentSpanId", "parent_span_id",
})

_OTEL_SPAN_KINDS = {
    "INTERNAL": "INTERNAL", "SERVER": "SERVER", "CLIENT": "CLIENT",
    "PRODUCER": "PRODUCER", "CONSUMER": "CONSUMER",
}


class AppInsightsTelemetry:
    """Export ADLC's OTel-shaped span dicts to Application Insights.

    Implements :class:`adlc.ports.Telemetry`. Configuration is lazy: nothing is
    imported or configured until the first :meth:`emit`, so importing this
    module is free and the credential-free conformance suite never touches
    Azure.
    """

    name = "appinsights"
    kind = "telemetry"

    def __init__(self, connection_string: str | None = None, tracer_name: str = "adlc") -> None:
        self._connection_string = connection_string
        self._tracer_name = tracer_name
        self._tracer: Any = None
        self._configured = False
        self._disabled_reason: str | None = None

    # -- Adapter protocol -------------------------------------------------

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, non-raising, no network.

        Checks only two things: the connection string is set, and the SDK is
        importable. ``find_spec`` locates the module without importing it, so
        this stays fast and free of side effects.
        """
        try:
            if not (os.environ.get(CONNECTION_STRING_ENV) or "").strip():
                return False, (
                    f"{CONNECTION_STRING_ENV} is not set — falling back to the spine's "
                    "credential-free otel-file telemetry"
                )
            missing = [m for m in REQUIRED_MODULES if not _module_present(m)]
            if missing:
                return False, (
                    f"{CONNECTION_STRING_ENV} is set but {', '.join(missing)} "
                    "is not importable - install `adlc[azure]` "
                    "(pip install azure-monitor-opentelemetry)"
                )
            return True, (
                f"{CONNECTION_STRING_ENV} is set and azure-monitor-opentelemetry is importable"
            )
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"Application Insights probe failed: {exc}"

    # -- Telemetry protocol -----------------------------------------------

    def emit(self, span: dict[str, Any]) -> None:
        """Emit one OTel-shaped span. Never raises.

        Telemetry is observability, not control flow — a broken exporter must
        not fail a run. On any error the adapter disables itself, records the
        reason on :attr:`disabled_reason`, and returns.

        Accepted span shape (all optional except ``name``)::

            {"name": "...", "kind": "INTERNAL|CLIENT|...",
             "attributes": {"feature_flag.key": "...", "gen_ai.provider.name": "..."},
             "status": {"code": "OK|ERROR", "description": "..."},
             "events": [{"name": "feature_flag.evaluation", "attributes": {...}}]}

        ``startTime``/``start_time`` and ``endTime``/``end_time`` are accepted
        and recorded as attributes rather than used to back-date the span:
        ``configure_azure_monitor`` exports through the live tracer, and
        fabricating timestamps on a live span would misrepresent when the work
        happened.
        """
        if self._disabled_reason is not None:
            return
        try:
            tracer = self._ensure_tracer()
            if tracer is None:
                return
            name = str(span.get("name") or "adlc.span")
            attributes = self._flatten(span)
            with tracer.start_as_current_span(
                name, kind=self._span_kind(span.get("kind"))
            ) as otel_span:
                for key, value in attributes.items():
                    otel_span.set_attribute(key, value)
                for event in _as_dicts(span.get("events")):
                    otel_span.add_event(
                        str(event.get("name") or "event"),
                        attributes=_sanitize(event.get("attributes") or {}),
                    )
                self._apply_status(otel_span, span.get("status"))
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
            self._disabled_reason = f"{type(exc).__name__}: {exc}"

    # -- Introspection ----------------------------------------------------

    @property
    def disabled_reason(self) -> str | None:
        """Why this adapter stopped exporting, or ``None`` while healthy."""
        return self._disabled_reason

    # -- Convenience builders ---------------------------------------------
    #
    # Signature-compatible with the spine default so this adapter is a drop-in
    # replacement. The span bodies are byte-for-byte the ones `otel_file`
    # produces -- deliberately duplicated rather than imported, so that neither
    # sink can silently change the wire shape of the other.

    def emit_flag_evaluation(
        self, *, key: str, variant: str, value: Any, reason: str,
        provider: str, context_id: str | None = None, flag_set_id: str | None = None,
    ) -> None:
        """Emit a ``feature_flag.evaluation`` event using current semconv names.

        The OTel spec states the event name MUST be exactly
        ``feature_flag.evaluation``.
        """
        self.emit({
            "name": "feature_flag.evaluation",
            "feature_flag.key": key,
            "feature_flag.provider.name": provider,
            "feature_flag.result.variant": variant,
            "feature_flag.result.value": value,
            "feature_flag.result.reason": reason.lower(),
            "feature_flag.context.id": context_id,
            "feature_flag.set.id": flag_set_id,
        })

    def emit_agent_invocation(
        self, *, agent: str, operation: str = "invoke_agent",
        model: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
    ) -> None:
        """Emit a ``gen_ai.*`` span for an agent invocation."""
        self.emit({
            "name": operation,
            "gen_ai.operation.name": operation,
            "gen_ai.agent.name": agent,
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": tokens_in,
            "gen_ai.usage.output_tokens": tokens_out,
        })

    def flush(self, timeout_millis: int = 5_000) -> bool:
        """Best-effort flush of the tracer provider. Never raises."""
        try:
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            force_flush = getattr(provider, "force_flush", None)
            return bool(force_flush(timeout_millis)) if callable(force_flush) else False
        except Exception:  # noqa: BLE001
            return False

    # -- Internals --------------------------------------------------------

    def _ensure_tracer(self) -> Any:
        if self._tracer is not None:
            return self._tracer
        if self._configured:
            return None

        connection_string = self._connection_string or os.environ.get(CONNECTION_STRING_ENV)
        if not (connection_string or "").strip():
            self._disabled_reason = f"{CONNECTION_STRING_ENV} is not set"
            return None

        # Imported lazily so the module is importable without azure[monitor].
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import trace

        configure_azure_monitor(connection_string=connection_string)
        self._configured = True
        self._tracer = trace.get_tracer(self._tracer_name)
        return self._tracer

    def _flatten(self, span: dict[str, Any]) -> dict[str, Any]:
        """Collect attributes, verbatim, plus a little run context.

        The spine's default (:class:`~adlc.adapters.telemetry.otel_file.OtelFileTelemetry`)
        emits **flat** spans -- semconv attributes sit at the top level next to
        ``name``, not inside an ``attributes`` map::

            {"name": "feature_flag.evaluation",
             "feature_flag.key": "...", "feature_flag.result.variant": "..."}

        Both shapes are accepted. The rule that keeps them straight is simple
        and, crucially, does not need a hard-coded list of semconv names that
        would rot: **a top-level key containing a dot is an OTel attribute and
        is passed through verbatim**; a top-level key without a dot is ADLC
        metadata and is namespaced under ``adlc.``. That way
        ``feature_flag.key`` and ``gen_ai.provider.name`` survive untouched
        while ``runId`` becomes ``adlc.runId``.
        """
        merged: dict[str, Any] = dict(span.get("attributes") or {})

        for key in ("traceId", "trace_id", "spanId", "span_id",
                    "parentSpanId", "parent_span_id",
                    "startTime", "start_time", "endTime", "end_time"):
            if (value := span.get(key)) not in (None, ""):
                merged.setdefault(f"adlc.span.{_snake(key)}", value)

        for key, value in span.items():
            if key in _RESERVED_KEYS or value is None or value in ("", [], {}):
                continue
            # A dotted key is an OTel semantic-convention attribute. Never rename.
            merged.setdefault(key if "." in key else f"adlc.{key}", value)

        sanitized = _sanitize(merged)
        if len(sanitized) < len(merged):
            sanitized["adlc.telemetry.truncated"] = True
        return sanitized

    @staticmethod
    def _span_kind(value: Any) -> Any:
        try:
            from opentelemetry.trace import SpanKind

            name = _OTEL_SPAN_KINDS.get(str(value or "INTERNAL").upper(), "INTERNAL")
            return getattr(SpanKind, name, SpanKind.INTERNAL)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _apply_status(otel_span: Any, status: Any) -> None:
        if not status:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            code = status.get("code") if isinstance(status, dict) else status
            description = status.get("description") if isinstance(status, dict) else None
            mapped = StatusCode.ERROR if str(code).upper() == "ERROR" else StatusCode.OK
            otel_span.set_status(Status(mapped, description))
        except Exception:  # noqa: BLE001 - status is decoration, never fatal
            return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_present(dotted: str) -> bool:
    """True if ``dotted`` is importable, without importing it."""
    try:
        return find_spec(dotted) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _sanitize(attributes: Any) -> dict[str, Any]:
    """Coerce to OTel-legal attribute values, keeping **keys verbatim**.

    OTel attribute values must be ``str``/``bool``/``int``/``float`` or a
    homogeneous sequence of those. Anything else is JSON-ish stringified rather
    than dropped, so no information disappears without a trace.
    """
    if not isinstance(attributes, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if len(out) >= MAX_ATTRIBUTES:
            break
        out[str(key)] = _coerce(value)
    return out


def _coerce(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_ATTRIBUTE_CHARS]
    if isinstance(value, (list, tuple)):
        items = [_coerce(v) for v in value]
        if items and all(isinstance(v, type(items[0])) for v in items):
            return items
        return str(items)[:MAX_ATTRIBUTE_CHARS]
    return str(value)[:MAX_ATTRIBUTE_CHARS]


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _snake(name: str) -> str:
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)
