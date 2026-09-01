"""Build the standalone feedback console: reference consumer #2.

``report.html`` is the *first* consumer of ``adlc-feedback-targets/v1``. A
contract with exactly one consumer is not a contract, it is an internal function
call with extra steps. This module is the second consumer, and it is deliberately
built from nothing the report package owns:

* it reads ``feedback-targets.json`` -- not a ``RunDir``, not a ``ReportContext``;
* it uses the portable SDK -- not ``report/assets/*.js``;
* it imports nothing from :mod:`adlc.stages.report`.

If the GUI is replaced tomorrow, the replacement's job is exactly this file's
job, and this file keeps working as the reference for how to do it.

Assembly is sentinel replacement, not :meth:`str.format`. CSS and JS are dense
with braces; ``str.format`` would demand every one of them be doubled, which is
a silent-breakage generator rather than a template engine.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from adlc.stages.feedback_sdk import sdk_source

__all__ = ["build_console", "console_asset", "write_console"]

_PACKAGE = "adlc"
_DIR = "feedback-console"

#: Replaced verbatim, longest-first is irrelevant because every sentinel is
#: distinct and appears exactly once.
_CSS = "/*ADLC:CSS*/"
_TARGETS = "/*ADLC:TARGETS*/"
_SDK = "/*ADLC:SDK*/"
_CONSOLE = "/*ADLC:CONSOLE*/"


@cache
def console_asset(name: str) -> str:
    """Read one console asset through :mod:`importlib.resources`.

    Resource access rather than ``Path(__file__)`` so this works identically
    from a source checkout and from inside an installed wheel.
    """
    text = (files(_PACKAGE) / "assets" / _DIR / name).read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"console asset {name} is empty")
    return text


def _json_island(targets: dict[str, Any]) -> str:
    """Serialise the manifest so it cannot terminate its own script block.

    A ``</script>`` inside any string -- an artifact path, a finding body, a
    persona name -- would end the block and drop the rest of the document into
    the DOM as markup. Escaping ``<`` removes the sequence entirely; JSON
    unescapes ``\\u003c`` back to ``<`` on parse, so no data is altered.
    """
    text = json.dumps(targets, ensure_ascii=False, sort_keys=True)
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_console(targets: dict[str, Any]) -> str:
    """Return a single self-contained HTML document for ``targets``.

    No network access, no relative references: the result opens from ``file://``
    and can be emailed as one file, exactly like ``report.html``.
    """
    if targets.get("schemaVersion") != "adlc-feedback-targets/v1":
        raise ValueError(
            "console expects an adlc-feedback-targets/v1 document, got "
            f"{targets.get('schemaVersion')!r}"
        )
    html = console_asset("console.html")
    for sentinel in (_CSS, _TARGETS, _SDK, _CONSOLE):
        if html.count(sentinel) != 1:
            raise RuntimeError(f"console template must contain {sentinel} exactly once")
    # Order matters only in that substituted content must not itself contain a
    # later sentinel. Assets are ours and asserted ASCII-sentinel-free by tests.
    html = html.replace(_CSS, console_asset("console.css"))
    html = html.replace(_SDK, sdk_source())
    html = html.replace(_CONSOLE, console_asset("console.js"))
    return html.replace(_TARGETS, _json_island(targets))


def write_console(targets: dict[str, Any], out: Path) -> Path:
    """Write the console to ``out`` and return the path written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_console(targets), encoding="utf-8")
    return out
