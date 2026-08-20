"""L9 — persona generator."""

from __future__ import annotations

from pathlib import Path

from adlc.stages import enrich_personas as ep

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_personas_are_grounded_in_user_stories(spec_text: str) -> None:
    personas, unmapped = ep.extract_personas(spec_text)
    roles = [p["role"] for p in personas]
    assert roles == ["Reader", "Administrator"]

    reader = personas[0]
    assert reader["criteria"] == ["US1-AC1", "US1-AC2"]
    assert any("night" in goal for goal in reader["goals"])
    assert any("so that" in goal for goal in reader["goals"])
    assert reader["sources"], "a persona must cite the story it came from"

    admin = personas[1]
    assert admin["criteria"] == ["US2-AC1"]
    assert admin["proficiency"].startswith("High")

    assert unmapped == ["FR-001", "FR-002", "NFR-001"]


def test_persona_goals_do_not_repeat_criteria_ids(spec_text: str) -> None:
    personas, _ = ep.extract_personas(spec_text)
    admin = personas[1]
    assert admin["goals"] == [
        "set the workspace default theme — so that new readers inherit the house style"
    ]
    for persona in personas:
        for goal in persona["goals"]:
            assert not goal.endswith(")")


def test_persona_pain_points_come_from_the_spec(spec_text: str) -> None:
    personas, _ = ep.extract_personas(spec_text)
    reader_pains = " ".join(personas[0]["pains"]).lower()
    assert "no way to switch" in reader_pains or "cannot use the reader" in reader_pains
    admin_pains = " ".join(personas[1]["pains"]).lower()
    assert "slow" in admin_pains or "error-prone" in admin_pains
    for persona in personas:
        for pain in persona["pains"]:
            assert not pain.lower().startswith(("given ", "when ", "then "))


def test_persona_accessibility_always_has_a_baseline(spec_text: str) -> None:
    personas, _ = ep.extract_personas(spec_text)
    for persona in personas:
        assert persona["accessibility"], "every persona needs accessibility needs"
        assert persona["accessibility"][-1] == ep._A11Y_BASELINE
    reader_a11y = " ".join(personas[0]["accessibility"]).lower()
    assert "keyboard" in reader_a11y
    assert "contrast" in reader_a11y


def test_persona_names_are_deterministic_and_unique(spec_text: str) -> None:
    first = [p["name"] for p in ep.extract_personas(spec_text)[0]]
    second = [p["name"] for p in ep.extract_personas(spec_text)[0]]
    assert first == second
    assert len(set(first)) == len(first)


def test_no_actor_means_no_personas() -> None:
    personas, unmapped = ep.extract_personas("# A spec\n\nSomething happens. FR-001 applies.\n")
    assert personas == []
    assert unmapped == ["FR-001"]


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


def test_generate_writes_personas_md(run_dir: Path, spec_text: str, cfg) -> None:
    written = ep.generate(run_dir, spec_text, cfg)
    assert [p.name for p in written] == ["personas.md"]

    body = written[0].read_text(encoding="utf-8")
    assert body.startswith("# Personas — Dark Mode for the Reader")
    assert "## 1. " in body and "## 2. " in body
    assert "| Role | Reader |" in body
    assert "| Role | Administrator |" in body
    assert "### Goals" in body
    assert "### Pain points" in body
    assert "### Accessibility needs" in body
    assert "US1-AC1, US1-AC2" in body
    assert "FR-001, FR-002, NFR-001" in body  # unclaimed-criteria warning
    assert "{{" not in body and "{%" not in body, "template left unrendered"


def test_generate_is_deterministic(run_dir: Path, spec_text: str, cfg) -> None:
    first = ep.generate(run_dir, spec_text, cfg)[0].read_text(encoding="utf-8")
    second = ep.generate(run_dir, spec_text, cfg)[0].read_text(encoding="utf-8")
    assert first == second


def test_generate_falls_back_to_spec_on_disk(run_dir: Path, cfg) -> None:
    written = ep.generate(run_dir, "", cfg)
    assert [p.name for p in written] == ["personas.md"]
    assert "Reader" in written[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_generate_returns_empty_for_empty_spec(bare_run_dir: Path, cfg) -> None:
    assert ep.generate(bare_run_dir, "", cfg) == []
    assert not (bare_run_dir / "enrichment").exists()


def test_generate_returns_empty_when_no_actor_is_named(tmp_path: Path, cfg) -> None:
    run = tmp_path / "run"
    assert ep.generate(run, "# Spec\n\nThe system does a thing.\n", cfg) == []
    assert not (run / "enrichment").exists()


def test_generate_returns_empty_on_garbage_arguments(tmp_path: Path, cfg) -> None:
    assert ep.generate(None, None, cfg) == []  # type: ignore[arg-type]
    assert ep.generate(tmp_path / "x", 42, cfg) == []  # type: ignore[arg-type]
    assert ep.generate(tmp_path / "x", b"bytes", cfg) == []  # type: ignore[arg-type]


def test_generate_returns_empty_when_output_path_is_a_file(
    tmp_path: Path, spec_text: str, cfg
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "enrichment").write_text("not a directory", encoding="utf-8")
    assert ep.generate(run, spec_text, cfg) == []


def test_generate_returns_empty_when_the_template_is_missing(
    run_dir: Path, spec_text: str, cfg, monkeypatch
) -> None:
    monkeypatch.setattr(ep, "TEMPLATE_NAME", "does-not-exist.md.j2")
    assert ep.generate(run_dir, spec_text, cfg) == []
    assert not (run_dir / "enrichment" / "personas.md").exists()


def test_generate_honours_the_skip_flag(run_dir: Path, spec_text: str, skip_cfg) -> None:
    assert ep.generate(run_dir, spec_text, skip_cfg) == []
    assert not (run_dir / "enrichment").exists()
