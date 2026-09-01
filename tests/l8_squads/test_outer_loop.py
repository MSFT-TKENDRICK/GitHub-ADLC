"""L8 — routing a failed completeness review back into the outer loop.

The gate's verdict is only worth having if something acts on it. This module
pins the thing that acts: :func:`~adlc.stages.complete.iterate_on_feedback`.

Three properties carry the design.

**It routes outward, never inward.** The inner loop patches code against a fixed
plan. If the evidence does not demonstrate the request, the plan is what was
wrong, and patching would produce more evidence for the same wrong thing. So the
successor is a fresh run -- spec, enrich and graph all run again.

**It appends, never edits.** The predecessor is left exactly as it was and the
successor carries ``referencesRun``, so a redesign is a new node in the audit
trail rather than an overwrite of the record that prompted it.

**It carries only admissible findings.** The gate discards uncited claims, and
discards claims citing a digest absent from the pack as *fabricated*. Both rules
are re-applied here, because the gate screens in memory and leaves no trace on
disk -- so a fabricated finding would otherwise be copied into the next run's
brief and shape the redesign, having already been ruled inadmissible for the
vote. That regression is the reason this module exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages.complete import GATE_ID, iterate_on_feedback

from .l8_fixtures import sha, write_review

RUN_ID = "2026-08-20-out1"
BRIEF = "Add dark mode to the settings page so it can be read at night.\n"

#: Digests that really appear in the pack, so a citation to one is checkable.
REAL = [sha("US1-AC1"), sha("US1-AC2")]
#: Well-formed, entirely invented. The shape is the danger: it looks checkable.
FABRICATED = sha("never-collected-this")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path, profile="full")


@pytest.fixture
def rd(cfg: Config) -> RunDir:
    """A run that has been through the gate, with a pack on disk to screen against."""
    run = RunDir(cfg, RUN_ID)
    run.create(profile="full", brief_text=BRIEF)
    write_pack(run)
    return run


def write_pack(rd: RunDir, hashes: list[str] | None = None) -> Path:
    digests = REAL if hashes is None else hashes
    pack = {
        "runId": rd.run_id,
        "requirements": [
            {"id": f"US1-AC{i + 1}", "artifactSha256": [d]}
            for i, d in enumerate(digests)
        ],
        "evidence": [{"artifactSha256": d, "kind": "video"} for d in digests],
    }
    path = rd.path / "completeness-pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    return path


def write_gate(rd: RunDir, status: str, message: str = "the squad blocked") -> Path:
    payload = {"id": GATE_ID, "required": True, "status": status, "message": message}
    path = rd.gates_dir / f"{GATE_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def review(rd: RunDir, member: str, verdict: str, findings=None) -> Path:
    return write_review(
        rd.path, member, verdict, findings, squad=GATE_ID, run_id=rd.run_id,
    )


def successor_of(cfg: Config, result: dict[str, Any]) -> RunDir:
    return RunDir(cfg, result["successorRun"])


def last_stage(rd: RunDir, stage: str = "complete") -> dict[str, Any]:
    results = [s for s in rd.stage_results() if s["stage"] == stage]
    assert results, f"no `{stage}` stage result was written"
    return results[-1]


def sibling_runs(cfg: Config, exclude: str) -> list[str]:
    root = cfg.root / ".adlc" / "runs"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != exclude)


class TestOnlyAFailureIterates:
    """A successor run is a real cost. Only a blocking verdict may create one."""

    @pytest.mark.parametrize("status", ["pass", "not_run", "skipped"])
    def test_a_non_failing_gate_creates_nothing(
        self, cfg: Config, rd: RunDir, status: str
    ) -> None:
        write_gate(rd, status)
        result = iterate_on_feedback(cfg, rd)

        assert result["iterated"] is False
        assert result["successorRun"] is None
        assert sibling_runs(cfg, RUN_ID) == []

    def test_an_unevaluated_gate_creates_nothing_and_says_so(
        self, cfg: Config, rd: RunDir
    ) -> None:
        result = iterate_on_feedback(cfg, rd)

        assert result["iterated"] is False
        assert sibling_runs(cfg, RUN_ID) == []
        assert "not been evaluated" in last_stage(rd)["message"]

    def test_not_iterating_is_recorded_as_ok_not_as_a_failure(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """A passing gate must not leave a `fail` stage behind -- the aggregate
        reads stage results, and a spurious failure here would sink a good run."""
        write_gate(rd, "pass")
        iterate_on_feedback(cfg, rd)

        assert last_stage(rd)["status"] == "ok"

    def test_a_failure_creates_exactly_one_successor(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        review(rd, "completeness-auditor", "block", [("high", "US1-AC2 unproven", REAL[0])])
        result = iterate_on_feedback(cfg, rd)

        assert result["iterated"] is True
        assert sibling_runs(cfg, RUN_ID) == [result["successorRun"]]

    def test_the_successor_is_a_new_run_not_the_current_one(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)

        assert result["successorRun"] != RUN_ID


class TestItRoutesOutward:
    """The whole point of the gate: a failure is a design problem, not a bug."""

    def test_the_route_is_outer(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)

        assert result["route"] == "outer"
        assert last_stage(rd)["data"]["route"] == "outer"

    def test_the_route_is_never_inner(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)

        assert result["route"] != "inner"

    def test_the_stage_message_names_the_successor_and_the_route(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)
        message = last_stage(rd)["message"]

        assert result["successorRun"] in message
        assert "route=outer" in message

    def test_the_failure_stays_visible_on_the_run_that_failed(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """Creating a successor must not launder the failure. If this stage went
        `ok`, a blocked run would reduce to a passing one that happens to have a
        sibling."""
        write_gate(rd, "fail")
        iterate_on_feedback(cfg, rd)

        assert last_stage(rd)["status"] == "fail"


class TestTheAuditTrailIsAppendOnly:
    def test_the_successor_references_the_run_it_came_from(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)

        seed = json.loads((successor_of(cfg, result).path / "seed.json").read_text())
        assert seed["referencesRun"] == RUN_ID

    def test_the_predecessor_brief_is_untouched(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        review(rd, "completeness-auditor", "block", [("high", "unproven", REAL[0])])
        iterate_on_feedback(cfg, rd)

        assert rd.brief.read_text(encoding="utf-8") == BRIEF

    def test_the_successor_carries_the_original_request_forward(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """The redesign amends the request; it does not replace it."""
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)

        assert BRIEF.strip() in successor_of(cfg, result).brief.read_text(encoding="utf-8")

    def test_the_successor_inherits_the_profile(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)

        seed = json.loads((successor_of(cfg, result).path / "seed.json").read_text())
        assert seed["profile"] == "full"

    def test_the_successor_brief_frames_findings_as_amendments_not_bugs(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd)
        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")

        assert "amendments to the brief" in brief
        assert "not as bugs to patch" in brief

    def test_the_successor_brief_carries_the_gate_verdict(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail", message="2/3 blocked: the recording proves nothing")
        result = iterate_on_feedback(cfg, rd)

        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert "the recording proves nothing" in brief


class TestOnlyAdmissibleFindingsReachTheRedesign:
    """The regression this module was written for.

    The gate strips inadmissible citations from its own in-memory reviews. That
    mutation never reaches disk, so anything re-reading the reviews must re-apply
    the rule or it silently readmits what the gate threw out.
    """

    def test_a_cited_finding_reaches_the_successor_brief(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "no proof of persistence", REAL[1])])
        result = iterate_on_feedback(cfg, rd)

        assert "no proof of persistence" in successor_of(cfg, result).brief.read_text(
            encoding="utf-8"
        )

    def test_an_uncited_finding_never_reaches_the_successor_brief(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        review(rd, "relevance-auditor", "block", [("high", "feels underbaked", "")])
        result = iterate_on_feedback(cfg, rd)

        assert "feels underbaked" not in successor_of(cfg, result).brief.read_text(
            encoding="utf-8"
        )

    def test_a_fabricated_citation_never_reaches_the_successor_brief(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """A digest that is not in the pack was ruled inadmissible for the vote.
        It must not get a second, unexamined hearing as an amendment."""
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "invented shortfall", FABRICATED)])
        result = iterate_on_feedback(cfg, rd)

        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert "invented shortfall" not in brief
        assert FABRICATED not in brief

    def test_a_fabricated_citation_is_recorded_rather_than_dropped_silently(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "invented shortfall", FABRICATED)])
        result = iterate_on_feedback(cfg, rd)

        fabricated = result["fabricatedCitations"]
        assert [f["title"] for f in fabricated] == ["invented shortfall"]
        assert FABRICATED in fabricated[0]["hashes"]

    def test_a_member_whose_only_finding_was_fabricated_is_not_credited(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "invented", FABRICATED)])
        result = iterate_on_feedback(cfg, rd)

        assert result["members"] == []

    def test_a_partly_fabricated_finding_survives_on_its_real_citation(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """Screening drops bad hashes, not whole findings. A reviewer who cites
        one real digest and fumbles another has still made a checkable claim."""
        write_gate(rd, "fail")
        path = rd.reviews_dir / f"{GATE_ID}.completeness-auditor.md"
        path.write_text(
            "---\n"
            f"squad: {GATE_ID}\nmember: completeness-auditor\nverdict: block\n"
            f"runId: {rd.run_id}\n---\n\n"
            f"## [high] partly grounded\n`{REAL[0]}` and `{FABRICATED}`\n\nProse.\n",
            encoding="utf-8",
        )
        result = iterate_on_feedback(cfg, rd)

        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert "partly grounded" in brief
        assert REAL[0] in brief
        assert FABRICATED not in brief

    def test_a_fabricated_digest_is_scrubbed_from_reviewer_prose_too(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """Screening clears the parsed citation field, but reviewers also write
        the digest into the sentence, and the sentence is quoted verbatim into
        the brief. An invented hash looks exactly as checkable in prose."""
        write_gate(rd, "fail")
        path = rd.reviews_dir / f"{GATE_ID}.grounding-auditor.md"
        path.write_text(
            "---\n"
            f"squad: {GATE_ID}\nmember: grounding-auditor\nverdict: block\n"
            f"runId: {rd.run_id}\n---\n\n"
            f"## [high] weak grounding\n`{REAL[0]}`\n\n"
            f"The clip {FABRICATED} does not show the toggle.\n",
            encoding="utf-8",
        )
        result = iterate_on_feedback(cfg, rd)

        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert FABRICATED not in brief
        assert "[unverifiable digest removed]" in brief
        assert "does not show the toggle" in brief

    def test_a_real_digest_in_prose_is_preserved(self, cfg: Config, rd: RunDir) -> None:
        """Redaction must not damage a legitimate reference."""
        write_gate(rd, "fail")
        path = rd.reviews_dir / f"{GATE_ID}.grounding-auditor.md"
        path.write_text(
            "---\n"
            f"squad: {GATE_ID}\nmember: grounding-auditor\nverdict: block\n"
            f"runId: {rd.run_id}\n---\n\n"
            f"## [high] weak grounding\n`{REAL[0]}`\n\n"
            f"The clip {REAL[1]} stops before the reload.\n",
            encoding="utf-8",
        )
        result = iterate_on_feedback(cfg, rd)

        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert REAL[1] in brief
        assert "[unverifiable digest removed]" not in brief

    def test_a_blocked_run_with_nothing_admissible_says_so_explicitly(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """An empty findings section must not read as "redesign, reason unstated"."""
        write_gate(rd, "fail")
        review(rd, "relevance-auditor", "block", [("high", "vibes", "")])
        result = iterate_on_feedback(cfg, rd)

        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert "no finding survived citation screening" in brief

    def test_an_unreadable_pack_labels_the_findings_as_unverified(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """Without a pack nothing can be checked. Carrying the claims is fine;
        carrying them as though they had been checked is not."""
        (rd.path / "completeness-pack.json").write_text("{ not json", encoding="utf-8")
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "unverifiable claim", REAL[0])])
        result = iterate_on_feedback(cfg, rd)

        assert result["packVerified"] is False
        brief = successor_of(cfg, result).brief.read_text(encoding="utf-8")
        assert "Citations unverified" in brief
        assert "unverifiable claim" in brief

    def test_a_readable_pack_is_marked_verified(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "grounded", REAL[0])])
        result = iterate_on_feedback(cfg, rd)

        assert result["packVerified"] is True
        assert "Citations unverified" not in successor_of(cfg, result).brief.read_text(
            encoding="utf-8"
        )

    def test_findings_from_another_squad_are_ignored(self, cfg: Config, rd: RunDir) -> None:
        """Reviews share one directory. A security adversary's finding is about
        the code, which this reviewer never saw and this brief must not inherit."""
        write_gate(rd, "fail")
        write_review(
            rd.path, "security-adversary", "block",
            [("critical", "sql injection in settings", "src/db.py:42")],
            squad="adversarial_review", run_id=rd.run_id,
        )
        result = iterate_on_feedback(cfg, rd)

        assert "sql injection" not in successor_of(cfg, result).brief.read_text(
            encoding="utf-8"
        )
        assert result["members"] == []


class TestIterationCanBeDisabled:
    """CI records the verdict; creating runs is the orchestrator's job."""

    def test_no_successor_is_created(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        result = iterate_on_feedback(cfg, rd, iterate=False)

        assert result["iterated"] is False
        assert result["successorRun"] is None
        assert sibling_runs(cfg, RUN_ID) == []

    def test_the_failure_is_still_recorded(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        iterate_on_feedback(cfg, rd, iterate=False)

        stage = last_stage(rd)
        assert stage["status"] == "fail"
        assert "iteration is disabled" in stage["message"]

    def test_the_feedback_is_still_returned_for_rendering(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        review(rd, "completeness-auditor", "block", [("high", "US1-AC2 unproven", REAL[0])])
        result = iterate_on_feedback(cfg, rd, iterate=False)

        assert "US1-AC2 unproven" in result["feedback"]

    def test_screening_still_applies_when_iteration_is_disabled(
        self, cfg: Config, rd: RunDir
    ) -> None:
        write_gate(rd, "fail")
        review(rd, "grounding-auditor", "block", [("high", "invented", FABRICATED)])
        result = iterate_on_feedback(cfg, rd, iterate=False)

        assert result["feedback"] == ""
        assert result["fabricatedCitations"]


class TestItReadsTheVerdictRobustly:
    def test_the_gate_file_is_preferred(self, cfg: Config, rd: RunDir) -> None:
        write_gate(rd, "fail")
        assert iterate_on_feedback(cfg, rd)["gateStatus"] == "fail"

    def test_it_falls_back_to_the_reduced_run_record(self, cfg: Config, rd: RunDir) -> None:
        """`adlc reduce` folds gate files into run.json. A run reduced and then
        tidied must still be routable."""
        (rd.path / "run.json").write_text(
            json.dumps({
                "schemaVersion": "adlc-run/v1", "runId": rd.run_id,
                "gates": [{"id": GATE_ID, "status": "fail", "message": "blocked"}],
            }),
            encoding="utf-8",
        )
        result = iterate_on_feedback(cfg, rd)

        assert result["gateStatus"] == "fail"
        assert result["iterated"] is True

    def test_an_unreadable_gate_file_does_not_crash(self, cfg: Config, rd: RunDir) -> None:
        (rd.gates_dir / f"{GATE_ID}.json").write_text("{ not json", encoding="utf-8")
        result = iterate_on_feedback(cfg, rd)

        assert result["iterated"] is False

    def test_another_gates_failure_does_not_trigger_a_redesign(
        self, cfg: Config, rd: RunDir
    ) -> None:
        """Only this gate routes outward. A failing security gate is an inner-loop
        problem and must not silently re-spec the feature."""
        (rd.gates_dir / "sast.json").write_text(
            json.dumps({"id": "sast", "status": "fail"}), encoding="utf-8"
        )
        result = iterate_on_feedback(cfg, rd)

        assert result["iterated"] is False
        assert sibling_runs(cfg, RUN_ID) == []
