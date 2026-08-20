"""Task graph section: the mermaid flowchart of the task DAG.

The mermaid source is emitted as escaped text inside ``<div class="mermaid">``.
The CDN script upgrades it to a diagram; offline the source stays readable as
plain text, so the report is useful with no network.
"""

from __future__ import annotations

from typing import Any

from adlc.stages.report.context import ReportContext, escape


def _graph_mermaid(graph: dict[str, Any] | None) -> str:
    if not graph or not graph.get("nodes"):
        return "flowchart LR\n  none[No task graph]"
    lines = ["flowchart LR"]
    levels: dict[int, list[dict]] = {}
    for node in graph["nodes"]:
        levels.setdefault(node.get("level", 0), []).append(node)
    for level in sorted(levels):
        lines.append(f'  subgraph L{level}["level {level}"]')
        for node in levels[level]:
            title = str(node.get("title", ""))[:44].replace('"', "'")
            lines.append(f'    {node["id"]}["{node["id"]}<br/>{title}"]')
        lines.append("  end")
    for node in graph["nodes"]:
        for dep in node.get("dependsOn") or []:
            lines.append(f"  {dep} --> {node['id']}")
    return "\n".join(lines)


def render(ctx: ReportContext) -> str:
    return "\n".join(
        [
            "  <h2>Task graph</h2>",
            f'  <div class="mermaid">{escape(_graph_mermaid(ctx.graph))}</div>',
            '  <p class="note">Nodes on the same level ran concurrently in isolated worktrees; each level ends at a',
            "    patch barrier where patches are applied in id order and tests run.</p>",
        ]
    )
