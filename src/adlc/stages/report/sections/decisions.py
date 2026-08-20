"""Decisions section: ADR cards wired to native GitHub PR reviews.

The review deep link is built from the run document's ``repo`` and ``prNumber``
fields. Those are untrusted (they come from a git remote / ``GITHUB_REPOSITORY``
and the run record), so the assembled URL is routed through :func:`escape`
before it is placed in the ``href`` attribute. The original interpolated it raw
-- an HTML-injection bug fixed here. Renders a stated reason when no ADRs exist.
"""

from __future__ import annotations

from typing import Any

from adlc.config import Config
from adlc.stages.adr import list_adrs
from adlc.stages.report.context import ReportContext, escape, omission


def _adr_cards(cfg: Config, run: dict[str, Any], repo: str, pr: int | None) -> str:
    adrs = list_adrs(cfg)
    if not adrs:
        return omission("No architecture decisions recorded yet.")
    base = f"https://github.com/{repo}/pull/{pr}/files" if repo and pr else ""
    href = escape(base)
    cards = []
    for adr in adrs:
        cls = {"accepted": "ok", "rejected": "bad", "proposed": "warn"}.get(adr.status, "warn")
        actions = (
            f'<a class="btn ok" href="{href}#submit-review" target="_blank" rel="noopener">Approve &rarr; accept</a>'
            f'<a class="btn bad" href="{href}#submit-review" target="_blank" rel="noopener">Request changes &rarr; revise</a>'
            if base else '<span class="muted">Link a pull request to enable review actions.</span>'
        )
        cards.append(
            f'<article class="card"><header><span class="pill {cls}">{escape(adr.status)}</span>'
            f'<h3>{escape(adr.number)} &mdash; {escape(adr.title)}</h3></header>'
            f'<p class="mono muted">docs/decisions/{escape(adr.path.name)}</p>'
            f'<div class="actions">{actions}</div></article>'
        )
    return "\n".join(cards)


def render(ctx: ReportContext) -> str:
    return "\n".join(
        [
            "  <h2>Decisions</h2>",
            f'  <div class="cards">{_adr_cards(ctx.cfg, ctx.run, ctx.repo, ctx.pr)}</div>',
            '  <p class="note">Decisions are recorded through native GitHub pull request reviews:',
            "    <em>Approve</em> accepts the ADR, <em>Request changes</em> rejects it and opens a successor run.",
            "    History is never rewritten.</p>",
        ]
    )
