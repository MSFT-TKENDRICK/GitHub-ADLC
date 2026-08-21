"""Feedback section -- Layer 7-UI: one submitted act that retriggers the loop.

The evidence, reasoning and diff sections each persist annotations into the
shared ``window.adlcFeedback`` client registry. This section is where those
three surfaces become a single ``adlc-human-feedback/v1`` pack: it adds the
holistic controls (verdict, route, summary, self-declared name), assembles the
pack in the browser, and offers three egress paths -- download, copy, and, when
the report is served by the loopback server, a same-origin POST.

Backend-less by contract: everything except the POST works from ``file://`` with
no network. The section renders nothing for a bare context (no ``runId``), so it
stays byte-for-byte absent from the pre-split output and from any run that has no
identity to bind feedback to. For a real run it always renders, because a
holistic verdict plus a summary is a legitimate act even when nothing was
annotatable -- but every egress control stays disabled until the pack carries a
summary or at least one item, so the reviewer can never hand over an empty pack.

Two digests deserve a note. ``packDigest`` is computed in the browser by a
canonicaliser that is byte-identical to :func:`adlc.stages.feedback.pack_digest`
(see ``assets/feedback.js``). ``reportDigest`` is computed from the report's own
served bytes, so it is omitted on the ``file://`` path where those bytes are
unreadable; it cannot be emitted from Python because the report embeds the
digest and so has no fixed point. This module therefore emits only the values it
can state with certainty -- ``runId`` and ``candidateSha`` -- and leaves the rest
to the page.
"""

from __future__ import annotations

import json

from adlc.stages.report.context import ReportContext, escape
from adlc.stages.report.shell import read_asset

#: Mirrors of :mod:`adlc.serve`. The report is backend-less and must not import
#: the server module, so the three values the page needs are duplicated here and
#: kept honest by ``test_feedback_ui.py::test_server_constants_match``.
_SUBMIT_PATH = "/feedback"
_NONCE_HEADER = "X-ADLC-Nonce"
_MAX_BODY_BYTES = 4 * 1024 * 1024

_SCHEMA_VERSION = "adlc-human-feedback/v1"

#: Scoped styling. Only ``feedback.js`` and this file are in the write-set, so
#: there is no separate stylesheet: a small block reusing ``base.css`` tokens
#: keeps the section self-contained. ``:focus-visible`` rules guarantee a
#: visible keyboard focus indicator; the conflict banner never relies on colour
#: alone -- it carries a symbol and text, and the border is only reinforcement.
_STYLE = """  <style>
  .fb { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:16px 18px; margin:14px 0 }
  .fb .fb-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:14px; margin-bottom:12px }
  .fb label { display:block; font-size:12px; text-transform:uppercase;
    letter-spacing:.05em; color:var(--muted); margin-bottom:5px }
  .fb select, .fb textarea, .fb input[type=text] { width:100%; background:var(--panel2);
    color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px 10px;
    font:inherit }
  .fb textarea { resize:vertical; min-height:74px }
  .fb .fb-field { margin-bottom:12px }
  .fb button { cursor:pointer; font:inherit }
  .fb button[disabled] { opacity:.5; cursor:not-allowed }
  .fb :focus-visible { outline:2px solid var(--accent); outline-offset:2px }
  .fb .fb-conflict:empty, .fb .fb-status:empty, .fb .fb-error:empty,
  .fb .fb-guidance:empty { display:none }
  .fb .fb-conflict { margin:12px 0; padding:10px 14px; border-radius:8px;
    border:1px solid var(--warn); border-left:4px solid var(--warn);
    background:var(--panel2); color:var(--fg); font-size:13px }
  .fb .fb-error { margin:12px 0; padding:10px 14px; border-radius:8px;
    border:1px solid var(--bad); border-left:4px solid var(--bad);
    background:var(--panel2); color:var(--fg); font-size:13px }
  .fb .fb-status { margin:12px 0; padding:10px 14px; border-radius:8px;
    border:1px solid var(--ok); border-left:4px solid var(--ok);
    background:var(--panel2); color:var(--fg); font-size:13px }
  .fb .fb-fallback { margin-top:10px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:12px }
  </style>"""


def _config_script(ctx: ReportContext) -> str:
    """Embed the values the page must not have to guess, guarded against early
    ``</script>`` termination.

    ``<`` becomes ``\\u003c`` so a stray ``</script>`` cannot close the block;
    ``JSON.parse`` restores it. Only trustworthy, self-produced values go here
    (``runId`` and ``candidateSha``); reviewer free text is collected client-side
    and never rendered into this element.
    """
    payload = {
        "schemaVersion": _SCHEMA_VERSION,
        "runId": ctx.rd.run_id,
        "candidateSha": str(ctx.run.get("headSha") or ""),
        "submitPath": _SUBMIT_PATH,
        "nonceHeader": _NONCE_HEADER,
        "maxBodyBytes": _MAX_BODY_BYTES,
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="adlc-feedback-config">{raw}</script>'


def render(ctx: ReportContext) -> str:
    # A bare context (no loaded run) renders nothing: no heading, no controls.
    # This keeps the pre-split output byte-identical and never offers a submit
    # UI for a run with no identity to bind feedback to.
    if not ctx.run.get("runId"):
        return ""

    head = str(ctx.run.get("headSha") or "")
    if head:
        candidate = f'<span class="mono hash" title="{escape(head)}">{escape(head[:12])}&hellip;</span>'
    else:
        candidate = '<span class="muted">none recorded (outside a git checkout)</span>'

    controls = (
        '<div class="fb-grid">'
        '<div><label for="adlc-verdict">Verdict</label>'
        '<select id="adlc-verdict">'
        '<option value="revise" selected>revise &mdash; iterate on this run</option>'
        '<option value="accept">accept &mdash; ship it</option>'
        '<option value="reject">reject &mdash; do not ship</option>'
        "</select></div>"
        '<div><label for="adlc-route">Route</label>'
        '<select id="adlc-route">'
        '<option value="outer" selected>outer &mdash; re-spec then re-implement</option>'
        '<option value="inner">inner &mdash; re-implement only</option>'
        "</select></div>"
        "</div>"
    )

    return "\n".join(
        [
            "  <h2>Submit feedback</h2>",
            _STYLE,
            (
                '  <p class="note">Assemble one'
                ' <span class="mono">adlc-human-feedback/v1</span> pack from the'
                " annotations, critiques and diff decisions above, plus a holistic"
                " verdict. Download it, copy it, or &mdash; when this report is served"
                ' by <span class="mono">adlc report serve</span> &mdash; submit it'
                " directly to retrigger the design loop.</p>"
            ),
            f'  <p class="note">Reviewing commit {candidate}.</p>',
            '  <div class="fb" data-run-id="' + escape(ctx.rd.run_id) + '">',
            "    " + controls,
            (
                '    <div class="fb-field"><label for="adlc-submitted-by">Your name'
                " (optional, advisory only)</label>"
                '<input id="adlc-submitted-by" type="text" maxlength="128"'
                ' autocomplete="name"></div>'
            ),
            (
                '    <div class="fb-field"><label for="adlc-summary">Summary</label>'
                '<textarea id="adlc-summary" rows="4" maxlength="4000"'
                ' placeholder="What must change, and why. Quoted verbatim into the'
                ' successor brief."></textarea></div>'
            ),
            '    <p class="note fb-guidance" id="adlc-guidance" role="status" aria-live="polite"></p>',
            (
                '    <p class="note" id="adlc-counts">Reading annotations from the'
                " surfaces above&hellip;</p>"
            ),
            '    <div class="fb-conflict" id="adlc-conflict" role="alert" aria-live="assertive"></div>',
            '    <div class="actions">',
            (
                '      <button type="button" class="btn" id="adlc-download"'
                ' aria-describedby="adlc-guidance">Download pack</button>'
            ),
            (
                '      <button type="button" class="btn" id="adlc-copy"'
                ' aria-describedby="adlc-guidance">Copy pack</button>'
            ),
            (
                '      <button type="button" class="btn ok" id="adlc-submit" disabled'
                ' aria-describedby="adlc-guidance adlc-submit-note">Submit to loopback'
                " server</button>"
            ),
            "    </div>",
            '    <p class="note" id="adlc-submit-note" hidden></p>',
            (
                '    <textarea id="adlc-copy-fallback" class="fb-fallback" rows="6" readonly'
                ' hidden aria-label="Feedback pack JSON, selectable for manual copy">'
                "</textarea>"
            ),
            '    <div class="fb-status" id="adlc-status" role="status" aria-live="polite"></div>',
            (
                '    <div class="fb-error" id="adlc-error" role="alert"'
                ' aria-live="assertive" tabindex="-1"></div>'
            ),
            "  </div>",
            "  " + _config_script(ctx),
            "  <script>\n" + read_asset("feedback.js") + "\n  </script>",
        ]
    )
