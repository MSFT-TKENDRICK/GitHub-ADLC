"""Lighthouse CI evidence collector (L6).

Runs `lhci` against the candidate build, emits a redacted ``lighthouse.json``
and a normalised ``lighthouse-measurements.json`` whose ``measurements[]``
entries are byte-compatible with ``schemas/evidence-review-pack.schema.json``.

This module also hosts the **shared L6 toolkit** (budget loading, JSON-pointer
extraction, redaction, subprocess execution, measurement emission) used by
:mod:`adlc.adapters.evidence.k6` and :mod:`adlc.adapters.evidence.axe`.
L6's exclusive paths are exactly three modules, so the toolkit lives in the
first of them rather than in a fourth file; the blast radius is contained to
L6 because ``adlc.config.load_adapters`` swallows ``ImportError``.

Nothing here is ever allowed to fabricate a measurement. If a tool did not run,
could not be parsed, or did not report a metric, the metric is emitted in
``unmeasured[]`` with ``status: "not_run"`` and a specific cause -- never as a
zero, a default, or a silent pass.

See ``docs/evidence.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from adlc.ports import ArtifactRef, Run

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = [
    "LIGHTHOUSE_CATALOGUE",
    "UNMEASURED_SCHEMA_VERSION",
    "LighthouseCollector",
    "Measurement",
    "MetricSpec",
    "ToolResult",
    "Unmeasured",
    "aggregate_values",
    "artifact_ref",
    "build_measurements",
    "clear_previous",
    "coerce_number",
    "collector_options",
    "evaluate_budget",
    "extract_lighthouse",
    "find_executable",
    "find_node_package",
    "json_pointer",
    "load_benchmarks",
    "metrics_for",
    "read_json",
    "redact",
    "redact_text",
    "redact_url",
    "resolve_run_dir",
    "run_tool",
    "safe_config",
    "sha256_file",
    "target_urls",
    "timeout_for",
    "write_json",
    "write_measurement_files",
    "write_measurements",
    "write_unmeasured",
]

#: Sidecar document recording budgets that could not be measured.
UNMEASURED_SCHEMA_VERSION = "adlc-unmeasured/v1"
REDACTED = "[REDACTED]"
DEFAULT_TIMEOUT_SECONDS = 600
MAX_CAPTURED_OUTPUT = 8_000

#: Machine-readable reasons a metric has no value. All map to ``not_run``.
Cause = Literal[
    "tool_unavailable",
    "tool_failed",
    "tool_timeout",
    "output_missing",
    "output_unreadable",
    "metric_absent",
    "metric_unmapped",
]


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class MetricSpec(TypedDict, total=False):
    """One entry of ``metrics[]`` in ``benchmarks.yaml``.

    Mirrors ``schemas/benchmarks.schema.json``.
    """

    id: str
    collector: str
    budget: float
    direction: str
    unit: str
    description: str
    source: str
    scale: float
    aggregate: str
    optional: bool


class Measurement(TypedDict):
    """Normalised measurement.

    Key-for-key identical to ``evidence-review-pack.schema.json``
    ``#/properties/measurements/items`` (which is ``additionalProperties:
    false``), so the spine can copy these straight into the sanitised pack.
    """

    metricId: str
    value: float
    budget: float
    passed: bool
    collector: str
    artifactSha256: str


class Unmeasured(TypedDict, total=False):
    """A budget that could not be measured. Never a pass, never a value."""

    metricId: str
    collector: str
    budget: float
    direction: str
    status: Literal["not_run"]
    cause: str
    reason: str


class ToolResult(TypedDict, total=False):
    ran: bool
    exitCode: int | None
    stdout: str
    stderr: str
    reason: str
    cause: str
    command: list[str]
    durationSeconds: float
    scanErrors: list[Any]


# ---------------------------------------------------------------------------
# Hashing / artifacts
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for ``path``. Raises only on I/O."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def artifact_ref(path: Path, run_dir: Path | None, kind: str, mime: str) -> ArtifactRef:
    """Build a hash-verified :class:`~adlc.ports.ArtifactRef` for ``path``.

    ``ArtifactRef.path`` is relative to the run directory (``evidence/<variant>/
    lighthouse.json``) to match ``run.json``'s ``artifacts[]``.
    """
    sha, size = sha256_file(path)
    rel = path.as_posix()
    if run_dir is not None:
        try:
            rel = path.resolve().relative_to(run_dir.resolve()).as_posix()
        except (ValueError, OSError):
            rel = path.as_posix()
    return {"path": rel, "kind": kind, "mimeType": mime, "sha256": sha, "bytes": size}


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def read_json(path: Path) -> Any | None:
    """Parse ``path`` as JSON. Returns ``None`` instead of raising."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def clear_previous(out: Path, patterns: tuple[str, ...]) -> None:
    """Delete this collector's outputs from an earlier attempt.

    Evidence must describe *this* run. A stale ``lighthouse.json`` left behind by
    a previous attempt would otherwise be re-hashed and presented as current.
    """
    for pattern in patterns:
        for stale in sorted(out.glob(pattern)):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)


def json_pointer(doc: Any, pointer: str) -> Any | None:
    """Resolve an RFC 6901 JSON Pointer. Returns ``None`` when absent."""
    if not pointer or not pointer.startswith("/"):
        return None
    node = doc
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def coerce_number(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a real number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, str):
        try:
            return coerce_number(float(value.strip()))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Redaction
#
# HAR files, Lighthouse network audits and axe HTML snippets routinely carry
# bearer tokens, session cookies and signed URLs. Evidence is uploaded as a
# build artifact and shown to reviewers, so it is scrubbed on the way out.
# ---------------------------------------------------------------------------

SENSITIVE_KEY_RE = re.compile(
    r"^(?:x-)?(?:authorization|proxy-authorization|cookie|cookies|set-cookie|"
    r"api[-_]?key|auth[-_]?token|access[-_]?token|id[-_]?token|refresh[-_]?token|"
    r"session[-_]?id|session[-_]?token|client[-_]?secret|secret|password|passwd|pwd|"
    r"token|bearer|credential|credentials|signature|sas|amz-security-token|"
    r"csrf[-_]?token|xsrf[-_]?token|set[-_]?cookie2)$",
    re.IGNORECASE,
)

#: Query-string parameters whose *values* are stripped from any URL we emit.
#: A pattern rather than a list: over-redacting a query value costs nothing,
#: under-redacting leaks a credential into a build artifact.
SENSITIVE_QUERY_PARAM_RE = re.compile(
    r"(?i)(?:^|[-_])(?:access[-_]?token|refresh[-_]?token|id[-_]?token|session[-_]?token|"
    r"auth[-_]?token|api[-_]?key|apikey|token|secret|password|passwd|pwd|credential|"
    r"signature|sig|sas|auth|code|key|session|sid)s?(?:$|[-_])"
)

_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{12,}")
#: Attribute names that mark an HTML attribute value as credential-bearing.
_HTML_SENSITIVE_NAME = (
    r"(?:token|secret|password|passwd|api[-_]?key|apikey|csrf|xsrf|session|"
    r"authorization|auth[-_]?token|credential|nonce)"
)
_HTML_SECRET_ATTR_RE = re.compile(
    rf"(?i)((?:data-)?[\w-]*{_HTML_SENSITIVE_NAME}[\w-]*)\s*=\s*([\"'])(?:(?!\2).){{4,}}?\2"
)
_HTML_ATTR_NAME = rf"[\"'][^\"'>]*{_HTML_SENSITIVE_NAME}[^\"'>]*[\"']"
#: ``<input name="csrf_token" value="...">`` in either attribute order. axe
#: echoes page markup verbatim, and hidden inputs are where session/CSRF values
#: live.
_HTML_NAME_THEN_VALUE_RE = re.compile(
    rf"(?i)(name\s*=\s*{_HTML_ATTR_NAME}[^>]*?value\s*=\s*)([\"'])[^\"']*([\"'])"
)
_HTML_VALUE_THEN_NAME_RE = re.compile(
    rf"(?i)(value\s*=\s*)([\"'])[^\"']*([\"'])([^>]*?name\s*=\s*{_HTML_ATTR_NAME})"
)
_SECRET_LITERAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
)
#: ``TOKEN=value`` shaped strings -- one per element of a recorded command line,
#: or a whole line of a captured tool log.
_ASSIGNMENT_RE = re.compile(
    rf"(?im)^(--?[\w-]+[\s=])?([\w.-]*{_HTML_SENSITIVE_NAME}[\w.-]*)=(?!\[REDACTED\])\S+"
)


def redact_url(value: str) -> str:
    """Strip the values of credential-bearing query parameters from a URL."""
    if "://" not in value or "?" not in value:
        return value
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return value
    if not pairs:
        return value
    scrubbed = [
        (name, REDACTED if SENSITIVE_QUERY_PARAM_RE.search(name) else val)
        for name, val in pairs
    ]
    if scrubbed == pairs:
        return value
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(scrubbed), parts.fragment)
    )


def redact_text(value: str) -> str:
    """Scrub secret-shaped substrings out of a free-text value.

    Inert base64 data URIs (Lighthouse screenshots) are returned untouched --
    they carry no credentials and scanning megabytes of base64 is both slow and
    a corruption risk.
    """
    if value.startswith("data:") and ";base64," in value[:128]:
        return value
    out = redact_url(value)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _HTML_SECRET_ATTR_RE.sub(lambda m: f'{m.group(1)}="{REDACTED}"', out)
    out = _HTML_NAME_THEN_VALUE_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(3)}", out
    )
    out = _HTML_VALUE_THEN_NAME_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(3)}{m.group(4)}", out
    )
    out = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}={REDACTED}", out)
    for pattern in _SECRET_LITERAL_RES:
        out = pattern.sub(REDACTED, out)
    return out


def _is_name_value_list(value: Any) -> bool:
    """True for HAR-shaped ``[{"name": ..., "value": ...}, ...]`` collections."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) and "name" in item and "value" in item for item in value)
    )


def redact(node: Any, _depth: int = 0) -> Any:
    """Deep-copy ``node`` with credentials removed.

    Rules (documented in ``docs/evidence.md``):

    1. Any mapping key matching :data:`SENSITIVE_KEY_RE` has its value replaced.
       When that value is a HAR-shaped ``name``/``value`` list the structure is
       kept and only the values are replaced, so reviewers still see *which*
       headers or cookies were present.
    2. HAR-shaped ``{"name": ..., "value": ...}`` pairs are redacted by name --
       this covers HAR ``headers[]``, ``cookies[]`` and ``queryString[]``.
    3. Credential-bearing URL query parameters are stripped everywhere.
    4. ``Bearer``/``Basic`` prefixes, JWTs and well-known token literals are
       replaced in any string.
    """
    if _depth > 64:
        return node
    if isinstance(node, dict):
        name = node.get("name")
        pair_is_sensitive = (
            isinstance(name, str) and "value" in node and bool(SENSITIVE_KEY_RE.match(name.strip()))
        )
        out: dict[str, Any] = {}
        for key, value in node.items():
            key_is_sensitive = isinstance(key, str) and bool(SENSITIVE_KEY_RE.match(key.strip()))
            if key_is_sensitive and _is_name_value_list(value):
                out[key] = [
                    {**redact(item, _depth + 1), "value": REDACTED} for item in value
                ]
            elif key_is_sensitive or (pair_is_sensitive and key == "value"):
                out[key] = REDACTED
            else:
                out[key] = redact(value, _depth + 1)
        return out
    if isinstance(node, list):
        return [redact(item, _depth + 1) for item in node]
    if isinstance(node, str):
        return redact_text(node)
    return node


# ---------------------------------------------------------------------------
# Config / run-directory resolution
# ---------------------------------------------------------------------------


def safe_config(out: Path) -> Config | None:
    """Load :class:`~adlc.config.Config`, never raising."""
    from adlc.config import Config as _Config

    for start in (out, Path.cwd()):
        loaded = _load_config_or_none(_Config, _find_repo_root(start))
        if loaded is not None:
            return loaded
    return None


def _load_config_or_none(config_cls: Any, root: Path) -> Any | None:
    try:
        return config_cls.load(root)
    except Exception:  # noqa: BLE001 - a broken config must never break evidence
        return None


def _resolve_or_none(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _find_repo_root(start: Path) -> Path:
    current = start.resolve() if start.exists() else Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def resolve_run_dir(run: Run, out: Path) -> Path | None:
    """Locate ``runs/<run-id>/`` given the evidence output directory.

    Per plan §4.1 ``out`` is ``runs/<run-id>/evidence/<variant>/``, so the run
    directory is normally two levels up. Falls back to matching ``runId`` and
    then to ``Config.run_dir``.
    """
    out = out if out.is_absolute() else (Path.cwd() / out)
    run_id = (run or {}).get("runId") or ""
    for candidate in (out, *out.parents):
        if candidate.name == "evidence" and candidate.parent != candidate:
            return candidate.parent
    for candidate in (out, *out.parents):
        if (run_id and candidate.name == run_id) or (
            candidate / "enrichment" / "benchmarks.yaml"
        ).is_file():
            return candidate
    if run_id:
        cfg = safe_config(out)
        if cfg is not None:
            candidate = cfg.run_dir(run_id)
            if candidate.is_dir():
                return candidate
    return None


# ---------------------------------------------------------------------------
# benchmarks.yaml
# ---------------------------------------------------------------------------


def load_benchmarks(run_dir: Path | None) -> dict[str, Any]:
    """Read ``<run>/enrichment/benchmarks.yaml``. Never raises.

    Tolerates a missing file, a bare top-level ``metrics`` list, and unknown
    keys -- strictness lives in ``schemas/benchmarks.schema.json``, not here.
    """
    if run_dir is None:
        return {"metrics": []}
    path = run_dir / "enrichment" / "benchmarks.yaml"
    if not path.is_file():
        path = run_dir / "enrichment" / "benchmarks.yml"
    if not path.is_file():
        return {"metrics": []}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {"metrics": []}
    if isinstance(doc, list):
        return {"metrics": [m for m in doc if isinstance(m, dict)]}
    if not isinstance(doc, dict):
        return {"metrics": []}
    metrics = doc.get("metrics")
    doc["metrics"] = [m for m in metrics if isinstance(m, dict)] if isinstance(metrics, list) else []
    return doc


def metrics_for(doc: dict[str, Any], collector: str) -> list[MetricSpec]:
    """Well-formed metrics owned by ``collector``, in declaration order."""
    out: list[MetricSpec] = []
    for raw in doc.get("metrics", []):
        if str(raw.get("collector", "")).strip().lower() != collector:
            continue
        budget = coerce_number(raw.get("budget"))
        metric_id = str(raw.get("id", "")).strip()
        direction = str(raw.get("direction", "")).strip().lower()
        if not metric_id or budget is None:
            continue
        if direction not in ("lower_is_better", "higher_is_better"):
            continue
        spec: MetricSpec = {
            "id": metric_id,
            "collector": collector,
            "budget": budget,
            "direction": direction,
        }
        for key in ("unit", "description", "source", "aggregate"):
            if isinstance(raw.get(key), str) and raw[key].strip():
                spec[key] = raw[key].strip()  # type: ignore[literal-required]
        scale = coerce_number(raw.get("scale"))
        if scale is not None:
            spec["scale"] = scale
        spec["optional"] = bool(raw.get("optional", False))
        out.append(spec)
    return out


def collector_options(doc: dict[str, Any], collector: str) -> dict[str, Any]:
    collectors = doc.get("collectors")
    options = collectors.get(collector) if isinstance(collectors, dict) else None
    return dict(options) if isinstance(options, dict) else {}


def timeout_for(doc: dict[str, Any], collector: str, default: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    for source in (collector_options(doc, collector), doc.get("target") or {}):
        if isinstance(source, dict):
            value = coerce_number(source.get("timeoutSeconds"))
            if value is not None and value > 0:
                return int(value)
    return default


def target_urls(doc: dict[str, Any], collector: str, run: Run | None = None) -> list[str]:
    """Resolve the URLs a browser collector should visit.

    Precedence: ``ADLC_TARGET_URL`` env var → ``collectors.<name>.urls`` →
    ``target.url`` → ``run["capabilities"]["targetUrl"]`` (the same field the
    spine's Playwright collector reads). Returns ``[]`` when no target is
    configured; the collector then reports ``not_run`` rather than guessing an
    address.
    """
    env_url = (os.environ.get("ADLC_TARGET_URL") or "").strip()
    if env_url:
        return [env_url]
    options = collector_options(doc, collector)
    urls = options.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if isinstance(urls, list):
        cleaned = [str(u).strip() for u in urls if str(u).strip()]
        if cleaned:
            return cleaned
    single = options.get("url")
    if isinstance(single, str) and single.strip():
        return [single.strip()]
    target = doc.get("target")
    if isinstance(target, dict):
        base = target.get("url")
        if isinstance(base, str) and base.strip():
            return [base.strip()]
    capability = ((run or {}).get("capabilities") or {}).get("targetUrl")
    if isinstance(capability, str) and capability.strip() and capability.strip() != "about:blank":
        return [capability.strip()]
    return []


# ---------------------------------------------------------------------------
# Budget evaluation + measurement emission
# ---------------------------------------------------------------------------


def evaluate_budget(value: float, budget: float, direction: str) -> bool:
    if direction == "higher_is_better":
        return value >= budget
    return value <= budget


def aggregate_values(values: list[float], spec: MetricSpec) -> float | None:
    """Combine per-URL values. ``worst`` is the safe default."""
    if not values:
        return None
    mode = str(spec.get("aggregate") or "worst").lower()
    higher_is_better = spec.get("direction") == "higher_is_better"
    if mode == "first":
        return values[0]
    if mode == "mean":
        return sum(values) / len(values)
    if mode == "sum":
        return sum(values)
    if mode == "best":
        return max(values) if higher_is_better else min(values)
    return min(values) if higher_is_better else max(values)


def build_measurements(
    specs: list[MetricSpec],
    values: dict[str, float | None],
    collector: str,
    artifact_sha: str,
    reasons: dict[str, tuple[str, str]],
    default_reason: tuple[str, str] | None = None,
) -> tuple[list[Measurement], list[Unmeasured]]:
    """Split declared budgets into measured and explicitly-unmeasured buckets.

    A metric only becomes a :class:`Measurement` when a real number was
    extracted from a real artifact. Everything else lands in ``unmeasured`` with
    ``status: "not_run"``.
    """
    fallback = default_reason or ("metric_absent", "metric not present in tool output")
    measured: list[Measurement] = []
    unmeasured: list[Unmeasured] = []
    for spec in specs:
        metric_id = spec["id"]
        value = values.get(metric_id)
        if value is None or not artifact_sha:
            cause, reason = reasons.get(metric_id, fallback)
            if not artifact_sha and default_reason is None and metric_id not in reasons:
                cause, reason = "output_missing", "collector produced no artifact to measure"
            unmeasured.append(
                {
                    "metricId": metric_id,
                    "collector": collector,
                    "budget": spec["budget"],
                    "direction": spec.get("direction", "lower_is_better"),
                    "status": "not_run",
                    "cause": cause,
                    "reason": reason,
                }
            )
            continue
        measured.append(
            {
                "metricId": metric_id,
                "value": value,
                "budget": spec["budget"],
                "passed": evaluate_budget(value, spec["budget"], spec.get("direction", "")),
                "collector": collector,
                "artifactSha256": artifact_sha,
            }
        )
    return measured, unmeasured


def write_measurements(out: Path, collector: str, measurements: list[Measurement]) -> Path:
    """Write ``<collector>-measurements.json`` as a **bare JSON array**.

    The spine reads ``*-measurements.json`` as a list — see
    ``adlc.stages.evidence.collect_measurements`` and
    ``adlc.adapters.evals.deterministic.DeterministicRubricRunner._measurements``
    — matching the shape its own ``local`` collector emits. Every item is
    key-for-key compatible with ``evidence-review-pack.schema.json``.
    """
    return write_json(out / f"{collector}-measurements.json", measurements)


def write_unmeasured(
    out: Path,
    collector: str,
    run: Run,
    variant: str,
    tool: ToolResult,
    unmeasured: list[Unmeasured],
) -> Path:
    """Write ``<collector>-unmeasured.json`` — budgets that could **not** be measured.

    Deliberately a different filename. A not-run entry has no ``value``, so it
    must never appear in ``*-measurements.json``: the spine would either drop it
    or emit ``value: null`` and invalidate the review pack. Its *absence* from
    the measurements array is exactly what makes the budget check fail —
    ``metric_within_budget`` scores ``0.0`` for a metric with no measurement.
    This file records **why**, so the failure is actionable rather than mute.
    """
    payload = {
        "schemaVersion": UNMEASURED_SCHEMA_VERSION,
        "collector": collector,
        "runId": (run or {}).get("runId", ""),
        "variant": variant,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tool": tool,
        "unmeasured": unmeasured,
    }
    return write_json(out / f"{collector}-unmeasured.json", payload)


def write_measurement_files(
    out: Path,
    collector: str,
    run: Run,
    variant: str,
    tool: ToolResult,
    measurements: list[Measurement],
    unmeasured: list[Unmeasured],
) -> list[Path]:
    """Emit both halves of the normalised record, measured first."""
    paths = [write_measurements(out, collector, measurements)]
    if unmeasured:
        paths.append(write_unmeasured(out, collector, run, variant, tool, unmeasured))
    return paths


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------


def find_executable(name: str, cfg: Config | None = None, start: Path | None = None) -> str | None:
    """Locate ``name`` on ``PATH`` or in a nearby ``node_modules/.bin``.

    Pure filesystem inspection -- safe to call from :meth:`detect`.
    """
    found = shutil.which(name)
    if found:
        return found
    roots: list[Path] = []
    if cfg is not None and getattr(cfg, "root", None) is not None:
        roots.append(Path(cfg.root))
    if start is not None:
        roots.append(start)
    roots.append(Path.cwd())
    seen: set[Path] = set()
    for root in roots:
        resolved = _resolve_or_none(root)
        if resolved is None:
            continue
        for candidate in (resolved, *resolved.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            bin_dir = candidate / "node_modules" / ".bin"
            for suffix in ("", ".cmd", ".ps1", ".exe"):
                exe = bin_dir / f"{name}{suffix}"
                if exe.is_file():
                    return str(exe)
    return None


def find_node_package(
    package: str, cfg: Config | None = None, start: Path | None = None
) -> Path | None:
    """Return the ``node_modules`` directory containing ``package``, else ``None``.

    Deliberately a filesystem check: resolving through ``node`` would mean
    spawning a subprocess from :meth:`detect`, which the contract forbids.
    """
    relative = Path(*package.split("/"))
    roots: list[Path] = []
    if cfg is not None and getattr(cfg, "root", None) is not None:
        roots.append(Path(cfg.root))
    if start is not None:
        roots.append(start)
    roots.append(Path.cwd())
    for entry in (os.environ.get("NODE_PATH") or "").split(os.pathsep):
        if entry.strip():
            base = Path(entry.strip())
            if (base / relative / "package.json").is_file():
                return base
    for root in roots:
        resolved = _resolve_or_none(root)
        if resolved is None:
            continue
        for candidate in (resolved, *resolved.parents):
            modules = candidate / "node_modules"
            if (modules / relative / "package.json").is_file():
                return modules
    return None


def run_tool(
    command: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> ToolResult:
    """Run a CLI tool with a hard timeout. Never raises, never hangs.

    stdout/stderr are truncated and redacted before being stored -- tool logs
    routinely echo request URLs that carry signed tokens.
    """
    merged = {**os.environ, **(env or {})}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ran": False,
            "exitCode": None,
            "command": redact(command),
            "durationSeconds": round(time.monotonic() - started, 3),
            "cause": "tool_timeout",
            "reason": f"{Path(command[0]).name} exceeded the {timeout}s budget and was killed",
            "stdout": "",
            "stderr": "",
        }
    except (OSError, ValueError) as exc:
        return {
            "ran": False,
            "exitCode": None,
            "command": redact(command),
            "durationSeconds": round(time.monotonic() - started, 3),
            "cause": "tool_unavailable",
            "reason": f"could not execute {Path(command[0]).name}: {exc}",
            "stdout": "",
            "stderr": "",
        }
    return {
        "ran": True,
        "exitCode": proc.returncode,
        "command": redact(command),
        "durationSeconds": round(time.monotonic() - started, 3),
        "cause": "" if proc.returncode == 0 else "tool_failed",
        "reason": (
            ""
            if proc.returncode == 0
            else f"{Path(command[0]).name} exited {proc.returncode}"
        ),
        "stdout": redact_text((proc.stdout or "")[-MAX_CAPTURED_OUTPUT:]),
        "stderr": redact_text((proc.stderr or "")[-MAX_CAPTURED_OUTPUT:]),
    }


# ---------------------------------------------------------------------------
# Lighthouse metric catalogue
# ---------------------------------------------------------------------------

#: metric id -> (JSON Pointer into the LHR, multiplier, lhci assertion target)
LIGHTHOUSE_CATALOGUE: dict[str, tuple[str, float, str]] = {
    "lcp_ms": ("/audits/largest-contentful-paint/numericValue", 1.0, "largest-contentful-paint"),
    "fcp_ms": ("/audits/first-contentful-paint/numericValue", 1.0, "first-contentful-paint"),
    "si_ms": ("/audits/speed-index/numericValue", 1.0, "speed-index"),
    "tti_ms": ("/audits/interactive/numericValue", 1.0, "interactive"),
    "tbt_ms": ("/audits/total-blocking-time/numericValue", 1.0, "total-blocking-time"),
    "ttfb_ms": ("/audits/server-response-time/numericValue", 1.0, "server-response-time"),
    "cls": ("/audits/cumulative-layout-shift/numericValue", 1.0, "cumulative-layout-shift"),
    "total_byte_weight_kb": ("/audits/total-byte-weight/numericValue", 1 / 1024, ""),
    "dom_size": ("/audits/dom-size/numericValue", 1.0, "dom-size"),
    "performance_score": ("/categories/performance/score", 100.0, "categories:performance"),
    "accessibility_score": ("/categories/accessibility/score", 100.0, "categories:accessibility"),
    "best_practices_score": (
        "/categories/best-practices/score", 100.0, "categories:best-practices",
    ),
    "seo_score": ("/categories/seo/score", 100.0, "categories:seo"),
    "pwa_score": ("/categories/pwa/score", 100.0, "categories:pwa"),
}

#: Lighthouse working files that leak page HTML / network detail if left behind.
_LHCI_WORKDIR = ".lighthouseci"


def extract_lighthouse(report: dict[str, Any], specs: list[MetricSpec]) -> dict[str, float | None]:
    """Map one Lighthouse Result (LHR) onto ``{metricId: value | None}``.

    Category scores are reported on a 0-100 scale so budgets read naturally
    (``budget: 90, direction: higher_is_better``).
    """
    values: dict[str, float | None] = {}
    for spec in specs:
        metric_id = spec["id"]
        pointer = spec.get("source")
        scale = spec.get("scale")
        if not pointer:
            entry = LIGHTHOUSE_CATALOGUE.get(metric_id)
            if entry is None:
                values[metric_id] = None
                continue
            pointer, catalogue_scale, _ = entry
            scale = catalogue_scale if scale is None else scale
        raw = coerce_number(json_pointer(report, pointer))
        values[metric_id] = None if raw is None else raw * float(scale if scale is not None else 1)
    return values


def _lhci_assertions(specs: list[MetricSpec]) -> dict[str, Any]:
    """Translate budgets into lighthouserc assertions (advisory: our own
    comparison in :func:`build_measurements` is the authority)."""
    assertions: dict[str, Any] = {}
    for spec in specs:
        entry = LIGHTHOUSE_CATALOGUE.get(spec["id"])
        if entry is None or not entry[2]:
            continue
        _, scale, target = entry
        higher_is_better = spec.get("direction") == "higher_is_better"
        if target.startswith("categories:"):
            if not higher_is_better:
                continue
            assertions[target] = ["warn", {"minScore": round(spec["budget"] / 100.0, 4)}]
        else:
            budget = spec["budget"] / (float(scale) if scale else 1.0)
            key = "minNumericValue" if higher_is_better else "maxNumericValue"
            assertions[target] = ["warn", {key: budget}]
    return assertions


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class LighthouseCollector:
    """Lighthouse CI evidence collector.

    Requires ``lhci`` (``npm i -g @lhci/cli``) plus a Chrome/Chromium binary.
    Optional: with ``lhci`` absent the spine's Playwright collector alone still
    satisfies the credential-free conformance suite.
    """

    name = "lighthouse"
    kind = "evidence"

    #: Outputs owned by this collector, cleared before every attempt.
    OUTPUTS = (
        "lighthouse.json",
        "lighthouse-[0-9]*.json",
        "lighthouse-measurements.json",
        "lighthouse-unmeasured.json",
        "lighthouserc.json",
        _LHCI_WORKDIR,
    )

    INSTALL_HINT = "install with `npm i -g @lhci/cli` (https://github.com/GoogleChrome/lighthouse-ci)"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        try:
            exe = find_executable("lhci", cfg)
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"lighthouse detection failed: {exc}"
        if exe is None:
            return False, f"lhci not on PATH — {LighthouseCollector.INSTALL_HINT}"
        return True, f"lhci available at {exe}"

    def collect(self, run: Run, variant: str, out: Path) -> list[ArtifactRef]:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        clear_previous(out, self.OUTPUTS)
        run_dir = resolve_run_dir(run, out)
        cfg = safe_config(out)
        benchmarks = load_benchmarks(run_dir)
        specs = metrics_for(benchmarks, self.name)
        options = collector_options(benchmarks, self.name)
        urls = target_urls(benchmarks, self.name, run)

        artifacts: list[ArtifactRef] = []
        available, reason = self.detect(cfg)  # type: ignore[arg-type]
        if not available:
            return self._abort(run, variant, out, run_dir, specs, "tool_unavailable", reason)
        if not urls:
            return self._abort(
                run, variant, out, run_dir, specs, "output_missing",
                "no target URL configured — set target.url in benchmarks.yaml "
                "or the ADLC_TARGET_URL environment variable",
            )

        rc_path = self._write_config(out, urls, options, specs)
        artifacts.append(
            artifact_ref(rc_path, run_dir, "lighthouse_config", "application/json")
        )

        exe = find_executable("lhci", cfg) or "lhci"
        command = [exe, "autorun", f"--config={rc_path}"]
        command.extend(str(arg) for arg in options.get("extraArgs", []) if str(arg).strip())
        tool = run_tool(command, cwd=out, timeout=timeout_for(benchmarks, self.name))

        reports = self._harvest(out, urls)
        produced_output = (out / _LHCI_WORKDIR).exists()
        report_paths: list[Path] = []
        for index, report in enumerate(reports):
            target = out / ("lighthouse.json" if index == 0 else f"lighthouse-{index}.json")
            report_paths.append(write_json(target, redact(report)))
        shutil.rmtree(out / _LHCI_WORKDIR, ignore_errors=True)

        if not report_paths:
            # `lhci` ran but produced nothing readable. Fail loudly, measure nothing.
            cause = tool.get("cause") or (
                "output_unreadable" if produced_output else "output_missing"
            )
            detail = tool.get("reason") or (
                "lhci wrote no parseable Lighthouse Result document"
                if produced_output
                else "lhci produced no Lighthouse report"
            )
            return self._abort(run, variant, out, run_dir, specs, cause, detail, artifacts, tool)

        primary_sha = artifact_ref(report_paths[0], run_dir, "lighthouse", "application/json")
        artifacts.append(primary_sha)
        for extra in report_paths[1:]:
            artifacts.append(artifact_ref(extra, run_dir, "lighthouse", "application/json"))

        per_url = [extract_lighthouse(report, specs) for report in reports]
        values: dict[str, float | None] = {}
        reasons: dict[str, tuple[str, str]] = {}
        for spec in specs:
            found = [v for v in (page.get(spec["id"]) for page in per_url) if v is not None]
            values[spec["id"]] = aggregate_values(found, spec)
            if not found and not spec.get("source") and spec["id"] not in LIGHTHOUSE_CATALOGUE:
                reasons[spec["id"]] = (
                    "metric_unmapped",
                    (
                        f"'{spec['id']}' is not a known Lighthouse metric — declare a "
                        "`source` JSON Pointer in benchmarks.yaml"
                    ),
                )
        measurements, unmeasured = build_measurements(
            specs, values, self.name, primary_sha["sha256"], reasons,
        )
        for path in write_measurement_files(
            out, self.name, run, variant, tool, measurements, unmeasured
        ):
            artifacts.append(
                artifact_ref(path, run_dir, "evidence_measurements", "application/json")
            )
        return artifacts

    # -- helpers ---------------------------------------------------------

    def _abort(
        self,
        run: Run,
        variant: str,
        out: Path,
        run_dir: Path | None,
        specs: list[MetricSpec],
        cause: str,
        reason: str,
        artifacts: list[ArtifactRef] | None = None,
        tool: ToolResult | None = None,
    ) -> list[ArtifactRef]:
        """Record why nothing was measured. Emits no values, ever."""
        result: list[ArtifactRef] = list(artifacts or [])
        if not specs:
            return result
        _, unmeasured = build_measurements(
            specs, {}, self.name, "", {}, default_reason=(cause, reason),
        )
        info: ToolResult = tool or {"ran": False, "cause": cause, "reason": reason}
        for path in write_measurement_files(out, self.name, run, variant, info, [], unmeasured):
            result.append(
                artifact_ref(path, run_dir, "evidence_measurements", "application/json")
            )
        return result

    def _write_config(
        self,
        out: Path,
        urls: list[str],
        options: dict[str, Any],
        specs: list[MetricSpec],
    ) -> Path:
        runs = coerce_number(options.get("numberOfRuns")) or 1
        settings: dict[str, Any] = {
            "chromeFlags": str(
                options.get("chromeFlags") or "--headless=new --no-sandbox --disable-gpu"
            ),
        }
        preset = str(options.get("preset") or "desktop").lower()
        if preset in ("desktop", "mobile"):
            settings["preset"] = preset
        config = {
            "ci": {
                "collect": {
                    "url": urls,
                    "numberOfRuns": max(1, int(runs)),
                    "settings": settings,
                },
                "assert": {"assertions": _lhci_assertions(specs)},
                "upload": {
                    "target": "filesystem",
                    "outputDir": str(out / _LHCI_WORKDIR),
                },
            }
        }
        return write_json(out / "lighthouserc.json", redact(config))

    def _harvest(self, out: Path, urls: list[str]) -> list[dict[str, Any]]:
        """Find the LHR JSON documents lhci wrote, ordered to match ``urls``."""
        workdir = out / _LHCI_WORKDIR
        manifest = read_json(workdir / "manifest.json")
        by_url: dict[str, dict[str, Any]] = {}
        if isinstance(manifest, list):
            for entry in manifest:
                if not isinstance(entry, dict):
                    continue
                report = read_json(Path(str(entry.get("jsonPath", ""))))
                if not isinstance(report, dict):
                    continue
                key = str(entry.get("url", ""))
                if key not in by_url or entry.get("isRepresentativeRun"):
                    by_url[key] = report
        ordered: list[dict[str, Any]] = []
        for url in urls:
            match = by_url.get(url) or by_url.get(url.rstrip("/")) or by_url.get(url + "/")
            if match is not None:
                ordered.append(match)
        if ordered:
            return ordered
        for candidate in sorted(workdir.glob("lhr-*.json")) or sorted(out.rglob("lhr-*.json")):
            report = read_json(candidate)
            if isinstance(report, dict) and "audits" in report:
                ordered.append(report)
        return ordered
