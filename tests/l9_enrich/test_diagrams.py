"""L9 — Mermaid diagram generator and validator."""

from __future__ import annotations

from html import escape, unescape
from pathlib import Path

import pytest

from adlc.stages import enrich_diagrams as ed

# ---------------------------------------------------------------------------
# validate_mermaid
# ---------------------------------------------------------------------------

VALID = [
    pytest.param("flowchart TB\n    a1[\"Reader\"] --> b1[\"Library\"]\n", id="flowchart"),
    pytest.param("graph LR\n    a --> b\n", id="graph"),
    pytest.param(
        'flowchart TD\n    subgraph s1["Group"]\n        n1["One"]\n    end\n    n1 --> n1\n',
        id="subgraph",
    ),
    pytest.param('flowchart LR\n    a1(["Service"]) --> d1[("Store")]\n', id="nested-shapes"),
    pytest.param('flowchart LR\n    a -->|"yes"| b\n', id="edge-label"),
    # mermaid defaults the direction when it is omitted -- verified against the
    # mermaid 11 parser, so the validator must not reject these.
    pytest.param('flowchart\n    a["A"] --> b["B"]\n', id="bare-flowchart"),
    pytest.param('graph\n    a["A"] --> b["B"]\n', id="bare-graph"),
    pytest.param('flowchart TB;\n    a["A"] --> b["B"]\n', id="direction-semicolon"),
    pytest.param(
        "erDiagram\n    READER ||--o{ PREFERENCE : has_many\n"
        "    READER {\n        uuid id\n        string email\n    }\n",
        id="erdiagram",
    ),
    pytest.param("erDiagram\n    READER\n    PREFERENCE\n", id="erdiagram-no-rels"),
    pytest.param(
        'erDiagram\n    "Reader Two" ||--o{ PREF : has\n', id="er-quoted-entity"
    ),
    pytest.param(
        'erDiagram\n    R {\n        uuid id PK\n        string e "note"\n    }\n',
        id="er-attribute-keys",
    ),
    pytest.param("%% a comment\nflowchart TB\n    a --> b\n", id="leading-comment"),
]

INVALID = [
    pytest.param("", id="empty"),
    pytest.param("   \n\n", id="whitespace"),
    pytest.param("%% only a comment\n", id="comment-only"),
    pytest.param("flowchat TB\n    a --> b\n", id="typo-header"),
    pytest.param('flowchart XY\n    a["A"] --> b["B"]\n', id="bad-direction"),
    pytest.param('graph XY\n    a["A"] --> b["B"]\n', id="bad-direction-graph"),
    pytest.param('flowchart TB\n    a["unclosed\n', id="unterminated-quote"),
    pytest.param('flowchart TB\n    a["label"\n', id="unclosed-bracket"),
    pytest.param('flowchart TB\n    a["one"} --> b\n', id="mismatched-bracket"),
    pytest.param("flowchart TB\n    a[label with | pipe] --> b\n", id="pipe-in-label"),
    pytest.param('flowchart TB\n    a[he said "hi" here] --> b\n', id="quote-in-label"),
    pytest.param('flowchart TB\n    a -->|"unclosed --> b\n', id="unpaired-pipe"),
    pytest.param("flowchart TB\n    a --> b\n    b -->\n", id="dangling-edge"),
    pytest.param('flowchart TB\n    subgraph s1["Group"]\n        n1["One"]\n', id="open-subgraph"),
    pytest.param('flowchart TB\n    n1["One"]\n    end\n', id="orphan-end"),
    pytest.param('flowchart TB\n    end["Nope"] --> b\n', id="reserved-id"),
    pytest.param("erDiagram\n    READER ||-o{ PREFERENCE : has\n", id="bad-er-cardinality"),
    pytest.param("erDiagram\n    READER ||--o{ PREFERENCE\n", id="er-missing-label"),
    pytest.param("erDiagram\n    READER {\n        uuid id\n", id="er-open-block"),
    pytest.param("erDiagram\n    READER {\n        uuid\n    }\n", id="er-bad-attribute"),
]


@pytest.mark.parametrize("source", VALID)
def test_validate_mermaid_accepts_valid(source: str) -> None:
    ok, errors = ed.validate_mermaid(source)
    assert ok, errors


@pytest.mark.parametrize("source", INVALID)
def test_validate_mermaid_rejects_invalid(source: str) -> None:
    ok, errors = ed.validate_mermaid(source)
    assert not ok
    assert errors, "a rejection must explain itself"


def test_validate_mermaid_never_raises_on_junk() -> None:
    for junk in ("\x00\x01", "[[[[", '"""', "}{)(", "erDiagram\n}\n", "flowchart TB\n" * 500):
        ok, errors = ed.validate_mermaid(junk)
        assert isinstance(ok, bool)
        assert isinstance(errors, list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_sanitize_label_strips_breaking_characters() -> None:
    label = ed.sanitize_label('Reader | "dark" [mode] {v2} <b>')
    for bad in ("|", '"', "[", "]", "{", "}", "<", ">"):
        assert bad not in label
    assert "Reader" in label


def test_sanitize_label_never_empty() -> None:
    assert ed.sanitize_label("") == "unnamed"
    assert ed.sanitize_label("|||") == "unnamed"


def test_er_name_normalises() -> None:
    assert ed.er_name("Theme Preference") == "THEME_PREFERENCE"
    assert ed.er_name("2fa token").startswith("E_")
    assert ed.er_name("") == "ENTITY"


def test_mermaid_id_is_keyword_free() -> None:
    assert ed.mermaid_id("n", "end", 3) not in ed.RESERVED_IDS
    assert ed.mermaid_id("n", "", 1) == "n1"


# ---------------------------------------------------------------------------
# The spine's report.py rendering path
# ---------------------------------------------------------------------------

#: Characters that break a Mermaid node label, an HTML embed, or both.
BANNED_IN_LABELS = "<>#|\"[]{}()`;\\~"


def test_sanitize_label_strips_everything_that_breaks_mermaid_or_html() -> None:
    raw = (
        "Search & Filter 50% done? Yes! a+b c/d e,f g.h i:j it's -x- "
        "<script> #hash |pipe| \"q\" [b] {c} (d) `t` ; \\ ~"
    )
    label = ed.sanitize_label(raw, limit=200)
    for char in BANNED_IN_LABELS:
        assert char not in label, f"{char!r} survived sanitisation"
    # The safe punctuation is deliberately kept -- all of it is verified to
    # parse inside a quoted label by mermaid 11's own parser.
    for char in "&+%?!':/,.-":
        assert char in label, f"{char!r} was stripped but is safe"
    ok, errors = ed.validate_mermaid(f'flowchart TB\n    n1["{label}"] --> n2["b"]\n')
    assert ok, errors


def test_diagrams_survive_the_report_html_escape_roundtrip(
    run_dir: Path, spec_text: str, cfg
) -> None:
    """``report.py`` embeds Mermaid as ``escape(source)`` inside a div, which the
    report shows as literal source text for the reader to copy into a renderer.
    The browser unescapes it on display, so the round-trip must be lossless or
    what gets copied out no longer parses."""
    for path in ed.generate(run_dir, spec_text, cfg):
        source = path.read_text(encoding="utf-8")
        round_tripped = unescape(escape(source))
        assert round_tripped == source, f"{path.name} is altered by HTML escaping"
        ok, errors = ed.validate_mermaid(round_tripped)
        assert ok, f"{path.name} invalid after escape round-trip: {errors}"


def test_escape_roundtrip_survives_ampersands_and_quotes() -> None:
    label = ed.sanitize_label("Tom & Jerry 100% \"quoted\"")
    diagram = f'flowchart LR\n    n1["{label}"] --> n2["b"]\n'
    assert ed.validate_mermaid(diagram)[0]
    assert unescape(escape(diagram)) == diagram


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extracts_actors_routes_entities(run_dir: Path, spec_text: str) -> None:
    ctx = ed.read_spec_context(run_dir, spec_text)
    assert ed.feature_title(ctx["spec"]) == "Dark Mode for the Reader"
    assert [a.lower() for a in ed.extract_actors(ctx["spec"])] == ["reader", "administrator"]

    routes = ed.extract_routes(ctx)
    assert "/library" in routes
    assert "/settings/appearance" in routes
    assert "/api/v1/preferences" in routes

    entities = ed.extract_entities(ctx)
    assert set(entities) == {"READER", "THEME_PREFERENCE", "WORKSPACE"}
    assert ("uuid", "id") in entities["READER"]
    assert ("string", "email") in entities["READER"]

    rels = ed.extract_relationships(ctx, entities)
    assert ("READER", "||", "o{", "THEME_PREFERENCE", "has many") in rels
    assert ("THEME_PREFERENCE", "}o", "||", "WORKSPACE", "belongs to") in rels


def test_field_bullets_are_not_mistaken_for_entities(run_dir: Path, spec_text: str) -> None:
    ctx = ed.read_spec_context(run_dir, spec_text)
    entities = ed.extract_entities(ctx)
    for bogus in ("ID", "EMAIL", "THEME", "CREATED_AT", "DEFAULT_THEME"):
        assert bogus not in entities


def test_relationships_are_not_invented() -> None:
    ctx = {
        "spec": "# X\n\n## Key Entities\n\n- **Alpha**: one\n- **Beta**: two\n",
        "plan": "",
        "data_model": "",
        "contracts": "",
    }
    entities = ed.extract_entities(ctx)
    assert set(entities) == {"ALPHA", "BETA"}
    assert ed.extract_relationships(ctx, entities) == []


def test_extract_services_falls_back(run_dir: Path, spec_text: str) -> None:
    ctx = ed.read_spec_context(run_dir, spec_text)
    services = ed.extract_services(ctx, "fallback")
    assert "Theme Service" in services
    assert "Preferences API" in services
    assert ed.extract_services({"plan": "", "spec": ""}, "fallback") == ["fallback"]


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


def test_generate_writes_three_valid_diagrams(run_dir: Path, spec_text: str, cfg) -> None:
    written = ed.generate(run_dir, spec_text, cfg)
    names = {p.name for p in written}
    assert names == {"architecture.mmd", "sitemap.mmd", "data-model.mmd"}

    for path in written:
        assert path.parent == run_dir / "enrichment"
        assert path.is_file()
        ok, errors = ed.validate_mermaid(path.read_text(encoding="utf-8"))
        assert ok, f"{path.name} is not valid Mermaid: {errors}"


def test_generated_diagrams_are_grounded(run_dir: Path, spec_text: str, cfg) -> None:
    ed.generate(run_dir, spec_text, cfg)
    enrichment = run_dir / "enrichment"

    architecture = (enrichment / "architecture.mmd").read_text(encoding="utf-8")
    assert architecture.startswith("%%")
    assert "flowchart TB" in architecture
    assert "Reader" in architecture and "Administrator" in architecture
    assert "Theme Service" in architecture
    assert '[("Theme Preference")]' in architecture, "entity labels are humanised"
    # Layer-to-layer edges only -- node-level wiring is not in the spec.
    for edge in ("actors --> surfaces", "surfaces --> services", "services --> data"):
        assert edge in architecture

    sitemap = (enrichment / "sitemap.mmd").read_text(encoding="utf-8")
    assert "flowchart LR" in sitemap
    assert "/settings/appearance" in sitemap
    assert "/settings" in sitemap  # intermediate segment is materialised
    assert "/api/v1/preferences/:readerId" in sitemap, "{param} is rewritten to :param"
    assert "{" not in sitemap and "}" not in sitemap

    data_model = (enrichment / "data-model.mmd").read_text(encoding="utf-8")
    assert "erDiagram" in data_model
    assert "READER ||--o{ THEME_PREFERENCE : has_many" in data_model
    assert "THEME_PREFERENCE }o--|| WORKSPACE : belongs_to" in data_model
    assert "uuid id" in data_model


def test_generate_is_deterministic(run_dir: Path, spec_text: str, cfg) -> None:
    first = {p.name: p.read_text(encoding="utf-8") for p in ed.generate(run_dir, spec_text, cfg)}
    second = {p.name: p.read_text(encoding="utf-8") for p in ed.generate(run_dir, spec_text, cfg)}
    assert first == second


def test_generate_skips_facets_with_nothing_to_model(tmp_path: Path, cfg) -> None:
    run = tmp_path / "run"
    spec = "# Feature Specification: Rename a log field\n\nAs a operator, I want cleaner logs.\n"
    written = ed.generate(run, spec, cfg)
    names = {p.name for p in written}
    assert "architecture.mmd" in names
    assert "sitemap.mmd" not in names, "no routes in the spec -> no sitemap"
    assert "data-model.mmd" not in names, "no entities in the spec -> no ER diagram"
    ok, errors = ed.validate_mermaid((run / "enrichment" / "architecture.mmd").read_text("utf-8"))
    assert ok, errors


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_generate_returns_empty_for_empty_spec(bare_run_dir: Path, cfg) -> None:
    assert ed.generate(bare_run_dir, "", cfg) == []
    assert not (bare_run_dir / "enrichment").exists()


def test_generate_returns_empty_when_output_path_is_a_file(tmp_path: Path, cfg) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "enrichment").write_text("I am a file, not a directory", encoding="utf-8")
    assert ed.generate(run, "# Feature: X\n\nAs a user, I want a thing.\n", cfg) == []


def test_generate_returns_empty_on_garbage_arguments(tmp_path: Path, cfg) -> None:
    assert ed.generate(None, None, cfg) == []  # type: ignore[arg-type]
    assert ed.generate(tmp_path / "missing", 12345, cfg) == []  # type: ignore[arg-type]
    assert ed.generate(tmp_path / "missing", ["not", "text"], cfg) == []  # type: ignore[arg-type]
    assert ed.generate(tmp_path / "missing", "", object()) == []  # type: ignore[arg-type]


def test_generate_tolerates_a_missing_config(tmp_path: Path) -> None:
    """cfg is only consulted for the skip list; a None must not break anything."""
    written = ed.generate(tmp_path / "run", "# Feature: X\n\nAs a user, I want it.\n", None)
    assert isinstance(written, list)
    assert {p.name for p in written} == {"architecture.mmd"}


def test_generate_survives_a_broken_builder(run_dir, spec_text, cfg, monkeypatch) -> None:
    def boom(_ctx):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(ed, "build_architecture", boom)
    written = ed.generate(run_dir, spec_text, cfg)
    names = {p.name for p in written}
    assert "architecture.mmd" not in names
    assert {"sitemap.mmd", "data-model.mmd"} <= names


def test_generate_refuses_to_write_invalid_mermaid(run_dir, spec_text, cfg, monkeypatch) -> None:
    monkeypatch.setattr(ed, "build_architecture", lambda _ctx: "not a diagram at all\n")
    written = ed.generate(run_dir, spec_text, cfg)
    assert "architecture.mmd" not in {p.name for p in written}
    assert not (run_dir / "enrichment" / "architecture.mmd").exists()


def test_generate_honours_the_skip_flag(run_dir: Path, spec_text: str, skip_cfg) -> None:
    assert ed.generate(run_dir, spec_text, skip_cfg) == []
    assert not (run_dir / "enrichment").exists()
