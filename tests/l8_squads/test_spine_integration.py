"""End-to-end integration with the spine's gate stage.

These tests do not fixture the boundary -- they run the spine's real
``run_gates`` over a real run directory, let the spine's real
``evidence_completeness`` gate produce its verdict, and assert that the L8 gates
plug in correctly on top of it.

That matters because every other L8 test stubs the precondition. This is the one
that would catch the delegation contract drifting.
"""

from __future__ import annotations

import json
from pathlib import Path

from adlc.config import Config
from adlc.reduce import aggregate_passed, collect_gates
from adlc.runs import RunDir, sha256_file
from adlc.stages.gates import available_gates, run_gates

from .l8_fixtures import write_review

SQUAD_IDS = ("adversarial_review", "evidence_review")


def _seed_run(repo: Path) -> tuple[Config, RunDir]:
    cfg = Config(root=repo, profile="full")
    rd = RunDir(cfg, "2026-08-19-t3st")
    rd.create(profile="full", brief_text="# Integration\n")
    return cfg, rd


def _plant_evidence(rd: RunDir, *, honest: bool = True) -> str:
    """Write one real evidence file and a pack that does (or does not) match it."""
    artifact = rd.evidence_dir / "candidate-a" / "trace.zip"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"PK\x03\x04 pretend playwright trace")
    digest = sha256_file(artifact)
    cited = digest if honest else "f" * 64

    rd.review_pack.write_text(
        json.dumps(
            {
                "runId": rd.run_id,
                "candidateSha": "cafebabe",
                "workflowRunId": None,
                "collector": "adlc/0.1.0",
                "requirements": [
                    {"id": "US1-AC1", "text": "the thing happens", "source": "spec/spec.md"}
                ],
                "measurements": [],
                "coverage": [
                    {
                        "requirementId": "US1-AC1",
                        "evidenceKinds": ["playwright_trace"],
                        "artifactSha256": [cited],
                        "present": True,
                    }
                ],
                "screenshots": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return digest


class TestRegistration:
    def test_both_squad_gates_are_discoverable_by_the_spine(self) -> None:
        registry = available_gates()
        for gate_id in SQUAD_IDS:
            assert gate_id in registry, f"{gate_id} entry point did not load"
            assert registry[gate_id].id == gate_id

    def test_both_are_required_in_the_full_profile(self, repo: Path) -> None:
        cfg = Config(root=repo, profile="full")
        for gate_id in SQUAD_IDS:
            assert cfg.is_required(gate_id) is True

    def test_neither_is_required_in_the_minimal_profile(self, repo: Path) -> None:
        cfg = Config(root=repo, profile="minimal")
        for gate_id in SQUAD_IDS:
            assert cfg.is_required(gate_id) is False


class TestDelegationAgainstTheRealSpineGate:
    def test_real_passing_coverage_lets_the_advisory_gate_pass(self, repo: Path) -> None:
        cfg, rd = _seed_run(repo)
        _plant_evidence(rd, honest=True)

        run_gates(cfg, rd, ["evidence_completeness", "evidence_review"])

        completeness = json.loads((rd.gates_dir / "evidence_completeness.json").read_text())
        review = json.loads((rd.gates_dir / "evidence_review.json").read_text())

        assert completeness["status"] == "pass", completeness["message"]
        assert review["status"] == "pass"
        assert review["observed"]["preconditionStatus"] == "pass"
        assert review["observed"]["advisory"]["verdict"] == "not_run"

    def test_real_failing_coverage_propagates_to_the_advisory_gate(self, repo: Path) -> None:
        cfg, rd = _seed_run(repo)
        # The pack cites a hash no file on disk hashes to. This is the exact
        # thing evidence_completeness exists to catch.
        _plant_evidence(rd, honest=False)

        run_gates(cfg, rd, ["evidence_completeness", "evidence_review"])

        completeness = json.loads((rd.gates_dir / "evidence_completeness.json").read_text())
        review = json.loads((rd.gates_dir / "evidence_review.json").read_text())

        assert completeness["status"] == "fail"
        assert review["status"] == "fail"
        assert "evidence_completeness" in review["message"]

    def test_a_warning_squad_verdict_still_leaves_the_aggregate_green(self, repo: Path) -> None:
        cfg, rd = _seed_run(repo)
        digest = _plant_evidence(rd, honest=True)
        write_review(
            rd.path,
            "requirements-auditor",
            "warn",
            [("critical", "this evidence cannot demonstrate US1-AC1", digest)],
            squad="evidence_review",
        )

        run_gates(cfg, rd, ["evidence_completeness", "evidence_review"])
        review = json.loads((rd.gates_dir / "evidence_review.json").read_text())

        assert review["status"] == "pass"
        assert review["message"].startswith("WARN:")
        # And the spine's own aggregate must agree: advisory never blocks.
        passed, failures = aggregate_passed(
            [g for g in collect_gates(rd, cfg) if g["id"] in ("evidence_completeness", "evidence_review")]
        )
        assert passed is True, failures


class TestAdversarialThroughTheSpine:
    def test_quorum_failure_surfaces_in_the_spine_aggregate(self, repo: Path) -> None:
        cfg, rd = _seed_run(repo)
        write_review(rd.path, "security-adversary", "block", [("critical", "IDOR", "src/a.ts:L1-L9")])
        write_review(rd.path, "performance-adversary", "block", [("high", "N+1", "src/b.ts:L4")])
        write_review(rd.path, "accessibility-adversary", "pass", [])

        result = run_gates(cfg, rd, ["adversarial_review"])

        assert result["passed"] is False
        assert any("adversarial_review: FAIL" in f for f in result["failures"])

    def test_no_verdicts_becomes_a_fail_closed_not_run(self, repo: Path) -> None:
        cfg, rd = _seed_run(repo)

        result = run_gates(cfg, rd, ["adversarial_review"])
        gate = json.loads((rd.gates_dir / "adversarial_review.json").read_text())

        assert gate["status"] == "not_run"
        assert result["passed"] is False
        assert any("adversarial_review: NOT_RUN" in f for f in result["failures"])

    def test_no_verdicts_is_harmless_under_the_minimal_profile(self, repo: Path) -> None:
        cfg = Config(root=repo, profile="minimal")
        rd = RunDir(cfg, "2026-08-19-t3st")
        rd.create(profile="minimal", brief_text="# Integration\n")

        run_gates(cfg, rd, ["adversarial_review"])
        gates = [g for g in collect_gates(rd, cfg) if g["id"] == "adversarial_review"]

        assert gates[0]["status"] == "not_run"
        assert gates[0]["required"] is False
        assert aggregate_passed(gates)[0] is True


class TestUnavailableDegradesNotCrashes:
    def test_missing_squads_config_becomes_not_run_via_the_spine(self, tmp_path: Path) -> None:
        # No .adlc/squads.yaml and no templates/.adlc/squads.yaml, so detect()
        # reports unavailable and run_gates must synthesise not_run.
        cfg = Config(root=tmp_path, profile="full")
        rd = RunDir(cfg, "2026-08-19-t3st")
        rd.create(profile="full", brief_text="# Integration\n")

        run_gates(cfg, rd, list(SQUAD_IDS))

        for gate_id in SQUAD_IDS:
            gate = json.loads((rd.gates_dir / f"{gate_id}.json").read_text())
            assert gate["status"] == "not_run"
            assert "gate unavailable" in gate["message"]
            assert "squads.yaml" in gate["message"]

    def test_neither_gate_ever_raises_through_the_spine(self, repo: Path) -> None:
        cfg, rd = _seed_run(repo)
        # Deliberately hostile inputs: a corrupt precondition, a review with no
        # frontmatter, and a pack that is not JSON.
        (rd.gates_dir / "evidence_completeness.json").write_text("{ corrupt", encoding="utf-8")
        (rd.reviews_dir / "adversarial_review.broken.md").write_text("garbage", encoding="utf-8")
        rd.review_pack.write_text("{ also corrupt", encoding="utf-8")

        run_gates(cfg, rd, list(SQUAD_IDS))

        for gate_id in SQUAD_IDS:
            gate = json.loads((rd.gates_dir / f"{gate_id}.json").read_text())
            # `run_gates` converts a raising gate into this message. Its absence
            # is the assertion: these gates degrade, they do not explode.
            assert "raised" not in gate["message"], gate["message"]
            assert gate["status"] in ("pass", "fail", "not_run")
