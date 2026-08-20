"""Citation-or-discard: an unfalsifiable claim must never gate a merge."""

from __future__ import annotations

from pathlib import Path

import pytest
from l8_fixtures import make_pack, make_run, sha, write_pack, write_review

from adlc.adapters.gate.adversarial_review import (
    ARTIFACT_SHA_RE,
    FILE_LINE_CITATION_RE,
    AdversarialReviewGate,
    parse_review,
)
from adlc.adapters.gate.evidence_review import EvidenceReviewGate
from adlc.config import Config

CITE = "src/api/documents.ts:L88-L104"


class TestFileLineCitationShape:
    @pytest.mark.parametrize(
        "text",
        [
            "`src/api/documents.ts:L88-L104`",
            "src/api/documents.ts:L88-104",
            "src/app.py:L12",
            "packages/ui/src/Drawer.tsx:L1-L2",
            "a-b_c/d.e2e.ts:L9",
        ],
    )
    def test_accepts_file_and_line(self, text: str) -> None:
        assert FILE_LINE_CITATION_RE.search(text) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "src/api/documents.ts",          # a bare path is not evidence
            "the documents module",
            "line 88",
            "src/api/documents.ts:88",       # no L prefix -> ambiguous, rejected
            "L88",
        ],
    )
    def test_rejects_everything_weaker(self, text: str) -> None:
        assert FILE_LINE_CITATION_RE.search(text) is None


class TestArtifactShaCitationShape:
    def test_accepts_a_bare_64_hex_digest(self) -> None:
        assert ARTIFACT_SHA_RE.search(f"artifactSha256 {sha('x')}") is not None

    @pytest.mark.parametrize(
        "text",
        [
            "artifactSha256 3f1c9a",                     # truncated
            "artifactSha256 " + "z" * 64,                # not hex
            "artifactSha256 " + sha("x").upper(),        # uppercase is not the digest form we emit
            "sha256:" + "0" * 63,                        # wrong length
        ],
    )
    def test_rejects_everything_else(self, text: str) -> None:
        assert ARTIFACT_SHA_RE.search(text) is None


class TestParseReview:
    def test_splits_findings_and_captures_citations(self, run_dir: Path) -> None:
        path = write_review(
            run_dir,
            "security-adversary",
            "block",
            [("critical", "IDOR", CITE), ("medium", "verbose log", "src/log.ts:L4")],
        )
        review = parse_review(path)
        assert review.verdict == "block"
        assert review.member == "security-adversary"
        assert [f.severity for f in review.findings] == ["critical", "medium"]
        assert review.cited_findings == review.findings
        assert review.uncited_findings == []

    def test_uncited_finding_is_separable(self, run_dir: Path) -> None:
        path = write_review(
            run_dir,
            "security-adversary",
            "block",
            [("critical", "feels insecure", "")],
        )
        review = parse_review(path)
        assert review.uncited_findings and not review.cited_findings
        assert review.blocking_findings(("high", "critical")) == []

    def test_missing_file_does_not_raise(self, run_dir: Path) -> None:
        review = parse_review(run_dir / "reviews" / "nope.md")
        assert review.parse_error.startswith("unreadable")
        assert review.verdict == "abstain"


class TestAdversarialCitationEnforcement:
    def test_uncited_blocks_cannot_reach_quorum(self, cfg: Config, run_dir: Path) -> None:
        # Three members all shout `block` at `critical` -- but cite nothing.
        for member in ("security-adversary", "performance-adversary", "accessibility-adversary"):
            write_review(run_dir, member, "block", [("critical", "this feels wrong", "")])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass", "uncited claims must never fail a build"
        assert result["observed"]["blockingVotes"] == []
        assert len(result["observed"]["discardedFindings"]) == 3
        assert all(
            d["reason"] == "no file-line citation" for d in result["observed"]["discardedFindings"]
        )
        assert len(result["observed"]["unsupportedBlockVerdicts"]) == 3

    def test_one_cited_finding_rescues_a_members_vote(self, cfg: Config, run_dir: Path) -> None:
        write_review(
            run_dir,
            "security-adversary",
            "block",
            [("critical", "uncited", ""), ("high", "cited", CITE)],
        )
        write_review(run_dir, "performance-adversary", "block", [("high", "N+1", "src/list.ts:L3")])
        write_review(run_dir, "accessibility-adversary", "pass", [])

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "fail"
        assert len(result["observed"]["discardedFindings"]) == 1
        assert sorted(result["observed"]["blockingVotes"]) == [
            "performance-adversary",
            "security-adversary",
        ]


class TestEvidenceCitationEnforcement:
    def _sound_run(self, run_dir: Path) -> tuple[dict, dict]:
        pack = make_pack()
        write_pack(run_dir, pack)
        run = make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")])
        return pack, run

    def test_cited_concern_downgrades_to_warn_but_never_fails(
        self, cfg: Config, run_dir: Path
    ) -> None:
        _, run = self._sound_run(run_dir)
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("high", "screenshot cannot demonstrate a timing property", sha("US1-AC2"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(run, cfg)

        assert result["status"] == "pass"
        assert result["message"].startswith("WARN:")
        assert result["severity"] == "medium"
        assert result["observed"]["advisory"]["quorumMet"] is True

    def test_uncited_concern_is_discarded(self, cfg: Config, run_dir: Path) -> None:
        _, run = self._sound_run(run_dir)
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("high", "the evidence feels thin", "")],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(run, cfg)

        assert result["status"] == "pass"
        assert not result["message"].startswith("WARN:")
        assert result["observed"]["advisory"]["quorumMet"] is False
        assert len(result["observed"]["advisory"]["discardedFindings"]) == 1

    def test_hash_absent_from_the_pack_is_treated_as_fabricated(
        self, cfg: Config, run_dir: Path
    ) -> None:
        _, run = self._sound_run(run_dir)
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("high", "invented evidence", sha("this-hash-is-not-in-the-pack"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(run, cfg)

        advisory = result["observed"]["advisory"]
        assert result["status"] == "pass"
        assert advisory["quorumMet"] is False, "a fabricated hash must not carry a verdict"
        assert len(advisory["fabricatedCitations"]) == 1
        assert "cited a hash absent from the pack" in result["message"]
