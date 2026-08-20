"""The reducer -- the ONLY writer of ``run.json``.

Folds immutable ``stages/*.json`` files plus on-disk gate results and artifacts
into the canonical ``adlc-run/v1`` document.

Two rules are enforced here and nowhere else:

1. **Append-only history.** Stage results are never edited, so re-running a
   stage produces ``attempt: n+1`` and the reducer keeps both.
2. **Fail closed.** A gate that is ``required`` and did not run
   (``status == "not_run"``) makes the aggregate FAIL. A gate result is only
   green if it was actually evaluated.
"""

from __future__ import annotations

import json
from pathlib import Path

from adlc import RUN_SCHEMA_VERSION
from adlc.config import Config
from adlc.ports import GateResult, Run, RunStatus
from adlc.runs import RunDir, read_json, utcnow, write_json

#: Stage -> run status promotion. Later stages win.
_STATUS_ORDER: tuple[tuple[str, RunStatus], ...] = (
    ("intake", "draft"),
    ("qualify", "draft"),
    ("spec", "specced"),
    ("enrich", "specced"),
    ("graph", "specced"),
    ("build", "built"),
    ("evidence", "built"),
    ("eval", "evaluated"),
    ("gate", "gated"),
    ("report", "reported"),
    ("review", "decided"),
)
_STATUS_RANK = {status: idx for idx, (_, status) in enumerate(_STATUS_ORDER)}


def _derive_status(stages: list[dict], has_decision: bool) -> RunStatus:
    if has_decision:
        return "decided"
    best: RunStatus = "draft"
    seen = {s.get("stage") for s in stages if s.get("status") == "ok"}
    for stage_name, status in _STATUS_ORDER:
        if stage_name in seen and _STATUS_RANK[status] >= _STATUS_RANK[best]:
            best = status
    return best


def collect_gates(rd: RunDir, cfg: Config) -> list[GateResult]:
    """Read ``gates/*.json`` and stamp each with its required-ness.

    Any gate that the profile requires but which produced no result file at all
    is synthesised as ``not_run`` -- silence must never read as success.
    """
    gates: dict[str, GateResult] = {}
    if rd.gates_dir.is_dir():
        for item in sorted(rd.gates_dir.glob("*.json")):
            try:
                result: GateResult = read_json(item)
            except (json.JSONDecodeError, OSError):
                continue
            gate_id = result.get("id") or item.stem
            result["id"] = gate_id
            result["required"] = cfg.is_required(gate_id)
            gates[gate_id] = result

    for required_id in cfg.required_gates():
        if required_id not in gates:
            gates[required_id] = {
                "id": required_id,
                "required": True,
                "status": "not_run",
                "severity": "high",
                "observed": {},
                "expected": {},
                "message": (
                    f"required gate '{required_id}' produced no result -- "
                    "treated as not_run, which fails the aggregate"
                ),
                "evidence": [],
            }
    return [gates[key] for key in sorted(gates)]


def aggregate_passed(gates: list[GateResult]) -> tuple[bool, list[str]]:
    """The single source of truth for 'is this run green?'.

    Fail closed: ``required`` + (``fail`` or ``not_run``) => not green.
    """
    failures: list[str] = []
    for gate in gates:
        if not gate.get("required"):
            continue
        status = gate.get("status")
        if status == "fail":
            failures.append(f"{gate['id']}: FAIL - {gate.get('message', '')}".strip())
        elif status == "not_run":
            failures.append(
                f"{gate['id']}: NOT_RUN - {gate.get('message', 'gate did not execute')}"
            )
    return (not failures), failures


def reduce_run(cfg: Config, rd: RunDir) -> Run:
    """Fold everything on disk into ``run.json`` and write it. Idempotent."""
    seed_path = rd.path / "seed.json"
    run: Run = read_json(seed_path) if seed_path.is_file() else {}

    stages = rd.stage_results()
    gates = collect_gates(rd, cfg)
    artifacts = rd.scan_artifacts()

    variants = list(run.get("variants") or [])
    decision = run.get("decision")
    experiment_ref = run.get("experimentRef")

    # Later stages may contribute variants / decision / pr number via `data`.
    pr_number = run.get("prNumber")
    for stage in stages:
        data = stage.get("data") or {}
        if isinstance(data.get("variants"), list) and data["variants"]:
            variants = data["variants"]
        if isinstance(data.get("decision"), dict):
            decision = data["decision"]
        if data.get("experimentRef"):
            experiment_ref = data["experimentRef"]
        if data.get("prNumber") is not None:
            pr_number = data["prNumber"]

    reduced: Run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "runId": rd.run_id,
        "createdAt": run.get("createdAt") or utcnow(),
        "referencesRun": run.get("referencesRun"),
        "repo": run.get("repo", ""),
        "baseSha": run.get("baseSha"),
        "headSha": run.get("headSha"),
        "prNumber": pr_number,
        "status": _derive_status(stages, bool(decision)),
        "profile": cfg.profile,
        "capabilities": run.get("capabilities") or {},
        "stages": stages,
        "variants": variants,
        "gates": gates,
        "artifacts": artifacts,
        "decision": decision,
        "experimentRef": experiment_ref,
    }
    write_json(rd.run_json, reduced)
    return reduced


def load_run(rd: RunDir) -> Run:
    if rd.run_json.is_file():
        return read_json(rd.run_json)
    seed = rd.path / "seed.json"
    if seed.is_file():
        return read_json(seed)
    raise FileNotFoundError(f"no run.json or seed.json in {rd.path}")


def write_gate(rd: RunDir, result: GateResult) -> Path:
    """Persist one gate result. Stages call this; they never touch run.json."""
    return write_json(rd.gates_dir / f"{result['id']}.json", result)
