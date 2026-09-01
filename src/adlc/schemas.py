"""JSON Schema validation for ADLC artifacts.

Schemas live in ``schemas/`` at the repo root and are the acceptance oracle for
every workstream. They are a published contract -- consumers and feedback GUIs
read them directly -- which is why they sit at the root rather than inside the
package, and why the wheel has to go out of its way to carry a copy.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Where to look for ``schemas/``, in order.
#:
#: The wheel copy is written to ``_schemas`` rather than ``schemas`` because
#: this module is ``adlc/schemas.py``; a sibling ``adlc/schemas/`` directory
#: would make ``import adlc.schemas`` depend on FileFinder preferring a real
#: module over a namespace package. That is true today and is not a thing to
#: rest a published contract on.
SCHEMA_DIR_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "schemas",   # repo checkout
    Path(__file__).resolve().parent / "_schemas",      # installed wheel
    Path(__file__).resolve().parent / "schemas",       # legacy layout
)


class ValidationError(Exception):
    """Raised when an artifact does not satisfy its schema."""

    def __init__(self, schema: str, errors: list[str]) -> None:
        self.schema = schema
        self.errors = errors
        detail = "\n  - ".join(errors)
        super().__init__(f"{schema} validation failed:\n  - {detail}")


def schema_dir() -> Path:
    for candidate in SCHEMA_DIR_CANDIDATES:
        if candidate.is_dir():
            return candidate
    searched = "\n  - ".join(str(c) for c in SCHEMA_DIR_CANDIDATES)
    raise FileNotFoundError(
        "could not locate the ADLC schemas/ directory. The installed adlc "
        "package is missing its bundled schemas, so no artifact can be "
        "validated. Reinstall adlc, or run from a repo checkout.\nSearched:\n"
        f"  - {searched}"
    )


@lru_cache(maxsize=32)
def load_schema(name: str) -> dict[str, Any]:
    path = schema_dir() / f"{name}.schema.json"
    if not path.is_file():
        raise FileNotFoundError(f"schema '{name}' not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(name: str, payload: Any) -> None:
    """Validate ``payload`` against ``schemas/<name>.schema.json``.

    Raises :class:`ValidationError` listing *every* problem, not just the first
    -- a partial error list makes agent self-correction slow.
    """
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("jsonschema is required: pip install jsonschema") from exc

    schema = load_schema(name)
    validator = jsonschema.Draft202012Validator(schema)
    errors = [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]
    if errors:
        raise ValidationError(name, errors)


def is_valid(name: str, payload: Any) -> tuple[bool, list[str]]:
    try:
        validate(name, payload)
    except ValidationError as exc:
        return False, exc.errors
    return True, []
