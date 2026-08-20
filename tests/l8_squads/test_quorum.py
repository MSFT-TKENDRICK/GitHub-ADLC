"""Quorum arithmetic and the adversarial squad's blocking decision."""

from __future__ import annotations

from pathlib import Path

import pytest

from adlc.adapters.gate.adversarial_review import (
    AdversarialReviewGate,
    load_squads,
    quorum_threshold,
)
from adlc.config import Config

from .l8_fixtures import make_run, write_review, write_squads

CITE = "src/api/documents.ts:L88-L104"


class TestQuorumThreshold:
    @pytest.mark.parametrize(
        ("quorum", "members", "expected"),
        [
            ("2/3", 3, 2),      # read literally when the denominator matches
            ("1/1", 1, 1),
            ("3/3", 3, 3),
            ("2/3", 6, 4),      # scaled, so adding members cannot weaken the squad
            ("1/2", 5, 3),      # ceil(0.5 * 5)
            (2, 3, 2),          # bare integer
            ("2", 3, 2),        # stringified integer
            ("all", 4, 4),
            ("unanimous", 4, 4),
            ("any", 4, 1),
            ("one", 4, 1),
        ],
    )
    def test_expressions(self, quorum: object, members: int, expected: int) -> None:
        assert quorum_threshold(quorum, members) == expected

    @pytest.mark.parametrize(
        ("quorum", "members", "expected"),
        [
            ("9/3", 3, 3),       # clamped: never unreachable
            (0, 3, 1),           # clamped: never zero, a squad always needs a vote
            (-4, 3, 1),
            ("2/0", 3, 3),       # nonsense denominator -> unanimous, the safe end
            ("banana", 3, 3),    # unparseable -> unanimous, never "1 vote is enough"
            (True, 3, 3),        # bool is an int subclass; must not become 1
        ],
    )
    def test_degenerate_input_fails_safe(self, quorum: object, members: int, expected: int) -> None:
        assert quorum_threshold(quorum, members) == expected

    def test_zero_members_never_divides_by_zero(self) -> None:
        assert quorum_threshold("2/3", 0) == 1


class TestSquadConfig:
    def test_loads_from_vendored_location(self, cfg: Config) -> None:
        squad = load_squads(cfg, "adversarial_review")
        assert squad.blocking is True
        assert squad.quorum == "2/3"
        assert squad.threshold == 2
        assert squad.members == (
            "security-adversary",
            "performance-adversary",
            "accessibility-adversary",
        )
        assert squad.blocking_severities == ("high", "critical")
        assert squad.source.endswith("squads.yaml")

    def test_falls_back_to_builtin_when_squad_absent(self, repo: Path) -> None:
        write_squads(repo, "version: 1\nsquads: {}\n")
        squad = load_squads(Config(root=repo, profile="full"), "adversarial_review")
        assert squad.threshold == 2
        assert "built-in defaults" in squad.source

    def test_unparseable_file_falls_back_without_raising(self, repo: Path) -> None:
        write_squads(repo, "version: 1\nsquads: [this is not a mapping\n")
        squad = load_squads(Config(root=repo, profile="full"), "adversarial_review")
        assert squad.threshold == 2
        assert squad.members


class TestAdversarialQuorum:
    def test_two_of_three_cited_blocks_fail_the_gate(self, cfg: Config, run_dir: Path) -> None:
        write_review(run_dir, "security-adversary", "block", [("critical", "IDOR", CITE)])
        write_review(run_dir, "performance-adversary", "block", [("high", "N+1", "src/api/list.ts:L61-L74")])
        write_review(run_dir, "accessibility-adversary", "pass", [])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "fail"
        assert result["required"] is True
        assert result["severity"] == "high"
        assert sorted(result["observed"]["blockingVotes"]) == [
            "performance-adversary",
            "security-adversary",
        ]
        assert result["observed"]["quorumMet"] is True
        # gates/<id>.json plus one run-relative path per verdict file.
        assert result["evidence"] == [
            "gates/adversarial_review.json",
            "reviews/adversarial_review.accessibility-adversary.md",
            "reviews/adversarial_review.performance-adversary.md",
            "reviews/adversarial_review.security-adversary.md",
        ]

    def test_one_of_three_does_not_reach_quorum(self, cfg: Config, run_dir: Path) -> None:
        write_review(run_dir, "security-adversary", "block", [("critical", "IDOR", CITE)])
        write_review(run_dir, "performance-adversary", "pass", [])
        write_review(run_dir, "accessibility-adversary", "abstain", [])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert result["observed"]["blockingVotes"] == ["security-adversary"]
        assert result["observed"]["quorumMet"] is False
        assert result["observed"]["abstained"] == ["accessibility-adversary"]

    def test_medium_severity_is_not_a_blocking_vote(self, cfg: Config, run_dir: Path) -> None:
        write_review(run_dir, "security-adversary", "block", [("medium", "nit", CITE)])
        write_review(run_dir, "performance-adversary", "block", [("low", "nit", CITE)])
        write_review(run_dir, "accessibility-adversary", "block", [("medium", "nit", CITE)])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert result["observed"]["blockingVotes"] == []
        assert len(result["observed"]["unsupportedBlockVerdicts"]) == 3

    def test_non_blocking_squad_never_fails(self, repo: Path, run_dir: Path) -> None:
        write_squads(
            repo,
            "version: 1\nsquads:\n  adversarial_review:\n    blocking: false\n    quorum: \"2/3\"\n"
            "    citation: file-line\n    members:\n      - id: security-adversary\n"
            "      - id: performance-adversary\n      - id: accessibility-adversary\n",
        )
        write_review(run_dir, "security-adversary", "block", [("critical", "IDOR", CITE)])
        write_review(run_dir, "performance-adversary", "block", [("high", "N+1", CITE)])

        result = AdversarialReviewGate().evaluate(make_run(), Config(root=repo, profile="full"))

        assert result["status"] == "pass"
        assert result["observed"]["quorumMet"] is True
        assert "non-blocking" in result["message"]

    def test_missing_member_is_reported_not_assumed_passing(self, cfg: Config, run_dir: Path) -> None:
        write_review(run_dir, "security-adversary", "pass", [])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert sorted(result["observed"]["membersMissing"]) == [
            "accessibility-adversary",
            "performance-adversary",
        ]
        assert result["severity"] == "medium"

    def test_reviews_from_another_squad_are_ignored(self, cfg: Config, run_dir: Path) -> None:
        write_review(run_dir, "security-adversary", "block", [("critical", "IDOR", CITE)])
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("high", "thin evidence", CITE)],
            squad="evidence_review",
        )

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["observed"]["reviewsFound"] == 1
        assert result["observed"]["blockingVotes"] == ["security-adversary"]

    def test_malformed_review_is_recorded_not_fatal(self, cfg: Config, run_dir: Path) -> None:
        (run_dir / "reviews" / "adversarial_review.broken.md").write_text(
            "no frontmatter here at all\n", encoding="utf-8"
        )
        write_review(run_dir, "security-adversary", "block", [("critical", "IDOR", CITE)])
        write_review(run_dir, "performance-adversary", "block", [("high", "N+1", CITE)])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "fail"
        assert any(e["error"] == "no YAML frontmatter" for e in result["observed"]["parseErrors"])

    def test_invalid_verdict_degrades_to_abstain(self, cfg: Config, run_dir: Path) -> None:
        path = run_dir / "reviews" / "adversarial_review.security-adversary.md"
        path.write_text(
            "---\nsquad: adversarial_review\nmember: security-adversary\n"
            "verdict: SHIP IT\n---\n\n## [critical] IDOR\n`" + CITE + "`\n",
            encoding="utf-8",
        )
        write_review(run_dir, "performance-adversary", "block", [("high", "N+1", CITE)])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert "security-adversary" in result["observed"]["abstained"]
