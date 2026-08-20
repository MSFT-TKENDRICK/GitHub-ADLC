"""`detect()` contract and the credential-free `not_run` degrade path.

Nothing here touches the network, spawns a subprocess, or needs a secret.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from l8_fixtures import make_run, write_squads

from adlc.adapters.gate.adversarial_review import AdversarialReviewGate
from adlc.adapters.gate.evidence_review import EvidenceReviewGate
from adlc.config import Config
from adlc.ports import GATE_IDS, GateRunner

GATES = (AdversarialReviewGate, EvidenceReviewGate)


@pytest.mark.parametrize("gate_cls", GATES, ids=lambda c: c.id)
class TestAdapterContract:
    def test_declared_id_is_known_to_the_framework(self, gate_cls: type) -> None:
        assert gate_cls.id in GATE_IDS

    def test_is_optional_by_default(self, gate_cls: type) -> None:
        assert gate_cls.required_by_default is False

    def test_satisfies_the_gate_runner_protocol(self, gate_cls: type) -> None:
        assert isinstance(gate_cls(), GateRunner)

    def test_kind_is_gate(self, gate_cls: type) -> None:
        assert gate_cls.kind == "gate"

    def test_detect_reports_unavailable_with_a_specific_reason(
        self, gate_cls: type, tmp_path: Path
    ) -> None:
        available, reason = gate_cls.detect(Config(root=tmp_path))
        assert available is False
        # The reason is surfaced verbatim in capabilities.json, so it must name
        # exactly where we looked.
        assert ".adlc/squads.yaml" in reason
        assert "templates/.adlc/squads.yaml" in reason

    def test_detect_reports_available_from_the_vendored_location(
        self, gate_cls: type, cfg: Config
    ) -> None:
        available, reason = gate_cls.detect(cfg)
        assert available is True
        assert reason.endswith("squads.yaml")

    def test_detect_reports_available_from_the_template_location(
        self, gate_cls: type, tmp_path: Path
    ) -> None:
        write_squads(tmp_path, "version: 1\nsquads: {}\n", location="templates")
        available, reason = gate_cls.detect(Config(root=tmp_path))
        assert available is True
        assert "templates" in reason

    def test_detect_does_not_raise_on_a_nonexistent_root(self, gate_cls: type) -> None:
        available, reason = gate_cls.detect(Config(root=Path("/definitely/not/here/at/all")))
        assert available is False
        assert reason

    def test_detect_does_not_raise_when_squads_path_is_a_directory(
        self, gate_cls: type, tmp_path: Path
    ) -> None:
        (tmp_path / ".adlc" / "squads.yaml").mkdir(parents=True)
        available, _ = gate_cls.detect(Config(root=tmp_path))
        assert available is False


class TestCredentialFreeDegradation:
    """No verdict files anywhere must produce `not_run` with a reason, never `pass`."""

    def test_adversarial_reports_not_run_with_no_verdict_files(
        self, cfg: Config, run_dir: Path
    ) -> None:
        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"
        assert result["observed"]["reviewsFound"] == 0
        assert "no adversarial_review verdict files" in result["message"]
        assert result["evidence"] == []

    def test_adversarial_reports_not_run_when_reviews_dir_is_absent(
        self, cfg: Config, repo: Path
    ) -> None:
        (repo / ".adlc" / "runs" / "2026-08-19-t3st").mkdir(parents=True)

        result = AdversarialReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"

    def test_evidence_reports_not_run_with_no_pack(self, cfg: Config, run_dir: Path) -> None:
        result = EvidenceReviewGate().evaluate(make_run(), cfg)

        assert result["status"] == "not_run"
        assert "not found" in result["message"]

    def test_adversarial_reports_not_run_without_a_run_id(self, cfg: Config) -> None:
        result = AdversarialReviewGate().evaluate({}, cfg)

        assert result["status"] == "not_run"
        assert "runId" in result["message"]

    @pytest.mark.parametrize("gate_cls", GATES, ids=lambda c: c.id)
    def test_not_run_still_reports_required_from_the_profile(
        self, gate_cls: type, cfg: Config, run_dir: Path
    ) -> None:
        # `full` profile marks both squads required, so `not_run` must be
        # reported as required and let the aggregator fail closed.
        result = gate_cls().evaluate(make_run(), cfg)
        assert result["status"] == "not_run"
        assert result["required"] is True

    @pytest.mark.parametrize("gate_cls", GATES, ids=lambda c: c.id)
    def test_minimal_profile_marks_the_squads_optional(
        self, gate_cls: type, repo: Path, run_dir: Path
    ) -> None:
        result = gate_cls().evaluate(make_run(), Config(root=repo, profile="minimal"))
        assert result["status"] == "not_run"
        assert result["required"] is False

    @pytest.mark.parametrize("gate_cls", GATES, ids=lambda c: c.id)
    def test_evaluate_never_raises_without_a_squads_file(
        self, gate_cls: type, tmp_path: Path
    ) -> None:
        # detect() says unavailable, but a caller may still evaluate. It must
        # degrade, not explode.
        result = gate_cls().evaluate(make_run(), Config(root=tmp_path, profile="full"))
        assert result["status"] == "not_run"
        assert result["message"]

    @pytest.mark.parametrize("gate_cls", GATES, ids=lambda c: c.id)
    def test_result_shape_matches_the_gate_result_contract(
        self, gate_cls: type, cfg: Config, run_dir: Path
    ) -> None:
        result = gate_cls().evaluate(make_run(), cfg)
        assert set(result) >= {
            "id", "required", "status", "severity", "observed", "expected", "message", "evidence",
        }
        assert result["id"] == gate_cls.id
        assert result["status"] in ("pass", "fail", "not_run")
        assert result["severity"] in ("low", "medium", "high", "critical")
        assert isinstance(result["evidence"], list)
        assert isinstance(result["message"], str) and result["message"]
