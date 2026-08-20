"""Policy and meta-schema checks for the JSON Schemas in ``schemas/``.

Two distinct things are checked, and it is worth being precise about which is
which because only one of them is a correctness guarantee:

* **Meta-schema validation** -- the document is a structurally valid JSON Schema
  under Draft 2020-12. This catches real breakage: a misplaced keyword compiles
  into a schema that silently constrains less than its author intended.
* **Project policy** -- every schema declares the dialect this project
  standardises on, carries ``$id`` and ``title``, and does not declare an object
  type with no properties. These are conventions, not correctness.

What this does **not** do: it does not resolve remote ``$ref`` targets, and it
does not prove a schema accepts or rejects any particular document. Those are the
job of the tests that validate real artifacts against these schemas.

Exit codes: 0 clean, 1 findings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"

#: The dialect this project standardises on. ``adlc.schemas`` compiles with
#: ``Draft202012Validator`` explicitly, so a mismatched ``$schema`` here does not
#: change how validation runs today -- it means the file disagrees with the
#: validator that will be used on it, which is a latent trap rather than an
#: immediate fault.
EXPECTED_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def check(path: Path) -> list[str]:
    """Return every problem found in one schema file."""
    name = path.relative_to(SCHEMA_DIR).as_posix()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{name}: not valid JSON - {exc}"]

    if not isinstance(document, dict):
        return [f"{name}: top level must be an object"]

    problems: list[str] = []

    dialect = document.get("$schema")
    if not dialect:
        problems.append(f"{name}: missing '$schema' declaration")
    elif dialect != EXPECTED_DIALECT:
        problems.append(
            f"{name}: declares dialect {dialect!r} but this project validates with "
            f"{EXPECTED_DIALECT!r}; keyword semantics would differ from what the "
            f"author wrote"
        )

    if not document.get("$id"):
        problems.append(f"{name}: missing '$id'")
    if not document.get("title"):
        problems.append(f"{name}: missing 'title'")

    # An object schema with no properties accepts every object.
    if document.get("type") == "object" and not (
        document.get("properties") or document.get("patternProperties")
    ):
        problems.append(f"{name}: object schema declares no properties, so it constrains nothing")

    try:
        import jsonschema
    except ImportError:
        # Reporting "fine" because the validator is missing would be a false
        # green, so this is a finding rather than a silent skip.
        return [*problems, f"{name}: jsonschema not installed, meta-schema check did not run"]

    try:
        jsonschema.Draft202012Validator.check_schema(document)
    except jsonschema.SchemaError as exc:
        problems.append(f"{name}: not a valid Draft 2020-12 schema - {exc.message}")

    return problems


def main() -> int:
    if not SCHEMA_DIR.is_dir():
        print(f"no schemas directory at {SCHEMA_DIR}", file=sys.stderr)
        return 1

    schemas = sorted(SCHEMA_DIR.rglob("*.json"))
    if not schemas:
        # The hook runs unconditionally, so an empty set means the schemas were
        # deleted rather than that there was nothing to do. Reporting success
        # here would be the vacuous green this repository rejects.
        print(f"no schemas found under {SCHEMA_DIR} - nothing was checked", file=sys.stderr)
        return 1

    findings = [problem for path in schemas for problem in check(path)]
    for problem in findings:
        print(f"schema: {problem}", file=sys.stderr)

    if findings:
        print(f"\n{len(findings)} problem(s) across {len(schemas)} schema(s)", file=sys.stderr)
        return 1

    print(f"{len(schemas)} schema(s) pass meta-schema and policy checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
