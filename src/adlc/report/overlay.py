"""Human-feedback overlay for the PWA report.

The report renderer owns the host chrome; this module appends the GUI-agnostic
feedback layer as data islands plus inline assets.  The payloads are derived from
``feedback-targets`` so the shipped report and future GUIs share one contract.
"""

from __future__ import annotations

import json
from functools import cache
from html import escape as _html_escape
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any

from adlc.config import Config
from adlc.reduce import load_run
from adlc.runs import RunDir
from adlc.stages.feedback_targets import compute_targets

__all__ = ["asset_source", "inject_overlay", "json_script"]

_ASSET_DIR = files("adlc") / "assets" / "feedback-overlay"
_SUBMIT_PATH = "/feedback"
_NONCE_HEADER = "X-ADLC-Nonce"
_MAX_BODY_BYTES = 4 * 1024 * 1024
_HUMAN_FEEDBACK_SCHEMA = "adlc-human-feedback/v1"


def escape(value: Any) -> str:
    return _html_escape("" if value is None else str(value), quote=True)


def omission(reason: str) -> str:
    return f'<p class="muted">{escape(reason)}</p>'


@cache
def asset_source(name: str) -> str:
    text = _ASSET_DIR.joinpath(name).read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"feedback overlay asset {name!r} is empty or missing")
    return text


def json_script(element_id: str, payload: dict[str, Any]) -> str:
    """Embed parseable JSON that cannot terminate its own script block.

    ``<`` is escaped to ``\\u003c`` so a ``</script>`` (or ``<!--``) hiding in an
    artifact path cannot close the ``<script>`` element; ``JSON.parse`` restores
    it. ``ensure_ascii`` keeps the overlay payload ASCII.
    """
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
    safe = raw.replace("<", "\\u003c")
    return f'<script type="application/json" id="{element_id}">{safe}</script>'


def inject_overlay(html: str, cfg: Config, rd: RunDir) -> str:
    """Append the human-feedback overlay immediately before ``</body>``."""
    targets = compute_targets(cfg, rd)
    block = "\n".join(_overlay_fragments(rd, targets))
    marker = "</body>"
    if marker not in html:
        return html + "\n" + block
    return html.replace(marker, block + "\n" + marker, 1)


def _overlay_fragments(rd: RunDir, targets: dict[str, Any]) -> list[str]:
    fragments = [
        "<!-- ADLC human-feedback overlay -->",
        f"<style>\n{asset_source('annotate.css')}\n{_feedback_style()}\n</style>",
        _evidence_overlay(rd, targets),
        _critique_overlay(rd, targets),
        _diff_overlay(rd, targets),
        _feedback_overlay(rd),
        json_script("adlc-evidence-data", _evidence_payload(rd, targets)),
        json_script("adlc-critique-data", _critique_payload(rd, targets)),
        json_script("adlc-diff-model", _diff_payload(rd, targets)),
        json_script("adlc-feedback-config", _feedback_config(rd)),
    ]
    for name in ("annotate.js", "critique.js", "diff.js", "feedback.js"):
        fragments.append(f"<script>\n{asset_source(name)}\n</script>")
    return fragments


def _evidence_payload(rd: RunDir, targets: dict[str, Any]) -> dict[str, Any]:
    artifacts = []
    for normalized in _overlay_artifacts(targets):
        if normalized.get("annotatable"):
            artifacts.append({
                "sha256": normalized.get("sha256"),
                "path": normalized.get("path"),
                "kind": normalized.get("kind"),
                "inlined": bool(normalized.get("inline")),
                "reason": normalized.get("inlineOmittedReason") or "",
                "bytes": normalized.get("bytes"),
                "annotatable": bool(normalized.get("annotatable")),
            })
    return {
        "runId": rd.run_id,
        "requirements": targets.get("requirements") or [],
        "artifacts": artifacts,
    }


def _overlay_artifact(art: dict[str, Any], *, remaining: int | None = None) -> dict[str, Any]:
    out = dict(art)
    path = str(out.get("path") or "")
    media_type = str(out.get("mediaType") or "")
    if PurePosixPath(path).suffix.lower() == ".svg" or media_type == "image/svg+xml":
        out["inline"] = None
        out["annotatable"] = True
        out["inlineOmittedReason"] = "an SVG can carry executable script, so it is not inlined"
    inline = out.get("inline")
    if isinstance(inline, str) and remaining is not None and len(inline) > remaining:
        out["inline"] = None
        out["inlineOmittedReason"] = (
            "not inlined: the document budget is exhausted; hash and size above still identify it"
        )
    return out


def _overlay_artifacts(targets: dict[str, Any]) -> list[dict[str, Any]]:
    remaining = int((targets.get("budgets") or {}).get("totalBytes") or 0)
    out = []
    for art in targets.get("artifacts") or []:
        normalized = _overlay_artifact(art, remaining=remaining)
        inline = normalized.get("inline")
        if isinstance(inline, str):
            remaining -= len(inline)
        out.append(normalized)
    return out


def _critique_payload(rd: RunDir, targets: dict[str, Any]) -> dict[str, Any]:
    return {"runId": rd.run_id, "targets": targets.get("reasoning") or []}


def _diff_payload(rd: RunDir, targets: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    diff = targets.get("diff") or {}
    for prefix, key in (("dd-m", "measurements"), ("dd-c", "coverage"), ("dd-s", "screenshots")):
        for index, row in enumerate(diff.get(key) or []):
            did = f"{prefix}-{index}"
            rows[did] = {
                "targetKind": row.get("targetKind"),
                "targetId": row.get("targetId"),
                "sha256": row.get("sha256"),
                "artifactSha256": row.get("sha256"),
            }
    return {"runId": rd.run_id, "rows": rows}


def _feedback_config(rd: RunDir) -> dict[str, Any]:
    try:
        run = load_run(rd)
    except (OSError, ValueError):
        run = {}
    return {
        "schemaVersion": _HUMAN_FEEDBACK_SCHEMA,
        "runId": rd.run_id,
        "candidateSha": str(run.get("headSha") or ""),
        "submitPath": _SUBMIT_PATH,
        "nonceHeader": _NONCE_HEADER,
        "maxBodyBytes": _MAX_BODY_BYTES,
    }


def _evidence_overlay(rd: RunDir, targets: dict[str, Any]) -> str:
    figures = []
    inlined = 0
    skipped = 0
    for art in _overlay_artifacts(targets):
        if not art.get("annotatable"):
            continue
        path = escape(art.get("path"))
        kind = escape(art.get("kind"))
        sha = escape(art.get("sha256"))
        reason = escape(art.get("inlineOmittedReason") or "not inlined")
        src = art.get("inline")
        if src:
            inlined += 1
            body = (
                '<div class="annot-stage">'
                f'<img class="annot-img" alt="Evidence artifact: {path}" src="{escape(src)}">'
                f'<svg class="annot-overlay" role="img" aria-label="Markup overlay for {path}." '
                'viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>'
                '<div class="annot-labels" aria-hidden="true"></div></div>'
            )
            degraded = ""
        else:
            skipped += 1
            body = (
                f'<p class="annot-degraded">This artifact was not inlined ({reason}). '
                "Its hash and size still identify the evidence.</p>"
            )
            degraded = ' data-degraded="1"'
        figures.append(
            f'<figure class="annot-artifact" data-sha="{sha}" data-path="{path}" '
            f'data-kind="{kind}" data-annotatable="1"{degraded}>'
            f'<figcaption class="annot-cap"><span class="mono">{path}</span> '
            f'<span class="tag">{kind}</span> <span class="mono">{sha}</span></figcaption>'
            f"{body}<div class=\"annot-mount\"></div></figure>"
        )
    summary = (
        f'<p class="annot-budget note">Inlined {inlined} image(s); '
        f"{skipped} image(s) not inlined and kept below with hash, size and reason.</p>"
    )
    content = summary + ("".join(figures) if figures else omission("No annotatable artifacts were captured."))
    return f'<section class="feedback-overlay annot-root" data-run-id="{escape(rd.run_id)}"><h2>Annotate evidence</h2>{content}</section>'


def _critique_overlay(rd: RunDir, targets: dict[str, Any]) -> str:
    cards = []
    present_kinds = {str(t.get("targetKind") or "") for t in targets.get("reasoning") or []}
    for target in targets.get("reasoning") or []:
        cid = escape(target.get("id"))
        title = escape(target.get("targetTitle"))
        text = escape(target.get("text"))
        kind = escape(target.get("targetKind"))
        ref = escape(target.get("targetRef"))
        if target.get("targetKind") == "rubric_criterion":
            badge = "Pass" if target.get("confidence") == "True" else "Fail"
        else:
            badge = escape(target.get("severity") or target.get("confidence") or kind)
        cards.append(
            f'<article class="card rcard" data-critique-id="{cid}" aria-labelledby="{cid}-title">'
            f'<h4 class="rcard-title" id="{cid}-title">{title}</h4>'
            f'<p><span class="tag">{kind}</span> <span class="pill">{badge}</span> '
            f'<span class="mono">{ref}</span></p>'
            f'<div class="reasoning-src" tabindex="0" role="group" '
            f'aria-label="Reasoning text for {title}">{text}</div>'
            f'<fieldset class="stance"><legend>Stance</legend>'
            f'<label><input type="radio" name="{cid}-stance" value="agree"> agree</label>'
            f'<label><input type="radio" name="{cid}-stance" value="disagree"> disagree</label>'
            f'<label><input type="radio" name="{cid}-stance" value="needs_evidence"> needs evidence</label>'
            f'<label><input type="radio" name="{cid}-stance" value="out_of_scope"> out of scope</label>'
            f'</fieldset><label for="{cid}-comment">Comment</label>'
            f'<textarea id="{cid}-comment"></textarea>'
            f'<button type="button" data-critique-save>Record critique</button>'
            f'<button type="button" data-critique-clear>Clear critique</button>'
            f'<p class="critique-status" role="status" aria-live="polite"></p></article>'
        )
    content = "".join(cards) if cards else omission("No critique-able reasoning was recorded.")
    omissions = []
    if "squad_finding" not in present_kinds:
        omissions.append(omission("No adversarial squad reviews were recorded for this run."))
    if "rubric_criterion" not in present_kinds:
        omissions.append(omission("No rubric score was recorded for this run."))
    if "adr" not in present_kinds:
        omissions.append(omission("No architecture decision records exist for this run."))
    content += "".join(omissions)
    return f'<section class="feedback-overlay"><h2>Reasoning critique</h2><div class="rcards">{content}</div></section>'


def _diff_overlay(rd: RunDir, targets: dict[str, Any]) -> str:
    rows_html = []
    diff = targets.get("diff") or {}
    row_map = _diff_payload(rd, targets)["rows"]
    by_id = list(row_map.items())
    diff_rows = (
        list(diff.get("measurements") or [])
        + list(diff.get("coverage") or [])
        + list(diff.get("screenshots") or [])
    )
    for (did, meta), row in zip(by_id, diff_rows, strict=False):
        label_raw = str(row.get("label") or row.get("targetId") or "")
        label = escape(label_raw)
        kind = escape(meta.get("targetKind"))
        change = escape(row.get("change"))
        detail = _diff_detail(row, meta, targets)
        rows_html.append(
            f'<tr><th scope="row">{label}</th><td>{kind}</td><td>{change}</td>'
            f"<td>{detail}</td>"
            f'<td><fieldset class="dd" data-decision-id="{did}"><legend>Decision for {kind} {label}</legend>'
            f'<button type="button" class="dd-btn dd-accept" aria-pressed="false" '
            f'aria-label="Accept change to {kind} {label}">Accept</button>'
            f'<button type="button" class="dd-btn dd-reject" aria-pressed="false" '
            f'aria-label="Reject change to {kind} {label}">Reject</button>'
            f'<input class="dd-comment" aria-label="Comment on {kind} {label}"></fieldset></td></tr>'
        )
    content = (
        '<p class="dd-status"><span data-diff-count>No decisions recorded yet.</span></p>'
        '<p class="sr-only" role="status" aria-live="polite" data-diff-live></p>'
        '<table><thead><tr><th scope="col">Target</th><th scope="col">Kind</th>'
        '<th scope="col">Change</th><th scope="col">Detail</th><th scope="col">Decision</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>'
        if rows_html
        else omission("No evidence diff decisions are available for this run.")
    )
    return f'<section class="feedback-overlay diff-sec"><h2>Evidence changes since baseline</h2>{content}</section>'


def _fmt_value(value: Any) -> str:
    if value is True:
        return "&#10003; yes"
    if value is False:
        return "&#10007; no"
    return escape(value)


def _signed(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return escape(value)
    prefix = "+" if num > 0 else ""
    return f"{prefix}{num:g}"


def _artifact_for_screenshot(target_id: str, targets: dict[str, Any]) -> dict[str, Any] | None:
    for art in _overlay_artifacts(targets):
        path = str(art.get("path") or "")
        if path.endswith("/" + target_id) or path == target_id:
            return art
    return None


def _diff_detail(row: dict[str, Any], meta: dict[str, Any], targets: dict[str, Any]) -> str:
    kind = str(meta.get("targetKind") or "")
    if kind == "measurement":
        crossed = str(row.get("budgetCrossed") or "none")
        if crossed == "entered_breach":
            label = "Entered breach; now failing; budget crossing"
        elif crossed == "left_breach":
            label = "Left breach"
        else:
            label = "Changed"
        return (
            f'{label}: baseline {_fmt_value(row.get("baselineValue"))} &rarr; '
            f'{_fmt_value(row.get("value"))} ({_signed(row.get("delta"))})'
        )
    if kind == "coverage":
        change = str(row.get("change") or "")
        if change == "lost":
            return "Evidence lost; regression; lost evidence"
        if change == "gained":
            return "Evidence gained"
        return escape(change)
    if kind == "screenshot":
        change = str(row.get("change") or "")
        art = _artifact_for_screenshot(str(row.get("targetId") or ""), targets)
        cand = art.get("inline") if art else None
        base = row.get("baselineInline")
        parts = [f"{escape(change.title())} screenshot"]
        if change == "changed":
            parts.append("Overlay difference blend")
        if change == "unchanged":
            parts.append("unchanged &mdash; identical hash")
        if cand:
            parts.append(f'<img alt="candidate {escape(row.get("targetId"))}" src="{escape(cand)}">')
        if base:
            parts.append(f'<img alt="baseline {escape(row.get("targetId"))}" src="{escape(base)}">')
        elif change in {"changed", "removed"}:
            reason = row.get("inlineOmittedReason") or "image not found in the run directory"
            parts.append(f"Difference blend unavailable: {escape(reason)}")
        if row.get("sha256") or row.get("baselineSha256"):
            parts.append(
                f'SHA-256 changed: {escape(row.get("baselineSha256"))} &rarr; {escape(row.get("sha256"))}'
            )
        return "; ".join(parts)
    return ""


def _feedback_overlay(rd: RunDir) -> str:
    cfg = _feedback_config(rd)
    head = escape(cfg.get("candidateSha", ""))
    return (
        '<section class="feedback-overlay"><h2>Submit feedback</h2>'
        '<p class="note">Assemble one <span class="mono">adlc-human-feedback/v1</span> pack from '
        'annotations, critiques and diff decisions.</p>'
        f'<p class="note">Reviewing commit <span class="mono hash" title="{head}">{escape(head[:12])}&hellip;</span>.</p>'
        f'<div class="fb" data-run-id="{escape(rd.run_id)}">'
        '<div class="fb-grid"><div><label for="adlc-verdict">Verdict</label>'
        '<select id="adlc-verdict"><option value="revise" selected>revise</option>'
        '<option value="accept">accept</option><option value="reject">reject</option></select></div>'
        '<div><label for="adlc-route">Route</label><select id="adlc-route">'
        '<option value="outer" selected>outer</option><option value="inner">inner</option></select></div></div>'
        '<div class="fb-field"><label for="adlc-submitted-by">Your name</label>'
        '<input id="adlc-submitted-by" type="text" maxlength="128" autocomplete="name"></div>'
        '<div class="fb-field"><label for="adlc-summary">Summary</label>'
        '<textarea id="adlc-summary" rows="4" maxlength="4000"></textarea></div>'
        '<p class="note fb-guidance" id="adlc-guidance" role="status" aria-live="polite"></p>'
        '<div class="fb-conflict" id="adlc-conflict" role="alert" aria-live="assertive"></div>'
        '<div class="actions"><button type="button" class="btn" id="adlc-download" aria-describedby="adlc-guidance">Download pack</button>'
        '<button type="button" class="btn" id="adlc-copy" aria-describedby="adlc-guidance">Copy pack</button>'
        '<button type="button" class="btn ok" id="adlc-submit" aria-describedby="adlc-guidance adlc-submit-note">Submit to loopback server</button></div>'
        '<p class="note" id="adlc-submit-note" hidden></p>'
        '<textarea id="adlc-copy-fallback" class="fb-fallback" rows="6" readonly hidden aria-label="Feedback pack JSON, selectable for manual copy"></textarea>'
        '<div class="fb-status" id="adlc-status" role="status" aria-live="polite"></div>'
        '<div class="fb-error" id="adlc-error" role="alert" aria-live="assertive" tabindex="-1"></div>'
        '</div></section>'
    )


def _feedback_style() -> str:
    return """
.feedback-overlay { margin:24px auto; max-width:1180px; padding:0 24px }
.reasoning-src { white-space:pre-wrap; background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:10px }
.fb { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin:14px 0 }
.fb .fb-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; margin-bottom:12px }
.fb label { display:block; font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin-bottom:5px }
.fb select, .fb textarea, .fb input[type=text] { width:100%; background:var(--panel2); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px 10px; font:inherit }
.fb button[aria-disabled="true"] { opacity:.5; cursor:not-allowed }
.fb .fb-conflict:not(.has-content), .fb .fb-status:not(.has-content), .fb .fb-error:not(.has-content), .fb .fb-guidance:not(.has-content) { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0 }
"""
