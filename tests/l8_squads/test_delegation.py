"""EvidenceReviewGate delegates the blocking check; it never recomputes it.

The deterministic coverage check lives in the spine's `evidence_completeness`
gate (`adlc.adapters.gate.evidence_completeness`) and is the only thing allowed
to block on coverage. These tests pin the *boundary*: this gate reads that
gate's recorded verdict, and its own advisory verdict is capped at a warning.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from adlc.adapters.gate import evidence_completeness, evidence_review
from adlc.adapters.gate.evidence_review import (
    DETERMINISTIC_GATE_ID,
    EvidenceReviewGate,
    read_precondition,
)
from adlc.config import Config
from adlc.runs import RunDir

from .l8_fixtures import (
    make_pack,
    make_run,
    sha,
    write_pack,
    write_precondition,
    write_review,
)


class TestNoDuplication:
    """The deterministic check must exist in exactly one place."""

    def test_the_blocking_gate_is_the_spine_gate(self) -> None:
        assert evidence_completeness.EvidenceCompletenessGate.id == DETERMINISTIC_GATE_ID

    def test_this_gate_does_not_reimplement_hash_verification(self) -> None:
        source = inspect.getsource(evidence_review)
        # `sha256_file` / rglob over evidence/ is how evidence_completeness
        # verifies hashes. If either appears here, the check has been forked.
        assert "sha256_file" not in source
        assert "evidence_dir" not in source

    def test_the_spine_gate_still_blocks_by_default(self) -> None:
        assert evidence_completeness.EvidenceCompletenessGate.required_by_default is True
        assert EvidenceReviewGate.required_by_default is False


class TestReadPrecondition:
    def test_reads_the_gate_file_from_disk(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "pass")
        rd = RunDir(cfg, "2026-08-19-t3st")
        result = read_precondition(rd, make_run())
        assert result is not None
        assert result["status"] == "pass"

    def test_falls_back_to_the_reduced_run_json(self, cfg: Config) -> None:
        rd = RunDir(cfg, "2026-08-19-t3st")
        run = make_run()
        run["gates"] = [{"id": DETERMINISTIC_GATE_ID, "status": "fail", "message": "from run.json"}]
        result = read_precondition(rd, run)
        assert result is not None
        assert result["message"] == "from run.json"

    def test_disk_wins_over_run_json(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "pass", message="from disk")
        run = make_run()
        run["gates"] = [{"id": DETERMINISTIC_GATE_ID, "status": "fail", "message": "stale"}]
        result = read_precondition(RunDir(cfg, "2026-08-19-t3st"), run)
        assert result is not None
        assert result["message"] == "from disk"

    def test_returns_none_when_absent(self, cfg: Config, run_dir: Path) -> None:
        assert read_precondition(RunDir(cfg, "2026-08-19-t3st"), make_run()) is None

    def test_corrupt_gate_file_does_not_raise(self, cfg: Config, run_dir: Path) -> None:
        (run_dir / "gates" / f"{DETERMINISTIC_GATE_ID}.json").write_text("{ nope", encoding="utf-8")
        assert read_precondition(RunDir(cfg, "2026-08-19-t3st"), make_run()) is None


class TestBlockingIsDelegated:
    def test_precondition_absent_is_not_run(self, cfg: Config, run_dir: Path) -> None:
        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"
        assert result["required"] is True  # required + not_run => aggregate fails closed
        assert DETERMINISTIC_GATE_ID in result["message"]

    def test_precondition_not_run_propagates_as_not_run(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "not_run")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"
        assert "did not run" in result["message"]
        assert result["observed"]["preconditionStatus"] == "not_run"

    def test_precondition_fail_fails_and_names_the_owning_gate(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_precondition(run_dir, "fail")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "fail"
        assert result["severity"] == "high"
        assert f"`{DETERMINISTIC_GATE_ID}`" in result["message"]

    def test_a_passing_squad_cannot_rescue_a_failed_precondition(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_precondition(run_dir, "fail")
        write_pack(run_dir, make_pack())
        write_review(run_dir, "requirements-auditor", "pass", [], squad="evidence_review")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "fail", "an LLM `pass` must never rescue a red deterministic check"

    def test_squad_verdicts_are_not_even_read_when_the_precondition_failed(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_precondition(run_dir, "fail")
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("high", "concern", sha("US1-AC1"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "fail"
        assert "advisory" not in result["observed"]


class TestAdvisoryIsCapped:
    def test_passing_precondition_with_no_squad_passes(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "pass")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert result["observed"]["advisory"]["verdict"] == "not_run"
        assert "advisory squad did not run" in result["message"]

    def test_squad_quorum_downgrades_to_warn_but_never_fails(
        self, cfg: Config, run_dir: Path
    ) -> None:
        write_precondition(run_dir, "pass")
        write_pack(run_dir, make_pack())
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("critical", "evidence does not demonstrate US1-AC2", sha("US1-AC2"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass", "the advisory half must never fail the build"
        assert result["message"].startswith("WARN:")
        assert result["severity"] == "medium"
        assert result["observed"]["advisory"]["quorumMet"] is True

    def test_squad_with_no_quorum_passes_quietly(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "pass")
        write_pack(run_dir, make_pack())
        write_review(run_dir, "requirements-auditor", "pass", [], squad="evidence_review")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert not result["message"].startswith("WARN:")
        assert result["observed"]["advisory"]["quorumMet"] is False

    def test_verdict_is_discarded_when_citations_cannot_be_screened(
        self, cfg: Config, run_dir: Path
    ) -> None:
        # Precondition passed but the pack is gone, so no citation can be
        # checked. An unscreenable verdict must not be trusted.
        write_precondition(run_dir, "pass")
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("critical", "concern", sha("US1-AC2"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert not result["message"].startswith("WARN:")
        assert "could not be screened" in result["message"]
        assert result["observed"]["advisory"]["verdict"] == "not_run"

    def test_unparseable_pack_discards_the_verdict(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "pass")
        (run_dir / "evidence-review-pack.json").write_text("{ not json", encoding="utf-8")
        write_review(
            run_dir,
            "requirements-auditor",
            "warn",
            [("critical", "concern", sha("US1-AC2"))],
            squad="evidence_review",
        )

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "pass"
        assert "not valid JSON" in result["message"]


class TestResultShape:
    def test_run_without_run_id_is_not_run(self, cfg: Config) -> None:
        result = EvidenceReviewGate().evaluate({}, cfg)
        assert result["status"] == "not_run"
        assert "runId" in result["message"]

    def test_evidence_paths_are_run_relative(self, cfg: Config, run_dir: Path) -> None:
        write_precondition(run_dir, "pass")
        write_pack(run_dir, make_pack())
        write_review(run_dir, "requirements-auditor", "pass", [], squad="evidence_review")

        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert "gates/evidence_review.json" in result["evidence"]
        assert "reviews/evidence_review.requirements-auditor.md" in result["evidence"]
        assert all(not Path(e).is_absolute() for e in result["evidence"])
