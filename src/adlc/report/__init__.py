"""The evidence report: one self-contained, navigable HTML file per run.

Split into modules by *what the reader is asking*, not by technology:

* :mod:`~adlc.report.model` -- the whole page's data, computed in Python so the
  browser only paints.
* :mod:`~adlc.report.diff` -- unified-diff parsing with word-level highlights
  pre-resolved.
* :mod:`~adlc.report.media` -- the end-to-end recording and the before/after
  comparisons, embedded within a hard byte budget.
* :mod:`~adlc.report.adr` -- decision records parsed into detail views with a
  classified citation index.
* :mod:`~adlc.report.assets` / :mod:`~adlc.report.html` -- the inline CSS/JS and
  the page shell.

``adlc.stages.report`` remains the stage entry point and re-exports
:func:`render` and :func:`run_report` from here.
"""

from __future__ import annotations

from adlc.report.html import render, run_report

__all__ = ["render", "run_report"]
