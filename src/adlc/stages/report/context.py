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


#: Whole-document ceiling on inlined ``data:`` image bytes.
#:
#: Two properties of this number were previously wrong in ways that cancelled
#: out of every test but not out of the artifact:
#:
#: * It is charged in **encoded** bytes -- what actually lands in the file --
#:   not raw file size. base64 is 4/3, so charging raw under-counts by a third.
#: * It is scoped to the **document**, not to a section. Two sections each
#:   holding a private allowance of the same nominal size produce a document
#:   twice the intended size, and each section's own accounting looks correct.
#:
#: The number exists only to keep ``report.html`` mailable -- that is the entire
#: reason images are inlined rather than referenced -- so it is sized against
#: real attachment limits (Outlook 20 MB, Gmail 25 MB) with room for the rest of
#: the document.
MAX_INLINE_BYTES_DOCUMENT = 12 * 1024 * 1024


def encoded_data_uri_len(raw_len: int, mime: str) -> int:
    """Exact length of the ``data:`` URI for ``raw_len`` bytes, without encoding.

    Lets a caller test the budget *before* paying for the base64 allocation.
    """
    return len(f"data:{mime};base64,") + 4 * ((raw_len + 2) // 3)


@dataclass
class InlineBudget:
    """One document-scoped allowance for inlined image bytes.

    Deliberately mutable and deliberately shared: it is the only thing that
    knows the true size of the document being assembled.
    """

    total: int = MAX_INLINE_BYTES_DOCUMENT
    spent: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.spent)

    def charge(self, encoded_len: int) -> bool:
        """Charge ``encoded_len`` against the budget if it fits.

        Returns whether it fit. A rejected charge costs nothing, so a single
        oversized image never strands the budget for the images after it.
        """
        if encoded_len > self.remaining:
            return False
        self.spent += encoded_len
        return True


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
    #: Shared across every section -- see :class:`InlineBudget`. Mutable by
    #: design even though the context is frozen: sections charge against one
    #: allowance as they render.
    inline_budget: InlineBudget = field(default_factory=lambda: InlineBudget())
