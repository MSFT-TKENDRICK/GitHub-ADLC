"""Timeline section: the per-stage execution log as an ordered list."""

from __future__ import annotations

from typing import Any

from adlc.stages.report.context import ReportContext, escape


def _timeline(stages: list[dict[str, Any]]) -> str:
    items = []
    for stage in stages:
        status = stage.get("status", "ok")
        cls = {"ok": "ok", "fail": "bad", "skipped": "warn"}.get(status, "warn")
        items.append(
            f'<li class="{cls}"><div class="t-head">'
            f'<span class="mono">{escape(stage.get("stage", ""))}</span>'
            f'<span class="tag">attempt {stage.get("attempt", 1)}</span>'
            f'<time>{escape(stage.get("startedAt", ""))}</time></div>'
            f'<p>{escape(stage.get("message", ""))}</p></li>'
        )
    return "\n".join(items)


def render(ctx: ReportContext) -> str:
    return "\n".join(
        [
            "  <h2>Timeline</h2>",
            f'  <ol class="timeline">{_timeline(ctx.stages)}</ol>',
        ]
    )
