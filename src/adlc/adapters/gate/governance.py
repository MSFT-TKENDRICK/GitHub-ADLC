"""The ``governance`` gate — Agent Governance Toolkit verification.

Runs ``agt verify --evidence <path> --strict`` (and ``agt lint-policy`` when a
policy is present), emits the AGT evidence document into the run's ``gates/``
directory, and maps the outcome onto a :class:`~adlc.ports.GateResult`.

Fail-closed contract (``CONTRIBUTING.md`` rule 6, PLAN §4.2): this gate returns
``pass`` **only** when ``agt`` actually ran to completion and reported success.
Anything else -- AGT absent, CLI missing, timeout, crash -- is ``not_run``, and
the spine turns ``required + not_run`` into a build failure. There is no code
path here that reports ``pass`` for a check that did not execute.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.maf.middleware import resolve_policy_path
from adlc.reduce import write_gate
from adlc.runs import RunDir

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config
    from adlc.ports import GateResult, Run

__all__ = ["GovernanceGate"]

AGT_BINARY = "agt"
AGT_INSTALL_HINT = 'agt CLI not on PATH — pip install "adlc[governance]"'

#: Generous but bounded. `agt verify` is a local scan; if it has not finished by
#: now something is wrong, and hanging a CI job is worse than a `not_run`.
DEFAULT_TIMEOUT_SECONDS = 180

#: Written relative to ``runs/<run-id>/``.
GATE_REPORT = "gates/governance.json"
GATE_EVIDENCE = "gates/governance-evidence.json"

#: Emitted by :class:`~adlc.adapters.agents.maf_governed.MafGovernedRunner`.
RUNNER_DECISIONS = "gates/governance-decisions.json"


class GovernanceGate:
    """``GateRunner`` for AGT policy verification."""

    id = "governance"
    name = "governance"
    kind = "gate"
    required_by_default = False

    # -- detection --------------------------------------------------------
    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, non-raising: a PATH lookup and a stat. No network, no subprocess."""
        try:
            binary = shutil.which(AGT_BINARY)
        except Exception:  # noqa: BLE001 - which() must never break detection
            return False, AGT_INSTALL_HINT
        if binary is None:
            return False, AGT_INSTALL_HINT
        if resolve_policy_path(cfg) is None:
            return False, "agt CLI present but no policy found — expected .adlc/policy.yaml"
        return True, f"agt CLI at {binary}"

    # -- evaluation -------------------------------------------------------
    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        required = cfg.is_required(self.id)
        rd = _run_dir(run, cfg)
        gates_dir = rd.gates_dir
        evidence_path = rd.path / GATE_EVIDENCE

        available, reason = self.detect(cfg)
        if not available:
            return self._finish(
                rd,
                {
                    "id": self.id,
                    "required": required,
                    "status": "not_run",
                    "severity": "high" if required else "medium",
                    "observed": {"agtAvailable": False},
                    "expected": {"agtAvailable": True, "command": "agt verify --strict"},
                    "message": f"governance not verified: {reason}",
                    "evidence": [GATE_REPORT],
                },
            )

        try:
            gates_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._finish(
                rd,
                {
                    "id": self.id,
                    "required": required,
                    "status": "not_run",
                    "severity": "high" if required else "medium",
                    "observed": {"error": f"cannot create {gates_dir}: {exc}"},
                    "expected": {"gatesDir": str(gates_dir)},
                    "message": f"governance not verified: {exc}",
                    "evidence": [],
                },
                write_report=False,
            )

        timeout = _timeout(cfg)
        verify = _run_agt(
            ["verify", "--evidence", str(evidence_path), "--strict"],
            cwd=cfg.root,
            timeout=timeout,
        )

        policy = resolve_policy_path(cfg)
        lint = None
        if policy is not None:
            lint = _run_agt(["lint-policy", str(policy)], cwd=cfg.root, timeout=timeout)

        observed: dict[str, Any] = {
            "agtAvailable": True,
            "policy": str(policy) if policy else None,
            "verify": verify.as_dict(),
        }
        if lint is not None:
            observed["lintPolicy"] = lint.as_dict()

        attestation = _read_attestation(evidence_path)
        if attestation is not None:
            observed["attestation"] = attestation

        runtime_decisions = _read_runtime_decisions(rd.path / RUNNER_DECISIONS)
        if runtime_decisions is not None:
            observed["runtimeDecisions"] = runtime_decisions

        evidence = [GATE_REPORT]
        if evidence_path.is_file():
            evidence.append(GATE_EVIDENCE)
        if runtime_decisions is not None:
            evidence.append(RUNNER_DECISIONS)

        expected = {
            "verifyExitCode": 0,
            "lintPolicyExitCode": 0,
            "strict": True,
            "runtimeDenied": 0,
        }

        status, severity, message = _classify(verify, lint, runtime_decisions)

        return self._finish(
            rd,
            {
                "id": self.id,
                "required": required,
                "status": status,
                "severity": severity,
                "observed": observed,
                "expected": expected,
                "message": message,
                "evidence": evidence,
            },
        )

    # -- reporting --------------------------------------------------------
    @staticmethod
    def _finish(rd: RunDir, result: GateResult, *, write_report: bool = True) -> GateResult:
        """Persist the sidecar so a direct `evaluate()` still leaves evidence.

        ``adlc.stages.gates`` calls ``reduce.write_gate`` on the returned result
        anyway, to the same path, so this is idempotent rather than a second
        source of truth. Losing it must never change the verdict -- the
        ``GateResult`` itself is what the reducer folds into ``run.json``, which
        only ``adlc reduce`` ever writes.
        """
        if write_report:
            try:
                write_gate(rd, result)
            except (OSError, KeyError, TypeError, ValueError):
                pass
        return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _CommandResult:
    __slots__ = ("args", "error", "ran", "returncode", "stderr", "stdout")

    def __init__(
        self,
        args: list[str],
        *,
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        ran: bool,
        error: str = "",
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.ran = ran
        self.error = error

    @property
    def ok(self) -> bool:
        return self.ran and self.returncode == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": " ".join(self.args),
            "ran": self.ran,
            "exitCode": self.returncode,
            "stdout": _tail(self.stdout),
            "stderr": _tail(self.stderr),
            "error": self.error,
        }


def _tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _run_agt(args: list[str], *, cwd: Path, timeout: int) -> _CommandResult:
    argv = [AGT_BINARY, *args]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _CommandResult(
            argv,
            returncode=None,
            ran=False,
            error=f"timed out after {timeout}s",
        )
    except (OSError, ValueError) as exc:
        return _CommandResult(argv, returncode=None, ran=False, error=f"{type(exc).__name__}: {exc}")
    return _CommandResult(
        argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        ran=True,
    )


def _classify(
    verify: _CommandResult,
    lint: _CommandResult | None,
    runtime_decisions: dict[str, Any] | None,
) -> tuple[str, str, str]:
    """Map command outcomes onto ``(status, severity, message)``.

    Order matters. "Did not run" outranks "ran and failed", because a `not_run`
    is a statement about our own confidence, not about the repo.
    """
    if not verify.ran:
        return (
            "not_run",
            "high",
            f"governance not verified: `agt verify` did not complete ({verify.error})",
        )
    if lint is not None and not lint.ran:
        return (
            "not_run",
            "high",
            f"governance not verified: `agt lint-policy` did not complete ({lint.error})",
        )

    if lint is not None and not lint.ok:
        return (
            "fail",
            "high",
            (
                f"AGT policy is invalid: `agt lint-policy` exited {lint.returncode}. "
                f"{_tail(lint.stderr or lint.stdout, 400)}"
            ),
        )
    if not verify.ok:
        return (
            "fail",
            "high",
            (
                f"`agt verify --strict` exited {verify.returncode}. "
                f"{_tail(verify.stderr or verify.stdout, 400)}"
            ),
        )

    denied = int((runtime_decisions or {}).get("denied") or 0)
    if denied:
        total = (runtime_decisions or {}).get("total")
        return (
            "fail",
            "high",
            f"{denied} of {total} governed tool call(s) were blocked by policy during the run",
        )

    checked = (runtime_decisions or {}).get("total")
    suffix = f"; {checked} governed tool call(s) all permitted" if checked else ""
    return "pass", "low", f"`agt verify --strict` passed{suffix}"


def _read_attestation(path: Path) -> dict[str, Any] | None:
    """Read back the evidence document AGT wrote, if it produced one."""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    summary = {
        key: data[key]
        for key in ("grade", "compliance_grade", "coverage_pct", "coverage", "score", "controls")
        if key in data
    }
    return summary or {"keys": sorted(data)[:20]}


def _read_runtime_decisions(path: Path) -> dict[str, Any] | None:
    """Read the middleware decision log emitted by ``MafGovernedRunner``."""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "engine": data.get("engine"),
        "policy": data.get("policy"),
        "total": data.get("total"),
        "denied": data.get("denied"),
    }


def _run_dir(run: Run, cfg: Config) -> RunDir:
    run_id = (run or {}).get("runId") or "unknown"
    return RunDir(cfg, str(run_id))


def _timeout(cfg: Config) -> int:
    if env := os.environ.get("ADLC_GOVERNANCE_TIMEOUT"):
        try:
            return max(1, int(env))
        except ValueError:
            pass
    raw = getattr(cfg, "raw", {}) or {}
    section = raw.get("governance") if isinstance(raw, dict) else None
    if isinstance(section, dict):
        try:
            return max(1, int(section.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_TIMEOUT_SECONDS
