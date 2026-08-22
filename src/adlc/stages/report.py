"""Report stage entry point.

The report outgrew a single module once it had to carry a laid-out task graph, a
diff engine, embedded media and decision detail views, so the implementation now
lives in the :mod:`adlc.report` package. This module stays because it is the
import path the CLI, the conformance driver and the stage vocabulary all use, and
breaking that to satisfy a directory layout would be a poor trade.
"""

from __future__ import annotations

from adlc.report.html import fill, render, run_report
from adlc.report.model import build_model, graph_mermaid

__all__ = ["build_model", "fill", "graph_mermaid", "render", "run_report"]
