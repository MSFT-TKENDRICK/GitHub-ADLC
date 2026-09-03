"""``adlc.stages.adr`` -- MADR v4 architecture decision records.

Previously exercised only incidentally through CLI/pipeline tests (68%
coverage). Adds direct unit coverage of `_next_number`, `_slugify`,
`create_adr` (default and fully-populated fields, sequential numbering),
`list_adrs` (empty dir, missing dir, malformed entries falling back to
'unknown'/stem), and `set_status` (valid transition, invalid status,
missing ADR, and both branches of the `adlc-review-sha` rewrite -- the
key already present vs. absent from the front matter).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adlc.config import Config
from adlc.stages.adr import STATUSES, _next_number, _slugify, create_adr, list_adrs, set_status


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path)


class TestSlugify:
    def test_lowercases_and_hyphenates(self) -> None:
        assert _slugify("Use LaunchDarkly for Flags") == "use-launchdarkly-for-flags"

    def test_collapses_repeated_separators(self) -> None:
        assert _slugify("A --- B___C") == "a-b-c"

    def test_truncates_to_60_chars(self) -> None:
        long_title = "x" * 100
        assert len(_slugify(long_title)) <= 60

    def test_empty_title_falls_back_to_decision(self) -> None:
        assert _slugify("") == "decision"
        assert _slugify("!!!") == "decision"


class TestNextNumber:
    def test_returns_0001_for_empty_directory(self, tmp_path: Path) -> None:
        assert _next_number(tmp_path) == "0001"

    def test_increments_past_the_highest_existing_number(self, tmp_path: Path) -> None:
        (tmp_path / "0001-first.md").write_text("", encoding="utf-8")
        (tmp_path / "0003-third.md").write_text("", encoding="utf-8")
        assert _next_number(tmp_path) == "0004"

    def test_ignores_files_not_matching_the_nnnn_dash_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "0001-first.md").write_text("", encoding="utf-8")
        (tmp_path / "README.md").write_text("", encoding="utf-8")
        assert _next_number(tmp_path) == "0002"


class TestCreateAdr:
    def test_slug_property_returns_the_file_stem(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Use flagd as the default flag provider")
        assert adr.slug == adr.path.stem
        assert adr.slug.startswith("0001-use-flagd")

    def test_creates_first_adr_with_default_fields(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Use flagd as the default flag provider")
        assert adr.number == "0001"
        assert adr.status == "proposed"
        assert adr.path.is_file()
        text = adr.path.read_text(encoding="utf-8")
        assert "status: proposed" in text
        assert "# Use flagd as the default flag provider" in text
        assert "_To be completed._" in text

    def test_second_adr_increments_the_number(self, cfg: Config) -> None:
        create_adr(cfg, "First decision")
        second = create_adr(cfg, "Second decision")
        assert second.number == "0002"

    def test_populates_all_optional_fields(self, cfg: Config) -> None:
        adr = create_adr(
            cfg,
            "Adopt OpenFeature",
            context="We need vendor-neutral flags.",
            drivers=["Avoid lock-in", "OSS standard"],
            options=["OpenFeature", "Bespoke SDK"],
            chosen="OpenFeature",
            justification="it is the CNCF standard",
            consequences=["New dependency", "Simpler migration later"],
            confirmation="Verified via the flagd conformance suite.",
            status="accepted",
            run_id="2026-08-19-a1b2",
            review_sha="deadbeef",
            decision_makers="platform team",
        )
        text = adr.path.read_text(encoding="utf-8")
        assert "status: accepted" in text
        assert "adlc-run: 2026-08-19-a1b2" in text
        assert "adlc-review-sha: deadbeef" in text
        assert "decision-makers: platform team" in text
        assert "* Avoid lock-in" in text
        assert 'Chosen option: "OpenFeature", because it is the CNCF standard' in text

    def test_run_id_absent_uses_na_placeholders(self, cfg: Config) -> None:
        adr = create_adr(cfg, "No run yet")
        text = adr.path.read_text(encoding="utf-8")
        assert "adlc-run: n/a" in text
        assert "_None._" in text


class TestListAdrs:
    def test_returns_empty_list_when_directory_missing(self, cfg: Config) -> None:
        assert list_adrs(cfg) == []

    def test_returns_created_adrs_sorted_by_number(self, cfg: Config) -> None:
        create_adr(cfg, "First")
        create_adr(cfg, "Second")
        adrs = list_adrs(cfg)
        assert [a.number for a in adrs] == ["0001", "0002"]
        assert [a.title for a in adrs] == ["First", "Second"]

    def test_malformed_adr_falls_back_to_unknown_status_and_stem_title(self, cfg: Config) -> None:
        cfg.decisions_dir.mkdir(parents=True, exist_ok=True)
        (cfg.decisions_dir / "0001-broken.md").write_text("no front matter here\n", encoding="utf-8")
        adrs = list_adrs(cfg)
        assert adrs[0].status == "unknown"
        assert adrs[0].title == "0001-broken"


class TestSetStatus:
    def test_transitions_status_and_persists_it(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Adopt flagd")
        updated = set_status(cfg, adr.number, "accepted")
        assert updated.status == "accepted"
        assert "status: accepted" in adr.path.read_text(encoding="utf-8")

    def test_rejects_an_unrecognised_status(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Adopt flagd")
        with pytest.raises(ValueError, match="status must be one of"):
            set_status(cfg, adr.number, "bogus-status")

    def test_raises_when_no_adr_matches_the_number(self, cfg: Config) -> None:
        cfg.decisions_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            set_status(cfg, "0099", "accepted")

    def test_accepts_all_documented_statuses(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Adopt flagd")
        for status in STATUSES:
            set_status(cfg, adr.number, status)

    def test_review_sha_replaces_existing_placeholder(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Adopt flagd", run_id="run-1", review_sha="oldsha")
        set_status(cfg, adr.number, "accepted", review_sha="newsha")
        text = adr.path.read_text(encoding="utf-8")
        assert "adlc-review-sha: newsha" in text
        assert "oldsha" not in text

    def test_review_sha_inserted_when_front_matter_key_absent(self, cfg: Config) -> None:
        cfg.decisions_dir.mkdir(parents=True, exist_ok=True)
        # A hand-authored ADR with no adlc-review-sha key at all.
        path = cfg.decisions_dir / "0001-manual.md"
        path.write_text("---\nstatus: proposed\n---\n\n# Manual decision\n", encoding="utf-8")
        set_status(cfg, "0001", "accepted", review_sha="freshsha")
        text = path.read_text(encoding="utf-8")
        assert "adlc-review-sha: freshsha" in text

    def test_number_is_zero_padded_when_looked_up(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Adopt flagd")
        updated = set_status(cfg, "1", "accepted")
        assert updated.number == adr.number
