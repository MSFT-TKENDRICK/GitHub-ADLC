"""L9 — persona feedback: what the personas did, and what they were thinking.

The risk this stage carries is not that it produces too little, but that it
produces something that *reads* like user research and is not. Three properties
keep it honest, and they are what this suite pins:

**It cannot claim evidence the evidence gate does not agree it has.** ``covering``
is resolved through the review pack's coverage map rather than a second,
differently-coarse heuristic. An artifact sitting on disk is not enough; a hash
the pack names but disk does not have is not enough either. Both must agree, or
the persona pane and the evidence pane would describe the same run differently.

**Its verdicts are restatements of signals, not opinions.** Blocked, partial,
confused and satisfied each trace to a specific fact about the run, and swapping
that fact must swap the verdict.

**It never launders a simulation into a human.** ``simulated`` is set by the code
path that produced the record, and an ingested record from a real session is
never overwritten by a regenerated one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adlc.config import Config
from adlc.runs import RunDir
from adlc.schemas import is_valid
from adlc.stages.persona_feedback import (
    SCHEMA,
    build_feedback,
    load_feedback,
    personas_dir,
    run_persona_feedback,
)

SPEC = """\
# Feature: Dark mode

## Problem

Readers cannot use the app at night.

## User Story 1 — Reader switches theme (Priority: P1)

As a Reader, I want to toggle a dark theme so that I can read at night.

- **US1-AC1**: The theme toggle switches the page to dark within 200ms.
- **US1-AC2**: The chosen theme survives a reload.

## User Story 2 — Administrator sets the default (Priority: P2)

As an Administrator, I want to set the workspace default theme so that new
readers inherit the house style.

- **US2-AC1**: The workspace default applies to a reader who has never chosen.
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path, profile="minimal")


@pytest.fixture
def rd(cfg: Config) -> RunDir:
    run = RunDir(cfg, "2026-08-21-p3rs")
    for directory in (run.spec_dir, run.evidence_dir / "candidate-a", run.stages_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (run.spec_dir / "spec.md").write_text(SPEC, encoding="utf-8")
    return run


def put_artifact(rd: RunDir, name: str, body: bytes = b"x") -> str:
    """Write an evidence file and return its sha256, as scan_artifacts sees it."""
    import hashlib

    path = rd.evidence_dir / "candidate-a" / name
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def put_pack(rd: RunDir, coverage: list[dict]) -> None:
    rd.review_pack.write_text(
        json.dumps({"runId": rd.run_id, "coverage": coverage}, indent=2), encoding="utf-8"
    )


def cover(req: str, *, kinds: list[str], hashes: list[str]) -> dict:
    return {
        "requirementId": req,
        "evidenceKinds": kinds,
        "artifactSha256": hashes,
        "present": bool(hashes),
    }


def by_scenario(records: list[dict], persona: str = "reader") -> dict[str, dict]:
    return {r["scenarioId"]: r for r in records if r["personaId"] == persona}


def latest_stage(rd: RunDir) -> dict:
    """The most recent `persona_feedback.<attempt>.json`."""
    newest = max(rd.stages_dir.glob("persona_feedback.*.json"))
    return json.loads(newest.read_text(encoding="utf-8"))


class TestGrounding:
    def test_evidence_is_claimed_only_where_the_coverage_map_agrees(
        self, rd: RunDir
    ) -> None:
        """An artifact on disk is not evidence *for a requirement* until the pack says so."""
        orphan = put_artifact(rd, "unrelated.png", b"loose")
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=[])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["artifactSha256"] == []
        assert record["verdict"] == "blocked"
        assert orphan not in json.dumps(record)

    def test_a_hash_the_pack_names_but_disk_lacks_is_not_claimed(self, rd: RunDir) -> None:
        """The pack can go stale. A citation to a file that is gone is not checkable."""
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=["a" * 64])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["artifactSha256"] == []
        assert record["verdict"] == "blocked"

    def test_a_hash_on_both_sides_is_claimed(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "toggle.png", b"png-bytes")
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=[digest])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["artifactSha256"] == [digest]

    def test_every_claimed_hash_is_a_well_formed_digest(self, rd: RunDir) -> None:
        import re

        digest = put_artifact(rd, "toggle.png", b"png-bytes")
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=[digest])])

        for record in build_feedback(rd):
            for claimed in record["artifactSha256"]:
                assert re.fullmatch(r"[a-f0-9]{64}", claimed)

    def test_persona_records_are_not_evidence_for_themselves(self, rd: RunDir) -> None:
        """A regenerated record must not cite the previous generation of itself."""
        digest = put_artifact(rd, "toggle.png", b"png-bytes")
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=[digest])])
        run_persona_feedback(cfg_of(rd), rd)

        persona_hashes = {
            a["sha256"] for a in rd.scan_artifacts() if a.get("kind") == "persona_feedback"
        }
        assert persona_hashes, "records should be scanned as artifacts"
        for record in build_feedback(rd):
            assert not (set(record["artifactSha256"]) & persona_hashes)

    def test_the_walkthrough_speaks_the_coverage_maps_vocabulary(self, rd: RunDir) -> None:
        """The kind shown to a reader must match the kind the gate scored.

        The file is a ``.bin``; the pack calls it a trace. Both statements are
        true, and a report that made both would read as carelessness.
        """
        digest = put_artifact(rd, "session.bin", b"trace")
        put_pack(rd, [cover("US1-AC1", kinds=["playwright_trace"], hashes=[digest])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        prose = json.dumps(record)
        assert "playwright_trace" in prose
        assert "file" not in record["steps"][1]["observation"]

    def test_the_coverage_map_can_reclassify_a_file_as_visual(self, rd: RunDir) -> None:
        """Trusting the map means trusting it in both directions."""
        digest = put_artifact(rd, "session.bin", b"frames")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])

        assert by_scenario(build_feedback(rd))["US1-AC1"]["verdict"] == "satisfied"

    def test_no_pack_at_all_means_nothing_is_covered(self, rd: RunDir) -> None:
        put_artifact(rd, "toggle.png", b"png-bytes")
        records = build_feedback(rd)
        assert records
        assert all(r["verdict"] == "blocked" for r in records)

    def test_an_unreadable_pack_does_not_crash_the_stage(self, rd: RunDir) -> None:
        rd.review_pack.write_text("{{{ not json", encoding="utf-8")
        assert all(r["verdict"] == "blocked" for r in build_feedback(rd))


class TestVerdictsFollowSignals:
    def test_no_artifact_reads_as_blocked(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["verdict"] == "blocked"
        assert record["sentiment"] < 0
        assert record["friction"][0]["severity"] == "high"

    def test_a_missed_budget_reads_as_partial(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "toggle.png", b"png-bytes")
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=[digest])])
        (rd.evidence_dir / "candidate-a" / "perf-measurements.json").write_text(
            json.dumps([{"metricId": "lcp_ms", "value": 900, "budget": 200}]), encoding="utf-8"
        )

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["verdict"] == "partial"
        assert "lcp_ms" in record["steps"][1]["observation"]
        assert record["friction"][0]["severity"] == "medium"

    def test_a_met_budget_does_not_downgrade_the_verdict(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "toggle.png", b"png-bytes")
        put_pack(rd, [cover("US1-AC1", kinds=["screenshot"], hashes=[digest])])
        (rd.evidence_dir / "candidate-a" / "perf-measurements.json").write_text(
            json.dumps([{"metricId": "lcp_ms", "value": 120, "budget": 200}]), encoding="utf-8"
        )

        assert by_scenario(build_feedback(rd))["US1-AC1"]["verdict"] == "satisfied"

    def test_non_visual_evidence_alone_reads_as_confused(self, rd: RunDir) -> None:
        """Logs prove the machine acted. They do not prove a human could tell."""
        digest = put_artifact(rd, "run.log", b"log")
        put_pack(rd, [cover("US1-AC1", kinds=["log"], hashes=[digest])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["verdict"] == "confused"
        assert "nothing shows me the screen" in record["steps"][1]["observation"]

    def test_visual_evidence_reads_as_satisfied(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "flow.webm", b"video")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert record["verdict"] == "satisfied"
        assert record["sentiment"] > 0
        assert record["friction"] == []

    def test_every_friction_point_names_the_requirement_it_came_from(
        self, rd: RunDir
    ) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        for record in build_feedback(rd):
            for point in record["friction"]:
                assert point["requirementId"] == record["scenarioId"]

    def test_an_access_need_is_deferred_not_asserted(self, rd: RunDir) -> None:
        """This stage cannot tell whether an access need was met, and says so."""
        digest = put_artifact(rd, "flow.webm", b"video")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])

        record = by_scenario(build_feedback(rd))["US1-AC1"]
        last = record["steps"][-1]
        assert "accessibility gate" in last["outcome"]
        assert last["confidence"] < 0.5

    def test_every_step_carries_a_visible_thought(self, rd: RunDir) -> None:
        """The thought process is the point of the pane, not an optional extra."""
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        for record in build_feedback(rd):
            for step in record["steps"]:
                assert step["thought"].strip()
                assert 0.0 <= step["confidence"] <= 1.0


class TestOwnership:
    def test_a_persona_walks_the_criteria_its_own_story_declared(self, rd: RunDir) -> None:
        records = build_feedback(rd)
        assert by_scenario(records, "reader").keys() == {"US1-AC1", "US1-AC2"}
        assert by_scenario(records, "administrator").keys() == {"US2-AC1"}

    def test_a_persona_owning_nothing_walks_everything(self, rd: RunDir) -> None:
        """A persona with nothing to say means the spec forgot to connect them."""
        (rd.spec_dir / "spec.md").write_text(
            SPEC.replace("- **US2-AC1**", "\n## Criteria\n\n- **US2-AC1**"), encoding="utf-8"
        )
        records = build_feedback(rd)
        assert by_scenario(records, "administrator").keys() == {
            "US1-AC1", "US1-AC2", "US2-AC1"
        }

    def test_no_personas_means_no_records(self, rd: RunDir) -> None:
        (rd.spec_dir / "spec.md").write_text("# Feature\n\nNo stories here.", encoding="utf-8")
        assert build_feedback(rd) == []

    def test_no_requirements_means_no_records(self, rd: RunDir) -> None:
        (rd.spec_dir / "spec.md").write_text(
            "## User Story 1\n\nAs a Reader, I want a thing so that I benefit.\n",
            encoding="utf-8",
        )
        assert build_feedback(rd) == []


class TestSummaries:
    def test_each_scenario_gets_its_own_tldr(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "flow.webm", b"video")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])

        tldrs = [r["tldr"] for r in build_feedback(rd)]
        assert len(set(tldrs)) == len(tldrs), "two scenarios must not share a summary"

    def test_a_tldr_fits_the_budget(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        for record in build_feedback(rd):
            assert 0 < len(record["tldr"]) <= 150

    def test_a_tldr_names_the_persona_and_the_scenario(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        record = by_scenario(build_feedback(rd))["US1-AC1"]
        assert "Goran" in record["tldr"]
        assert "US1-AC1" in record["tldr"]

    def test_the_verdict_reaches_the_summary(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "flow.webm", b"video")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])
        records = by_scenario(build_feedback(rd))
        assert records["US1-AC1"]["tldr"] != records["US1-AC2"]["tldr"]


class TestProvenance:
    def test_a_generated_record_always_declares_itself_simulated(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        for record in build_feedback(rd):
            assert record["simulated"] is True
            assert "deterministic" in record["source"]

    def test_generation_is_deterministic_apart_from_the_timestamp(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "flow.webm", b"video")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])

        def stripped() -> list[dict]:
            return [{k: v for k, v in r.items() if k != "recordedAt"} for r in build_feedback(rd)]

        assert stripped() == stripped()

    def test_every_generated_record_conforms_to_the_schema(self, rd: RunDir) -> None:
        digest = put_artifact(rd, "flow.webm", b"video")
        put_pack(rd, [cover("US1-AC1", kinds=["video"], hashes=[digest])])
        for record in build_feedback(rd):
            valid, errors = is_valid(SCHEMA, record)
            assert valid, errors


class TestRunStage:
    def test_records_land_where_the_scanner_hashes_them(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        run_persona_feedback(cfg_of(rd), rd)

        assert list(personas_dir(rd).glob("*.json"))
        kinds = {a["kind"] for a in rd.scan_artifacts()}
        assert "persona_feedback" in kinds

    def test_a_real_session_is_never_overwritten_by_a_regeneration(
        self, rd: RunDir
    ) -> None:
        """A human's session outranks anything this module can derive."""
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        run_persona_feedback(cfg_of(rd), rd)

        ingested = personas_dir(rd) / "reader--us1-ac1.json"
        assert ingested.is_file(), "naming convention changed; update this test"
        payload = json.loads(ingested.read_text(encoding="utf-8"))
        payload.update({
            "simulated": False,
            "source": "moderated session, 2026-08-21",
            "tldr": "Goran got there but hated the animation.",
        })
        ingested.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        run_persona_feedback(cfg_of(rd), rd)

        after = json.loads(ingested.read_text(encoding="utf-8"))
        assert after["simulated"] is False
        assert after["tldr"] == "Goran got there but hated the animation."

    def test_the_stage_counts_real_and_simulated_separately(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        run_persona_feedback(cfg_of(rd), rd)
        ingested = personas_dir(rd) / "reader--us1-ac1.json"
        payload = json.loads(ingested.read_text(encoding="utf-8"))
        payload["simulated"] = False
        ingested.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        run_persona_feedback(cfg_of(rd), rd)
        stage = latest_stage(rd)
        assert stage["data"]["realSessions"] == 1
        assert stage["status"] == "ok"
        assert "1 from real sessions" in stage["message"]

    def test_a_run_with_no_personas_is_skipped_not_failed(self, rd: RunDir) -> None:
        (rd.spec_dir / "spec.md").write_text("# Feature\n\nNothing.", encoding="utf-8")
        result = run_persona_feedback(cfg_of(rd), rd)

        assert result["records"] == []
        assert latest_stage(rd)["status"] == "skipped"

    def test_the_stage_reports_friction_across_every_record(self, rd: RunDir) -> None:
        put_pack(rd, [cover("US1-AC1", kinds=[], hashes=[])])
        run_persona_feedback(cfg_of(rd), rd)
        stage = latest_stage(rd)
        assert stage["data"]["frictionPoints"] == 3
        assert stage["data"]["verdicts"]["blocked"] == 3


class TestLoad:
    def test_a_malformed_record_is_surfaced_not_dropped(self, rd: RunDir) -> None:
        """"No personas ran" and "the records were corrupt" call for opposite responses."""
        personas_dir(rd).mkdir(parents=True, exist_ok=True)
        (personas_dir(rd) / "broken.json").write_text("{ nope", encoding="utf-8")

        loaded = load_feedback(rd)
        assert len(loaded) == 1
        assert loaded[0]["_invalid"]
        assert "unreadable" in loaded[0]["_invalid"][0]

    def test_a_schema_violation_is_flagged_with_its_errors(self, rd: RunDir) -> None:
        personas_dir(rd).mkdir(parents=True, exist_ok=True)
        (personas_dir(rd) / "thin.json").write_text(
            json.dumps({"personaId": "x", "verdict": "elated"}), encoding="utf-8"
        )

        loaded = load_feedback(rd)
        assert loaded[0]["_invalid"]
        assert loaded[0]["_path"].startswith("evidence/personas/")

    def test_no_directory_reads_as_empty(self, rd: RunDir) -> None:
        assert load_feedback(rd) == []


def cfg_of(rd: RunDir) -> Config:
    """The config a RunDir was built from, without reaching into private state."""
    return Config(root=rd.path.parents[2], profile="minimal")
