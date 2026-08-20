"""Gate stage -- run gate adapters and enforce the fail-closed aggregate.

The one rule that matters: a gate the profile marks ``required`` which returns
``fail`` **or** ``not_run`` makes the aggregate fail. "We couldn't check" is not
"it's fine". Everything else here is bookkeeping.
"""

from __future__ import annotations

from typing import Any

from adlc.config import Config, load_adapters
from adlc.ports import GateResult
from adlc.reduce import aggregate_passed, collect_gates, load_run, write_gate
from adlc.runs import RunDir, utcnow


def available_gates() -> dict[str, type]:
    return load_adapters("gate")


def run_gates(cfg: Config, rd: RunDir, gate_ids: list[str] | None = None) -> dict[str, Any]:
    started = utcnow()
    registry = available_gates()
    run = load_run(rd)

    requested = gate_ids or list(cfg.required_gates())
    rd.gates_dir.mkdir(parents=True, exist_ok=True)

    executed: list[GateResult] = []
    for gate_id in requested:
        cls = registry.get(gate_id)
        if cls is None:
            result: GateResult = {
                "id": gate_id,
                "required": cfg.is_required(gate_id),
                "status": "not_run",
                "severity": "high",
                "observed": {},
                "expected": {},
                "message": f"no adapter registered for gate '{gate_id}'",
                "evidence": [],
            }
            write_gate(rd, result)
            executed.append(result)
            continue

        try:
            available, reason = cls.detect(cfg)
        except Exception as exc:  # noqa: BLE001
            available, reason = False, f"detect() raised {type(exc).__name__}: {exc}"

        if not available:
            result = {
                "id": gate_id,
                "required": cfg.is_required(gate_id),
                "status": "not_run",
                "severity": "high",
                "observed": {},
                "expected": {},
                "message": f"gate unavailable: {reason}",
                "evidence": [],
            }
        else:
            try:
                result = cls().evaluate(run, cfg)
                result.setdefault("id", gate_id)
                result["required"] = cfg.is_required(gate_id)
            except Exception as exc:  # noqa: BLE001 - a broken gate is never a pass
                result = {
                    "id": gate_id,
                    "required": cfg.is_required(gate_id),
                    "status": "not_run",
                    "severity": "high",
                    "observed": {},
                    "expected": {},
                    "message": f"gate raised {type(exc).__name__}: {exc}",
                    "evidence": [],
                }
        write_gate(rd, result)
        executed.append(result)

    all_gates = collect_gates(rd, cfg)
    passed, failures = aggregate_passed(all_gates)

    rd.write_stage(
        "gate",
        status="ok" if passed else "fail",
        outputs=[f"gates/{g['id']}.json" for g in executed],
        message=(
            f"{len(executed)} gate(s) evaluated; aggregate "
            + ("PASS" if passed else "FAIL: " + "; ".join(failures))
        ),
        data={
            "requested": requested,
            "aggregatePassed": passed,
            "failures": failures,
            "results": [
                {"id": g["id"], "required": g["required"], "status": g["status"],
                 "message": g.get("message", "")}
                for g in all_gates
            ],
        },
        started_at=started,
    )
    return {"passed": passed, "failures": failures, "gates": all_gates}
