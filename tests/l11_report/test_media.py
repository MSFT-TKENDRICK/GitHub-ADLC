"""L11 — the hero recording and the before/after slideshow.

The report is one file that opens from ``file://``, so every frame it shows
travels inside it as a ``data:`` URI. That makes embedding a budget problem, and
the way the budget is spent is a correctness question rather than a cosmetic one:

* **A dropped file is never silent.** Something too large to embed renders as a
  card carrying its path and size. Silence would be indistinguishable from "we
  never recorded that", which is the exact failure this framework exists to
  prevent.
* **The hero is paid first.** The end-to-end recording is the artifact the page
  is built around; sixty screenshots must not eat the budget before it is taken.
* **Pairing states its own confidence.** Before/after matching is inferred, so
  every slide records which rule produced it. A confident-looking lie about what
  is being compared is worse than an admitted guess.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.report.media import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, build_media
from adlc.runs import RunDir


def png(rgb: tuple[int, int, int] = (255, 255, 255), width: int = 4, height: int = 4) -> bytes:
    """A real, valid PNG -- the embedder reads bytes, so fixtures must be bytes."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


WEBM = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQRChYECGFOAZwEAAAAAAAAA"
)


@pytest.fixture
def rd(tmp_path: Path) -> RunDir:
    cfg = Config(root=tmp_path, profile="full")
    run = RunDir(cfg, "2026-08-20-c100")
    (run.evidence_dir / "candidate-a").mkdir(parents=True, exist_ok=True)
    return run


def shots_dir(rd: RunDir) -> Path:
    return rd.evidence_dir / "candidate-a"


def write(rd: RunDir, name: str, data: bytes) -> Path:
    path = shots_dir(rd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def labels(media: dict[str, Any]) -> list[str]:
    return [p["label"] for p in media["pairs"]]


class TestHeroRecording:
    def test_a_recording_becomes_the_hero_and_is_embedded_inline(self, rd: RunDir) -> None:
        write(rd, "walkthrough.webm", WEBM)
        hero = build_media(rd, [])["hero"]
        assert hero is not None
        assert hero["mime"] == "video/webm"
        assert hero["embedded"] is True
        assert hero["src"].startswith("data:video/webm;base64,")

    def test_the_longest_recording_wins_the_hero_slot(self, rd: RunDir) -> None:
        """An evidence run makes one end-to-end capture plus incidental clips."""
        write(rd, "unit-clip.webm", WEBM)
        write(rd, "full-run.webm", WEBM + b"\x00" * 4096)
        media = build_media(rd, [])
        assert media["hero"]["name"] == "full-run.webm"
        assert [v["name"] for v in media["videos"]] == ["unit-clip.webm"]

    def test_no_recording_is_reported_as_absence_not_as_an_error(self, rd: RunDir) -> None:
        assert build_media(rd, [])["hero"] is None

    def test_an_oversized_recording_is_linked_with_a_stated_reason(self, rd: RunDir) -> None:
        write(rd, "huge.webm", b"\x00" * (MAX_VIDEO_BYTES + 1))
        hero = build_media(rd, [])["hero"]
        assert hero["embedded"] is False
        assert hero["src"] == ""
        assert "exceeds" in hero["reason"]
        assert hero["path"], "the reader must still be able to go and find it"

    def test_the_artifact_digest_is_carried_onto_the_media_item(self, rd: RunDir) -> None:
        path = write(rd, "walkthrough.webm", WEBM)
        artifacts = [{"path": rd.rel(path), "sha256": "d" * 64}]
        assert build_media(rd, artifacts)["hero"]["sha256"] == "d" * 64


class TestPairingRules:
    def test_an_explicit_before_after_filename_pairs_with_high_confidence(
        self, rd: RunDir
    ) -> None:
        write(rd, "settings-before.png", png((240, 240, 245)))
        write(rd, "settings-after.png", png((24, 26, 32)))
        pair = build_media(rd, [])["pairs"][0]
        assert pair["rule"] == "filename declares before/after"
        assert pair["confidence"] == "high"
        assert pair["before"]["name"] == "settings-before.png"
        assert pair["after"]["name"] == "settings-after.png"

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("x-baseline.png", "x-candidate.png"),
            ("x.prev.png", "x.current.png"),
            ("x_old.png", "x_new.png"),
            ("x-control.png", "x-treatment.png"),
        ],
    )
    def test_the_synonyms_teams_actually_use_are_recognised(
        self, rd: RunDir, before: str, after: str
    ) -> None:
        write(rd, before, png())
        write(rd, after, png((0, 0, 0)))
        assert build_media(rd, [])["pairs"][0]["confidence"] == "high"

    def test_the_label_names_the_subject_not_the_word_before(self, rd: RunDir) -> None:
        write(rd, "settings-before.png", png())
        write(rd, "settings-after.png", png((0, 0, 0)))
        assert labels(build_media(rd, [])) == ["Settings"]

    def test_the_same_capture_under_two_variants_pairs(self, rd: RunDir) -> None:
        for variant, colour in (("candidate-a", (255, 255, 255)), ("candidate-b", (0, 0, 0))):
            path = rd.evidence_dir / variant / "home.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png(colour))
        pair = build_media(rd, [])["pairs"][0]
        assert pair["rule"] == "same capture under two variants"
        assert pair["confidence"] == "high"

    def test_consecutive_captures_are_paired_but_labelled_low_confidence(
        self, rd: RunDir
    ) -> None:
        """A timeline is not a controlled comparison and must not claim to be."""
        write(rd, "menu-01.png", png())
        write(rd, "menu-02.png", png((0, 0, 0)))
        pair = build_media(rd, [])["pairs"][0]
        assert pair["rule"] == "consecutive captures in the same run"
        assert pair["confidence"] == "low"

    def test_an_unpaired_capture_is_shown_alone_and_says_so(self, rd: RunDir) -> None:
        write(rd, "solo.png", png())
        pair = build_media(rd, [])["pairs"][0]
        assert pair["before"] is None
        assert pair["after"]["name"] == "solo.png"
        assert pair["confidence"] == "none"

    def test_a_capture_is_never_used_in_two_pairs(self, rd: RunDir) -> None:
        write(rd, "a-before.png", png())
        write(rd, "a-after.png", png((0, 0, 0)))
        write(rd, "b-01.png", png())
        write(rd, "b-02.png", png((1, 1, 1)))
        media = build_media(rd, [])
        used = [
            shot["path"]
            for pair in media["pairs"]
            for shot in (pair["before"], pair["after"])
            if shot
        ]
        assert len(used) == len(set(used))

    def test_every_capture_appears_somewhere(self, rd: RunDir) -> None:
        for name in ("a-before.png", "a-after.png", "orphan.png"):
            write(rd, name, png())
        media = build_media(rd, [])
        shown = {
            shot["name"]
            for pair in media["pairs"]
            for shot in (pair["before"], pair["after"])
            if shot
        }
        assert shown == {"a-before.png", "a-after.png", "orphan.png"}


class TestBudget:
    def test_an_oversized_screenshot_is_linked_rather_than_dropped(self, rd: RunDir) -> None:
        write(rd, "huge.png", b"\x00" * (MAX_IMAGE_BYTES + 1))
        media = build_media(rd, [])
        assert media["screenshots"][0]["embedded"] is False
        assert media["budget"]["linked"] == 1

    def test_the_budget_reports_what_it_spent(self, rd: RunDir) -> None:
        write(rd, "a.png", png())
        write(rd, "b.png", png((0, 0, 0)))
        budget = build_media(rd, [])["budget"]
        assert budget["embedded"] == 2
        assert budget["linked"] == 0
        assert budget["remainingBytes"] < budget["totalBytes"]

    def test_a_run_with_no_evidence_directory_is_handled(self, tmp_path: Path) -> None:
        run = RunDir(Config(root=tmp_path, profile="full"), "2026-08-20-none")
        media = build_media(run, [])
        assert media["hero"] is None
        assert media["pairs"] == []

    def test_a_non_media_file_is_ignored(self, rd: RunDir) -> None:
        write(rd, "trace.zip", b"PK\x03\x04")
        assert build_media(rd, [])["screenshots"] == []
