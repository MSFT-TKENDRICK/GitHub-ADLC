"""JSON Schema validation for ADLC artifacts.

Schemas live in ``schemas/`` at the repo root and are the acceptance oracle for
every workstream.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_DIR_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "schemas",   # repo checkout
    Path(__file__).resolve().parent / "schemas",       # installed wheel
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
    raise FileNotFoundError("could not locate the ADLC schemas/ directory")


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
