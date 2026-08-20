"""Reasoning section -- STUB owned by layer 5.

Layer 5 turns this into critiques of agent-authored reasoning. Until then it
renders nothing: :func:`render` returns ``""`` so
:func:`adlc.stages.report.render.render_body` omits it entirely -- no heading,
no empty ``<section>``, no "coming soon" placeholder. Emitting nothing is
deliberate: the report must stay byte-for-byte identical to the pre-split
output, which is this layer's acceptance criterion. Do not emit markup here;
that is layer 5's change to make.
"""

from __future__ import annotations

from adlc.stages.report.context import ReportContext


def render(ctx: ReportContext) -> str:
    return ""
