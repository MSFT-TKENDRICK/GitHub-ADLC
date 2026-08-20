"""Local dependency audit -- credential-free default supply-chain gate.

Uses whichever ecosystem auditor is present (`pip-audit`, `npm audit`) and
reports ``not_run`` when none is available, which fails a required gate rather
than passing silently.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from adlc.config import Config
from adlc.ports import GateResult, Run

_SEVERITY_RANK = {"low": 0, "moderate": 1, "medium": 1, "high": 2, "critical": 3}


class DepsLocalGate:
    id = "deps_local"
    name = "deps-local"
    kind = "gate"
    required_by_default = True

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        manifests = [
            name for name in ("pyproject.toml", "requirements.txt", "setup.cfg", "package.json")
            if (cfg.root / name).is_file()
        ]
        if not manifests:
            return True, "no dependency manifests in this repository"
        available = [tool for tool in ("pip-audit", "npm") if shutil.which(tool)]
        if not available:
            return False, f"manifests present ({', '.join(manifests)}) but no auditor on PATH"
        return True, f"available auditors: {', '.join(available)}"

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        threshold = (cfg.raw.get("gates") or {}).get("depsMaxSeverity", "high")
        base: GateResult = {
            "id": self.id,
            "required": cfg.is_required(self.id),
            "severity": "high",
            "expected": {"maxSeverityAllowed": threshold, "blockingFindings": 0},
            "evidence": [f"gates/{self.id}.json"],
        }

        manifests = {
            "pypi": [
                name for name in ("pyproject.toml", "requirements.txt", "setup.cfg")
                if (cfg.root / name).is_file()
            ],
            "npm": ["package.json"] if (cfg.root / "package.json").is_file() else [],
        }

        # No manifests at all means there is genuinely nothing to audit. That is
        # a real pass, not an unchecked one -- distinct from "manifests exist but
        # we have no auditor", which must fail closed.
        if not any(manifests.values()):
            return {
                **base, "status": "pass",
                "observed": {"manifests": manifests, "findings": 0},
                "message": "no dependency manifests found - nothing to audit",
            }

        findings: list[dict] = []
        engines: list[str] = []
        unaudited: list[str] = []

        if manifests["pypi"]:
            if shutil.which("pip-audit"):
                engines.append("pip-audit")
                findings.extend(self._pip_audit(cfg.root))
            else:
                unaudited.append("python (install pip-audit)")

        if manifests["npm"]:
            if shutil.which("npm"):
                engines.append("npm-audit")
                findings.extend(self._npm_audit(cfg.root))
            else:
                unaudited.append("npm (install node/npm)")

        if unaudited:
            return {
                **base, "status": "not_run",
                "observed": {"manifests": manifests, "unaudited": unaudited},
                "message": (
                    "dependency manifests present but no auditor available for: "
                    + ", ".join(unaudited)
                ),
            }

        limit = _SEVERITY_RANK.get(str(threshold).lower(), 2)
        blocking = [
            f for f in findings
            if _SEVERITY_RANK.get(str(f.get("severity", "low")).lower(), 0) >= limit
        ]
        return {
            **base,
            "status": "pass" if not blocking else "fail",
            "observed": {
                "engines": engines, "manifests": manifests, "total": len(findings),
                "blocking": len(blocking), "detail": blocking[:25],
            },
            "message": (
                f"{len(findings)} advisory finding(s), none at or above '{threshold}'"
                if not blocking
                else f"{len(blocking)} finding(s) at or above '{threshold}'"
            ),
        }

    # -- engines -----------------------------------------------------------
    @staticmethod
    def _pip_audit(root: Path) -> list[dict]:
        proc = subprocess.run(  # noqa: S603
            ["pip-audit", "-f", "json", "--progress-spinner", "off"],
            cwd=str(root), capture_output=True, text=True, check=False,
        )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        out: list[dict] = []
        for dep in data.get("dependencies", data if isinstance(data, list) else []):
            for vuln in dep.get("vulns", []) or []:
                out.append({
                    "ecosystem": "pypi", "package": dep.get("name"),
                    "id": vuln.get("id"),
                    "severity": (vuln.get("severity") or "high").lower(),
                })
        return out

    @staticmethod
    def _npm_audit(root: Path) -> list[dict]:
        proc = subprocess.run(  # noqa: S603
            ["npm", "audit", "--json"],
            cwd=str(root), capture_output=True, text=True, check=False,
        )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        out: list[dict] = []
        for name, item in (data.get("vulnerabilities") or {}).items():
            out.append({
                "ecosystem": "npm", "package": name,
                "id": (item.get("via") or [{}])[0].get("url")
                if isinstance(item.get("via"), list) and item.get("via")
                and isinstance(item["via"][0], dict) else None,
                "severity": str(item.get("severity", "low")).lower(),
            })
        return out
