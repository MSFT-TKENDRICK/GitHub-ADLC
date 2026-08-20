"""ADLC configuration and adapter registry.

Adapter selection order (plan section 4.5):
    1. explicit ``config.yaml`` override
    2. first *detected* adapter, in registration order
    3. the documented spine default (always credential-free)

A missing optional adapter is never an error -- it becomes a ``not_run`` gate
with a reason. A missing **required** adapter fails closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import yaml

from adlc.ports import AdapterKind

ADLC_DIR = ".adlc"

#: Spine defaults. Every one of these is credential-free and ships in the spine.
SPINE_DEFAULTS: dict[str, str] = {
    "agents": "fake",
    "taskstore": "sqlite",
    "evals": "deterministic",
    "evidence": "local",
    "flags": "flagd-file",
    "telemetry": "otel-file",
}

#: Gates that are required in each profile. `required + not_run` => FAIL.
PROFILE_REQUIRED_GATES: dict[str, tuple[str, ...]] = {
    "minimal": ("tests", "secrets_local", "deps_local", "evidence_completeness"),
    "full": (
        "tests", "secrets_local", "deps_local", "evidence_completeness",
        "security", "code_quality", "evals", "governance",
        "adversarial_review", "evidence_review",
    ),
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "profile": "minimal",
    "adapters": {},
    "limits": {
        "maxParallel": 4,
        "maxInnerIterations": 2,
        "maxOuterIterations": 1,
        "maxTurns": 200,
        "maxAiCredits": 500,
    },
    "gates": {"required": None, "optional": []},
    "qualify": {"minScore": 50},
    "eval": {"threshold": 0.7},
}


@dataclass
class Config:
    """Resolved ADLC configuration for a repository."""

    root: Path
    profile: str = "minimal"
    adapters: dict[str, str] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    gates: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- paths ------------------------------------------------------------
    @property
    def adlc_dir(self) -> Path:
        return self.root / ADLC_DIR

    @property
    def runs_dir(self) -> Path:
        return self.adlc_dir / "runs"

    @property
    def decisions_dir(self) -> Path:
        return self.root / "docs" / "decisions"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    # -- gates ------------------------------------------------------------
    def required_gates(self) -> tuple[str, ...]:
        override = (self.gates or {}).get("required")
        if override:
            return tuple(override)
        return PROFILE_REQUIRED_GATES.get(self.profile, PROFILE_REQUIRED_GATES["minimal"])

    def is_required(self, gate_id: str) -> bool:
        return gate_id in self.required_gates()

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, root: Path | str | None = None) -> Config:
        root = Path(root or find_repo_root()).resolve()
        data = dict(DEFAULT_CONFIG)
        cfg_path = root / ADLC_DIR / "config.yaml"
        if cfg_path.is_file():
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            data = _deep_merge(data, loaded)
        if env := os.environ.get("ADLC_PROFILE"):
            data["profile"] = env
        return cls(
            root=root,
            profile=data.get("profile", "minimal"),
            adapters=data.get("adapters", {}) or {},
            limits=data.get("limits", {}) or {},
            gates=data.get("gates", {}) or {},
            raw=data,
        )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up to the nearest git repo root, else use cwd."""
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return cur


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


def load_adapters(kind: AdapterKind) -> dict[str, type]:
    """Load all adapters registered under ``adlc.<kind>``.

    A broken third-party adapter must never crash the spine, so import errors
    are swallowed and simply make that adapter undiscoverable.
    """
    found: dict[str, type] = {}
    for ep in entry_points(group=f"adlc.{kind}"):
        try:
            found[ep.name] = ep.load()
        except Exception:  # noqa: BLE001 - a bad leaf must not break the spine
            continue
    return found


def detect_all(cfg: Config, kind: AdapterKind) -> dict[str, tuple[bool, str]]:
    results: dict[str, tuple[bool, str]] = {}
    for name, cls in load_adapters(kind).items():
        try:
            results[name] = cls.detect(cfg)
        except Exception as exc:  # noqa: BLE001
            results[name] = (False, f"detect() raised: {exc}")
    return results


def select_adapter(cfg: Config, kind: AdapterKind, override: str | None = None) -> Any:
    """Resolve one adapter instance for ``kind``.

    Raises ``LookupError`` only when even the spine default is missing, which
    means the installation itself is broken.
    """
    adapters = load_adapters(kind)
    if not adapters:
        raise LookupError(f"no adapters registered for kind '{kind}'")

    wanted = override or cfg.adapters.get(kind)
    if wanted:
        if wanted not in adapters:
            raise LookupError(f"adapter '{wanted}' not registered for kind '{kind}'")
        return adapters[wanted]()

    for name, cls in adapters.items():
        if name == SPINE_DEFAULTS.get(kind):
            continue
        try:
            available, _ = cls.detect(cfg)
        except Exception:  # noqa: BLE001
            available = False
        if available:
            return cls()

    default_name = SPINE_DEFAULTS.get(kind)
    if default_name and default_name in adapters:
        return adapters[default_name]()
    return next(iter(adapters.values()))()


def capabilities(cfg: Config) -> dict[str, Any]:
    """Full capability probe -- the body of ``capabilities.json``."""
    kinds: tuple[AdapterKind, ...] = (
        "agents", "taskstore", "evals", "evidence",
        "flags", "telemetry", "gate", "daytwo", "export",
    )
    out: dict[str, Any] = {"profile": cfg.profile, "kinds": {}, "selected": {}}
    for kind in kinds:
        detections = detect_all(cfg, kind)
        out["kinds"][kind] = {
            name: {"available": ok, "reason": reason}
            for name, (ok, reason) in detections.items()
        }
        if kind in SPINE_DEFAULTS:
            try:
                out["selected"][kind] = type(select_adapter(cfg, kind)).__name__
            except LookupError as exc:
                out["selected"][kind] = f"ERROR: {exc}"
    return out
