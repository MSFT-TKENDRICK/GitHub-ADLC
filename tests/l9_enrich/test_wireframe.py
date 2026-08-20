"""L9 — Excalidraw wireframe generator.

The point of these tests is blunt: a ``.excalidraw`` file that Excalidraw cannot
open is worse than no file at all, so everything asserts against the real
element schema (``excalidraw/packages/element/src/types.ts``).
"""

from __future__ import annotations

import json
from pathlib import Path

from adlc.stages import enrich_wireframe as ew

# ---------------------------------------------------------------------------
# Document shape
# ---------------------------------------------------------------------------


def test_document_has_the_required_envelope(spec_text: str) -> None:
    document = ew.build_document(spec_text)
    assert set(document) >= {"type", "version", "source", "elements", "appState", "files"}
    assert document["type"] == "excalidraw"
    assert document["version"] == 2
    assert isinstance(document["source"], str) and document["source"]
    assert isinstance(document["files"], dict)
    assert document["appState"]["viewBackgroundColor"] == "#ffffff"
    assert document["appState"]["gridSize"] == 20


def test_every_element_carries_the_base_schema(spec_text: str) -> None:
    elements = ew.build_document(spec_text)["elements"]
    assert len(elements) > 15
    for element in elements:
        missing = [key for key in ew._BASE_KEYS if key not in element]
        assert not missing, f"{element.get('type')} missing {missing}"
        assert element["type"] in ew._VALID_TYPES
        assert element["isDeleted"] is False
        assert element["groupIds"] == []
        assert element["angle"] == 0
        assert 0 <= element["opacity"] <= 100
        assert element["updated"] == ew.FIXED_UPDATED


def test_text_and_arrow_elements_carry_their_extra_fields(spec_text: str) -> None:
    elements = ew.build_document(spec_text)["elements"]
    texts = [e for e in elements if e["type"] == "text"]
    arrows = [e for e in elements if e["type"] == "arrow"]
    assert texts and arrows

    for element in texts:
        for key in ew._TEXT_KEYS:
            assert key in element
        assert element["text"] == element["originalText"]
        assert element["fontFamily"] in (ew.FONT_EXCALIFONT, ew.FONT_NUNITO, ew.FONT_CASCADIA)
        assert element["textAlign"] in {"left", "center", "right"}
        assert element["verticalAlign"] in {"top", "middle", "bottom"}
        assert element["containerId"] is None
        assert element["width"] > 0 and element["height"] > 0

    for element in arrows:
        for key in ew._LINEAR_KEYS:
            assert key in element
        assert len(element["points"]) >= 2
        assert element["endArrowhead"] == "arrow"
        assert element["elbowed"] is False


def test_element_ids_are_unique_and_stable(spec_text: str) -> None:
    first = [e["id"] for e in ew.build_document(spec_text)["elements"]]
    second = [e["id"] for e in ew.build_document(spec_text)["elements"]]
    assert first == second, "ids must be derived, not random"
    assert len(set(first)) == len(first)


def test_wireframe_is_grounded_in_the_spec(spec_text: str) -> None:
    elements = ew.build_document(spec_text)["elements"]
    blob = "\n".join(e.get("text", "") for e in elements if e["type"] == "text")
    assert "Dark Mode for the Reader" in blob
    assert "/library" in blob
    assert "(primary)" in blob, "a primary CTA must be labelled"
    assert "Toggle" in blob, "the CTA verb comes from the story"
    assert "US1-AC1" in blob, "the annotation cites acceptance criteria"
    assert "Reader" in blob


def test_outline_falls_back_without_routes_or_stories() -> None:
    facts = ew.outline("# Feature Specification: Tiny change\n\nNothing else.\n")
    assert facts["title"] == "Tiny change"
    assert facts["nav"], "nav always has something to draw"
    assert facts["blocks"], "content always has something to draw"
    assert facts["cta"] == "Continue"


# ---------------------------------------------------------------------------
# validate_excalidraw
# ---------------------------------------------------------------------------


def test_validator_accepts_generated_output(spec_text: str) -> None:
    ok, errors = ew.validate_excalidraw(ew.build_document(spec_text))
    assert ok, errors


def test_validator_rejects_broken_documents(spec_text: str) -> None:
    good = ew.build_document(spec_text)

    assert ew.validate_excalidraw("not a dict")[0] is False
    assert ew.validate_excalidraw({})[0] is False
    assert ew.validate_excalidraw({**good, "type": "drawing"})[0] is False
    assert ew.validate_excalidraw({**good, "version": 1})[0] is False
    assert ew.validate_excalidraw({**good, "elements": []})[0] is False
    assert ew.validate_excalidraw({**good, "elements": "nope"})[0] is False
    assert ew.validate_excalidraw({**good, "files": None})[0] is False

    stripped = dict(good["elements"][0])
    stripped.pop("seed")
    assert ew.validate_excalidraw({**good, "elements": [stripped]})[0] is False

    duplicated = [good["elements"][0], good["elements"][0]]
    ok, errors = ew.validate_excalidraw({**good, "elements": duplicated})
    assert not ok
    assert any("duplicates id" in e for e in errors)

    bad_text = dict(next(e for e in good["elements"] if e["type"] == "text"))
    bad_text["textAlign"] = "justify"
    assert ew.validate_excalidraw({**good, "elements": [bad_text]})[0] is False

    bad_arrow = dict(next(e for e in good["elements"] if e["type"] == "arrow"))
    bad_arrow["points"] = [[0, 0]]
    assert ew.validate_excalidraw({**good, "elements": [bad_arrow]})[0] is False

    bad_number = dict(good["elements"][0])
    bad_number["x"] = "12"
    assert ew.validate_excalidraw({**good, "elements": [bad_number]})[0] is False


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


def test_generate_writes_parseable_excalidraw(run_dir: Path, spec_text: str, cfg) -> None:
    written = ew.generate(run_dir, spec_text, cfg)
    assert [p.name for p in written] == ["wireframe.excalidraw"]

    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert set(document) >= {"type", "version", "source", "elements", "appState", "files"}
    ok, errors = ew.validate_excalidraw(document)
    assert ok, errors


def test_generate_is_deterministic(run_dir: Path, spec_text: str, cfg) -> None:
    first = ew.generate(run_dir, spec_text, cfg)[0].read_text(encoding="utf-8")
    second = ew.generate(run_dir, spec_text, cfg)[0].read_text(encoding="utf-8")
    assert first == second


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_generate_returns_empty_for_empty_spec(bare_run_dir: Path, cfg) -> None:
    assert ew.generate(bare_run_dir, "", cfg) == []
    assert ew.generate(bare_run_dir, "   \n\n", cfg) == []
    assert not (bare_run_dir / "enrichment").exists()


def test_generate_returns_empty_on_garbage_arguments(tmp_path: Path, cfg) -> None:
    assert ew.generate(None, None, cfg) == []  # type: ignore[arg-type]
    assert ew.generate(tmp_path / "x", 42, cfg) == []  # type: ignore[arg-type]
    assert ew.generate(tmp_path / "x", b"bytes", cfg) == []  # type: ignore[arg-type]


def test_generate_returns_empty_when_output_path_is_a_file(
    tmp_path: Path, spec_text: str, cfg
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "enrichment").write_text("not a directory", encoding="utf-8")
    assert ew.generate(run, spec_text, cfg) == []


def test_generate_returns_empty_when_the_template_is_missing(
    run_dir: Path, spec_text: str, cfg, monkeypatch
) -> None:
    monkeypatch.setattr(ew, "TEMPLATE_NAME", "nope.j2")
    assert ew.generate(run_dir, spec_text, cfg) == []
    assert not (run_dir / "enrichment" / "wireframe.excalidraw").exists()


def test_generate_refuses_to_write_an_invalid_document(
    run_dir: Path, spec_text: str, cfg, monkeypatch
) -> None:
    monkeypatch.setattr(ew, "build_elements", lambda _spec: [{"id": "x", "type": "blob"}])
    assert ew.generate(run_dir, spec_text, cfg) == []
    assert not (run_dir / "enrichment" / "wireframe.excalidraw").exists()


def test_generate_survives_a_template_that_emits_bad_json(
    tmp_path: Path, run_dir: Path, spec_text: str, cfg, monkeypatch
) -> None:
    broken = tmp_path / "broken-templates"
    broken.mkdir()
    (broken / ew.TEMPLATE_NAME).write_text('{"type": "excalidraw", ', encoding="utf-8")
    monkeypatch.setattr(ew, "TEMPLATE_DIR", broken)
    assert ew.generate(run_dir, spec_text, cfg) == []
    assert not (run_dir / "enrichment" / "wireframe.excalidraw").exists()


def test_validator_rejects_non_finite_numbers(spec_text: str) -> None:
    """json.dumps writes NaN/Infinity; JSON.parse throws on both."""
    good = ew.build_document(spec_text)
    for value in (float("nan"), float("inf"), float("-inf")):
        broken = dict(good["elements"][0])
        broken["x"] = value
        ok, errors = ew.validate_excalidraw({**good, "elements": [broken]})
        assert not ok
        assert any("not valid JSON" in e for e in errors)


def test_generate_honours_the_skip_flag(run_dir: Path, spec_text: str, skip_cfg) -> None:
    assert ew.generate(run_dir, spec_text, skip_cfg) == []
    assert not (run_dir / "enrichment").exists()
