"""Raw run record section: the full run.json, escaped, inside a <details>."""

from __future__ import annotations

import json

from adlc.stages.report.context import ReportContext, escape


def render(ctx: ReportContext) -> str:
    run_json = escape(json.dumps(ctx.run, indent=2))
    return "\n".join(
        [
            "  <h2>Raw run record</h2>",
            f'  <details><summary>run.json (adlc-run/v1)</summary><pre>{run_json}</pre></details>',
        ]
    )
