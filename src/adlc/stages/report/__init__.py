"""Report stage -- one self-contained, interactive HTML report per run.

Design constraints that shaped this:

* **Single file, no build step, no framework.** It is uploaded as a CI artifact
  and linked from a PR, so it must open from ``file://`` with nothing installed.
* **No backend.** Human actions (accept / reject / revise an ADR, annotate
  evidence) are round-tripped through *native GitHub PR reviews* via pre-filled
  deep links, so there is nothing to host and nothing to authenticate.
* **Evidence is auditable, not decorative.** Every artifact is shown with its
  SHA-256 so a reader can verify what they are looking at.

The report is assembled from independently-owned *section* modules (see
:mod:`adlc.stages.report.sections`) substituted into a single HTML shell whose
CSS and JS live in real, editable asset files. Keeping CSS/JS out of the shell
template is what lets later layers inject JavaScript without doubling every
brace: :func:`str.format` never rescans a substituted value.

This package is the public surface. Import :func:`render` and :func:`run_report`
from here; everything else is an implementation detail.
"""

from __future__ import annotations

from adlc.stages.report.render import render, run_report

__all__ = ["render", "run_report"]
