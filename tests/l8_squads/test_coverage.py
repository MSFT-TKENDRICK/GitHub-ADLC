"""The deterministic coverage check is the only thing allowed to block."""

from __future__ import annotations

from pathlib import Path

from l8_fixtures import make_pack, make_run, sha, write_pack, write_review

from adlc.adapters.gate.evidence_review import EvidenceReviewGate, check_coverage
from adlc.config import Config


class TestCheckCoverage:
    def test_complete_and_hash_verified_passes(self) -> None:
        report = check_coverage(
            make_pack(),
            make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")]),
        )
        assert report.ok is True
        assert report["requirementsSatisfied"] == ["US1-AC1", "US1-AC2"]
        assert report["problems"] == []

    def test_requirement_with_no_coverage_entry_fails(self) -> None:
        pack = make_pack(
            coverage=[
                {
                    "requirementId": "US1-AC1",
                    "evidenceKinds": ["playwright_trace"],
                    "artifactSha256": [sha("US1-AC1")],
                    "present": True,
                }
            ]
        )
        report = check_coverage(pack, make_run(artifact_hashes=[sha("US1-AC1")]))
        assert report.ok is False
        assert {"scope": "US1-AC2", "reason": "no coverage entry"} in report["problems"]

    def test_present_false_fails(self) -> None:
        pack = make_pack()
        pack["coverage"][1]["present"] = False
        report = check_coverage(pack, make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")]))
        assert report.ok is False
        assert any("present: false" in p["reason"] for p in report["problems"])

    def test_hash_not_recorded_in_run_artifacts_fails(self) -> None:
        # The pack claims evidence the run never produced. This is the whole
        # point of hash verification: the pack is not self-certifying.
        report = check_coverage(make_pack(), make_run(artifact_hashes=[sha("US1-AC1")]))
        assert report.ok is False
        assert any("not present in run.json artifacts[]" in p["reason"] for p in report["problems"])

    def test_candidate_sha_mismatch_fails(self) -> None:
        report = check_coverage(
            make_pack(candidate_sha="0ldc0mmit"),
            make_run(head_sha="cafebabe", artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")]),
        )
        assert report.ok is False
        assert any("does not match run headSha" in p["reason"] for p in report["problems"])

    def test_missing_collector_fails(self) -> None:
        report = check_coverage(
            make_pack(collector=""),
            make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")]),
        )
        assert report.ok is False
        assert any(p["reason"] == "pack declares no collector" for p in report["problems"])

    def test_malformed_hash_fails(self) -> None:
        pack = make_pack()
        pack["coverage"][0]["artifactSha256"] = ["not-a-sha"]
        report = check_coverage(pack, make_run(artifact_hashes=[sha("US1-AC2")]))
        assert report.ok is False
        assert any("malformed artifactSha256" in p["reason"] for p in report["problems"])

    def test_empty_requirements_is_not_a_pass(self) -> None:
        report = check_coverage(make_pack(requirements=[], coverage=[]), make_run())
        assert report.ok is False, "verifying nothing must never look like verifying everything"

    def test_orphan_coverage_entry_is_reported(self) -> None:
        pack = make_pack()
        pack["coverage"].append(
            {"requirementId": "GHOST-1", "artifactSha256": [sha("g")], "present": True}
        )
        report = check_coverage(pack, make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")]))
        assert report.ok is False
        assert any("unknown requirementId" in p["reason"] for p in report["problems"])

    def test_hash_verification_can_be_relaxed_by_config(self) -> None:
        report = check_coverage(
            make_pack(),
            make_run(artifact_hashes=[]),
            {"requireHashVerification": False, "requireShaMatch": True},
        )
        assert report.ok is True

    def test_min_artifacts_per_requirement_is_honoured(self) -> None:
        report = check_coverage(
            make_pack(),
            make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")]),
            {"minArtifactsPerRequirement": 2},
        )
        assert report.ok is False
        assert any("2 required" in p["reason"] for p in report["problems"])


class TestEvidenceGateBlocking:
    def test_incomplete_coverage_fails_even_with_a_passing_squad(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_pack(run_dir, make_pack())
        run = make_run(artifact_hashes=[sha("US1-AC1")])  # US1-AC2 unverifiable
        write_review(
            run_dir,
            "requirements-auditor",
            "pass",
            [],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(run, cfg)

        assert result["status"] == "fail", "an LLM `pass` must never rescue missing coverage"
        assert result["severity"] == "high"
        assert result["required"] is True
        assert "deterministic evidence coverage failed" in result["message"]

    def test_complete_coverage_passes_with_no_squad_at_all(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_pack(run_dir, make_pack())
        run = make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")])

        result = EvidenceReviewGate().evaluate(run, cfg)

        assert result["status"] == "pass"
        assert result["observed"]["advisory"]["verdict"] == "not_run"
        assert "advisory squad did not run" in result["message"]

    def test_squad_verdict_can_only_downgrade_to_warn(self, cfg: Config, run_dir: Path) -> None:
        write_pack(run_dir, make_pack())
        run = make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")])
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("critical", "evidence does not demonstrate US1-AC2", sha("US1-AC2"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(run, cfg)

        assert result["status"] == "pass", "the advisory half must never fail the build"
        assert result["message"].startswith("WARN:")

    def test_missing_pack_is_not_run_not_pass(self, cfg: Config, run_dir: Path) -> None:
        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"
        assert "evidence review pack unavailable" in result["message"]
        assert result["required"] is True  # required + not_run => the aggregator fails closed

    def test_unparseable_pack_is_not_run(self, cfg: Config, run_dir: Path) -> None:
        (run_dir / "evidence-review-pack.json").write_text("{ not json", encoding="utf-8")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"
        assert "not valid JSON" in result["message"]

    def test_run_without_run_id_is_not_run(self, cfg: Config) -> None:
        result = EvidenceReviewGate().evaluate({}, cfg)
        assert result["status"] == "not_run"
        assert "runId" in result["message"]

    def test_evidence_paths_are_reported(self, cfg: Config, run_dir: Path) -> None:
        write_pack(run_dir, make_pack())
        run = make_run(artifact_hashes=[sha("US1-AC1"), sha("US1-AC2")])
        write_review(run_dir, "requirements-auditor", "pass", [], squad="evidence_review")

        result = EvidenceReviewGate().evaluate(run, cfg)

        assert any(e.endswith("evidence-review-pack.json") for e in result["evidence"])
        assert any(e.endswith("evidence_review.requirements-auditor.md") for e in result["evidence"])
