"""L8 — the completeness pack: what the code-blind reviewer is allowed to see.

The feature-completeness reviewer's independence rests entirely on the claim that
it never saw the implementation. If that claim is false the verdict is worthless,
and worse, it is *invisibly* worthless -- the review still runs, still returns an
answer, and nothing indicates the answer was contaminated.

So the boundary is defended twice, and this module tests both defences:

* :func:`~adlc.stages.complete.build_pack` builds from an **allowlist**. Nothing
  is copied wholesale, so a field added upstream cannot ride along.
* :func:`~adlc.stages.complete.assert_sanitised` independently scans the
  serialised pack for the fingerprints of excluded content and refuses to write
  on a hit.

The tests that matter most are the ones that put code, diffs, transcripts and
credentials *on disk in the run directory* and then assert none of it reaches the
pack. A test that only checks the happy path would pass just as well against a
pack builder that copied the whole run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages.complete import (
    EXCLUSIONS,
    LEAK_MARKERS,
    MAX_BRIEF_CHARS,
    SanitisationError,
    assert_sanitised,
    build_pack,
    run_complete,
)

BRIEF = "Add dark mode to the settings page so it can be read at night.\n"

SPEC = """# Spec

## User story 1

- **US1-AC1**: the settings page offers a dark theme toggle
- **US1-AC2**: the chosen theme survives a reload
"""

#: A patch, an agent transcript and a replay script -- the three things the
#: reviewer must never see -- written into the run directory as they really are.
PATCH = """diff --git a/src/theme.py b/src/theme.py
--- a/src/theme.py
+++ b/src/theme.py
@@ -1,3 +1,4 @@
+SECRET_TOKEN = "ghp_notarealtokenbutshapedlikeone"
 def theme():
     return "light"
"""

SESSION = json.dumps({
    "role": "assistant",
    "thinking": "I could not get the toggle working so I will claim it works.",
    "content": "Implemented the toggle.",
})

REPLAY = """#!/usr/bin/env node
await page.goto('https://internal.example.com/settings');
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A bare repo root. This deliberately shadows the package-level `cfg`
    fixture, which carries a squads config this module has no use for."""
    return Config(root=tmp_path, profile="full")


@pytest.fixture
def rd(cfg: Config) -> RunDir:
    """A run directory carrying evidence *and* the content that must not leak."""
    run = RunDir(cfg, "2026-08-20-c100")
    for directory in (run.spec_dir, run.patches_dir, run.evidence_dir / "candidate-a"):
        directory.mkdir(parents=True, exist_ok=True)

    run.brief.parent.mkdir(parents=True, exist_ok=True)
    run.brief.write_text(BRIEF, encoding="utf-8")
    (run.spec_dir / "spec.md").write_text(SPEC, encoding="utf-8")
    (run.patches_dir / "T001.patch").write_text(PATCH, encoding="utf-8")
    (run.evidence_dir / "candidate-a" / "session.json").write_text(SESSION, encoding="utf-8")
    (run.evidence_dir / "candidate-a" / "replay.mjs").write_text(REPLAY, encoding="utf-8")

    run.run_json.write_text(json.dumps({
        "schemaVersion": "adlc-run/v1",
        "runId": "2026-08-20-c100",
        "profile": "full",
        "headSha": "cafebabe",
        "status": "gated",
        "artifacts": [
            {
                "path": "evidence/candidate-a/walkthrough.webm",
                "kind": "video", "sha256": "a" * 64, "bytes": 2048,
            },
            {
                "path": "evidence/candidate-a/replay.mjs",
                "kind": "replay_script", "sha256": "b" * 64, "bytes": 120,
            },
        ],
        "gates": [
            {"id": "tests", "status": "pass", "required": True, "message": "ok"},
            {"id": "feature_completeness", "status": "not_run", "required": True, "message": ""},
        ],
    }), encoding="utf-8")

    run.review_pack.write_text(json.dumps({
        "runId": "2026-08-20-c100",
        "coverage": [
            {
                "requirementId": "US1-AC1", "present": True,
                "evidenceKinds": ["video"], "artifactSha256": ["a" * 64],
            },
            {"requirementId": "US1-AC2", "present": False,
             "evidenceKinds": [], "artifactSha256": []},
        ],
    }), encoding="utf-8")
    return run


class TestSanitiser:
    @pytest.mark.parametrize(("marker", "description"), LEAK_MARKERS)
    def test_every_declared_marker_is_actually_caught(
        self, marker: str, description: str
    ) -> None:
        """The marker list is only a guarantee if each entry is enforced."""
        with pytest.raises(SanitisationError) as raised:
            assert_sanitised({"note": f"leading text {marker} trailing text"})
        assert description in str(raised.value)

    def test_a_leak_is_caught_however_deeply_it_is_nested(self) -> None:
        pack = {"requirements": [{"evidence": [{"caption": "diff --git a/x b/x"}]}]}
        with pytest.raises(SanitisationError):
            assert_sanitised(pack)

    def test_detection_is_case_insensitive(self) -> None:
        with pytest.raises(SanitisationError):
            assert_sanitised({"note": "<HTML><body>"})

    def test_every_hit_is_reported_not_just_the_first(self) -> None:
        with pytest.raises(SanitisationError) as raised:
            assert_sanitised({"a": "diff --git", "b": "Authorization: Bearer x"})
        message = str(raised.value)
        assert "unified diff header" in message
        assert "credentials" in message

    def test_a_clean_pack_passes(self) -> None:
        assert_sanitised({"brief": {"text": BRIEF}, "requirements": [{"id": "US1-AC1"}]})


class TestPackExcludesTheImplementation:
    def test_no_diff_or_source_reaches_the_pack(self, cfg: Config, rd: RunDir) -> None:
        blob = json.dumps(build_pack(cfg, rd))
        assert "diff --git" not in blob
        assert "SECRET_TOKEN" not in blob
        assert "ghp_notarealtokenbutshapedlikeone" not in blob

    def test_no_agent_reasoning_reaches_the_pack(self, cfg: Config, rd: RunDir) -> None:
        blob = json.dumps(build_pack(cfg, rd))
        assert "I could not get the toggle working" not in blob
        assert "thinking" not in blob

    def test_no_replay_source_reaches_the_pack(self, cfg: Config, rd: RunDir) -> None:
        blob = json.dumps(build_pack(cfg, rd))
        assert "await page." not in blob
        assert "internal.example.com" not in blob

    def test_the_pack_it_builds_is_sanitised(self, cfg: Config, rd: RunDir) -> None:
        assert_sanitised(build_pack(cfg, rd))

    def test_an_excluded_artifact_is_summarised_not_quoted(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """A replay script still counts as evidence -- by digest, never by content."""
        entry = next(
            e for e in build_pack(cfg, rd)["evidence"] if e["kind"] == "replay_script"
        )

        assert entry["artifactSha256"] == "b" * 64
        assert entry["redacted"] is True
        assert "path" not in entry, "a path is a route back to the content"

    def test_gate_internals_are_reduced_to_a_verdict(self, cfg: Config, rd: RunDir) -> None:
        gate = build_pack(cfg, rd)["gates"][0]
        assert set(gate) == {"id", "status", "required", "tldr"}
        assert "observed" not in gate

    def test_this_gate_s_own_verdict_is_withheld(self, cfg: Config, rd: RunDir) -> None:
        """Showing the reviewer last run's verdict invites it to ratify itself."""
        assert [g["id"] for g in build_pack(cfg, rd)["gates"]] == ["tests"]


class TestPackContent:
    def test_the_brief_is_carried_verbatim_and_hashed(self, cfg: Config, rd: RunDir) -> None:
        brief = build_pack(cfg, rd)["brief"]
        assert brief["text"] == BRIEF
        assert len(brief["sha256"]) == 64
        assert brief["truncated"] is False

    def test_an_oversized_brief_is_truncated_and_says_so(self, cfg: Config, rd: RunDir) -> None:
        rd.brief.write_text("x" * (MAX_BRIEF_CHARS + 500), encoding="utf-8")
        brief = build_pack(cfg, rd)["brief"]
        assert len(brief["text"]) == MAX_BRIEF_CHARS
        assert brief["truncated"] is True

    def test_requirements_carry_their_coverage_verdict(self, cfg: Config, rd: RunDir) -> None:
        by_id = {r["id"]: r for r in build_pack(cfg, rd)["requirements"]}
        assert by_id["US1-AC1"]["covered"] is True
        assert by_id["US1-AC1"]["artifactSha256"] == ["a" * 64]
        assert by_id["US1-AC2"]["covered"] is False

    def test_uncovered_requirements_are_listed_explicitly(self, cfg: Config, rd: RunDir) -> None:
        assert build_pack(cfg, rd)["uncovered"] == ["US1-AC2"]

    def test_counts_agree_with_the_lists_they_summarise(self, cfg: Config, rd: RunDir) -> None:
        pack = build_pack(cfg, rd)
        counts = pack["counts"]
        assert counts["requirements"] == len(pack["requirements"]) == 2
        assert counts["covered"] == 1
        assert counts["uncovered"] == len(pack["uncovered"]) == 1
        assert counts["artifacts"] == len(pack["evidence"]) == 2

    def test_an_artifact_without_a_real_digest_is_not_offered_as_evidence(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """A citable digest is the whole mechanism; a blank one cannot be checked."""
        run = json.loads(rd.run_json.read_text(encoding="utf-8"))
        run["artifacts"].append({"path": "evidence/x.png", "kind": "screenshot", "sha256": ""})
        rd.run_json.write_text(json.dumps(run), encoding="utf-8")
        assert len(build_pack(cfg, rd)["evidence"]) == 2

    def test_the_pack_declares_its_own_blindfold(self, cfg: Config, rd: RunDir) -> None:
        """A reviewer that knows what it cannot see can say so instead of guessing."""
        excluded = build_pack(cfg, rd)["excluded"]
        assert len(excluded) == len(EXCLUSIONS)
        assert all(item["what"] and item["why"] for item in excluded)
        assert any("code" in item["what"].lower() for item in excluded)
        assert any("session" in item["what"].lower() for item in excluded)


class TestRunComplete:
    def test_a_clean_run_writes_a_valid_pack(self, cfg: Config, rd: RunDir) -> None:
        result = run_complete(cfg, rd)
        assert result["sanitised"] is not False
        written = json.loads((rd.path / "completeness-pack.json").read_text(encoding="utf-8"))
        assert written["runId"] == "2026-08-20-c100"

    def test_a_leaking_pack_is_never_written(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pack that leaks is worse than no pack: the review would still return
        a verdict, carrying an independence it no longer has."""
        import adlc.stages.complete as module

        def leaky(*args: object, **kwargs: object) -> dict:
            pack = build_pack(cfg, rd)
            pack["requirements"][0]["text"] = "diff --git a/src/theme.py b/src/theme.py"
            return pack

        monkeypatch.setattr(module, "build_pack", leaky)
        result = run_complete(cfg, rd)

        assert result["sanitised"] is False
        assert result["pack"] is None
        assert not (rd.path / "completeness-pack.json").exists()

    def test_the_refusal_is_recorded_as_a_failed_stage(
        self, cfg: Config, rd: RunDir, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import adlc.stages.complete as module

        monkeypatch.setattr(module, "build_pack", lambda *a, **k: {"note": "diff --git"})
        run_complete(cfg, rd)

        stage = rd.latest_stage("complete")
        assert stage["status"] == "fail"
        assert "refused to write" in stage["message"]

    def test_a_run_with_no_requirements_fails_rather_than_passing_vacuously(
        self, cfg: Config, rd: RunDir
    ) -> None:
        (rd.spec_dir / "spec.md").write_text("# Spec\n\nNo criteria here.\n", encoding="utf-8")
        run_complete(cfg, rd)
        stage = rd.latest_stage("complete")
        assert stage["status"] == "fail"
        assert "nothing to review completeness against" in stage["message"]
