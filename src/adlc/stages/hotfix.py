"""``adlc hotfix --incident FILE`` -- the day-2 stage.

What this stage is
------------------
A hotfix is **not a new kind of pipeline**. It is a deliberately narrow lap of
the ordinary one, driving the *spine's own* stage functions in process::

    incident -> brief.md -> RunDir.create -> run_intake -> run_qualify
                                                             |
                        narrow 3-node graph (no spec/enrich) <+
                                   |
                   run_build -> run_evidence -> run_gates -> reduce_run

Every one of those calls is the same function ``adlc run new``, ``adlc build``,
``adlc evidence`` and ``adlc gate`` use. Day-2 has no private code path, no
second gate set and no weaker bar. The only thing that makes it a *hotfix* is
that ``spec`` and ``enrich`` are skipped in favour of a fixed 3-node graph.

Invariants this module honours
------------------------------
* **It never writes ``run.json``.** Only :func:`adlc.reduce.reduce_run` does, and
  this stage calls it exactly the way ``adlc run new`` does -- it never composes
  that document itself.
* **Stage results go through** :meth:`adlc.runs.RunDir.write_stage`, so attempts
  are append-only and digests are computed the spine's way.
* **It fails closed.** A brief that does not qualify parks the run; gates that
  did not run are never reported as green; the process exits non-zero unless you
  explicitly opt out.
* **It touches no Azure API.** All of that lives behind the ``daytwo`` adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypedDict

from adlc.adapters.daytwo.sre_agent import Incident, SreAgentReceiver
from adlc.config import Config
from adlc.ports import PROTECTED_PATHS, StageResult
from adlc.reduce import reduce_run
from adlc.runs import RunDir, current_sha, new_run_id, utcnow, write_json
from adlc.stages.evidence import run_evidence
from adlc.stages.gates import run_gates
from adlc.stages.intake import run_intake, run_qualify

STAGE_NAME = "hotfix"

#: Gate ids a hotfix must clear -- the `minimal` profile's required set. A
#: hotfix does not get a weaker bar than any other change.
HOTFIX_GATE_IDS: tuple[str, ...] = (
    "tests", "secrets_local", "deps_local", "evidence_completeness",
)

#: The variant a hotfix captures evidence for. This is the spine's own treatment
#: variant name (see :mod:`adlc.stages.build`), reused rather than invented: a
#: bespoke "hotfix" variant would not match what ``run_build`` records, and the
#: report and the evidence pack would then disagree about what was measured.
HOTFIX_VARIANT = "candidate-a"

#: Used only when neither the incident nor ``config.yaml`` declares a write set.
#: Recorded as ``writeSetSource: "fallback"`` so nobody mistakes it for analysis.
FALLBACK_WRITE_SET: tuple[str, ...] = ("src/",)


class StepRecord(TypedDict, total=False):
    step: str
    status: str          # ok | fail | skipped
    message: str


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def run_hotfix(
    incident_path: Path | str | None = None,
    *,
    cfg: Config | None = None,
    incident: Incident | None = None,
    run_id: str | None = None,
    plan_only: bool = False,
    allow_unqualified: bool = False,
    runner_name: str | None = None,
    max_parallel: int | None = None,
) -> StageResult:
    """Convert an incident into a narrow, fully gated ADLC run.

    Returns the ``hotfix`` :class:`~adlc.ports.StageResult`, which is also
    persisted by :meth:`RunDir.write_stage`.

    ``plan_only`` still creates the run and writes ``brief.md`` and
    ``taskgraph.json`` -- so you can inspect exactly what would be built -- but
    executes no build, evidence, gate or reduce step.
    """
    started = utcnow()
    cfg = cfg or Config.load()
    receiver = SreAgentReceiver()

    if incident is None:
        incident = receiver.load(incident_path)

    steps: list[StepRecord] = []
    outputs = ["brief.md", "incident.json", "taskgraph.json"]

    # 1. incident -> brief -> run, through the SAME door `adlc run new` uses.
    rd = RunDir(cfg, run_id or new_run_id())
    rd.create(profile=cfg.profile, brief_text=receiver.to_brief(incident))
    write_json(rd.path / "incident.json", incident)

    source = f"sre-agent:{incident.get('id', 'incident')}"
    _step(steps, "intake", lambda: run_intake(cfg, rd, source))

    # 2. Qualify with the ordinary scorer. A brief that would be parked on day 1
    #    is parked on day 2 too, unless the operator explicitly overrides.
    qualification = _step(steps, "qualify", lambda: run_qualify(cfg, rd)) or {}
    qualified = bool(qualification.get("qualified"))
    if not qualified and not allow_unqualified and not plan_only:
        return _finish(
            rd, incident, steps, outputs, started, qualified=qualified,
            write_set_source="n/a", plan_only=plan_only, gates=None,
            halted=(
                f"brief did not qualify (score {qualification.get('score')} < threshold "
                f"{qualification.get('threshold')}); enrich the incident payload or pass "
                "--allow-unqualified"
            ),
        )

    # 3. Narrow task graph -- this is what replaces `spec` and `enrich`.
    graph, write_set_source = build_hotfix_graph(
        incident, run_id=rd.run_id, base_sha=current_sha(cfg.root) or "unknown", cfg=cfg
    )
    write_json(rd.taskgraph, graph)
    steps.append({"step": "graph", "status": "ok",
                  "message": f"{len(graph['nodes'])} node(s); writeSet from {write_set_source}"})

    if plan_only:
        return _finish(rd, incident, steps, outputs, started, qualified=qualified,
                       write_set_source=write_set_source, plan_only=True, gates=None)

    # 4. The ordinary pipeline. The same functions the CLI calls.
    _step(steps, "build", lambda: _build(cfg, rd, runner_name, max_parallel))
    _step(steps, "evidence", lambda: run_evidence(cfg, rd, HOTFIX_VARIANT))
    gates = _step(steps, "gate", lambda: run_gates(cfg, rd, list(HOTFIX_GATE_IDS)))

    result = _finish(rd, incident, steps, outputs, started, qualified=qualified,
                     write_set_source=write_set_source, plan_only=False, gates=gates)

    # 5. Reduce LAST, so run.json includes the hotfix stage above. This stage
    #    never composes run.json itself -- reduce_run is its only writer.
    _step(steps, "reduce", lambda: reduce_run(cfg, rd))
    return result


def build_hotfix_graph(
    incident: Incident,
    *,
    run_id: str,
    base_sha: str,
    cfg: Config | None = None,
) -> tuple[dict[str, Any], str]:
    """Build the 3-node hotfix graph. Returns ``(graph, write_set_source)``.

    The shape is fixed on purpose -- a hotfix that needs a bespoke decomposition
    is not a hotfix, it is a feature, and should go through ``adlc spec``.

    ==========  =====  ============================================
    node        level  purpose
    ==========  =====  ============================================
    ``T001``      0    regression test that reproduces the incident
    ``T002``      1    the minimal fix
    ``T003``      1    the incident record
    ==========  =====  ============================================

    ``T002`` and ``T003`` share level 1, so their write sets must not overlap
    (``docs/PLAN.md`` section 4.4) -- they cannot, one writes code and one
    writes ``docs/incidents/``.
    """
    incident_id = _safe(str(incident.get("id") or "incident"))
    title = str(incident.get("title") or "incident")
    fix_write_set, write_set_source = resolve_write_set(incident, cfg)

    test_path = f"tests/regression/test_{_safe(incident_id, sep='_')}.py"
    record_path = f"docs/incidents/{incident_id}.md"
    commands = _commands(cfg)

    def capsule(interfaces: str, do_not_touch_extra: Sequence[str] = ()) -> dict[str, Any]:
        return {
            "refs": [],
            "interfaces": interfaces,
            "conventions": (
                "Hotfix scope: change the minimum needed to clear the observed signal. "
                "No refactors, no dependency bumps, no drive-by cleanups."
            ),
            "commands": commands,
            "doNotTouch": [*PROTECTED_PATHS, *do_not_touch_extra],
            "budget": {"maxTotalBytes": 65536, "maxFileBytes": 8192, "maxFiles": 12},
        }

    signal_text = "; ".join(
        _signal_sentence(s) for s in (incident.get("signals") or [])
    ) or "no measured signal was supplied; establish one"
    context = f"Incident {incident_id} ({incident.get('severity', 'sev3')}): {title}."

    nodes: list[dict[str, Any]] = [
        {
            "id": "T001",
            "title": f"Reproduce incident {incident_id} with a failing regression test",
            "kind": "test",
            "dependsOn": [],
            "level": 0,
            "writeSet": [test_path],
            "acceptance": ["HF-AC1"],
            "rubricIds": [],
            "adrRefs": [],
            "context": capsule(
                f"{context} Write a test that FAILS on the current commit and captures: "
                f"{signal_text}."
            ),
        },
        {
            "id": "T002",
            "title": f"Apply the minimal fix for incident {incident_id}",
            "kind": "implement",
            "dependsOn": ["T001"],
            "level": 1,
            "writeSet": list(fix_write_set),
            "acceptance": ["HF-AC1", "HF-AC2"],
            "rubricIds": [],
            "adrRefs": [],
            "context": capsule(
                f"{context} Make the test written in T001 pass. Full detail is in "
                "brief.md and incident.json.",
                [test_path],
            ),
        },
        {
            "id": "T003",
            "title": f"Record incident {incident_id} and its mitigation",
            "kind": "doc",
            "dependsOn": ["T001"],
            "level": 1,
            "writeSet": [record_path],
            "acceptance": ["HF-AC3"],
            "rubricIds": [],
            "adrRefs": [],
            "context": capsule(
                f"{context} Record the suspected cause, the deployed commit and the "
                "mitigation. This is a postmortem note, not an ADR."
            ),
        },
    ]

    graph = {
        "runId": run_id,
        "baseSha": base_sha,
        "specDigest": "sha256:" + hashlib.sha256(
            json.dumps(incident, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "nodes": nodes,
    }
    return graph, write_set_source


def resolve_write_set(incident: Incident, cfg: Config | None) -> tuple[list[str], str]:
    """Resolve the fix node's write set, and say honestly where it came from.

    Order: incident hint -> ``config.yaml`` ``hotfix.writeSet`` -> fallback.
    A ``"fallback"`` source is surfaced in the stage message *and* in
    ``data.writeSetSource`` -- it is a placeholder, not an analysis result.
    """
    raw: Any = incident.get("writeSet") or incident.get("suspectedFiles")  # type: ignore[assignment]
    if paths := _clean_paths(raw):
        return paths, "incident"

    configured = ((cfg.raw if cfg else {}) or {}).get("hotfix", {})
    if isinstance(configured, dict) and (paths := _clean_paths(configured.get("writeSet"))):
        return paths, "config"

    return list(FALLBACK_WRITE_SET), "fallback"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m adlc.stages.hotfix``.

    Exit codes -- fail closed:

    ``0``
        Stage ok: the required gates ran **and** the aggregate passed (or
        ``--plan-only`` / ``--allow-incomplete`` was given).
    ``1``
        A step failed, the brief did not qualify, a required gate failed, or the
        gates did not run and you did not say that was acceptable.
    """
    parser = argparse.ArgumentParser(
        prog="adlc hotfix",
        description="Turn a day-2 incident into a narrow, fully gated ADLC run.",
    )
    parser.add_argument("--incident", type=Path, default=None,
                        help="incident payload JSON; defaults to $ADLC_INCIDENT_FILE / "
                             "$ADLC_INCIDENT_PAYLOAD / $GITHUB_EVENT_PATH")
    parser.add_argument("--plan-only", action="store_true",
                        help="create the run and write brief.md + taskgraph.json, but execute "
                             "no build, evidence, gate or reduce step")
    parser.add_argument("--allow-unqualified", action="store_true",
                        help="proceed even if the brief scores below qualify.minScore")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit 0 even if the required gates were not evaluated")
    parser.add_argument("--run-id", default=None, help="use this run id instead of minting one")
    parser.add_argument("--runner", default=None, help="AgentRunner adapter name")
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print the StageResult as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = run_hotfix(
            args.incident,
            run_id=args.run_id,
            plan_only=args.plan_only,
            allow_unqualified=args.allow_unqualified,
            runner_name=args.runner,
            max_parallel=args.max_parallel,
        )
    except Exception as exc:  # noqa: BLE001 - a CLI must report, not traceback
        print(f"adlc hotfix: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    data = result["data"]
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"[{result['status']}] {result['message']}")
        for step in data["steps"]:
            print(f"  {step['status']:>7}  {step['step']}: {step['message']}")

    if result["status"] == "fail":
        return 1
    if data["gatesEvaluated"] or args.plan_only or args.allow_incomplete:
        return 0
    print(
        "adlc hotfix: required gates were not evaluated - refusing to report success. "
        "Pass --allow-incomplete only if you accept an ungated result.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build(cfg: Config, rd: RunDir, runner_name: str | None, max_parallel: int | None) -> Any:
    from adlc.stages.build import run_build

    return run_build(cfg, rd, runner_name=runner_name, max_parallel=max_parallel)


def _step(steps: list[StepRecord], name: str, action: Callable[[], Any]) -> Any:
    """Run one pipeline step, recording failure instead of exploding.

    A day-2 responder needs a report, not a traceback -- but the failure is
    recorded as ``fail``, so the aggregate can never be mistaken for green.
    """
    try:
        outcome = action()
    except Exception as exc:  # noqa: BLE001 - report honestly, never fabricate
        steps.append({"step": name, "status": "fail",
                      "message": f"{type(exc).__name__}: {exc}"})
        return None
    steps.append({"step": name, "status": "ok", "message": _summarise(name, outcome)})
    return outcome


def _summarise(name: str, outcome: Any) -> str:
    if not isinstance(outcome, dict):
        return f"{name} completed"
    if name == "qualify":
        return f"score {outcome.get('score')}/100 (threshold {outcome.get('threshold')})"
    if name == "gate":
        if outcome.get("passed"):
            return "aggregate PASS"
        return "aggregate FAIL: " + "; ".join(outcome.get("failures") or [])
    if name == "build":
        return f"{len(outcome.get('failedNodes') or [])} failed node(s)"
    if name == "evidence":
        return f"{len(outcome.get('artifacts') or [])} artifact(s)"
    return f"{name} completed"


def _finish(
    rd: RunDir,
    incident: Incident,
    steps: list[StepRecord],
    outputs: list[str],
    started: str,
    *,
    qualified: bool,
    write_set_source: str,
    plan_only: bool,
    gates: dict[str, Any] | None,
    halted: str | None = None,
) -> StageResult:
    """Write the hotfix stage result through the spine's append-only writer."""
    failed = [s for s in steps if s["status"] == "fail"]
    gate_step = next((s for s in steps if s["step"] == "gate"), None)
    gates_evaluated = bool(gate_step and gate_step["status"] == "ok")
    gates_passed = bool(gates and gates.get("passed"))

    status = "ok"
    if halted or failed or (gates_evaluated and not gates_passed):
        status = "fail"

    message = _message(incident, status, failed, gates_evaluated, gates_passed,
                       plan_only, write_set_source, halted)

    return rd.write_stage(
        STAGE_NAME,
        status=status,
        outputs=outputs,
        message=message,
        data={
            "runId": rd.run_id,
            "incidentId": incident.get("id"),
            "incidentSchemaVersion": incident.get("schemaVersion"),
            "severity": incident.get("severity"),
            "source": incident.get("source"),
            "variant": HOTFIX_VARIANT,
            "writeSetSource": write_set_source,
            "planOnly": plan_only,
            "qualified": qualified,
            "gatesEvaluated": gates_evaluated,
            "gatesPassed": gates_passed,
            "gateIds": list(HOTFIX_GATE_IDS),
            "gateFailures": (gates or {}).get("failures") or [],
            "halted": halted,
            "steps": steps,
        },
        started_at=started,
    )


def _commands(cfg: Config | None) -> dict[str, str]:
    """Prefer a hotfix override, then the repo's own commands, then a default."""
    raw = (cfg.raw if cfg else {}) or {}
    configured = raw.get("hotfix", {})
    if isinstance(configured, dict) and isinstance(configured.get("commands"), dict):
        return {str(k): str(v) for k, v in configured["commands"].items() if v}
    if isinstance(raw.get("commands"), dict) and raw["commands"]:
        return {str(k): str(v) for k, v in raw["commands"].items() if v}
    return {"test": "python -m pytest -q", "lint": "ruff check ."}


def _message(
    incident: Incident,
    status: str,
    failed: Sequence[StepRecord],
    gates_evaluated: bool,
    gates_passed: bool,
    plan_only: bool,
    write_set_source: str,
    halted: str | None,
) -> str:
    parts = [
        (f"incident {incident.get('id')} ({incident.get('severity', 'sev3')}) -> "
         "brief.md + 3-node hotfix graph")
    ]
    if halted:
        parts.append(f"HALTED: {halted}")
    elif plan_only:
        parts.append("plan-only: no build, evidence, gate or reduce step was executed")
    elif failed:
        parts.append("failed step(s): " + ", ".join(s["step"] for s in failed))
    elif not gates_evaluated:
        parts.append(
            f"required gates ({', '.join(HOTFIX_GATE_IDS)}) were NOT evaluated - "
            "this run is not green"
        )
    elif not gates_passed:
        parts.append("required gates evaluated and the aggregate FAILED")
    else:
        parts.append(f"required gates passed: {', '.join(HOTFIX_GATE_IDS)}")
    if write_set_source == "fallback":
        parts.append(
            f"fix write set is the placeholder {list(FALLBACK_WRITE_SET)} - no incident hint "
            "and no hotfix.writeSet in config.yaml; refine it before trusting the build"
        )
    parts.append(f"status={status}")
    return "; ".join(parts)


def _signal_sentence(signal: dict[str, Any]) -> str:
    """One incident signal rendered for a task capsule."""
    label = signal.get("description") or signal.get("id") or "signal"
    if signal.get("value") is None:
        return str(label)
    return f"{label} (observed {signal.get('value')} vs threshold {signal.get('threshold')})"


def _clean_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    seen: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _safe(text: str, sep: str = "-") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", sep, str(text)).strip("-_.") or "incident"
    return slug[:64]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
