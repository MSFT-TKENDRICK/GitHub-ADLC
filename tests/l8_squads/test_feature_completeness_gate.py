"""L8 — the feature-completeness gate: the last question, asked from outside.

This gate departs from :mod:`~adlc.adapters.gate.evidence_review` in one way that
matters: it **blocks**. Evidence review is advisory because a deterministic hash
check sits underneath it and owns the blocking decision. Nothing sits underneath
"does this evidence show what was asked for" -- it is a judgement, and a judgement
that cannot stop the run is a comment, not a gate.

Two families of behaviour are pinned here.

**It fails closed.** A missing pack, a pack with no requirements, a squad that
never filed, or a quorum that cannot be reached all resolve to ``not_run``, which
the aggregate treats as a failure while the gate is required. "We could not check
whether we built the right thing" must never render as "we built the right thing".

**It cannot block on a hunch.** An uncited finding is discarded, and a finding
citing a digest that does not appear in the pack is discarded as *fabricated* --
an invented hash is worse than no hash, because it looks checkable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adlc.adapters.gate.feature_completeness import PACK_NAME, FeatureCompletenessGate
from adlc.config import Config

from .l8_fixtures import make_run, sha, write_review, write_squads

SQUADS = """
version: 1
defaults:
  citationPolicy: discard-uncited
  blockingSeverities: [high, critical]
  abstainCountsAsPass: false
squads:
  feature_completeness:
    blocking: true
    quorum: "2/3"
    citation: artifact-sha256
    routesTo: outer
    members:
      - id: completeness-auditor
      - id: grounding-auditor
      - id: relevance-auditor
"""

MEMBERS = ("completeness-auditor", "grounding-auditor", "relevance-auditor")

#: Digests that appear in the pack, so a citation to one is checkable.
IN_PACK = [sha("US1-AC1"), sha("US1-AC2")]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    write_squads(tmp_path, SQUADS)
    return Config(root=tmp_path, profile="full")


@pytest.fixture
def run_dir(cfg: Config) -> Path:
    path = cfg.root / ".adlc" / "runs" / "2026-08-19-t3st"
    (path / "reviews").mkdir(parents=True)
    (path / "gates").mkdir(parents=True)
    return path


def write_pack(
    run_dir: Path,
    *,
    run_id: str = "2026-08-19-t3st",
    requirements: int = 2,
    hashes: list[str] | None = None,
) -> Path:
    digests = IN_PACK if hashes is None else hashes
    pack = {
        "runId": run_id,
        "candidateSha": "cafebabe",
        "collector": "adlc.stages.complete",
        "brief": {"text": "Add dark mode.", "source": "brief.md", "sha256": "0" * 64},
        "requirements": [
            {
                "id": f"US1-AC{i + 1}",
                "text": f"requirement {i + 1}",
                "covered": True,
                "artifactSha256": [digests[i]] if i < len(digests) else [],
            }
            for i in range(requirements)
        ],
        "evidence": [
            {"artifactSha256": d, "kind": "video", "bytes": 1, "redacted": True}
            for d in digests
        ],
        "counts": {
            "requirements": requirements,
            "covered": requirements,
            "uncovered": 0,
            "artifacts": len(digests),
            "personaRecords": 0,
        },
        "excluded": [{"what": "Source code and diffs", "why": "independence"}],
    }
    path = run_dir / PACK_NAME
    path.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return path


def review(run_dir: Path, member: str, verdict: str, findings=None) -> Path:
    return write_review(
        run_dir, member, verdict, findings,
        squad="feature_completeness", run_id="2026-08-19-t3st",
    )


def evaluate(cfg: Config) -> dict:
    return FeatureCompletenessGate().evaluate(make_run(artifact_hashes=IN_PACK), cfg)


class TestFailsClosed:
    def test_a_missing_pack_is_not_run_not_pass(self, cfg: Config, run_dir: Path) -> None:
        result = evaluate(cfg)
        assert result["status"] == "not_run"
        assert "adlc complete" in result["message"]

    def test_an_unreadable_pack_is_not_run(self, cfg: Config, run_dir: Path) -> None:
        (run_dir / PACK_NAME).write_text("{ not json", encoding="utf-8")
        result = evaluate(cfg)
        assert result["status"] == "not_run"
        assert "not valid JSON" in result["observed"]["packNote"]

    def test_a_pack_with_no_requirements_is_not_run(self, cfg: Config, run_dir: Path) -> None:
        """With no statement of intent there is nothing to review the evidence against."""
        write_pack(run_dir, requirements=0, hashes=[])
        for member in MEMBERS:
            review(run_dir, member, "pass")
        result = evaluate(cfg)
        assert result["status"] == "not_run"
        assert "no requirements" in result["message"]

    def test_a_squad_that_never_filed_is_not_run(self, cfg: Config, run_dir: Path) -> None:
        write_pack(run_dir)
        result = evaluate(cfg)
        assert result["status"] == "not_run"
        assert "Nobody has confirmed" in result["message"]

    def test_too_few_members_filing_makes_quorum_unreachable(
        self, cfg: Config, run_dir: Path
    ) -> None:
        """One clean verdict out of three is not a clean bill of health."""
        write_pack(run_dir)
        review(run_dir, "completeness-auditor", "pass")
        result = evaluate(cfg)
        assert result["status"] == "not_run"
        assert "quorum is unreachable" in result["message"]

    def test_a_run_without_an_id_cannot_be_located(self, cfg: Config) -> None:
        result = FeatureCompletenessGate().evaluate({"runId": ""}, cfg)
        assert result["status"] == "not_run"

    def test_the_gate_is_required_under_the_full_profile(
        self, cfg: Config, run_dir: Path
    ) -> None:
        assert evaluate(cfg)["required"] is True


class TestPackIdentity:
    def test_a_pack_from_another_run_fails(self, cfg: Config, run_dir: Path) -> None:
        """A review of another run's evidence says nothing about this one."""
        write_pack(run_dir, run_id="2026-01-01-other")
        for member in MEMBERS:
            review(run_dir, member, "pass")
        result = evaluate(cfg)
        assert result["status"] == "fail"
        assert "belongs to run 2026-01-01-other" in result["message"]


class TestQuorum:
    def test_a_cited_quorum_blocks_the_run(self, cfg: Config, run_dir: Path) -> None:
        write_pack(run_dir)
        review(run_dir, "completeness-auditor", "block",
               [("high", "the reload requirement is never demonstrated", IN_PACK[0])])
        review(run_dir, "grounding-auditor", "block",
               [("high", "a screenshot cannot show persistence", IN_PACK[1])])
        review(run_dir, "relevance-auditor", "pass")

        result = evaluate(cfg)
        assert result["status"] == "fail"
        assert result["severity"] == "high"
        assert "the evidence does not demonstrate the request" in result["message"]

    def test_a_blocking_verdict_routes_to_the_outer_loop(
        self, cfg: Config, run_dir: Path
    ) -> None:
        """If the evidence does not answer the brief, patching the code is guessing."""
        write_pack(run_dir)
        for member in MEMBERS[:2]:
            review(run_dir, member, "block", [("high", "not demonstrated", IN_PACK[0])])
        review(run_dir, "relevance-auditor", "pass")

        message = evaluate(cfg)["message"]
        assert "outer-loop failure" in message
        assert "revisit the design" in message

    def test_one_reviewer_alone_cannot_block(self, cfg: Config, run_dir: Path) -> None:
        write_pack(run_dir)
        review(run_dir, "completeness-auditor", "block",
               [("high", "not demonstrated", IN_PACK[0])])
        review(run_dir, "grounding-auditor", "pass")
        review(run_dir, "relevance-auditor", "pass")
        assert evaluate(cfg)["status"] == "pass"

    def test_a_full_clean_squad_passes(self, cfg: Config, run_dir: Path) -> None:
        write_pack(run_dir)
        for member in MEMBERS:
            review(run_dir, member, "pass")
        result = evaluate(cfg)
        assert result["status"] == "pass"
        assert "2/2 requirement(s) backed by evidence" in result["message"]

    def test_the_expectation_states_what_the_reviewer_could_see(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_pack(run_dir)
        expected = evaluate(cfg)["expected"]
        assert PACK_NAME in expected["input"]
        assert "no source" in expected["input"]
        assert "no agent sessions" in expected["input"]


class TestFalsifiability:
    def test_an_uncited_concern_is_discarded_before_the_vote(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_pack(run_dir)
        for member in MEMBERS[:2]:
            review(run_dir, member, "block", [("high", "this feels thin", "")])
        review(run_dir, "relevance-auditor", "pass")

        result = evaluate(cfg)
        assert result["status"] == "pass", "an uncited claim cannot block a run"
        assert len(result["observed"]["review"]["discardedFindings"]) == 2

    def test_a_fabricated_digest_is_discarded(self, cfg: Config, run_dir: Path) -> None:
        """An invented hash is worse than no hash: it looks checkable."""
        write_pack(run_dir)
        for member in MEMBERS[:2]:
            review(run_dir, member, "block",
                   [("high", "invented evidence", sha("not-in-the-pack"))])
        review(run_dir, "relevance-auditor", "pass")

        result = evaluate(cfg)
        assert result["status"] == "pass"
        assert len(result["observed"]["review"]["fabricatedCitations"]) == 2
        assert "cited a hash absent from the pack" in result["message"]

    def test_the_exclusions_the_pack_declared_are_carried_into_the_result(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_pack(run_dir)
        for member in MEMBERS:
            review(run_dir, member, "pass")
        assert evaluate(cfg)["observed"]["excludedFromReview"] == ["Source code and diffs"]

    def test_the_reviews_that_were_read_are_named_as_evidence(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_pack(run_dir)
        for member in MEMBERS:
            review(run_dir, member, "pass")
        evidence = evaluate(cfg)["evidence"]
        assert any(PACK_NAME in item for item in evidence)
        assert sum("reviews/" in item for item in evidence) == 3


class TestDetection:
    def test_the_gate_is_unavailable_without_a_squad_configuration(
        self, tmp_path: Path
    ) -> None:
        available, why = FeatureCompletenessGate.detect(Config(root=tmp_path, profile="full"))
        assert available is False
        assert "no squad configuration" in why

    def test_the_gate_is_available_with_one(self, cfg: Config) -> None:
        available, why = FeatureCompletenessGate.detect(cfg)
        assert available is True
        assert "squads.yaml" in why
