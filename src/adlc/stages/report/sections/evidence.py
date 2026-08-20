"""Evidence section: the hash-verified artifact table.

Layer 4 owns this file. It will grow this read-only table into annotatable
evidence -- visual markup on screenshots, keyed by the SHA-256 shown here.
Today it renders exactly what the original report did, including the
empty-state row when a run captured no artifacts.
"""

from __future__ import annotations

from typing import Any

from adlc.stages.report.context import ReportContext, escape


def _artifact_rows(artifacts: list[dict[str, Any]]) -> str:
    rows = []
    for art in artifacts:
        size = art.get("bytes", 0)
        human = f"{size/1024:.1f} KB" if size >= 1024 else f"{size} B"
        digest = art.get("sha256", "")
        rows.append(
            f'<tr><td class="mono"><a href="{escape(art.get("path", ""))}">{escape(art.get("path", ""))}</a></td>'
            f'<td><span class="tag">{escape(art.get("kind", ""))}</span></td>'
            f'<td class="num">{human}</td>'
            f'<td class="mono hash" title="{escape(digest)}">{escape(digest[:16])}&hellip;</td></tr>'
        )
    return "\n".join(rows)


def render(ctx: ReportContext) -> str:
    artifact_rows = _artifact_rows(ctx.artifacts) or (
        '<tr><td colspan="4" class="muted">No artifacts captured.</td></tr>'
    )
    return "\n".join(
        [
            "  <h2>Evidence</h2>",
            '  <table><thead><tr><th>Artifact</th><th>Kind</th><th>Size</th><th>SHA-256</th></tr></thead>',
            f"  <tbody>{artifact_rows}</tbody></table>",
            '  <p class="note">Click a hash to copy it. Hashes are what the evidence gate verifies &mdash;',
            "    the reviewing agent never sees raw traces, HAR or console text.</p>",
        ]
    )
