"""Evidence section: hash-verified artifacts as an annotatable review surface.

Layer 4 owns this file. It grows the old read-only artifact table into an
*annotatable* surface: image artifacts are inlined as ``data:`` URIs (under a
strict byte budget) with a keyboard-first markup overlay. Annotations are
persisted client-side and exported, in exact ``$defs.annotation`` shape, through
the shared ``window.adlcFeedback`` client registry that the later feedback
layers read.

Purity deviation: sections are documented as pure functions of
:class:`ReportContext`, and :func:`adlc.stages.report.render.build_context` does
*not* read artifact bytes (it is a shared file this layer must not edit). So this
section reads image bytes -- and the review pack's requirement ids -- directly
from ``ctx.rd``. Every such read is bounded and wrapped in
``try/except (OSError, ValueError)`` so a missing, oversized or unreadable file
degrades to an explicit, *visible* note rather than raising or silently
vanishing. Nothing here mutates state.
"""

from __future__ import annotations

import base64
import json
from pathlib import PurePosixPath
from typing import Any

from adlc.runs import read_json
from adlc.stages.report.context import (
    MAX_INLINE_BYTES_DOCUMENT,
    ReportContext,
    encoded_data_uri_len,
    escape,
    omission,
)
from adlc.stages.report.shell import read_asset

#: Per-artifact ceiling on inlined image bytes. ``report.html`` is meant to
#: survive being emailed as one file, so an unbounded ``data:`` inline turns a
#: screenshot-heavy run into a multi-hundred-MiB attachment. Over-budget images
#: are never dropped silently: the row and a figure survive with the hash, the
#: size and the reason the image was not inlined.
MAX_INLINE_BYTES_PER_ARTIFACT = 2 * 1024 * 1024  # 2 MiB

#: Retained name for the whole-document ceiling, which now lives in
#: ``context`` because it is shared with the evidence-diff section rather than
#: owned by this one.
MAX_INLINE_BYTES_TOTAL = MAX_INLINE_BYTES_DOCUMENT

#: Raster image types we will inline. SVG is deliberately absent -- see
#: :func:`_classify_image`.
_RASTER_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
#: Every extension we treat as an image for classification (raster + svg).
_IMAGE_EXT = set(_RASTER_MIME) | {".svg"}

#: An artifact hash is citable in an annotation only if it is a real SHA-256.
_SHA256_HEX_LEN = 64


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_HEX_LEN and all(c in "0123456789abcdef" for c in value)


def _human_size(size: int) -> str:
    size = int(size)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def _is_image(path: str, kind: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _IMAGE_EXT or kind == "screenshot"


def _read_capped(ctx: ReportContext, rel: str, cap: int) -> bytes | None:
    """Read at most ``cap + 1`` bytes of ``rel`` from within the run dir.

    Reads are bounded (never load an arbitrarily large file to reject it) and
    confined to the run directory (a ``..`` or absolute ``rel`` is refused, not
    followed). Any filesystem or path error yields ``None`` -- an unreadable
    artifact degrades, it does not raise.
    """
    try:
        base = ctx.rd.path.resolve()
        target = (ctx.rd.path / rel).resolve()
        target.relative_to(base)  # ValueError if rel escapes the run dir
        with target.open("rb") as handle:
            return handle.read(cap + 1)
    except (OSError, ValueError):
        return None


def _classify_image(
    ctx: ReportContext, path: str, kind: str, size: int
) -> tuple[str | None, str, int]:
    """Decide whether one image is inlined. Returns ``(data_uri, reason, nbytes)``.

    ``data_uri`` is ``None`` when the image is not inlined, in which case
    ``reason`` states why in plain language; ``nbytes`` is the number of bytes
    actually inlined (0 when skipped).
    """
    ext = PurePosixPath(path).suffix.lower()
    if ext == ".svg":
        # SVG can carry <script>/onload; even referenced from an <img> it is a
        # needless script-execution surface in a document reviewers open from
        # file://. We refuse to inline it rather than sanitise it, and say so.
        return None, "an SVG can carry executable script, so it is not inlined", 0
    mime = _RASTER_MIME.get(ext) or ("image/png" if kind == "screenshot" else None)
    if mime is None:
        return None, "unrecognised image type; not inlined", 0
    cap = MAX_INLINE_BYTES_PER_ARTIFACT
    if size > cap:
        return None, f"it is {_human_size(size)}, over the {_human_size(cap)} per-image budget", 0
    raw = _read_capped(ctx, path, cap)
    if raw is None:
        return None, "its bytes could not be read from the run directory", 0
    if len(raw) > cap:
        return None, f"it exceeds the {_human_size(cap)} per-image inline budget", 0
    # Charge what the document will actually carry (base64, ~4/3 of raw), and
    # test the budget before paying for the encode.
    encoded_len = encoded_data_uri_len(len(raw), mime)
    if not ctx.inline_budget.charge(encoded_len):
        return (
            None,
            f"the {_human_size(ctx.inline_budget.total)} document inline budget was already reached",
            0,
        )
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return data_uri, "", encoded_len


def _load_requirements(ctx: ReportContext) -> list[dict[str, str]]:
    """Requirement ids/titles from the review pack, for the annotation form.

    Absent or malformed packs simply yield no checkboxes -- the form still
    offers a free-text requirement field.
    """
    try:
        pack = read_json(ctx.rd.review_pack)
    except (OSError, ValueError):
        return []
    reqs = pack.get("requirements") if isinstance(pack, dict) else None
    out: list[dict[str, str]] = []
    if isinstance(reqs, list):
        for entry in reqs:
            if isinstance(entry, dict) and entry.get("id"):
                out.append({"id": str(entry["id"])[:64], "text": str(entry.get("text", ""))[:160]})
    return out


def _json_script(payload: dict[str, Any]) -> str:
    """Embed ``payload`` as parseable JSON that cannot terminate the block early.

    ``<`` is escaped to ``\\u003c`` so a ``</script>`` (or ``<!--``) hiding in an
    artifact path cannot close the ``<script>`` element; ``JSON.parse`` restores
    it. ``ensure_ascii`` keeps the whole document ASCII.
    """
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="adlc-evidence-data">{raw}</script>'


def _hash_html(sha: str, *, short: int = 16) -> str:
    esha = escape(sha)
    label = escape(sha[:short])
    return (
        f'<button type="button" class="mono hash" title="{esha}"'
        f' aria-label="Copy full SHA-256 {esha}">{label}&hellip;</button>'
        f'<details><summary>Full SHA-256</summary><p class="mono">{esha}</p></details>'
    )


def _table_row(path: str, kind: str, size: int, sha: str, is_img: bool, inlined: bool, reason: str) -> str:
    epath, ekind = escape(path), escape(kind)
    if not is_img:
        cell = '<span class="muted">n/a</span>'
    elif inlined:
        cell = '<span class="annot-flag ok">inlined</span>'
    else:
        cell = f'<span class="annot-flag bad" title="{escape(reason)}">not inlined</span>'
    return (
        f'<tr><th scope="row" class="mono">{epath}</th>'
        f'<td><span class="tag">{ekind}</span></td>'
        f'<td class="num">{_human_size(size)}</td>'
        f"<td>{cell}</td>"
        f"<td>{_hash_html(sha)}</td></tr>"
    )


def _figure(
    *, sha: str, path: str, kind: str, size: int, data_uri: str | None, reason: str, annotatable: bool
) -> str:
    epath, ekind, esha = escape(path), escape(kind), escape(sha)
    esha16 = escape(sha[:16])
    human = _human_size(size)
    inlined = data_uri is not None
    flag = "ok" if inlined else "bad"
    cap = (
        '    <figcaption class="annot-cap">'
        f'<span class="mono">{epath}</span> '
        f'<span class="tag">{ekind}</span> '
        f'<span class="num">{human}</span> '
        f"{_hash_html(sha)} "
        f'<span class="annot-flag {flag}">{"inlined" if inlined else "not inlined"}</span>'
        "</figcaption>"
    )
    if inlined:
        body = (
            '    <div class="annot-stage">\n'
            f'      <img class="annot-img" alt="Evidence screenshot: {epath}" src="{data_uri}">\n'
            f'      <svg class="annot-overlay" role="img" aria-label="Markup overlay for {epath}.'
            " Use the annotation form below to add or edit markup with the keyboard.\""
            ' viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>\n'
            '      <div class="annot-labels" aria-hidden="true"></div>\n'
            "    </div>"
        )
    else:
        body = (
            f'    <p class="annot-degraded">This image was not inlined ({escape(reason)}). Its'
            f" SHA-256 ({esha16}&hellip;) and size ({human}) are shown so the evidence is still"
            " accounted for; open the artifact from the run directory to view it. You can still"
            " record a whole-image annotation below.</p>"
        )
    degraded_attr = "" if inlined else ' data-degraded="1"'
    return (
        f'  <figure class="annot-artifact" data-sha="{esha}" data-path="{epath}"'
        f' data-kind="{ekind}" data-annotatable="{"1" if annotatable else "0"}"{degraded_attr}>\n'
        f"{cap}\n"
        f"{body}\n"
        '    <div class="annot-mount"></div>\n'
        "  </figure>"
    )


def render(ctx: ReportContext) -> str:
    artifacts = ctx.artifacts
    if not artifacts:
        return "\n".join(
            ["  <h2>Evidence</h2>", "  " + omission("No artifacts were captured for this run.")]
        )

    requirements = _load_requirements(ctx)
    total_inlined = 0
    count_inlined = 0
    count_skipped = 0
    rows: list[str] = []
    figures: list[str] = []
    js_artifacts: list[dict[str, Any]] = []

    for art in artifacts:
        path = str(art.get("path", ""))
        kind = str(art.get("kind", ""))
        size = int(art.get("bytes", 0) or 0)
        sha = str(art.get("sha256", ""))
        is_img = _is_image(path, kind)
        data_uri: str | None = None
        reason = ""
        if is_img:
            data_uri, reason, nbytes = _classify_image(ctx, path, kind, size)
            if data_uri is not None:
                total_inlined += nbytes
                count_inlined += 1
            else:
                count_skipped += 1
            annotatable = _is_sha256(sha)
            figures.append(
                _figure(
                    sha=sha,
                    path=path,
                    kind=kind,
                    size=size,
                    data_uri=data_uri,
                    reason=reason,
                    annotatable=annotatable,
                )
            )
            js_artifacts.append(
                {
                    "sha256": sha,
                    "path": path,
                    "kind": kind,
                    "inlined": data_uri is not None,
                    "reason": reason,
                    "bytes": size,
                    "annotatable": annotatable,
                }
            )
        rows.append(_table_row(path, kind, size, sha, is_img, data_uri is not None, reason))

    summary = (
        f'  <p class="annot-budget note">Inlined {count_inlined} image(s) adding '
        f"{_human_size(total_inlined)} to the document, against a "
        f"{_human_size(ctx.inline_budget.total)} inline budget shared with the "
        f"evidence-diff section; "
        f"{count_skipped} image(s) not inlined and kept below with hash, size and reason.</p>"
    )
    figures_html = (
        "\n".join(figures)
        if figures
        else '  <p class="muted">No inlinable image artifacts were captured; '
        "there is nothing to annotate visually.</p>"
    )
    data_block = _json_script(
        {"runId": ctx.rd.run_id, "requirements": requirements, "artifacts": js_artifacts}
    )

    return "\n".join(
        [
            "  <h2>Evidence</h2>",
            f"  <style>\n{read_asset('annotate.css')}\n  </style>",
            (
                '  <table><caption class="sr-only">Evidence artifacts captured for this run</caption>'
                "<thead><tr><th>Artifact</th><th>Kind</th><th>Size</th>"
                "<th>Inlined</th><th>SHA-256</th></tr></thead>"
            ),
            f"  <tbody>{''.join(rows)}</tbody></table>",
            (
                '  <p class="note">Hashes are what the evidence gate verifies &mdash; the reviewing'
                " agent never sees raw traces, HAR or console text. Inlined screenshots are embedded"
                " as <code>data:</code> URIs so this report stays one self-contained file.</p>"
            ),
            summary,
            f'  <div class="annot-root" data-run-id="{escape(ctx.rd.run_id)}">',
            figures_html,
            "  </div>",
            f"  {data_block}",
            f"  <script>\n{read_asset('annotate.js')}\n  </script>",
        ]
    )
