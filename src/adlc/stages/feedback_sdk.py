"""Ship the portable feedback SDK to any GUI that wants it.

``report.html`` inlines the SDK because it must survive being emailed as one
file. A separate GUI -- a dev server, an editor extension, a desktop app --
wants it as a file it can import. Same bytes either way, from one source, so the
two can never drift into disagreeing about the canonical digest.

The ES-module wrapper is generated rather than hand-maintained for the same
reason: two copies of the same 700 lines is one copy plus a future bug.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from pathlib import Path

__all__ = ["ASSET_NAME", "esm_source", "sdk_source", "write_sdk"]

ASSET_NAME = "adlc-feedback.js"

_ESM_FOOTER = """
/* Generated ES-module surface -- do not edit; regenerate with `adlc feedback sdk`. */
const __adlc = globalThis.AdlcFeedbackSDK;
export default __adlc;
export const createSession = __adlc.createSession;
export const canonicalize = __adlc.canonicalize;
export const packDigest = __adlc.packDigest;
export const normalizePoint = __adlc.normalizePoint;
export const normalizeRect = __adlc.normalizeRect;
export const quantize = __adlc.quantize;
export const quantizeNumber = __adlc.quantizeNumber;
export const cleanText = __adlc.cleanText;
export const newId = __adlc.newId;
export const PACK_VERSION = __adlc.PACK_VERSION;
export const TARGETS_VERSION = __adlc.TARGETS_VERSION;
export const GEOMETRY_DECIMALS = __adlc.GEOMETRY_DECIMALS;
"""


@cache
def sdk_source() -> str:
    """The SDK as a classic script / CommonJS module.

    Read through :mod:`importlib.resources` rather than ``Path(__file__)`` so it
    works identically from a source checkout and from inside an installed wheel.
    """
    text = (files("adlc") / "assets" / "feedback-sdk" / ASSET_NAME).read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"feedback SDK asset {ASSET_NAME} is empty")
    return text


def esm_source() -> str:
    return sdk_source().rstrip("\n") + "\n" + _ESM_FOOTER


def write_sdk(out_dir: Path) -> list[Path]:
    """Write ``adlc-feedback.js`` and ``adlc-feedback.mjs`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in (
        (ASSET_NAME, sdk_source()),
        (ASSET_NAME.replace(".js", ".mjs"), esm_source()),
    ):
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
