"""The ordered registry of report sections.

Each entry is a module exposing ``render(ctx: ReportContext) -> str`` and owning
one contiguous slice of the report -- its heading and its body. The order of
:data:`SECTIONS` *is* the render order; :func:`adlc.stages.report.render_body`
drops any section that returns ``""`` (a stub, or a section with no data) without
leaving a gap.

Every section lives in its own module so that parallel workstreams have disjoint
write-sets. In particular the four interactive sections are owned by later
layers and only that layer edits that one file:

* ``evidence``  -- annotatable evidence (layer 4)
* ``reasoning`` -- critiques of agent reasoning (layer 5, stub today)
* ``diff``      -- accept/reject evidence deltas (layer 6, stub today)
* ``feedback``  -- the exported feedback pack (layer 7, stub today)

The three stubs are real, imported, registered modules that render nothing
*yet*. They are placed in their final render positions now so that lighting one
up never requires editing this registry -- which would reintroduce the merge
conflict the split exists to avoid.
"""

from __future__ import annotations

from adlc.stages.report.sections import (
    decisions,
    diff,
    evidence,
    feedback,
    gates,
    graph,
    rawrun,
    reasoning,
    rubric,
    summary,
    timeline,
)

#: Render order. Existing sections keep their historical positions; the three
#: interactive stubs sit between evidence and decisions in layer order (5, 6, 7),
#: where the review flow (see the evidence, critique the reasoning, review what
#: changed, submit) will read naturally once layers 5-7 fill them in.
SECTIONS = (
    summary,
    gates,
    rubric,
    graph,
    evidence,
    reasoning,
    diff,
    feedback,
    decisions,
    timeline,
    rawrun,
)

__all__ = ["SECTIONS"]
