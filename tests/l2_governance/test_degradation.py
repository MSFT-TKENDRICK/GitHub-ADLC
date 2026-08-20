"""The degradation path: no credentials, no MAF, no AGT — and nothing crashes.

`CONTRIBUTING.md` rules 4, 5 and 6. These tests must pass on a machine where
neither preview package is installed *and* on one where both are, so they never
assert "the import fails" — they assert the shape of the contract.
"""

from __future__ import annotations

import pytest

from adlc.adapters.agents.maf_governed import MafGovernedRunner
from adlc.adapters.gate.governance import GovernanceGate
from adlc.config import Config
from adlc.maf import detect_agt, detect_governance, detect_maf, middleware

DETECTORS = (detect_maf, detect_agt, detect_governance)


class TestDetectContract:
    @pytest.mark.parametrize("detector", DETECTORS)
    def test_returns_available_reason_pair(self, detector, cfg: Config) -> None:
        available, reason = detector(cfg)
        assert isinstance(available, bool)
        assert isinstance(reason, str)
        assert reason.strip(), "reason is surfaced verbatim in capabilities.json"

    @pytest.mark.parametrize("detector", DETECTORS)
    def test_never_raises_without_config(self, detector) -> None:
        assert isinstance(detector(None)[0], bool)

    @pytest.mark.parametrize("detector", DETECTORS)
    def test_makes_no_network_call(self, detector, cfg: Config, monkeypatch) -> None:
        import socket

        def _boom(*args, **kwargs):  # pragma: no cover - only runs on failure
            raise AssertionError("detect() must not touch the network")

        monkeypatch.setattr(socket, "socket", _boom)
        monkeypatch.setattr(socket, "create_connection", _boom)
        detector(cfg)

    @pytest.mark.parametrize("detector", DETECTORS)
    def test_runs_no_subprocess(self, detector, cfg: Config, monkeypatch) -> None:
        import subprocess

        def _boom(*args, **kwargs):  # pragma: no cover - only runs on failure
            raise AssertionError("detect() must not spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", _boom)
        monkeypatch.setattr(subprocess, "Popen", _boom)
        detector(cfg)


class TestMissingDependencies:
    def test_maf_missing_names_the_install(self, no_optional_deps, cfg: Config) -> None:
        available, reason = detect_maf(cfg)
        assert available is False
        assert "agent_framework" in reason
        assert "adlc[governance]" in reason

    def test_agt_missing_names_the_install(self, no_agt_only, cfg: Config) -> None:
        available, reason = detect_agt(cfg)
        assert available is False
        assert "agent-governance-toolkit" in reason
        assert "adlc[governance]" in reason

    def test_governance_reports_the_first_missing_half(
        self, no_agt_only, cfg: Config
    ) -> None:
        available, reason = detect_governance(cfg)
        assert available is False
        assert "agent-governance-toolkit" in reason

    def test_missing_policy_is_specific(self, monkeypatch, bare_cfg: Config) -> None:
        monkeypatch.setattr(middleware, "_module_present", lambda name: True)
        monkeypatch.setattr(middleware, "TEMPLATE_POLICY", bare_cfg.root / "nope.yaml")
        available, reason = detect_governance(bare_cfg)
        assert available is False
        assert ".adlc/policy.yaml" in reason


class TestRunnerDegradation:
    def test_detect_pair(self, cfg: Config) -> None:
        available, reason = MafGovernedRunner.detect(cfg)
        assert isinstance(available, bool)
        assert reason.strip()

    def test_unavailable_without_deps(self, no_optional_deps, cfg: Config) -> None:
        available, reason = MafGovernedRunner.detect(cfg)
        assert available is False
        assert "agent_framework" in reason

    def test_unavailable_without_credentials(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(middleware, "_module_present", lambda name: True)
        for env in (
            "ADLC_MAF_CHAT_CLIENT",
            "AZURE_AI_PROJECT_ENDPOINT",
            "AZURE_OPENAI_ENDPOINT",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(env, raising=False)
        available, reason = MafGovernedRunner.detect(cfg)
        assert available is False
        assert "chat client" in reason

    @pytest.mark.asyncio
    async def test_run_task_fails_closed_rather_than_running_ungoverned(
        self, no_optional_deps, cfg: Config, tmp_path
    ) -> None:
        node = {"id": "T001", "title": "x", "writeSet": ["src/a.py"]}
        outcome = await MafGovernedRunner().run_task(node, tmp_path, cfg)
        assert outcome["status"] == "fail"
        assert "governance unavailable" in outcome["log"]
        # Patch production belongs to the spine's executor, so a runner outcome
        # never carries one.
        assert "patchPath" not in outcome

    def test_is_registered_as_an_agent_adapter(self) -> None:
        assert MafGovernedRunner.kind == "agents"
        assert MafGovernedRunner.name == "maf"


class TestGateDegradation:
    def test_identity(self) -> None:
        assert GovernanceGate.id == "governance"
        assert GovernanceGate.kind == "gate"
        assert GovernanceGate.required_by_default is False

    def test_detect_without_agt_cli(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        available, reason = GovernanceGate.detect(cfg)
        assert available is False
        assert "agt" in reason
        assert "adlc[governance]" in reason

    def test_detect_without_policy(self, monkeypatch, bare_cfg: Config) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/agt")
        monkeypatch.setattr(middleware, "TEMPLATE_POLICY", bare_cfg.root / "nope.yaml")
        available, reason = GovernanceGate.detect(bare_cfg)
        assert available is False
        assert "policy" in reason

    def test_missing_agt_yields_not_run_never_pass(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        result = GovernanceGate().evaluate({"runId": "r1"}, cfg)
        assert result["status"] == "not_run"
        assert result["status"] != "pass"
        assert "not verified" in result["message"]

    def test_required_not_run_is_high_severity(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        # `full` profile marks `governance` required, and required+not_run must
        # be loud enough for the aggregator to fail the build on it.
        assert cfg.is_required("governance") is True
        result = GovernanceGate().evaluate({"runId": "r1"}, cfg)
        assert result["required"] is True
        assert result["severity"] == "high"


class TestNoCredentialsAnywhere:
    """No test in this suite may depend on a secret."""

    def test_engine_load_without_agt_is_explicit(self, no_optional_deps, cfg: Config) -> None:
        with pytest.raises(middleware.GovernanceUnavailable):
            middleware.PolicyEngine.load(cfg, strict=True)

    def test_engine_load_non_strict_returns_none(self, bare_cfg: Config, monkeypatch) -> None:
        monkeypatch.setattr(middleware, "TEMPLATE_POLICY", bare_cfg.root / "nope.yaml")
        assert middleware.PolicyEngine.load(bare_cfg, strict=False) is None


class TestGovernWrapperIsNotAnAllowSource:
    """`govern()` alone must never be accepted as a pre-execution verdict.

    It decides at call time by wrapping the callable it is handed, so probing
    it with a stand-in asks about an action the policy never actually saw. On
    an allow-by-default policy that reports "allowed" for a call nobody
    inspected — a fail-open. The engine refuses instead.
    """

    def test_load_refuses_when_only_the_wrapper_is_present(
        self, monkeypatch, cfg: Config
    ) -> None:
        monkeypatch.setattr(middleware, "_load_acs_runtime", lambda policy: (None, ""))
        monkeypatch.setattr(
            middleware, "_load_govern_wrapper", lambda: (lambda fn, **kw: fn, RuntimeError)
        )
        with pytest.raises(middleware.GovernanceUnavailable) as excinfo:
            middleware.PolicyEngine.load(cfg, strict=True)
        assert "agent_control_specification" in str(excinfo.value)

    def test_engine_without_a_runtime_denies_rather_than_permits(self, cfg: Config) -> None:
        engine = middleware.PolicyEngine(policy_path=cfg.adlc_dir / "policy.yaml")
        decision = engine.check("write_file", {"path": "src/a.py"})
        assert decision.permits is False

    def test_no_probe_helper_remains(self) -> None:
        assert not hasattr(middleware, "_govern_probe")
