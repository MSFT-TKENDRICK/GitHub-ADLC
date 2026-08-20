"""Rubric section: the eval meter and per-criterion score table.

Renders a stated reason (never an empty table) when a run has no rubric score,
matching the original output exactly.
"""

from __future__ import annotations

from typing import Any

from adlc.stages.report.context import ReportContext, escape, omission


def _rubric_block(score: dict[str, Any] | None) -> str:
    if not score:
        return omission("No rubric score recorded for this run.")
    rows = []
    for crit in score.get("criteria", []):
        cls = "ok" if crit.get("passed") else "bad"
        rows.append(
            f'<tr><td class="mono">{escape(crit.get("id", ""))}</td>'
            f'<td><span class="pill {cls}">{crit.get("score", 0):.2f}</span></td>'
            f'<td class="num">{crit.get("weight", 1)}</td>'
            f'<td>{escape(crit.get("rationale", ""))}</td></tr>'
        )
    overall = score.get("overall", 0)
    threshold = score.get("threshold", 0)
    pct = min(100, max(0, overall * 100))
    bar_cls = "ok" if score.get("passed") else "bad"
    return (
        f'<div class="meter"><div class="meter-fill {bar_cls}" style="width:{pct:.1f}%"></div>'
        f'<div class="meter-mark" style="left:{threshold*100:.1f}%" title="threshold {threshold}"></div></div>'
        f'<p class="muted">Overall <strong>{overall:.2f}</strong> against a threshold of '
        f'<strong>{threshold:.2f}</strong>.</p>'
        f'<table><thead><tr><th>Criterion</th><th>Score</th><th>Weight</th><th>Rationale</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def render(ctx: ReportContext) -> str:
    return "\n".join(
        [
            "  <h2>Rubric</h2>",
            f"  {_rubric_block(ctx.score)}",
        ]
    )
