"""Evidence stage -- capture artifacts and build the sanitised review pack.

Two outputs with very different trust levels:

* ``evidence/<variant>/`` -- the raw bundle (trace, HAR, console, video,
  screenshots, replay script). Useful to a **human**, hash-verified, archived.
* ``evidence-review-pack.json`` -- the **only** thing an agent reviewer is
  allowed to see. It contains requirement ids, normalised measurements, coverage
  claims and artifact hashes, and deliberately contains **no** raw HAR, trace,
  console text, replay source or HTML, because all of those leak source code and
  are attacker-controlled prompt-injection vectors.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from adlc.config import Config, select_adapter
from adlc.ports import ArtifactRef
from adlc.reduce import load_run
from adlc.runs import RunDir, sha256_file, utcnow, write_json
from adlc.schemas import is_valid

_AC_RE = re.compile(r"\*\*(?P<id>US\d+-AC\d+)\*\*\s*:?\s*(?P<text>.+?)$", re.MULTILINE)
_SCENARIO_RE = re.compile(r"^\s*Scenario:\s*(?P<id>US\d+-AC\d+)\s+(?P<text>.+?)$", re.MULTILINE)


def extract_requirements(rd: RunDir) -> list[dict[str, str]]:
    """Pull acceptance criteria from spec.md, falling back to the feature file."""
    requirements: list[dict[str, str]] = []
    seen: set[str] = set()

    spec = rd.spec_dir / "spec.md"
    if spec.is_file():
        text = spec.read_text(encoding="utf-8")
        for match in _AC_RE.finditer(text):
            rid = match.group("id")
            if rid not in seen:
                seen.add(rid)
                requirements.append({
                    "id": rid,
                    "text": match.group("text").strip(),
                    "source": "spec/spec.md",
                })

    feature = rd.enrichment_dir / "features" / "acceptance.feature"
    if feature.is_file():
        text = feature.read_text(encoding="utf-8")
        for match in _SCENARIO_RE.finditer(text):
            rid = match.group("id")
            if rid not in seen:
                seen.add(rid)
                requirements.append({
                    "id": rid,
                    "text": match.group("text").strip(),
                    "source": "enrichment/features/acceptance.feature",
                })
    return requirements


def collect_measurements(rd: RunDir, variant: str) -> list[dict[str, Any]]:
    """Normalised measurements emitted by evidence collectors."""
    out: list[dict[str, Any]] = []
    budgets: dict[str, dict[str, Any]] = {}
    bench = rd.enrichment_dir / "benchmarks.yaml"
    if bench.is_file():
        try:
            for metric in (yaml.safe_load(bench.read_text(encoding="utf-8")) or {}).get("metrics", []):
                budgets[metric["id"]] = metric
        except (yaml.YAMLError, KeyError, TypeError):
            pass

    for path in sorted((rd.evidence_dir / variant).glob("*-measurements.json")):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in items if isinstance(items, list) else []:
            metric_id = item.get("metricId")
            if not metric_id:
                continue
            budget = item.get("budget", budgets.get(metric_id, {}).get("budget"))
            value = item.get("value")
            out.append({
                "metricId": metric_id,
                "value": value,
                "budget": budget,
                "passed": bool(budget is None or (value is not None and value <= budget)),
                "collector": item.get("collector", "unknown"),
                "artifactSha256": item.get("artifactSha256", ""),
            })
    return out


def build_review_pack(
    cfg: Config, rd: RunDir, variant: str, artifacts: list[ArtifactRef]
) -> dict[str, Any]:
    run = load_run(rd)
    requirements = extract_requirements(rd)
    measurements = [m for m in collect_measurements(rd, variant) if m["artifactSha256"]]

    by_kind: dict[str, list[str]] = {}
    for artifact in artifacts:
        by_kind.setdefault(artifact["kind"], []).append(artifact["sha256"])

    # Coverage heuristic: which evidence kinds substantiate which requirement.
    kind_priority = ["playwright_trace", "screenshot", "console_log", "har", "video"]
    available = [k for k in kind_priority if by_kind.get(k)]

    coverage = []
    for requirement in requirements:
        hashes = [by_kind[k][0] for k in available]
        coverage.append({
            "requirementId": requirement["id"],
            "evidenceKinds": available,
            "artifactSha256": hashes,
            "present": bool(hashes),
        })

    pack = {
        "runId": rd.run_id,
        "candidateSha": run.get("headSha") or run.get("baseSha") or "",
        "workflowRunId": None,
        "collector": "adlc/0.1.0",
        "requirements": requirements,
        "measurements": measurements,
        "coverage": coverage,
        "screenshots": [
            {"artifactSha256": digest, "caption": "captured during evidence run", "redacted": True}
            for digest in by_kind.get("screenshot", [])[:10]
        ],
    }
    return pack


def run_evidence(cfg: Config, rd: RunDir, variant: str = "candidate-a") -> dict[str, Any]:
    started = utcnow()
    out_dir = rd.evidence_dir / variant
    out_dir.mkdir(parents=True, exist_ok=True)

    collector = select_adapter(cfg, "evidence")
    collector_name = getattr(collector, "name", type(collector).__name__)
    available, reason = type(collector).detect(cfg)

    run = load_run(rd)
    artifacts: list[ArtifactRef] = []
    if available:
        try:
            artifacts = collector.collect(run, variant, out_dir)
        except Exception as exc:  # noqa: BLE001 - report honestly, never fabricate
            reason = f"{collector_name} raised {type(exc).__name__}: {exc}"
            available = False

    # Re-hash whatever actually landed on disk; never trust the collector's list.
    artifacts = [
        {
            "path": rd.rel(path),
            "kind": _kind_for(path),
            "mimeType": _mime_for(path),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(out_dir.rglob("*"))
        if path.is_file()
    ]

    pack = build_review_pack(cfg, rd, variant, artifacts)
    write_json(rd.review_pack, pack)
    valid, errors = is_valid("evidence-review-pack", pack)

    status = "ok" if artifacts and valid else "fail"
    message = (
        f"{len(artifacts)} artifact(s) via {collector_name}"
        if artifacts
        else f"no evidence captured - {reason}"
    )
    if not valid:
        message += f"; review pack invalid: {errors[:3]}"

    rd.write_stage(
        "evidence",
        status=status,
        outputs=[a["path"] for a in artifacts] + [rd.rel(rd.review_pack)],
        message=message,
        data={
            "variant": variant,
            "collector": collector_name,
            "collectorAvailable": available,
            "collectorReason": reason,
            "artifacts": len(artifacts),
            "requirements": len(pack["requirements"]),
            "measurements": len(pack["measurements"]),
            "packValid": valid,
            "packErrors": errors[:5],
        },
        started_at=started,
    )
    return {"artifacts": artifacts, "pack": pack, "valid": valid}


def _kind_for(path: Path) -> str:
    return {
        ".zip": "playwright_trace", ".har": "har", ".webm": "video", ".png": "screenshot",
        ".jsonl": "console_log", ".ts": "replay_script", ".json": "json",
    }.get(path.suffix, "file")


def _mime_for(path: Path) -> str:
    return {
        ".zip": "application/zip", ".har": "application/json", ".webm": "video/webm",
        ".png": "image/png", ".jsonl": "application/x-ndjson", ".ts": "text/plain",
        ".json": "application/json",
    }.get(path.suffix, "application/octet-stream")
