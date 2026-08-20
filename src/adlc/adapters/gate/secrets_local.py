"""Local secret scan -- credential-free default security gate.

Uses `gitleaks` when present; otherwise falls back to a conservative built-in
pattern scan so the gate still produces a real signal on a bare runner. The
built-in scan is deliberately high-precision (well-known token shapes only) --
a noisy gate that people learn to ignore is worse than none.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from adlc.config import Config
from adlc.ports import GateResult, Run

#: High-precision, well-known credential shapes.
PATTERNS: dict[str, re.Pattern[str]] = {
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    "github_fine_grained": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "azure_storage_key": re.compile(r"AccountKey=[A-Za-z0-9+/]{80,}={0,2}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "launchdarkly_sdk": re.compile(r"\bsdk-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".adlc"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webm", ".zip", ".pdf", ".woff", ".woff2", ".ico"}
MAX_FILE_BYTES = 1_000_000


class SecretsLocalGate:
    id = "secrets_local"
    name = "secrets-local"
    kind = "gate"
    required_by_default = True

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        if shutil.which("gitleaks"):
            return True, "gitleaks on PATH"
        return True, "using built-in pattern scan (install gitleaks for deeper coverage)"

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        base: GateResult = {
            "id": self.id,
            "required": cfg.is_required(self.id),
            "severity": "critical",
            "expected": {"findings": 0},
            "evidence": [f"gates/{self.id}.json"],
        }

        if shutil.which("gitleaks"):
            findings, engine = self._gitleaks(cfg.root)
        else:
            findings, engine = self._builtin(cfg.root), "builtin"

        return {
            **base,
            "status": "pass" if not findings else "fail",
            "observed": {"engine": engine, "findings": len(findings), "detail": findings[:25]},
            "message": (
                f"no secrets detected ({engine})"
                if not findings
                else f"{len(findings)} potential secret(s) detected ({engine})"
            ),
        }

    # -- engines -----------------------------------------------------------
    @staticmethod
    def _gitleaks(root: Path) -> tuple[list[dict], str]:
        proc = subprocess.run(
            ["gitleaks", "detect", "--no-banner", "--report-format", "json", "--report-path", "-"],
            cwd=str(root), capture_output=True, text=True, check=False,
        )
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            data = []
        return [
            {
                "rule": item.get("RuleID"),
                "file": item.get("File"),
                "line": item.get("StartLine"),
            }
            for item in (data or [])
        ], "gitleaks"

    @staticmethod
    def _builtin(root: Path) -> list[dict]:
        findings: list[dict] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rule, pattern in PATTERNS.items():
                for match in pattern.finditer(text):
                    findings.append({
                        "rule": rule,
                        "file": path.relative_to(root).as_posix(),
                        "line": text[: match.start()].count("\n") + 1,
                    })
                    if len(findings) >= 100:
                        return findings
        return findings
