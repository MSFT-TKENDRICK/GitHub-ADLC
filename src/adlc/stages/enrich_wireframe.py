"""Low-fidelity wireframe enrichment (leaf L9).

Writes ``<run_dir>/enrichment/wireframe.excalidraw`` — a real Excalidraw
document that opens at https://excalidraw.com, in the VS Code Excalidraw
extension, and in Obsidian.

Schema verified against ``excalidraw/excalidraw`` @ ``master``:

* ``packages/element/src/types.ts`` — ``_ExcalidrawElementBase`` requires
  ``id x y strokeColor backgroundColor fillStyle strokeWidth strokeStyle
  roundness roughness opacity width height angle seed version versionNonce
  index isDeleted groupIds frameId boundElements updated link locked``.
  Text adds ``text fontSize fontFamily textAlign verticalAlign containerId
  originalText autoResize lineHeight``; ``line``/``arrow`` add ``points
  startBinding endBinding startArrowhead endArrowhead`` (+ ``elbowed`` on
  arrows, ``polygon`` on lines).
* ``packages/common/src/constants.ts`` — ``EXPORT_DATA_TYPES.excalidraw =
  "excalidraw"``, ``VERSIONS.excalidraw = 2``, ``FONT_FAMILY.Excalifont = 5``,
  ``FONT_FAMILY.Nunito = 6``, ``FONT_FAMILY.Cascadia = 3``,
  ``DEFAULT_GRID_SIZE = 20``, ``TEXT_ALIGN``/``VERTICAL_ALIGN`` string enums.

Output is **byte-for-byte deterministic** for a given spec: ids and seeds are
derived from a SHA-256 of the feature title plus the element index, and
``updated`` is a fixed epoch. That keeps the spine's stage ``digest`` stable
across re-runs.

Deterministic, offline, no LLM. ``generate()`` never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config

log = logging.getLogger(__name__)

#: Marks this generator in ``.adlc/config.yaml`` -> ``enrich.skip``.
FACET = "wireframe"

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "wireframe.excalidraw.j2"

DOC_TYPE = "excalidraw"
DOC_VERSION = 2
DOC_SOURCE = "https://github.com/MSFT-TKENDRICK/GitHub-ADLC (adlc enrich)"
GRID_SIZE = 20
VIEW_BACKGROUND = "#ffffff"

#: Fixed epoch-ms so re-running enrichment produces an identical file.
FIXED_UPDATED = 1_700_000_000_000

FONT_EXCALIFONT = 5
FONT_NUNITO = 6
FONT_CASCADIA = 3

INK = "#1e1e1e"
MUTED = "#868e96"
ACCENT = "#1971c2"
ACCENT_FILL = "#a5d8ff"
SURFACE = "#f8f9fa"
NOTE = "#e03131"

#: Canvas geometry for the low-fidelity page frame.
FRAME_X, FRAME_Y = 0, 0
FRAME_W, FRAME_H = 900, 640
PAD = 24

_TOP_LEVEL_KEYS = ("type", "version", "source", "elements", "appState", "files")
_BASE_KEYS = (
    "id", "type", "x", "y", "width", "height", "angle", "strokeColor", "backgroundColor",
    "fillStyle", "strokeWidth", "strokeStyle", "roundness", "roughness", "opacity", "seed",
    "version", "versionNonce", "index", "isDeleted", "groupIds", "frameId", "boundElements",
    "updated", "link", "locked",
)
_TEXT_KEYS = (
    "text", "fontSize", "fontFamily", "textAlign", "verticalAlign", "containerId",
    "originalText", "autoResize", "lineHeight",
)
_LINEAR_KEYS = ("points", "startBinding", "endBinding", "startArrowhead", "endArrowhead")
_VALID_TYPES = frozenset({"rectangle", "ellipse", "diamond", "text", "arrow", "line"})


# ---------------------------------------------------------------------------
# Spec mining (deliberately tiny — a wireframe is a sketch, not a spec replica)
# ---------------------------------------------------------------------------

_AS_A = re.compile(r"\bAs an?\s+([A-Za-z][A-Za-z0-9 \-/]{2,45}?)\s*(?=,|\s+I\s+)", re.IGNORECASE)
_I_WANT = re.compile(r"\bI\s+(?:want|need|would like)\s+(?:to\s+)?([^,.;]{4,80})", re.IGNORECASE)
_ROUTE = re.compile(r"[`\"']((?:/[A-Za-z0-9\-_{}:]+)+/?)[`\"']")
_STORY_HEADING = re.compile(r"^#{2,6}\s+(.*\bstory\b.*|.*\bscenario\b.*)$", re.IGNORECASE)
_AC_ID = re.compile(r"\b((?:US\d{1,3}-)?(?:AC|FR|NFR|SC|TR|UC)-?\d{1,3})\b")
_CTA_VERB = re.compile(
    r"\b(save|submit|publish|checkout|check out|confirm|apply|enable|toggle|export|import"
    r"|create|add|send|start|continue|upgrade|subscribe|sign up|log in|search|filter"
    r"|download|share|delete|approve)\b",
    re.IGNORECASE,
)


def _clean(text: str, limit: int = 48) -> str:
    out = re.sub(r"\s+", " ", (text or "")).strip().strip("*_`#").strip()
    out = re.sub(r"[.,;:]+$", "", out)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _feature_title(spec_text: str) -> str:
    for line in (spec_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = re.sub(
                r"^(feature\s+specification|specification|spec|feature)\s*[:\-]\s*",
                "",
                stripped[2:].strip(),
                flags=re.IGNORECASE,
            )
            return _clean(title, 60) or "Proposed change"
    return "Proposed change"


def outline(spec_text: str) -> dict[str, Any]:
    """The handful of facts a low-fi wireframe actually needs."""
    text = spec_text or ""
    title = _feature_title(text)

    nav: list[str] = []
    for match in _ROUTE.finditer(text):
        label = _clean(match.group(1), 18)
        if label and label not in nav:
            nav.append(label)
    if not nav:
        for line in text.splitlines():
            heading = _STORY_HEADING.match(line)
            if heading:
                label = _clean(re.split(r"\s+[-–—]\s+", heading.group(1))[-1], 18)
                if label and label not in nav:
                    nav.append(label)
    nav = nav[:5] or ["Home", "Settings"]

    blocks: list[tuple[str, str]] = []
    for line in text.splitlines():
        want = _I_WANT.search(line)
        if not want:
            continue
        actor = _AS_A.search(line)
        caption = _clean(actor.group(1).title(), 24) if actor else "User"
        label = _clean(want.group(1), 46)
        if label and label not in [b[1] for b in blocks]:
            blocks.append((caption, label))
    if not blocks:
        blocks = [("User", title)]
    blocks = blocks[:3]

    cta_match = _CTA_VERB.search(" ".join(b[1] for b in blocks)) or _CTA_VERB.search(text)
    cta = _clean(cta_match.group(1).title(), 20) if cta_match else "Continue"

    criteria: list[str] = []
    for match in _AC_ID.finditer(text):
        if match.group(1) not in criteria:
            criteria.append(match.group(1))

    return {"title": title, "nav": nav, "blocks": blocks, "cta": cta, "criteria": criteria[:8]}


# ---------------------------------------------------------------------------
# Element construction
# ---------------------------------------------------------------------------


class _Ids:
    """Deterministic id / seed / nonce source, derived from the feature title."""

    def __init__(self, salt: str) -> None:
        self._salt = salt
        self._n = 0

    def next(self) -> tuple[str, int, int]:
        self._n += 1
        digest = hashlib.sha256(f"{self._salt}#{self._n}".encode()).digest()
        element_id = digest[:12].hex()
        seed = int.from_bytes(digest[12:16], "big") % 2_147_483_647
        nonce = int.from_bytes(digest[16:20], "big") % 2_147_483_647
        return element_id, seed, nonce


def _base(ids: _Ids, kind: str, x: float, y: float, w: float, h: float, **over: Any) -> dict:
    element_id, seed, nonce = ids.next()
    element: dict[str, Any] = {
        "id": element_id,
        "type": kind,
        "x": float(x),
        "y": float(y),
        "width": float(w),
        "height": float(h),
        "angle": 0,
        "strokeColor": INK,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roundness": None,
        "roughness": 1,
        "opacity": 100,
        "seed": seed,
        "version": 1,
        "versionNonce": nonce,
        "index": None,
        "isDeleted": False,
        "groupIds": [],
        "frameId": None,
        "boundElements": None,
        "updated": FIXED_UPDATED,
        "link": None,
        "locked": False,
    }
    element.update(over)
    return element


def _box(ids: _Ids, x: float, y: float, w: float, h: float, **over: Any) -> dict:
    return _base(ids, "rectangle", x, y, w, h, **over)


def _text(
    ids: _Ids,
    x: float,
    y: float,
    body: str,
    size: int = 16,
    color: str = INK,
    font: int = FONT_EXCALIFONT,
    align: str = "left",
    width: float | None = None,
) -> dict:
    line_height = 1.25
    lines = body.split("\n")
    measured = max((len(line) for line in lines), default=1) * size * 0.55
    element = _base(
        ids,
        "text",
        x,
        y,
        width if width is not None else max(measured, size * 0.55),
        size * line_height * len(lines),
        strokeColor=color,
    )
    element.update(
        {
            "text": body,
            "originalText": body,
            "fontSize": size,
            "fontFamily": font,
            "textAlign": align,
            "verticalAlign": "top",
            "containerId": None,
            "autoResize": True,
            "lineHeight": line_height,
        }
    )
    return element


def _arrow(ids: _Ids, x: float, y: float, dx: float, dy: float, color: str = NOTE) -> dict:
    element = _base(
        ids, "arrow", x, y, abs(dx), abs(dy), strokeColor=color, strokeWidth=2, roughness=1
    )
    element.update(
        {
            "points": [[0, 0], [float(dx), float(dy)]],
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": "arrow",
            "elbowed": False,
        }
    )
    return element


def build_elements(spec_text: str) -> list[dict[str, Any]]:
    """Lay out header / nav / content blocks / primary CTA / annotations."""
    facts = outline(spec_text)
    ids = _Ids(facts["title"])
    elements: list[dict[str, Any]] = []

    inner_x = FRAME_X + PAD
    inner_w = FRAME_W - 2 * PAD

    elements.append(_box(ids, FRAME_X, FRAME_Y, FRAME_W, FRAME_H, strokeColor=MUTED))
    elements.append(
        _text(
            ids,
            FRAME_X,
            FRAME_Y - 34,
            f"Wireframe (low-fi) — {facts['title']}",
            size=20,
            font=FONT_NUNITO,
        )
    )

    header_y = FRAME_Y + PAD
    elements.append(
        _box(ids, inner_x, header_y, inner_w, 64, backgroundColor=SURFACE, fillStyle="solid")
    )
    elements.append(_text(ids, inner_x + 16, header_y + 20, facts["title"], size=20))
    elements.append(
        _text(ids, inner_x + inner_w - 130, header_y + 24, "[ account ]", size=14, color=MUTED)
    )

    nav_y = header_y + 64 + 16
    nav_x = inner_x
    for label in facts["nav"]:
        width = max(96.0, len(label) * 9.0 + 24)
        elements.append(_box(ids, nav_x, nav_y, width, 36, strokeColor=MUTED))
        elements.append(_text(ids, nav_x + 12, nav_y + 10, label, size=14, color=MUTED))
        nav_x += width + 12

    content_y = nav_y + 36 + 24
    content_h = 300
    block_gap = 16
    count = max(len(facts["blocks"]), 1)
    block_w = (inner_w - block_gap * (count - 1)) / count
    for i, (actor, label) in enumerate(facts["blocks"]):
        bx = inner_x + i * (block_w + block_gap)
        elements.append(_box(ids, bx, content_y, block_w, content_h))
        elements.append(
            _text(
                ids,
                bx + 14,
                content_y + 14,
                f"{actor}:",
                size=14,
                color=ACCENT,
                font=FONT_CASCADIA,
            )
        )
        wrapped = "\n".join(_wrap(label, max(12, int(block_w / 9))))
        elements.append(_text(ids, bx + 14, content_y + 40, wrapped, size=16))
        for row in range(4):
            elements.append(
                _box(
                    ids,
                    bx + 14,
                    content_y + 130 + row * 34,
                    block_w - 28,
                    22,
                    strokeColor=MUTED,
                    backgroundColor=SURFACE,
                    fillStyle="solid",
                    opacity=60,
                )
            )

    cta_y = content_y + content_h + 28
    cta_w, cta_h = 200.0, 48.0
    cta_x = inner_x + inner_w - cta_w
    elements.append(
        _box(
            ids,
            cta_x,
            cta_y,
            cta_w,
            cta_h,
            strokeColor=ACCENT,
            backgroundColor=ACCENT_FILL,
            fillStyle="solid",
            strokeWidth=2,
        )
    )
    elements.append(
        _text(ids, cta_x + 24, cta_y + 14, f"{facts['cta']} (primary)", size=16, color=ACCENT)
    )
    elements.append(_box(ids, inner_x, cta_y, 140, cta_h, strokeColor=MUTED))
    elements.append(_text(ids, inner_x + 24, cta_y + 14, "Cancel", size=16, color=MUTED))

    annotation_x = FRAME_X + FRAME_W + 60
    elements.append(
        _arrow(
            ids,
            cta_x + cta_w + 12,
            cta_y + 24,
            annotation_x - cta_x - cta_w - 24,
            0,
        )
    )
    criteria = ", ".join(facts["criteria"]) if facts["criteria"] else "none declared in spec"
    elements.append(
        _text(
            ids,
            annotation_x,
            cta_y + 6,
            f"Primary CTA for:\n{facts['title']}\nAcceptance criteria: {criteria}",
            size=14,
            color=NOTE,
        )
    )
    elements.append(
        _text(
            ids,
            FRAME_X,
            FRAME_Y + FRAME_H + 20,
            "Low-fidelity only: boxes are regions, not visual design.\n"
            "Generated by adlc enrich (L9) from spec.md — edit the spec, not this file.",
            size=13,
            color=MUTED,
            font=FONT_CASCADIA,
        )
    )
    return elements


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_excalidraw(document: Any) -> tuple[bool, list[str]]:
    """Check ``document`` against the fields Excalidraw's loader relies on."""
    errors: list[str] = []
    if not isinstance(document, dict):
        return False, [f"document is {type(document).__name__}, expected object"]
    for key in _TOP_LEVEL_KEYS:
        if key not in document:
            errors.append(f"missing top-level key {key!r}")
    if document.get("type") != DOC_TYPE:
        errors.append(f"type must be {DOC_TYPE!r}, got {document.get('type')!r}")
    if document.get("version") != DOC_VERSION:
        errors.append(f"version must be {DOC_VERSION}, got {document.get('version')!r}")
    if not isinstance(document.get("appState"), dict):
        errors.append("appState must be an object")
    if not isinstance(document.get("files"), dict):
        errors.append("files must be an object")

    elements = document.get("elements")
    if not isinstance(elements, list):
        return False, [*errors, "elements must be a list"]
    if not elements:
        errors.append("elements is empty")

    seen: set[str] = set()
    for i, element in enumerate(elements):
        where = f"elements[{i}]"
        if not isinstance(element, dict):
            errors.append(f"{where} is not an object")
            continue
        for key in _BASE_KEYS:
            if key not in element:
                errors.append(f"{where} missing {key!r}")
        kind = element.get("type")
        if kind not in _VALID_TYPES:
            errors.append(f"{where} has unsupported type {kind!r}")
        element_id = element.get("id")
        if not isinstance(element_id, str) or not element_id:
            errors.append(f"{where} has a non-string id")
        elif element_id in seen:
            errors.append(f"{where} duplicates id {element_id!r}")
        else:
            seen.add(element_id)
        for key in ("x", "y", "width", "height", "angle", "seed", "version", "versionNonce",
                    "opacity", "strokeWidth", "roughness", "updated"):
            value = element.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{where}.{key} must be numeric, got {value!r}")
            elif not math.isfinite(value):
                # json.dumps happily writes NaN/Infinity; JSON.parse throws on it.
                errors.append(f"{where}.{key} is {value!r}, which is not valid JSON")
        if not isinstance(element.get("groupIds"), list):
            errors.append(f"{where}.groupIds must be a list")
        if not isinstance(element.get("isDeleted"), bool):
            errors.append(f"{where}.isDeleted must be a bool")
        if not isinstance(element.get("locked"), bool):
            errors.append(f"{where}.locked must be a bool")
        if kind == "text":
            for key in _TEXT_KEYS:
                if key not in element:
                    errors.append(f"{where} (text) missing {key!r}")
            if not isinstance(element.get("text"), str) or not element.get("text"):
                errors.append(f"{where}.text must be a non-empty string")
            if element.get("textAlign") not in {"left", "center", "right"}:
                errors.append(f"{where}.textAlign is invalid")
            if element.get("verticalAlign") not in {"top", "middle", "bottom"}:
                errors.append(f"{where}.verticalAlign is invalid")
        if kind in {"arrow", "line"}:
            for key in _LINEAR_KEYS:
                if key not in element:
                    errors.append(f"{where} ({kind}) missing {key!r}")
            points = element.get("points")
            if not isinstance(points, list) or len(points) < 2:
                errors.append(f"{where}.points needs at least two points")
            elif any(not (isinstance(p, list) and len(p) == 2) for p in points):
                errors.append(f"{where}.points entries must be [x, y] pairs")
    return (not errors), errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_document(spec_text: str) -> dict[str, Any]:
    """Render the envelope template around generated elements and parse it back."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    elements = build_elements(spec_text)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    rendered = env.get_template(TEMPLATE_NAME).render(
        doc_type=DOC_TYPE,
        version=DOC_VERSION,
        source=DOC_SOURCE,
        elements_json=json.dumps(elements, indent=2, sort_keys=False),
        grid_size=GRID_SIZE,
        view_background_color=VIEW_BACKGROUND,
    )
    return json.loads(rendered)


def _skipped(cfg: Config | None) -> bool:
    try:
        skip = ((getattr(cfg, "raw", None) or {}).get("enrich") or {}).get("skip") or []
        return FACET in skip
    except Exception:  # noqa: BLE001 - config shape is not ours to trust
        return False


def generate(run_dir: Path, spec_text: str, cfg: Config) -> list[Path]:
    """Write ``run_dir/enrichment/wireframe.excalidraw`` and return its path.

    Never raises. The document is validated before it is written — an invalid
    Excalidraw file is worse than no file, so a failed check yields ``[]``.
    """
    try:
        if _skipped(cfg):
            log.info("enrich_wireframe: skipped via config (enrich.skip contains %r)", FACET)
            return []
        run_dir = Path(run_dir)
        if not (spec_text or "").strip():
            log.warning("enrich_wireframe: no spec text available, nothing to sketch")
            return []

        document = build_document(spec_text)
        ok, errors = validate_excalidraw(document)
        if not ok:
            log.error("enrich_wireframe: refusing to write an invalid document: %s", errors[:5])
            return []

        out_dir = run_dir / "enrichment"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "wireframe.excalidraw"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return [path]
    except Exception:  # noqa: BLE001 - contract: never break the run
        log.exception("enrich_wireframe: generation failed")
        return []
