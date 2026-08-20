"""The ``governance`` gate: honest ``not_run``, correct pass/fail mapping.

Every AGT invocation is mocked. Nothing here shells out to a real ``agt``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.gate import governance as gate_module
from adlc.adapters.gate.governance import GovernanceGate
from adlc.config import Config

RUN = {"runId": "2026-08-19-a1b2"}


def fake_agt(results: dict[str, tuple[int, str, str]], *, calls: list[list[str]] | None = None):
    """Patch ``subprocess.run`` to answer ``agt`` invocations from a script."""

    def _run(argv, **kwargs: Any):
        if calls is not None:
            calls.append(list(argv))
        subcommand = argv[1] if len(argv) > 1 else ""
        code, out, err = results.get(subcommand, (0, "", ""))
        if code == "timeout":  # sentinel
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
        if code == "oserror":
            raise OSError("agt vanished mid-flight")
        return subprocess.CompletedProcess(argv, code, out, err)

    return _run


@pytest.fixture
def agt_on_path(monkeypatch):
    monkeypatch.setattr(gate_module.shutil, "which", lambda name: f"/usr/bin/{name}")


class TestNotRun:
    def test_missing_cli(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(gate_module.shutil, "which", lambda name: None)
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "not_run"
        assert result["observed"]["agtAvailable"] is False

    def test_timeout_is_not_run_not_fail(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": ("timeout", "", "")})
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "not_run"
        assert "did not complete" in result["message"]

    def test_oserror_is_not_run(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": ("oserror", "", "")})
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "not_run"

    def test_lint_that_cannot_run_is_not_run(
        self, agt_on_path, monkeypatch, cfg: Config
    ) -> None:
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (0, "ok", ""), "lint-policy": ("timeout", "", "")}),
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "not_run"

    def test_not_run_never_claims_pass(self, monkeypatch, cfg: Config) -> None:
        """The single most important assertion in this file."""
        monkeypatch.setattr(gate_module.shutil, "which", lambda name: None)
        for _ in range(3):
            assert GovernanceGate().evaluate(RUN, cfg)["status"] != "pass"


class TestPassFail:
    def test_clean_verify_and_lint_pass(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (0, "Grade: A", ""), "lint-policy": (0, "ok", "")}),
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "pass"
        assert result["severity"] == "low"

    def test_verify_nonzero_fails(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (2, "", "coverage 41% below strict threshold")}),
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "fail"
        assert result["severity"] == "high"
        assert "coverage 41%" in result["message"]

    def test_invalid_policy_fails(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (0, "", ""), "lint-policy": (1, "", "unknown key 'ruls'")}),
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "fail"
        assert "policy is invalid" in result["message"]

    def test_runtime_denials_fail_the_gate(
        self, agt_on_path, monkeypatch, cfg: Config
    ) -> None:
        decisions = cfg.run_dir(RUN["runId"]) / "gates" / "governance-decisions.json"
        decisions.parent.mkdir(parents=True, exist_ok=True)
        decisions.write_text(
            json.dumps({"engine": "acs", "policy": "p", "total": 7, "denied": 2}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")})
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["status"] == "fail"
        assert "blocked by policy" in result["message"]
        assert result["observed"]["runtimeDecisions"]["denied"] == 2


class TestInvocation:
    def test_uses_strict_and_writes_evidence_into_gates_dir(
        self, agt_on_path, monkeypatch, cfg: Config
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")}, calls=calls),
        )
        GovernanceGate().evaluate(RUN, cfg)

        verify = next(call for call in calls if call[1] == "verify")
        assert verify[0] == "agt"
        assert "--strict" in verify
        assert "--evidence" in verify
        evidence_path = Path(verify[verify.index("--evidence") + 1])
        assert evidence_path.parent == cfg.run_dir(RUN["runId"]) / "gates"

    def test_lints_the_resolved_policy(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")}, calls=calls),
        )
        GovernanceGate().evaluate(RUN, cfg)
        lint = next(call for call in calls if call[1] == "lint-policy")
        assert Path(lint[2]).name == "policy.yaml"

    def test_writes_the_gate_report(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")})
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        report = cfg.run_dir(RUN["runId"]) / "gates" / "governance.json"
        assert report.is_file()
        assert json.loads(report.read_text(encoding="utf-8"))["status"] == result["status"]
        assert "gates/governance.json" in result["evidence"]

    def test_reads_back_the_agt_attestation(
        self, agt_on_path, monkeypatch, cfg: Config
    ) -> None:
        gates = cfg.run_dir(RUN["runId"]) / "gates"
        gates.mkdir(parents=True, exist_ok=True)

        def _run(argv, **kwargs: Any):
            if argv[1] == "verify":
                Path(argv[argv.index("--evidence") + 1]).write_text(
                    json.dumps({"grade": "A", "coverage_pct": 96}), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(gate_module.subprocess, "run", _run)
        result = GovernanceGate().evaluate(RUN, cfg)
        assert result["observed"]["attestation"] == {"grade": "A", "coverage_pct": 96}
        assert "gates/governance-evidence.json" in result["evidence"]

    def test_timeout_is_configurable(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        seen: dict[str, Any] = {}

        def _run(argv, **kwargs: Any):
            seen["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(gate_module.subprocess, "run", _run)
        monkeypatch.setenv("ADLC_GOVERNANCE_TIMEOUT", "7")
        GovernanceGate().evaluate(RUN, cfg)
        assert seen["timeout"] == 7

    def test_never_uses_a_shell(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        def _run(argv, **kwargs: Any):
            assert kwargs.get("shell") in (None, False)
            assert isinstance(argv, list)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(gate_module.subprocess, "run", _run)
        GovernanceGate().evaluate(RUN, cfg)


class TestGateResultShape:
    def test_matches_the_frozen_contract(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")})
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert set(result) == {
            "id",
            "required",
            "status",
            "severity",
            "observed",
            "expected",
            "message",
            "evidence",
        }
        assert result["id"] == "governance"
        assert isinstance(result["required"], bool)
        assert result["status"] in {"pass", "fail", "not_run"}
        assert result["severity"] in {"low", "medium", "high", "critical"}
        assert isinstance(result["evidence"], list)
        assert result["message"].strip()

    @pytest.mark.parametrize("agt_present", [True, False])
    def test_validates_against_the_adlc_run_schema(
        self, monkeypatch, cfg: Config, agt_present: bool
    ) -> None:
        """The gate result must satisfy `schemas/adlc-run.schema.json`.

        Both the happy path and the `not_run` path, since a degraded gate still
        has to reduce into a valid run document.
        """
        from adlc.schemas import validate

        monkeypatch.setattr(
            gate_module.shutil, "which", lambda name: f"/usr/bin/{name}" if agt_present else None
        )
        if agt_present:
            monkeypatch.setattr(
                gate_module.subprocess,
                "run",
                fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")}),
            )
        result = GovernanceGate().evaluate(RUN, cfg)

        run = {
            "schemaVersion": "adlc-run/v1",
            "runId": RUN["runId"],
            "createdAt": "2026-08-19T00:00:00Z",
            "repo": "owner/name",
            "baseSha": "0" * 40,
            "status": "gated",
            "profile": "full",
            "stages": [],
            "gates": [result],
            "artifacts": [],
        }
        validate("adlc-run", run)

    def test_json_serializable(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        """`reduce.write_gate` uses json.dumps with no default= fallback."""
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": (0, "ok", ""), "lint-policy": (0, "", "")})
        )
        result = GovernanceGate().evaluate(RUN, cfg)
        assert json.loads(json.dumps(result))["id"] == "governance"

    def test_evidence_lands_in_the_run_gates_dir(
        self, agt_on_path, monkeypatch, cfg: Config
    ) -> None:
        """Paths are resolved through RunDir, not hand-built."""
        from adlc.runs import RunDir

        calls: list[list[str]] = []
        monkeypatch.setattr(
            gate_module.subprocess,
            "run",
            fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")}, calls=calls),
        )
        GovernanceGate().evaluate(RUN, cfg)

        rd = RunDir(cfg, RUN["runId"])
        verify = next(call for call in calls if call[1] == "verify")
        assert Path(verify[verify.index("--evidence") + 1]).parent == rd.gates_dir
        assert (rd.gates_dir / "governance.json").is_file()

    def test_required_follows_the_profile(self, agt_on_path, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")})
        )
        minimal = Config(root=tmp_path, profile="minimal")
        full = Config(root=tmp_path, profile="full")
        assert GovernanceGate().evaluate(RUN, minimal)["required"] is False
        assert GovernanceGate().evaluate(RUN, full)["required"] is True

    def test_missing_run_id_does_not_crash(self, agt_on_path, monkeypatch, cfg: Config) -> None:
        monkeypatch.setattr(
            gate_module.subprocess, "run", fake_agt({"verify": (0, "", ""), "lint-policy": (0, "", "")})
        )
        result = GovernanceGate().evaluate({}, cfg)
        assert result["status"] in {"pass", "fail", "not_run"}
