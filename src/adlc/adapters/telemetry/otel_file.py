"""File-backed OpenTelemetry emitter -- the spine's default telemetry sink.

Writes OTel-shaped spans as JSONL so evidence is capturable with no collector,
no network and no account. Attribute names follow the current semantic
conventions so a real exporter (e.g. Application Insights) can consume the same
spans unchanged.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from adlc.config import Config


class OtelFileTelemetry:
    name = "otel-file"
    kind = "telemetry"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get("ADLC_OTEL_FILE", ".adlc/otel.jsonl"))
        self._lock = threading.Lock()

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "built-in file exporter (no collector required)"

    def bind(self, path: Path) -> None:
        self.path = path

    def emit(self, span: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in span.items() if v is not None}
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    # -- convenience builders ---------------------------------------------
    def emit_flag_evaluation(
        self, *, key: str, variant: str, value: Any, reason: str,
        provider: str, context_id: str | None = None, flag_set_id: str | None = None,
    ) -> None:
        """Emit a `feature_flag.evaluation` event using current semconv names."""
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
        """Emit a `gen_ai.*` span for an agent invocation."""
        self.emit({
            "name": operation,
            "gen_ai.operation.name": operation,
            "gen_ai.agent.name": agent,
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": tokens_in,
            "gen_ai.usage.output_tokens": tokens_out,
        })
