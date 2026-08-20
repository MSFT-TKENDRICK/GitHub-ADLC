"""The report context and the two helpers every section shares.

A section is a pure function ``render(ctx: ReportContext) -> str`` returning an
HTML fragment, or ``""`` to be omitted entirely. :class:`ReportContext` carries
everything a section could need -- the parsed run document plus the cheap
derived values that used to be recomputed inline -- so sections never touch the
filesystem and never recompute shared state.

:func:`escape` is the *single* HTML-escape in the report. Every section routes
untrusted text -- artifact paths, ADR titles, review prose, anything sourced
from a repo or a human -- through it, so no value can open or close a tag or an
attribute. :func:`omission` is the shared convention for "this section has no
data": a stated reason, never a silently empty table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as _html_escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config
    from adlc.runs import RunDir


def escape(value: Any) -> str:
    """HTML-escape ``value`` for use in text or a quoted attribute.

    Coerces to ``str`` first (``None`` becomes the empty string) so callers can
    pass numbers, paths or ``None`` without a ``TypeError`` sneaking a raw value
    into the document.
    """
    return _html_escape("" if value is None else str(value), quote=True)


def omission(reason: str) -> str:
    """Render a stated reason a section is empty rather than an empty shell."""
    return f'<p class="muted">{escape(reason)}</p>'


@dataclass(frozen=True)
class ReportContext:
    """Everything the sections need, computed once from ``(cfg, rd)``."""

    cfg: Config
    rd: RunDir
    run: dict[str, Any] = field(default_factory=dict)
    gates: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    score: dict[str, Any] | None = None
    graph: dict[str, Any] | None = None
    qualification: dict[str, Any] | None = None
    repo: str = ""
    pr: int | None = None
    passed: bool = True
    failures: list[str] = field(default_factory=list)
