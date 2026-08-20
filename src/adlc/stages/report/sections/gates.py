"""Gates section: the required/optional gate table with per-gate detail."""

from __future__ import annotations

import json
from typing import Any

from adlc.stages.report.context import ReportContext, escape

_STATUS_META = {
    "pass": ("ok", "&#10003;", "Pass"),
    "fail": ("bad", "&#10007;", "Fail"),
    "not_run": ("warn", "&#8213;", "Not run"),
}


def _gate_rows(gates: list[dict[str, Any]]) -> str:
    rows = []
    for gate in gates:
        cls, glyph, label = _STATUS_META.get(gate.get("status", ""), ("warn", "?", "Unknown"))
        required = "Required" if gate.get("required") else "Optional"
        rows.append(
            f'<tr class="g-{cls}">'
            f'<td><span class="pill {cls}">{glyph} {label}</span></td>'
            f'<td class="mono">{escape(gate.get("id", ""))}</td>'
            f'<td><span class="tag">{required}</span></td>'
            f'<td>{escape(gate.get("message", ""))}</td>'
            f'<td><details><summary>detail</summary>'
            f'<pre>{escape(json.dumps({"observed": gate.get("observed"), "expected": gate.get("expected")}, indent=2))}</pre>'
            f'</details></td></tr>'
        )
    return "\n".join(rows)


def render(ctx: ReportContext) -> str:
    return "\n".join(
        [
            "  <h2>Gates</h2>",
            '  <table><thead><tr><th>Status</th><th>Gate</th><th>Enforcement</th><th>Message</th><th></th></tr></thead>',
            f"  <tbody>{_gate_rows(ctx.gates)}</tbody></table>",
        ]
    )
