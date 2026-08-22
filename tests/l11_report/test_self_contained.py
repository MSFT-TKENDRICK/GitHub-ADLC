"""L11 — the report may not reach outside itself, and this is what enforces it.

:mod:`adlc.report.html` opens by promising "no network dependency, because of
where it has to work: attached to a CI run, downloaded to a laptop, opened from
``file://`` ... read on a plane". That promise was false for one release: the
shell loaded ``mermaid@11`` from a public CDN, unpinned and without SRI, purely
to pretty-print a collapsed *Diagram source* disclosure.

The cost of that convenience is worth stating plainly, because it is the reason
these tests are strict rather than advisory:

* **It breaks the artifact where it is most needed.** Offline, air-gapped, or
  simply on a plane, the fetch fails and the reader gets nothing.
* **It makes an archived artifact mutable.** ``@11`` is a moving target, so an
  evidence file opened a year from now executes whatever that tag resolves to
  *then*, not what was reviewed.
* **It aims that code at the evidence.** The script runs in a document holding
  the run's diffs, captures and reviewer notes, with no SRI and nothing to
  contain it. A compromised CDN reads the lot.

So the report renders the diagram as source text and ships a Content-Security
-Policy that forbids the fetch outright. The policy is the real guarantee: it
holds even if someone re-adds a tag like the one that was removed.
"""

from __future__ import annotations

import re

from adlc.report.assets import CSS, JS
from adlc.report.html import _SHELL

#: The one legitimate ``http`` literal in the viewer: the SVG element namespace,
#: which ``createElementNS`` requires and which is never dereferenced.
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def _csp() -> str:
    match = re.search(
        r"<meta\s+http-equiv=\"Content-Security-Policy\"\s+content=\"([^\"]+)\"", _SHELL
    )
    assert match, "the report shell ships no Content-Security-Policy"
    return match.group(1)


class TestNothingIsFetchedAtViewTime:
    def test_the_shell_declares_no_external_url(self) -> None:
        """Nothing in the static shell may point outside the file."""
        for scheme in ("http://", "https://", "//cdn"):
            assert scheme not in _SHELL, (
                f"{scheme!r} appears in the report shell; every asset must travel "
                "inside the file, or the evidence arrives without the proof"
            )

    def test_no_script_carries_a_src_attribute(self) -> None:
        """Both scripts are inline: the model JSON and the viewer."""
        for tag in re.findall(r"<script\b[^>]*>", _SHELL):
            assert "src=" not in tag, f"external script in the report shell: {tag}"

    def test_no_stylesheet_is_linked(self) -> None:
        assert "<link" not in _SHELL, "the CSS is inlined; a <link> would be a fetch"

    def test_the_css_loads_nothing(self) -> None:
        """No webfont, no background image, no @import."""
        assert "url(" not in CSS
        assert "@import" not in CSS
        assert "http" not in CSS

    def test_the_viewer_only_names_the_svg_namespace(self) -> None:
        """The single ``http`` literal in the JS is a namespace, not a fetch."""
        assert JS.count(_SVG_NAMESPACE) == JS.count("http"), (
            "the viewer names a URL other than the SVG namespace; it must not "
            "fetch anything at view time"
        )

    def test_the_viewer_does_not_reach_for_a_cdn_global(self) -> None:
        """No ``window.mermaid`` handshake: there is no script to provide it."""
        assert "window.mermaid" not in JS
        assert "mermaid.initialize" not in JS

    def test_the_diagram_source_is_still_shown(self) -> None:
        """Removing the renderer must not remove the diagram from the report."""
        assert 'class="mermaid"' in _SHELL
        assert "{{GRAPH_MERMAID}}" in _SHELL


class TestThePolicyEnforcesIt:
    def test_everything_is_denied_by_default(self) -> None:
        assert "default-src 'none'" in _csp()

    def test_scripts_and_styles_are_inline_only(self) -> None:
        """``'unsafe-inline'`` without a host source: inline runs, fetches do not."""
        policy = _csp()
        for directive in ("script-src 'unsafe-inline'", "style-src 'unsafe-inline'"):
            assert directive in policy
        assert "http" not in policy, "no host may be allowlisted in the policy"

    def test_media_is_restricted_to_data_uris(self) -> None:
        """Every capture is base64 inline, so ``data:`` is the whole allowance."""
        policy = _csp()
        assert "img-src data:" in policy
        assert "media-src data:" in policy

    def test_the_document_base_cannot_be_moved(self) -> None:
        """``base-uri 'none'`` stops injected content re-rooting relative URLs."""
        assert "base-uri 'none'" in _csp()

    def test_nothing_can_be_posted_outward(self) -> None:
        assert "form-action 'none'" in _csp()
