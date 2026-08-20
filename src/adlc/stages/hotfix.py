"""``adlc hotfix --incident FILE`` — the day-2 stage.

What this stage is
------------------
A hotfix is **not a new kind of pipeline**. It is a deliberately narrow lap of
the ordinary one. This stage does exactly three things it owns, and then hands
off to the *documented* CLI surface (``docs/PLAN.md`` §4.9) for everything else:

1. **incident → brief** — via :class:`~adlc.adapters.daytwo.sre_agent.SreAgentReceiver`,
   producing the same ``brief.md`` day-1 intake produces.
2. **brief → run** — by calling ``adlc run new --brief …``, i.e. the *same front
   door* a human-authored brief uses. Day-2 has no private entry point.
3. **narrow task graph** — 3 nodes, not a full decomposition: reproduce, fix,
   record. ``adlc spec`` / ``adlc enrich`` are skipped on purpose; that is what
   makes it a *hotfix*.

Then ``build → evidence → gate → reduce → report`` run unchanged, with the same
fail-closed aggregator. A hotfix earns its merge the same way every other change
does.

Invariants this module honours
------------------------------
* **It never writes ``run.json``.** Only ``adlc reduce`` may (``docs/PLAN.md``
  §4.2). This stage writes ``runs/<run>/stages/hotfix.<attempt>.json`` plus its
  own outputs (``brief.md``, ``incident.json``, ``taskgraph.json``).
* **Stage results are append-only.** A re-run computes ``attempt = n + 1`` by
  counting existing ``hotfix.*.json`` files; it never overwrites one.
* **It fails closed.** If the required gates were not actually evaluated, the
  process exits non-zero unless you explicitly pass ``--allow-incomplete`` (or
  ``--plan-only``, which asserts nothing). A hotfix that skipped its gates is
  never reported as green.
* **It invents no APIs.** Downstream work is done by shelling out to the
  ``adlc`` commands frozen in §4.9 — nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from adlc.adapters.daytwo.sre_agent import Incident, SreAgentReceiver
from adlc.config import Config
from adlc.ports import PROTECTED_PATHS, StageResult

STAGE_NAME = "hotfix"

#: Gate ids a hotfix must clear. The `minimal` profile's required set
#: (``adlc.config.PROFILE_REQUIRED_GATES``) — a hotfix does not get a weaker bar.
HOTFIX_GATE_IDS: tuple[str, ...] = (
    "tests", "secrets_local", "deps_local", "evidence_completeness",
)

#: The single candidate a hotfix builds. A hotfix is not an experiment, so there
#: is one variant and no control (``docs/PLAN.md`` §4.4: a candidate is a build
#: artifact at a commit, not automatically a flag variant).
HOTFIX_VARIANT = "hotfix"

#: Used only when neither the incident nor ``config.yaml`` declares a write set.
#: Recorded as ``writeSetSource: "fallback"`` so nobody mistakes it for analysis.
FALLBACK_WRITE_SET: tuple[str, ...] = ("src/",)


class CommandResult(TypedDict, total=False):
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    ok: bool


#: An executor runs one ``adlc`` command. Swappable so tests never shell out.
Executor = Callable[[Sequence[str], Path], CommandResult]


class StepRecord(TypedDict, total=False):
    step: str
    argv: list[str]
    status: str          # ok | fail | skipped
    returncode: int | None
    message: str


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


def subprocess_executor(argv: Sequence[str], cwd: Path) -> CommandResult:
    """Run a documented ``adlc`` command. The only place this module spawns."""
    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd), capture_output=True, text=True, check=False, timeout=3600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"argv": list(argv), "returncode": None, "stdout": "", "stderr": str(exc),
                "ok": False}
    return {
        "argv": list(argv),
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "ok": proc.returncode == 0,
    }


def plan_only_executor(argv: Sequence[str], cwd: Path) -> CommandResult:
    """Record the command that *would* run. Nothing is executed."""
    return {"argv": list(argv), "returncode": None, "stdout": "", "stderr": "", "ok": False}


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def run_hotfix(
    incident_path: Path | str | None = None,
    *,
    cfg: Config | None = None,
    incident: Incident | None = None,
    executor: Executor | None = None,
    plan_only: bool = False,
    run_id: str | None = None,
    base_sha: str | None = None,
) -> StageResult:
    """Convert an incident into a narrow ADLC run.

    Returns a :class:`~adlc.ports.StageResult`, which is also persisted to
    ``runs/<run>/stages/hotfix.<attempt>.json``.
    """
    started = _utcnow()
    cfg = cfg or Config.load()
    receiver = SreAgentReceiver()

    if incident is None:
        incident = receiver.load(incident_path)

    if plan_only:
        executor = plan_only_executor
    elif executor is None:
        executor = subprocess_executor

    steps: list[StepRecord] = []
    outputs: list[str] = []

    # 1. incident → brief. Written to a staging dir first because the run id is
    #    not known until `adlc run new` answers.
    staging = cfg.adlc_dir / "incoming" / _safe(str(incident.get("id", "incident")))
    brief_path = receiver.write_brief(incident, staging)

    # 2. brief → run, through the *day-1* front door.
    resolved_run_id, run_step = _create_run(cfg, brief_path, run_id, executor, plan_only)
    steps.append(run_step)
    minted_locally = run_step["status"] != "ok"
    run_dir = cfg.run_dir(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Copy our own artifacts into the run directory. `brief.md` may already have
    # been placed there by `adlc run new`; ours is byte-identical, so this is
    # idempotent rather than destructive.
    (run_dir / "brief.md").write_text(brief_path.read_text(encoding="utf-8"), encoding="utf-8")
    (run_dir / "incident.json").write_text(
        (staging / "incident.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    outputs += ["brief.md", "incident.json"]

    # 3. narrow task graph — 3 nodes, no spec/enrich pass.
    graph, write_set_source = build_hotfix_graph(
        incident, run_id=resolved_run_id, base_sha=base_sha or _resolve_base_sha(cfg), cfg=cfg
    )
    (run_dir / "taskgraph.json").write_text(
        json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    outputs.append("taskgraph.json")

    # 4. Hand off to the frozen CLI surface (docs/PLAN.md §4.9).
    for step in _downstream_steps(resolved_run_id, cfg):
        steps.append(_execute(step["step"], step["argv"], cfg.root, executor, plan_only))

    gates_step = next((s for s in steps if s["step"] == "gate"), None)
    gates_executed = bool(gates_step and gates_step["status"] == "ok")
    failed = [s for s in steps if s["status"] == "fail"]

    status: str = "fail" if failed else "ok"
    message = _message(
        incident, status, failed, gates_executed, plan_only, minted_locally, write_set_source
    )

    result: StageResult = {
        "stage": STAGE_NAME,
        "attempt": _next_attempt(run_dir),
        "status": status,  # type: ignore[typeddict-item]
        "startedAt": started,
        "endedAt": _utcnow(),
        "outputs": outputs,
        "digest": _digest(run_dir, outputs),
        "message": message,
        "data": {
            "runId": resolved_run_id,
            "incidentId": incident.get("id"),
            "incidentSchemaVersion": incident.get("schemaVersion"),
            "severity": incident.get("severity"),
            "source": incident.get("source"),
            "variant": HOTFIX_VARIANT,
            "taskNodeIds": [n["id"] for n in graph["nodes"]],
            "writeSetSource": write_set_source,
            "planOnly": plan_only,
            "runCreatedByCli": not minted_locally,
            "gatesEvaluated": gates_executed,
            "gateIds": list(HOTFIX_GATE_IDS),
            "steps": steps,
        },
    }
    _write_stage_result(run_dir, result)
    return result


def build_hotfix_graph(
    incident: Incident,
    *,
    run_id: str,
    base_sha: str,
    cfg: Config | None = None,
) -> tuple[dict[str, Any], str]:
    """Build the 3-node hotfix graph. Returns ``(graph, write_set_source)``.

    The shape is fixed on purpose — a hotfix that needs a bespoke decomposition
    is not a hotfix, it is a feature, and should go through ``adlc spec``.

    ==========  =====  ============================================
    node        level  purpose
    ==========  =====  ============================================
    ``T001``      0    regression test that reproduces the incident
    ``T002``      1    the minimal fix
    ``T003``      1    the incident record
    ==========  =====  ============================================

    ``T002`` and ``T003`` share level 1, so their write sets must not overlap
    (``docs/PLAN.md`` §4.4) — they cannot, one writes code and one writes
    ``docs/incidents/``.
    """
    incident_id = _safe(str(incident.get("id") or "incident"))
    title = str(incident.get("title") or "incident")
    fix_write_set, write_set_source = resolve_write_set(incident, cfg)

    test_path = f"tests/regression/test_{_safe(incident_id, sep='_')}.py"
    record_path = f"docs/incidents/{incident_id}.md"
    commands = _commands(cfg)

    def capsule(do_not_touch_extra: Sequence[str] = ()) -> dict[str, Any]:
        return {
            "refs": [],
            "interfaces": (
                f"Incident {incident_id} ({incident.get('severity', 'sev3')}): {title}. "
                "Full detail in the run's brief.md and incident.json."
            ),
            "conventions": (
                "Hotfix scope: change the minimum needed to clear the observed signal. "
                "No refactors, no dependency bumps, no drive-by cleanups."
            ),
            "commands": commands,
            "doNotTouch": [*PROTECTED_PATHS, *do_not_touch_extra],
            "budget": {"maxTotalBytes": 65536, "maxFileBytes": 8192, "maxFiles": 12},
        }

    signal_text = "; ".join(_signal_sentence(s) for s in (incident.get("signals") or [])) \
        or "see brief.md"

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
            "context": {
                **capsule(),
                "interfaces": (
                    f"Write a test that FAILS on the current commit and captures: {signal_text}."
                ),
            },
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
            "context": capsule([test_path]),
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
            "context": capsule(),
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

    Order: incident hint → ``config.yaml`` ``hotfix.writeSet`` → fallback.
    A ``"fallback"`` source is surfaced in the stage message *and* in
    ``data.writeSetSource`` — it is a placeholder, not an analysis result, and
    ``adlc graph`` should refine it before ``adlc build`` is trusted.
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
    """Entry point for ``adlc hotfix`` / ``python -m adlc.stages.hotfix``.

    Exit codes — fail closed:

    ``0``
        Stage ok **and** the required gates were actually evaluated (or
        ``--plan-only`` / ``--allow-incomplete`` was passed).
    ``1``
        A step failed, or the gates did not run and you did not say that was ok.
    """
    parser = argparse.ArgumentParser(
        prog="adlc hotfix",
        description="Turn a day-2 incident into a narrow, fully gated ADLC run.",
    )
    parser.add_argument("--incident", type=Path, default=None,
                        help="incident payload JSON; defaults to $ADLC_INCIDENT_FILE / "
                             "$ADLC_INCIDENT_PAYLOAD / $GITHUB_EVENT_PATH")
    parser.add_argument("--plan-only", action="store_true",
                        help="write brief/graph and print the commands that would run")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit 0 even if the required gates were not evaluated")
    parser.add_argument("--run-id", default=None, help="use this run id instead of minting one")
    parser.add_argument("--base-sha", default=None, help="override the graph's baseSha")
    parser.add_argument("--json", action="store_true", help="print the StageResult as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = run_hotfix(
            args.incident, plan_only=args.plan_only, run_id=args.run_id, base_sha=args.base_sha
        )
    except Exception as exc:  # noqa: BLE001 - a CLI must report, not traceback
        print(f"adlc hotfix: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"[{result['status']}] {result['message']}")
        for step in result["data"]["steps"]:
            print(f"  {step['status']:>7}  {' '.join(step['argv'])}")

    if result["status"] == "fail":
        return 1
    if result["data"]["gatesEvaluated"] or args.plan_only or args.allow_incomplete:
        return 0
    print(
        "adlc hotfix: required gates were not evaluated — refusing to report success. "
        "Re-run once the `adlc` CLI is available, or pass --allow-incomplete.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _downstream_steps(run_id: str, cfg: Config) -> list[dict[str, Any]]:
    """The frozen §4.9 commands a hotfix runs, in order.

    ``spec`` and ``enrich`` are absent by design — the narrow graph replaces
    them. ``reduce`` is what writes ``run.json``; this stage never does.
    """
    return [
        {"step": "build", "argv": ["adlc", "build", run_id, "--max-parallel",
                                   str((cfg.limits or {}).get("maxParallel", 4))]},
        {"step": "evidence", "argv": ["adlc", "evidence", run_id, "--variant", HOTFIX_VARIANT]},
        {"step": "gate", "argv": ["adlc", "gate", run_id, "--ids", ",".join(HOTFIX_GATE_IDS),
                                  "--profile", cfg.profile or "minimal"]},
        {"step": "reduce", "argv": ["adlc", "reduce", run_id]},
        {"step": "report", "argv": ["adlc", "report", run_id]},
    ]


def _create_run(
    cfg: Config,
    brief_path: Path,
    run_id: str | None,
    executor: Executor,
    plan_only: bool,
) -> tuple[str, StepRecord]:
    """Create the run through ``adlc run new --brief`` — the day-1 front door."""
    if run_id:
        return run_id, {
            "step": "run-new", "argv": ["adlc", "run", "new", "--brief", str(brief_path)],
            "status": "skipped", "returncode": None,
            "message": f"run id supplied explicitly: {run_id}",
        }

    argv = ["adlc", "run", "new", "--brief", str(brief_path), "--json"]
    record = _execute("run-new", argv, cfg.root, executor, plan_only)
    if record["status"] == "ok":
        if parsed := _run_id_from_json(record.get("message", "")):
            return parsed, record
        record["status"] = "fail"
        record["message"] = (
            "`adlc run new --json` produced no parsable runId; "
            f"output was: {record.get('message', '')[:200]}"
        )

    minted = _mint_run_id()
    record["message"] = (
        f"{record.get('message', '')} — minted run id {minted} locally instead"
    ).strip(" —")
    return minted, record


def _execute(
    step: str, argv: Sequence[str], cwd: Path, executor: Executor, plan_only: bool
) -> StepRecord:
    if plan_only:
        return {"step": step, "argv": list(argv), "status": "skipped", "returncode": None,
                "message": "--plan-only: not executed"}
    if executor is subprocess_executor and shutil.which(argv[0]) is None:
        return {"step": step, "argv": list(argv), "status": "skipped", "returncode": None,
                "message": f"`{argv[0]}` is not on PATH — install the adlc CLI and re-run"}

    result = executor(argv, cwd)
    output = (result.get("stdout") or "") + (result.get("stderr") or "")
    return {
        "step": step,
        "argv": list(argv),
        "status": "ok" if result.get("ok") else "fail",
        "returncode": result.get("returncode"),
        "message": output.strip()[:4000],
    }


_RUN_ID_RE = re.compile(r'"runId"\s*:\s*"([^"]+)"')


def _run_id_from_json(text: str) -> str | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("runId"), str):
                return data["runId"]
    match = _RUN_ID_RE.search(text or "")
    return match.group(1) if match else None


def _mint_run_id() -> str:
    now = datetime.now(UTC)
    suffix = hashlib.sha256(now.isoformat().encode("utf-8")).hexdigest()[:4]
    return f"{now:%Y-%m-%d}-{suffix}"


def _next_attempt(run_dir: Path) -> int:
    """Stage results are append-only — count, never overwrite."""
    stages = run_dir / "stages"
    if not stages.is_dir():
        return 1
    return 1 + sum(1 for _ in stages.glob(f"{STAGE_NAME}.*.json"))


def _write_stage_result(run_dir: Path, result: StageResult) -> Path:
    stages = run_dir / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    path = stages / f"{STAGE_NAME}.{result['attempt']}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
    return path


def _digest(run_dir: Path, outputs: Sequence[str]) -> str:
    hasher = hashlib.sha256()
    for name in sorted(outputs):
        path = run_dir / name
        hasher.update(name.encode("utf-8"))
        if path.is_file():
            hasher.update(path.read_bytes())
    return "sha256:" + hasher.hexdigest()


def _resolve_base_sha(cfg: Config) -> str:
    """Best-effort HEAD lookup. Never fatal — the graph records what we found."""
    if sha := os.environ.get("GITHUB_SHA"):
        return sha
    git_dir = cfg.root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref_path = git_dir / head[5:].strip()
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip()
            packed = git_dir / "packed-refs"
            if packed.is_file():
                target = head[5:].strip()
                for line in packed.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == target:
                        return parts[0]
        elif re.fullmatch(r"[0-9a-f]{40}", head):
            return head
    except OSError:
        pass
    return "unknown"


def _commands(cfg: Config | None) -> dict[str, str]:
    configured = ((cfg.raw if cfg else {}) or {}).get("hotfix", {})
    commands = configured.get("commands") if isinstance(configured, dict) else None
    if isinstance(commands, dict) and commands:
        return {str(k): str(v) for k, v in commands.items()}
    return {"test": "python -m pytest -q", "lint": "ruff check .", "build": ""}


def _message(
    incident: Incident,
    status: str,
    failed: Sequence[StepRecord],
    gates_executed: bool,
    plan_only: bool,
    minted_locally: bool,
    write_set_source: str,
) -> str:
    parts = [
        (f"incident {incident.get('id')} ({incident.get('severity', 'sev3')}) -> "
         "brief.md + 3-node hotfix graph")
    ]
    if plan_only:
        parts.append("plan-only: no downstream command was executed")
    elif failed:
        parts.append("failed step(s): " + ", ".join(s["step"] for s in failed))
    elif not gates_executed:
        parts.append(
            f"required gates ({', '.join(HOTFIX_GATE_IDS)}) were NOT evaluated — "
            "this run is not green"
        )
    else:
        parts.append(f"required gates evaluated: {', '.join(HOTFIX_GATE_IDS)}")
    if minted_locally and not plan_only:
        parts.append("run id minted locally because `adlc run new` did not answer")
    if write_set_source == "fallback":
        parts.append(
            f"fix write set is the placeholder {list(FALLBACK_WRITE_SET)} — no incident hint and "
            "no hotfix.writeSet in config.yaml; refine it before trusting `adlc build`"
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


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
