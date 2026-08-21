"""Diff section -- accept/reject the evidence deltas since the baseline run.

This is the layer-6 section. It renders the ``evidence-diff.json`` produced by
:mod:`adlc.stages.evidence_diff` as three deltas a human decides on -- measurement
movement, coverage movement, and changed screenshots -- and lets the reviewer
accept or reject each one. The decisions are held in the shared
``window.adlcFeedback`` registry in exact ``human-feedback-pack`` ``diffDecision``
shape, persisted to ``localStorage``, and picked up by the feedback section
(layer 7) at export time.

Deliberate deviation from the "sections are pure" convention: :func:`build_context`
does not read ``evidence-diff.json`` (it is a shared file this layer must not add a
read to), so this section reads it from ``ctx.rd`` via
:func:`adlc.stages.evidence_diff.diff_path`, wrapped in ``try/except`` and treating
every read/parse failure as "no diff". A run with no baseline and no diff file --
the pre-layer-6 state of every existing run -- therefore renders exactly ``""``,
leaving the report byte-for-byte as it was.

Everything here is self-contained: baseline and candidate screenshots are inlined
as ``data:`` URIs and the visual difference is a CSS ``mix-blend-mode``, so the
page needs no server, no network and no image library. Untrusted values (metric
ids, requirement ids, screenshot paths, collector names) are routed through
:func:`escape` in markup and through a JSON data island (with ``<`` neutralised)
for the client.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from adlc.runs import RunDir
from adlc.stages.report.context import ReportContext, escape
from adlc.stages.report.shell import read_asset

#: Per-image inline budget. An image larger than this degrades to hash + size
#: with a stated reason rather than bloating the single-file report.
_MAX_IMAGE_BYTES = 2 * 1024 * 1024

#: Cumulative inline budget for the whole screenshots section. Once exhausted,
#: the remaining images degrade -- never silently drop.
_MAX_SECTION_BYTES = 12 * 1024 * 1024

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# Glyphs as HTML entities so the source (and the rendered report) stay ASCII, and
# so every status is carried by a shape + a word, never by colour alone.
_CHECK = "&#10003;"
_CROSS = "&#10007;"
_UP = "&#9650;"
_DOWN = "&#9660;"
_WARN = "&#9888;"
_ARROW = "&rarr;"
_DASH = "&#8212;"
_PLUS = "&#43;"
_MINUS = "&#8722;"

_STYLE = """  <style>
  .diff-sec .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px;
    overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0 }
  .diff-sec :focus-visible { outline:2px solid var(--accent); outline-offset:2px }
  .diff-sec .dd-set { display:flex; gap:6px; align-items:center; flex-wrap:wrap;
    border:1px solid var(--line); border-radius:8px; padding:6px 8px; margin:0 }
  .diff-sec .dd-btn { cursor:pointer; padding:3px 10px; border-radius:6px;
    border:1px solid var(--line); background:var(--panel2); color:var(--fg); font-size:13px }
  .diff-sec .dd-btn[aria-pressed="true"] { border-width:2px; font-weight:700 }
  .diff-sec .dd-accept[aria-pressed="true"] { border-color:var(--ok); background:rgba(63,185,80,.16) }
  .diff-sec .dd-reject[aria-pressed="true"] { border-color:var(--bad); background:rgba(248,81,73,.16) }
  .diff-sec .dd-accept[aria-pressed="true"]::before { content:"\\2713\\00a0" }
  .diff-sec .dd-reject[aria-pressed="true"]::before { content:"\\2717\\00a0" }
  .diff-sec .dd-comment { background:var(--bg); color:var(--fg); border:1px solid var(--line);
    border-radius:6px; padding:3px 6px; font-size:13px; min-width:150px }
  .diff-sec tr.row-cross-in > * { border-top:2px solid var(--bad) }
  .diff-sec tr.row-cross-out > * { border-top:2px solid var(--ok) }
  .diff-sec .cross-in { color:var(--bad) }
  .diff-sec .cross-out { color:var(--ok) }
  .diff-sec .ss { border:1px solid var(--line); border-radius:10px; padding:12px;
    margin:12px 0; background:var(--panel) }
  .diff-sec .ss-pair { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:8px 0 }
  .diff-sec .ss-cell { margin:0 }
  .diff-sec .ss img { max-width:100%; height:auto; display:block; border:1px solid var(--line);
    border-radius:6px; background:#fff }
  .diff-sec .ss-lab { display:block; font-size:12px; color:var(--muted); margin-bottom:4px }
  .diff-sec .ss-blend-note { margin:8px 0; font-size:12px; color:var(--muted) }
  /* Difference blend: overlay the candidate cell onto the baseline cell and let the
     browser composite with mix-blend-mode. It reuses the two images already inlined
     side by side -- the same DOM nodes -- so nothing is inlined twice and the byte
     budget is charged once per image. */
  .diff-sec .ss.blend-on .ss-pair { display:block; position:relative; width:max-content;
    max-width:100%; background:#000; border-radius:6px; overflow:hidden }
  .diff-sec .ss.blend-on .ss-cell-cand { position:absolute; top:0; left:0 }
  .diff-sec .ss.blend-on .ss-lab { position:absolute; width:1px; height:1px; overflow:hidden;
    clip:rect(0,0,0,0); white-space:nowrap }
  .diff-sec .ss.blend-on .ss-cell img { border:0; border-radius:0; background:transparent }
  .diff-sec .ss.blend-on .ss-cell-cand img { mix-blend-mode:difference }
  .diff-sec .ss-degraded { border:1px dashed var(--line); border-radius:6px; padding:10px }
  .diff-sec .ss-facts { margin:8px 0; padding:8px 10px; border:1px solid var(--line);
    border-radius:6px; background:var(--panel2); font-size:13px }
  .diff-sec .ss-facts p { margin:4px 0 }
  </style>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render(ctx: ReportContext) -> str:
    doc = _load_diff(ctx)
    if doc is None:
        return ""

    measurements = [m for m in (doc.get("measurements") or []) if isinstance(m, dict)]
    coverage = [c for c in (doc.get("coverage") or []) if isinstance(c, dict)]
    screenshots = [s for s in (doc.get("screenshots") or []) if isinstance(s, dict)]

    # Nothing to decide on -- including a stated-absence diff (null baseline, empty
    # arrays). Render nothing so an existing run looks exactly as it did before.
    if not (measurements or coverage or screenshots):
        return ""

    baseline_run_id = doc.get("baselineRunId")
    baseline_label = escape(baseline_run_id) if baseline_run_id else "(none recorded)"
    run_id = ctx.rd.run_id

    model_rows: dict[str, dict[str, Any]] = {}
    entered = sum(1 for m in measurements if m.get("budgetCrossed") == "entered_breach")
    lost = sum(1 for c in coverage if c.get("change") == "lost")

    parts: list[str] = [
        '  <section class="diff-sec" aria-labelledby="diff-h">',
        _STYLE,
        '  <h2 id="diff-h">Evidence changes since baseline</h2>',
        (
            f'  <p class="muted">Movement in run <span class="mono">{escape(run_id)}</span> '
            f'versus baseline <span class="mono">{baseline_label}</span>. Accept or reject each '
            "change; decisions are saved in your browser and exported with the feedback pack.</p>"
        ),
    ]

    if entered:
        parts.append(
            '  <div class="banner bad" role="note"><strong>'
            f"{_WARN} {entered} budget crossing"
            f'{"" if entered == 1 else "s"}</strong><br>'
            f"{entered} measurement{'' if entered == 1 else 's'} that passed in the baseline now "
            "fail their budget. Each is marked <em>Entered breach</em> below.</div>"
        )
    if lost:
        parts.append(
            '  <div class="banner bad" role="note"><strong>'
            f"{_WARN} {lost} requirement{'' if lost == 1 else 's'} lost evidence"
            "</strong><br>These requirements were evidenced in the baseline and are not evidenced "
            "now &mdash; a regression in the audit trail. Each is marked <em>Evidence lost</em>.</div>"
        )

    parts.append(
        '  <p class="dd-status"><span data-diff-count>No decisions recorded yet.</span></p>'
    )
    parts.append('  <p class="sr-only" role="status" aria-live="polite" data-diff-live></p>')

    if measurements:
        parts.append("  <h3>Measurements</h3>")
        parts.append(_measurements_table(measurements, model_rows))
    if coverage:
        parts.append("  <h3>Coverage</h3>")
        parts.append(_coverage_table(coverage, model_rows))
    if screenshots:
        parts.append("  <h3>Screenshots</h3>")
        parts.append(_screenshots_block(ctx, screenshots, baseline_run_id, model_rows))

    parts.append(_data_island(run_id, model_rows))
    parts.append(f"  <script>\n{read_asset('diff.js')}\n  </script>")
    parts.append("  </section>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Loading (the documented purity deviation)
# ---------------------------------------------------------------------------


def _load_diff(ctx: ReportContext) -> dict[str, Any] | None:
    # Imported lazily: evidence_diff -> reduce -> adlc pulls the report package
    # back through this module during import, so a top-level import would form a
    # cycle. The call is only ever made at render time, well after import.
    from adlc.stages.evidence_diff import diff_path

    try:
        text = diff_path(ctx.rd).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def _measurements_table(measurements: list[dict[str, Any]], model_rows: dict[str, dict[str, Any]]) -> str:
    rows = []
    for i, m in enumerate(measurements):
        mid = str(m.get("metricId", ""))
        did = f"dd-m-{i}"
        meta: dict[str, Any] = {"targetKind": "measurement", "targetId": mid[:512]}
        sha = m.get("artifactSha256")
        if isinstance(sha, str) and sha:
            meta["artifactSha256"] = sha
        model_rows[did] = meta

        crossing = m.get("budgetCrossed", "none")
        row_cls = ""
        if crossing == "entered_breach":
            row_cls = ' class="row-cross-in"'
        elif crossing == "left_breach":
            row_cls = ' class="row-cross-out"'

        collector = m.get("collector")
        coll_tag = (
            f' <span class="tag">{escape(collector)}</span>'
            if isinstance(collector, str) and collector
            else ""
        )
        rows.append(
            f"      <tr{row_cls}>"
            f'<th scope="row" class="mono">{escape(mid)}<br>'
            f'<span class="tag">{_measure_change(m.get("change", ""))}</span>{coll_tag}</th>'
            f'<td class="num">{_num(m.get("baselineValue"))}</td>'
            f'<td class="num">{_num(m.get("value"))}</td>'
            f'<td class="num">{_delta(m.get("delta"))}</td>'
            f'<td class="num">{_num(m.get("budget"))}</td>'
            f"<td>{_result(m.get('baselinePassed'))} {_ARROW} {_result(m.get('passed'))}</td>"
            f"<td>{_crossing(crossing)}</td>"
            f'<td class="dd-cell">{_decision_group(did, "measurement", mid)}</td>'
            f"</tr>"
        )
    header = (
        '  <table class="diff-table">'
        '<caption class="sr-only">Measurement changes since the baseline run</caption>'
        "<thead><tr>"
        '<th scope="col">Metric</th>'
        '<th scope="col">Baseline</th>'
        '<th scope="col">Current</th>'
        '<th scope="col">Delta</th>'
        '<th scope="col">Budget</th>'
        f'<th scope="col">Result (was {_ARROW} now)</th>'
        '<th scope="col">Budget crossing</th>'
        '<th scope="col">Decision</th>'
        "</tr></thead>"
    )
    return header + "\n  <tbody>\n" + "\n".join(rows) + "\n  </tbody></table>"


def _measure_change(change: str) -> str:
    return {
        "added": f"{_PLUS} Added",
        "removed": f"{_MINUS} Removed",
        "changed": "Changed",
        "unchanged": f"{_DASH} Unchanged",
    }.get(change, escape(change))


def _crossing(value: Any) -> str:
    if value == "entered_breach":
        return f'<strong class="cross-in">{_WARN} Entered breach {_DASH} now failing</strong>'
    if value == "left_breach":
        return f'<span class="cross-out">{_DOWN} Left breach {_DASH} now passing</span>'
    return f'<span class="muted">{_DASH} No crossing</span>'


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _coverage_table(coverage: list[dict[str, Any]], model_rows: dict[str, dict[str, Any]]) -> str:
    rows = []
    for i, c in enumerate(coverage):
        rid = str(c.get("requirementId", ""))
        did = f"dd-c-{i}"
        model_rows[did] = {"targetKind": "coverage", "targetId": rid[:512]}

        change = c.get("change", "")
        row_cls = ' class="row-cross-in"' if change == "lost" else (
            ' class="row-cross-out"' if change == "gained" else ""
        )
        rows.append(
            f"      <tr{row_cls}>"
            f'<th scope="row" class="mono">{escape(rid)}</th>'
            f"<td>{_yesno(c.get('baselinePresent'))} {_ARROW} {_yesno(c.get('present'))}</td>"
            f"<td>{_kinds(c.get('baselineEvidenceKinds'))} {_ARROW} {_kinds(c.get('evidenceKinds'))}</td>"
            f"<td>{_coverage_change(change)}</td>"
            f'<td class="dd-cell">{_decision_group(did, "coverage", rid)}</td>'
            f"</tr>"
        )
    header = (
        '  <table class="diff-table">'
        '<caption class="sr-only">Requirement coverage changes since the baseline run</caption>'
        "<thead><tr>"
        '<th scope="col">Requirement</th>'
        f'<th scope="col">Evidenced (was {_ARROW} now)</th>'
        f'<th scope="col">Evidence kinds (was {_ARROW} now)</th>'
        '<th scope="col">Change</th>'
        '<th scope="col">Decision</th>'
        "</tr></thead>"
    )
    return header + "\n  <tbody>\n" + "\n".join(rows) + "\n  </tbody></table>"


def _coverage_change(change: str) -> str:
    if change == "lost":
        return f'<strong class="cross-in">{_WARN} Evidence lost {_DASH} regression</strong>'
    if change == "gained":
        return f'<span class="cross-out">{_UP} Evidence gained</span>'
    return {
        "added": f"{_PLUS} Added",
        "removed": f"{_MINUS} Removed",
        "unchanged": f'<span class="muted">{_DASH} Unchanged</span>',
    }.get(change, escape(change))


def _kinds(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return f'<span class="muted">{_DASH}</span>'
    return " ".join(f'<span class="tag">{escape(k)}</span>' for k in value)


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


def _screenshots_block(
    ctx: ReportContext,
    screenshots: list[dict[str, Any]],
    baseline_run_id: Any,
    model_rows: dict[str, dict[str, Any]],
) -> str:
    baseline_rd = (
        RunDir(ctx.cfg, baseline_run_id)
        if isinstance(baseline_run_id, str) and baseline_run_id
        else None
    )
    # Index each evidence tree once (not once per screenshot): rendering stays
    # linear in the number of files even with many screenshot pairs.
    cand_index = _build_image_index(ctx.rd.evidence_dir)
    base_index = _build_image_index(baseline_rd.evidence_dir if baseline_rd else None)
    budget = {"remaining": _MAX_SECTION_BYTES}
    blocks = []
    for i, s in enumerate(screenshots):
        rel = str(s.get("path", ""))
        change = s.get("change", "")
        did = f"dd-s-{i}"
        link_sha = s.get("sha256") or s.get("baselineSha256")
        meta: dict[str, Any] = {"targetKind": "screenshot", "targetId": rel[:512]}
        if isinstance(link_sha, str) and link_sha:
            meta["sha256"] = link_sha
        model_rows[did] = meta
        blocks.append(
            _screenshot_figure(
                s, rel, change, i, did, cand_index.get(rel), base_index.get(rel), budget
            )
        )
    note = (
        f'  <p class="note">Baseline and candidate images are inlined offline (up to '
        f"{_human(_MAX_IMAGE_BYTES)} each, {_human(_MAX_SECTION_BYTES)} total); the difference "
        "blend is computed by the browser. Anything larger degrades to its hash and size with a "
        "stated reason.</p>"
    )
    return "\n".join(blocks) + "\n" + note


def _screenshot_figure(
    s: dict[str, Any],
    rel: str,
    change: str,
    idx: int,
    did: str,
    cand_path: Path | None,
    base_path: Path | None,
    budget: dict[str, int],
) -> str:
    fig_id = f"ss-diff-{idx}"
    lines = [
        f'  <figure class="ss" id="{fig_id}">',
        (
            f'    <figcaption class="mono">{escape(rel)} '
            f'<span class="tag">{_screenshot_change(change)}</span></figcaption>'
        ),
    ]

    if change == "changed":
        cand = _prepare_image(cand_path, s.get("sha256"), budget)
        base = _prepare_image(base_path, s.get("baselineSha256"), budget)
        lines.append(_changed_facts(s, base, cand))
        lines.append('    <div class="ss-pair">')
        lines.append(
            f'      <div class="ss-cell ss-cell-base"><span class="ss-lab">Baseline</span>'
            f'{_img_html(base, "Baseline " + rel)}</div>'
        )
        lines.append(
            f'      <div class="ss-cell ss-cell-cand"><span class="ss-lab">Candidate</span>'
            f'{_img_html(cand, "Candidate " + rel)}</div>'
        )
        lines.append("    </div>")
        if cand["uri"] and base["uri"]:
            # The blend toggles a class on this figure that overlays the two cells
            # above with mix-blend-mode. It reuses those images, so each is inlined
            # exactly once and the byte budget is charged once per image.
            note_id = f"ss-blend-note-{idx}"
            lines.append(
                f'    <button type="button" class="btn" data-blend-toggle="{fig_id}" '
                f'data-ss-name="{escape(rel)}" aria-pressed="false" aria-controls="{note_id}" '
                f'aria-label="Overlay difference blend for {escape(rel)}">'
                "Overlay difference blend</button>"
            )
            lines.append(
                f'    <p class="ss-blend-note" id="{note_id}" data-blend-note hidden>'
                f"Difference blend {_DASH} black where identical, bright where changed.</p>"
            )
        else:
            lines.append(
                '    <p class="muted">Difference blend unavailable: one side is not inlined '
                "(see reason above).</p>"
            )
    elif change == "added":
        cand = _prepare_image(cand_path, s.get("sha256"), budget)
        lines.append(
            f'    <div><span class="ss-lab">Candidate (new, no baseline)</span>'
            f'{_img_html(cand, "Candidate " + rel)}</div>'
        )
    elif change == "removed":
        base = _prepare_image(base_path, s.get("baselineSha256"), budget)
        lines.append(
            f'    <div><span class="ss-lab">Baseline (removed in candidate)</span>'
            f'{_img_html(base, "Baseline " + rel)}</div>'
        )
    else:  # unchanged -- not inlined; hash and size only, to spend the budget on changes.
        sha = s.get("sha256") or s.get("baselineSha256")
        size = s.get("bytes") if s.get("bytes") is not None else s.get("baselineBytes")
        lines.append("    <details><summary>unchanged &mdash; identical hash</summary>")
        if isinstance(sha, str) and sha:
            lines.append(f"      <div>{_hash_html(sha)}</div>")
        if isinstance(size, int):
            lines.append(f'      <p class="num">{escape(_human(size))}</p>')
        lines.append("    </details>")

    lines.append(f'    {_decision_group(did, "screenshot", rel)}')
    lines.append("  </figure>")
    return "\n".join(lines)


def _screenshot_change(change: str) -> str:
    return {
        "added": f"{_PLUS} Added",
        "removed": f"{_MINUS} Removed",
        "changed": "Changed",
        "unchanged": f"{_DASH} Unchanged",
    }.get(change, escape(change))


def _hash_html(sha: str, *, short: int = 16) -> str:
    esha = escape(sha)
    label = escape(sha[:short])
    return (
        f'<button type="button" class="mono hash" title="{esha}"'
        f' aria-label="Copy full SHA-256 {esha}">{label}&hellip;</button>'
        f'<details><summary>Full SHA-256</summary><p class="mono">{esha}</p></details>'
    )


def _bytes_from(row: dict[str, Any], prep: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, int):
        return value
    size = prep.get("size")
    return size if isinstance(size, int) else None


def _changed_facts(s: dict[str, Any], base: dict[str, Any], cand: dict[str, Any]) -> str:
    base_sha = s.get("baselineSha256") if isinstance(s.get("baselineSha256"), str) else base.get("sha")
    cand_sha = s.get("sha256") if isinstance(s.get("sha256"), str) else cand.get("sha")
    base_size = _bytes_from(s, base, "baselineBytes")
    cand_size = _bytes_from(s, cand, "bytes")
    if isinstance(base_size, int) and isinstance(cand_size, int):
        delta = cand_size - base_size
        size_text = f"{_human(base_size)} {_ARROW} {_human(cand_size)} ({delta:+d} B)"
    elif isinstance(base_size, int):
        size_text = f"{_human(base_size)} {_ARROW} unknown"
    elif isinstance(cand_size, int):
        size_text = f"unknown {_ARROW} {_human(cand_size)}"
    else:
        size_text = "unknown"

    parts = [
        '    <div class="ss-facts" role="group" aria-label="Non-visual change facts">',
        (
            f"      <div><strong>SHA-256 changed:</strong> "
            f"{_hash_html(base_sha) if isinstance(base_sha, str) and base_sha else 'unknown'} "
            f"{_ARROW} {_hash_html(cand_sha) if isinstance(cand_sha, str) and cand_sha else 'unknown'}</div>"
        ),
        f"      <p><strong>Bytes:</strong> {size_text}</p>",
        "    </div>",
    ]
    return "\n".join(parts)


def _build_image_index(evidence_dir: Path | None) -> dict[str, Path]:
    """Map every image's variant-relative key to its on-disk path, once per run.

    Mirrors the producer's keying (``evidence/<variant>/home.png`` -> ``home.png``)
    and its symlink-escape guard, so the report inlines exactly the files the diff
    identified. Built once and reused for every screenshot, so rendering is linear
    in the evidence tree rather than quadratic in screenshots x files. Never raises;
    an unreadable or absent tree yields an empty index.
    """
    index: dict[str, Path] = {}
    if evidence_dir is None:
        return index
    try:
        if not evidence_dir.is_dir():
            return index
        resolved_root = evidence_dir.resolve()
        candidates = sorted(evidence_dir.rglob("*"))
    except OSError:
        return index
    for path in candidates:
        try:
            if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            if not path.resolve().is_relative_to(resolved_root):
                continue
            parts = path.relative_to(evidence_dir).parts
        except OSError:
            continue
        key = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
        index[key] = path  # last in sorted order wins, matching the producer
    return index


def _prepare_image(path: Path | None, sha: Any, budget: dict[str, int]) -> dict[str, Any]:
    """Read, budget-check and base64-encode one image; never raises.

    Returns ``{"uri", "reason", "size", "sha"}``: ``uri`` is a ``data:`` URI when
    the image is inlined, else ``None`` with a stated ``reason`` for the degraded
    rendering.
    """
    sha_str = sha if isinstance(sha, str) and sha else None
    if path is None:
        return {"uri": None, "reason": "image not found in the run directory", "size": None, "sha": sha_str}
    try:
        data = path.read_bytes()
    except OSError:
        return {"uri": None, "reason": "image could not be read", "size": None, "sha": sha_str}
    size = len(data)
    if size > _MAX_IMAGE_BYTES:
        return {
            "uri": None,
            "reason": f"not inlined: {_human(size)} exceeds the {_human(_MAX_IMAGE_BYTES)} per-image budget",
            "size": size,
            "sha": sha_str,
        }
    if size > budget["remaining"]:
        return {
            "uri": None,
            "reason": f"not inlined: the {_human(_MAX_SECTION_BYTES)} screenshot-section budget is exhausted",
            "size": size,
            "sha": sha_str,
        }
    budget["remaining"] -= size
    mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
    encoded = base64.b64encode(data).decode("ascii")
    return {"uri": f"data:{mime};base64,{encoded}", "reason": None, "size": size, "sha": sha_str}


def _img_html(prep: dict[str, Any], alt: str) -> str:
    if prep["uri"]:
        return f'<img alt="{escape(alt)}" src="{prep["uri"]}">'
    return _degraded(prep["reason"], prep["sha"], prep["size"])


def _degraded(reason: str, sha: Any, size: Any) -> str:
    parts = [f'<div class="ss-degraded"><p class="muted">{escape(reason)}.</p>']
    if isinstance(sha, str) and sha:
        parts.append(f"<div>{_hash_html(sha)}</div>")
    if isinstance(size, int):
        parts.append(f'<p class="num">{escape(_human(size))}</p>')
    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Shared cell + control helpers
# ---------------------------------------------------------------------------


def _decision_group(decision_id: str, kind: str, label: str) -> str:
    name = escape(f"{kind} {label}")
    return (
        f'<div class="dd" data-decision-id="{decision_id}" data-decided="none">'
        f'<fieldset class="dd-set"><legend class="sr-only">Decision for {name}</legend>'
        f'<button type="button" class="dd-btn dd-accept" aria-pressed="false" '
        f'aria-label="Accept change to {name}">Accept</button>'
        f'<button type="button" class="dd-btn dd-reject" aria-pressed="false" '
        f'aria-label="Reject change to {name}">Reject</button>'
        f'<label class="dd-comment-wrap"><span class="sr-only">Comment on {name}</span>'
        f'<input type="text" class="dd-comment" maxlength="4000" '
        f'aria-label="Comment on {name}" placeholder="Optional comment"></label>'
        f"</fieldset></div>"
    )


def _num(value: Any) -> str:
    if value is None:
        return f'<span class="muted">{_DASH}</span>'
    if isinstance(value, bool):
        return f"{_CHECK} yes" if value else f"{_CROSS} no"
    if isinstance(value, (int, float)):
        return escape(f"{value:g}")
    return escape(value)


def _delta(value: Any) -> str:
    if value is None:
        return f'<span class="muted">{_DASH}</span>'
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return escape(value)
    if value > 0:
        return f'{_UP} {escape(f"{value:+g}")}'
    if value < 0:
        return f'{_DOWN} {escape(f"{value:+g}")}'
    return f"{_DASH} 0"


def _result(value: Any) -> str:
    if value is True:
        return f'<span class="cross-out">{_CHECK} Pass</span>'
    if value is False:
        return f'<span class="cross-in">{_CROSS} Fail</span>'
    return f'<span class="muted">{_DASH} n/a</span>'


def _yesno(value: Any) -> str:
    if value is True:
        return f"{_CHECK} yes"
    if value is False:
        return f"{_CROSS} no"
    return f'<span class="muted">{_DASH} n/a</span>'


def _human(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def _data_island(run_id: str, model_rows: dict[str, dict[str, Any]]) -> str:
    """Embed the client model as JSON.

    Emitted inside ``<script type="application/json">`` and parsed with
    ``JSON.parse``. ``<`` is escaped to ``\\u003c`` so a hostile metric id or
    screenshot path containing ``</script>`` (or ``<!--``) cannot close the block
    early. ``ensure_ascii`` keeps everything else ASCII and inert.
    """
    model = {"runId": run_id, "rows": model_rows}
    payload = json.dumps(model, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    return f'  <script type="application/json" id="adlc-diff-model">{payload}</script>'
