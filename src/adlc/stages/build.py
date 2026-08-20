"""Build stage -- drives the topological executor over the task graph.

Resume semantics: the number of completed level barriers is recorded in each
build stage result, so re-running ``adlc build`` continues from the last
completed barrier instead of redoing merged work.
"""

from __future__ import annotations

import asyncio
from typing import Any

from adlc.config import Config, select_adapter
from adlc.executor import Executor
from adlc.ports import TaskGraph
from adlc.runs import RunDir, read_json, utcnow, write_json
from adlc.stages.graph import regenerate_capsules


def _completed_levels(rd: RunDir) -> int:
    """Highest completed barrier across previous build attempts."""
    best = 0
    for result in rd.stage_results():
        if result.get("stage") != "build":
            continue
        best = max(best, int((result.get("data") or {}).get("completedLevels", 0)))
    return best


def run_build(
    cfg: Config,
    rd: RunDir,
    *,
    runner_name: str | None = None,
    max_parallel: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    started = utcnow()

    if not rd.taskgraph.is_file():
        raise FileNotFoundError(f"{rd.taskgraph} not found - run `adlc graph` first")
    graph: TaskGraph = read_json(rd.taskgraph)

    runner = select_adapter(cfg, "agents", runner_name)
    parallel = max_parallel or int((cfg.limits or {}).get("maxParallel", 4))
    resume_from = _completed_levels(rd) if resume else 0

    if resume_from:
        graph = regenerate_capsules(cfg, graph, resume_from)
        write_json(rd.taskgraph, graph)

    executor = Executor(cfg, rd, runner, max_parallel=parallel)
    report = asyncio.run(executor.run(graph, resume_from=resume_from))

    # Refresh capsules for whatever comes next, so a later attempt is not stale.
    if report.completed_levels < report.levels:
        graph = regenerate_capsules(cfg, graph, report.completed_levels)
        write_json(rd.taskgraph, graph)

    failed = [n.node_id for n in report.nodes if n.status == "fail"]
    conflicts = [c for b in report.barriers for c in b["conflicts"]]
    test_failures = [b["level"] for b in report.barriers if not b["testsPassed"]]
    ok = not failed and not conflicts and not test_failures

    variants = [
        {"key": "control", "role": "control", "commit": (report.nodes and graph.get("baseSha")) or "", "flagKeys": []},
        {
            "key": "candidate-a",
            "role": "treatment",
            "commit": report.base_sha,
            "flagKeys": [f"adlc.exp.{rd.run_id}"],
        },
    ]

    data = {
        **report.to_data(),
        "runner": getattr(runner, "name", type(runner).__name__),
        "maxParallel": parallel,
        "resumedFrom": resume_from,
        "failedNodes": failed,
        "variants": variants,
    }

    message = (
        f"{len(report.nodes)} node(s) over {report.levels} level(s) "
        f"with runner '{data['runner']}' (parallel={parallel})"
    )
    if not ok:
        detail = []
        if failed:
            detail.append(f"{len(failed)} node failure(s)")
        if conflicts:
            detail.append(f"{len(conflicts)} patch conflict(s)")
        if test_failures:
            detail.append(f"tests failed at level(s) {test_failures}")
        message += " - " + "; ".join(detail)

    rd.write_stage(
        "build",
        status="ok" if ok else "fail",
        outputs=[n.patch for n in report.nodes if n.patch],
        message=message,
        data=data,
        started_at=started,
    )
    return data
