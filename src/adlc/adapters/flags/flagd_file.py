"""flagd file provider -- the spine's credential-free default flag backend.

Writes a `flagd`-schema JSON file (the CNCF OpenFeature reference format) with
one variant per candidate implementation, and evaluates it in-process so the
conformance suite needs no daemon, no network and no vendor account.

Telemetry uses the *current* OpenTelemetry feature-flag semantic conventions:
``feature_flag.key``, ``feature_flag.provider.name`` (dotted),
``feature_flag.result.variant`` / ``.value`` / ``.reason``,
``feature_flag.context.id``, ``feature_flag.set.id``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import FlagResult, Run

FLAGD_SCHEMA = "https://flagd.dev/schema/v0/flags.json"


class FlagdFileProvider:
    name = "flagd-file"
    kind = "flags"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._flags: dict[str, Any] = {}

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "built-in flagd file provider (no daemon or account required)"

    # -- authoring ---------------------------------------------------------
    def materialize(self, run: Run) -> Path:
        """Emit ``flags.flagd.json`` with one variant per candidate."""
        run_id = run.get("runId", "run")
        variants = run.get("variants") or []
        flag_key = f"adlc.exp.{run_id}"

        variant_map: dict[str, Any] = {v["key"]: v["key"] for v in variants} or {
            "control": "control"
        }
        default = next(
            (v["key"] for v in variants if v.get("role") == "control"),
            next(iter(variant_map)),
        )

        document = {
            "$schema": FLAGD_SCHEMA,
            "flags": {
                flag_key: {
                    "state": "ENABLED",
                    "variants": variant_map,
                    "defaultVariant": default,
                    "metadata": {"version": "1", "adlcRunId": run_id},
                }
            },
            "metadata": {"flagSetId": f"adlc/{run_id}"},
        }

        target = self.path or Path(".adlc") / "runs" / run_id / "flags.flagd.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.path = target
        self._flags = document["flags"]
        return target

    # -- evaluation --------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if self._flags:
            return self._flags
        if self.path and self.path.is_file():
            self._flags = json.loads(self.path.read_text(encoding="utf-8")).get("flags", {})
        return self._flags

    def evaluate(self, key: str, ctx: dict[str, Any]) -> FlagResult:
        flags = self._load()
        definition = flags.get(key)
        if definition is None:
            return {"key": key, "value": None, "variant": "", "reason": "FLAG_NOT_FOUND"}
        if definition.get("state") != "ENABLED":
            return {"key": key, "value": None, "variant": "", "reason": "DISABLED"}

        variants = definition.get("variants", {})
        requested = ctx.get("variant")
        if requested and requested in variants:
            return {
                "key": key, "value": variants[requested],
                "variant": requested, "reason": "TARGETING_MATCH",
            }
        default = definition.get("defaultVariant")
        return {
            "key": key, "value": variants.get(default),
            "variant": default or "", "reason": "DEFAULT",
        }

    def span_attributes(self, result: FlagResult, ctx: dict[str, Any]) -> dict[str, Any]:
        """OTel feature-flag semconv attributes for this evaluation."""
        return {
            "feature_flag.key": result.get("key"),
            "feature_flag.provider.name": self.name,
            "feature_flag.result.variant": result.get("variant"),
            "feature_flag.result.value": result.get("value"),
            "feature_flag.result.reason": (result.get("reason") or "").lower(),
            "feature_flag.context.id": ctx.get("targetingKey") or ctx.get("id"),
            "feature_flag.set.id": (self._flags and "adlc") or None,
        }
