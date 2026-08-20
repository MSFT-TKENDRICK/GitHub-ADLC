"""Layer 3 -- the report shell / section split is behaviour-preserving.

These tests pin the invariants the split must never break: the public import
surface, the self-contained shell, the six conformance fragments, the
brace-safety guarantee that is the whole reason the layer exists, the uniform
section contract (including stubs that render nothing), centralised escaping,
and the fix for the unescaped review-link interpolation.

Everything here runs offline with no credentials, reusing the L11 ``cfg``
fixture from ``conftest.py``.
"""

from __future__ import annotations

import pytest

from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages.adr import create_adr
from adlc.stages.report import render, run_report
from adlc.stages.report.context import ReportContext, escape, omission
from adlc.stages.report.render import render_body
from adlc.stages.report.sections import SECTIONS
from adlc.stages.report.sections import decisions as decisions_section
from adlc.stages.report.sections import diff as diff_section
from adlc.stages.report.sections import evidence as evidence_section
from adlc.stages.report.sections import feedback as feedback_section
from adlc.stages.report.sections import reasoning as reasoning_section
from adlc.stages.report.shell import read_asset, render_shell

CONFORMANCE_FRAGMENTS = ("ADLC run", "Gates", "Evidence", "Decisions", "Task graph", "flowchart")
STUB_SECTIONS = (reasoning_section, diff_section, feedback_section)


def _created_run(cfg: Config, run_id: str = "2026-08-20-she1") -> RunDir:
    """A freshly created run dir -- renderable offline from its seed alone."""
    rd = RunDir(cfg, run_id)
    rd.create(profile=cfg.profile, brief_text="# Brief\n\nA change.\n")
    return rd


def _context(cfg: Config, **overrides: object) -> ReportContext:
    rd = overrides.pop("rd", None) or RunDir(cfg, "2026-08-20-ctx0")
    return ReportContext(cfg=cfg, rd=rd, **overrides)


# ---------------------------------------------------------------------------
# Public surface and the self-contained shell
# ---------------------------------------------------------------------------


def test_public_api_is_importable_and_callable() -> None:
    assert callable(render)
    assert callable(run_report)


def test_render_starts_with_doctype(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    assert html.startswith("<!doctype html>")


def test_render_contains_all_conformance_fragments(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    for fragment in CONFORMANCE_FRAGMENTS:
        assert fragment in html, f"missing conformance fragment: {fragment!r}"


def test_render_is_self_contained(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    assert 'src="./' not in html
    assert 'href="./' not in html


def test_run_report_writes_file_and_returns_shape(cfg: Config) -> None:
    rd = _created_run(cfg)
    result = run_report(cfg, rd)
    # read_text reverses the write_text newline translation, so the round-tripped
    # length matches len(html) -- the in-memory count the contract reports as
    # "bytes" (the report is ASCII, so code points == bytes).
    written = rd.report.read_text(encoding="utf-8")
    assert result == {"path": str(rd.report), "bytes": len(written)}
    assert written.startswith("<!doctype html>")


# ---------------------------------------------------------------------------
# Brace safety -- the reason this layer exists
# ---------------------------------------------------------------------------


def test_assets_contain_braces() -> None:
    # If these ever lose their braces the guarantee below is vacuous.
    assert "{" in read_asset("base.css") and "}" in read_asset("base.css")
    assert "{" in read_asset("app.js") and "}" in read_asset("app.js")


def test_asset_braces_survive_rendering(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    # The asset text -- braces and all -- appears verbatim in the output. If
    # str.format had rescanned the substituted CSS/JS it would have raised on the
    # first bare brace instead of reaching here, or mangled the braces.
    assert read_asset("base.css") in html
    assert read_asset("app.js") in html
    assert ":root {" in html  # a concrete un-doubled brace from the CSS


def test_shell_does_not_rescan_substituted_braces(cfg: Config) -> None:
    # A body value packed with single braces must pass through untouched.
    body = "  <h2>Braces</h2>\n  <pre>{ not: a, format: field }</pre>"
    out = render_shell(_context(cfg), body)
    assert "{ not: a, format: field }" in out


# ---------------------------------------------------------------------------
# Asset loading
# ---------------------------------------------------------------------------


def test_read_asset_returns_text() -> None:
    css = read_asset("base.css")
    assert isinstance(css, str) and css.strip()


def test_read_asset_missing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        read_asset("does-not-exist.css")


# ---------------------------------------------------------------------------
# The section contract
# ---------------------------------------------------------------------------


def test_registry_has_expected_sections() -> None:
    names = [section.__name__.rsplit(".", 1)[-1] for section in SECTIONS]
    assert names == [
        "summary",
        "gates",
        "rubric",
        "graph",
        "evidence",
        "reasoning",
        "diff",
        "feedback",
        "decisions",
        "timeline",
        "rawrun",
    ]


def test_every_section_is_callable_and_returns_str(cfg: Config) -> None:
    ctx = _context(cfg)
    for section in SECTIONS:
        assert hasattr(section, "render"), f"{section.__name__} has no render()"
        fragment = section.render(ctx)
        assert isinstance(fragment, str), f"{section.__name__}.render did not return str"


def test_stub_sections_render_nothing(cfg: Config) -> None:
    ctx = _context(cfg)
    for section in STUB_SECTIONS:
        assert section.render(ctx) == "", f"{section.__name__} must render '' today"


def test_stub_sections_leave_no_trace_in_body(cfg: Config) -> None:
    body = render_body(_context(cfg))
    # No empty section wrappers or placeholder headings from the three stubs.
    assert "<section></section>" not in body
    for banned in ("Reasoning", "Feedback", "coming soon", "Coming soon"):
        assert banned not in body


# ---------------------------------------------------------------------------
# Centralised escaping
# ---------------------------------------------------------------------------


def test_escape_handles_all_dangerous_characters() -> None:
    assert escape('<a href="x">&y') == "&lt;a href=&quot;x&quot;&gt;&amp;y"


def test_escape_coerces_none_and_numbers() -> None:
    assert escape(None) == ""
    assert escape(42) == "42"


def test_omission_is_escaped_muted_paragraph() -> None:
    assert omission("No data <yet>") == '<p class="muted">No data &lt;yet&gt;</p>'


def test_evidence_escapes_untrusted_path(cfg: Config) -> None:
    ctx = _context(
        cfg,
        artifacts=[
            {
                "path": "evidence/<script>alert(1)</script>.png",
                "kind": "screenshot",
                "bytes": 2048,
                "sha256": "ab" * 32,
            }
        ],
    )
    out = evidence_section.render(ctx)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


# ---------------------------------------------------------------------------
# The unescaped review-link bug, fixed in this layer
# ---------------------------------------------------------------------------


def test_decisions_escapes_review_link(cfg: Config) -> None:
    create_adr(cfg, "Adopt dark mode", status="accepted")
    hostile_repo = 'owner"><img src=x onerror=alert(1)>/repo'
    ctx = _context(cfg, repo=hostile_repo, pr=7)
    out = decisions_section.render(ctx)
    assert '"><img src=x onerror=alert(1)>' not in out
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in out


def test_decisions_omission_when_no_adrs(cfg: Config) -> None:
    out = decisions_section.render(_context(cfg))
    expected = (
        "  <h2>Decisions</h2>\n"
        '  <div class="cards"><p class="muted">No architecture decisions recorded yet.</p></div>\n'
        '  <p class="note">Decisions are recorded through native GitHub pull request reviews:\n'
        "    <em>Approve</em> accepts the ADR, <em>Request changes</em> rejects it and opens a successor run.\n"
        "    History is never rewritten.</p>"
    )
    assert out == expected
