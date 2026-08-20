"""Report orchestration: build the context, assemble the sections, wrap them.

This module owns the two names the rest of the framework imports -- :func:`render`
and :func:`run_report` -- with exactly the signatures and return shapes they had
when the report was a single module. Everything else moved into sections, the
shell and the context; this file just wires them together.
"""

from __future__ import annotations

from typing import Any

from adlc.config import Config
from adlc.reduce import aggregate_passed, load_run
from adlc.runs import RunDir, read_json, utcnow
from adlc.stages.report.context import ReportContext
from adlc.stages.report.sections import SECTIONS
from adlc.stages.report.shell import render_shell


def build_context(cfg: Config, rd: RunDir) -> ReportContext:
    """Read everything the sections need from disk, exactly once."""
    run = load_run(rd)
    gates = run.get("gates") or []
    passed, failures = aggregate_passed(gates)

    score_path = rd.evals_dir / "rubric-score.json"
    score = read_json(score_path) if score_path.is_file() else None

    graph = read_json(rd.taskgraph) if rd.taskgraph.is_file() else None

    qual_path = rd.path / "qualification.json"
    qualification = read_json(qual_path) if qual_path.is_file() else None

    return ReportContext(
        cfg=cfg,
        rd=rd,
        run=run,
        gates=gates,
        artifacts=run.get("artifacts") or [],
        stages=run.get("stages") or [],
        score=score,
        graph=graph,
        qualification=qualification,
        repo=run.get("repo", ""),
        pr=run.get("prNumber"),
        passed=passed,
        failures=failures,
    )


def render_body(ctx: ReportContext) -> str:
    """Join every non-empty section in registry order.

    An empty section contributes nothing -- not even a separator -- so a stub or
    a data-less section leaves no trace in the output.
    """
    parts = [fragment for section in SECTIONS if (fragment := section.render(ctx))]
    return "\n\n".join(parts)


def render(cfg: Config, rd: RunDir) -> str:
    """Render the full self-contained ``report.html`` as a string."""
    ctx = build_context(cfg, rd)
    return render_shell(ctx, render_body(ctx))


def run_report(cfg: Config, rd: RunDir) -> dict[str, Any]:
    started = utcnow()
    html = render(cfg, rd)
    rd.report.write_text(html, encoding="utf-8")
    rd.write_stage(
        "report",
        outputs=[rd.rel(rd.report)],
        message=f"report.html rendered ({len(html)//1024} KB, self-contained)",
        data={"bytes": len(html)},
        started_at=started,
    )
    return {"path": str(rd.report), "bytes": len(html)}
