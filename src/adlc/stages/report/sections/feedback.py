"""Feedback section -- STUB owned by layer 7.

Layer 7 turns this into the exported feedback pack: the single JSON document
(evidence annotations, reasoning critiques, accept/reject deltas) that
retriggers the design outer loop. Until then it renders nothing: :func:`render`
returns ``""`` so :func:`adlc.stages.report.render.render_body` omits it
entirely -- no heading, no empty ``<section>``, no "coming soon" placeholder.
Emitting nothing is deliberate: the report must stay byte-for-byte identical to
the pre-split output, which is this layer's acceptance criterion. Do not emit
markup here; that is layer 7's change to make.
"""

from __future__ import annotations

from adlc.stages.report.context import ReportContext


def render(ctx: ReportContext) -> str:
    return ""
