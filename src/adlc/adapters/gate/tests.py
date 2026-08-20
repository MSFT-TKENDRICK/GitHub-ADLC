"""Tests gate -- runs the repo's own test command.

Credential-free and required in the ``minimal`` profile. If no test command is
configured this returns ``not_run`` (which fails a required gate) rather than a
vacuous pass -- "we didn't look" must never render as "it's fine".
"""

from __future__ import annotations

import subprocess

from adlc.config import Config
from adlc.ports import GateResult, Run


class TestsGate:
    id = "tests"
    name = "tests"
    kind = "gate"
    required_by_default = True

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        command = (cfg.raw.get("commands") or {}).get("test")
        if not command:
            return False, "no `commands.test` configured in .adlc/config.yaml"
        return True, f"test command configured: {command}"

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        command = (cfg.raw.get("commands") or {}).get("test")
        base: GateResult = {
            "id": self.id,
            "required": cfg.is_required(self.id),
            "severity": "high",
            "expected": {"exitCode": 0},
            "evidence": [f"gates/{self.id}.json"],
        }
        if not command:
            return {
                **base, "status": "not_run", "observed": {},
                "message": (
                    "no `commands.test` configured in .adlc/config.yaml - "
                    "cannot assert the change is tested"
                ),
            }

        proc = subprocess.run(
            command, cwd=str(cfg.root), shell=True,
            capture_output=True, text=True, check=False,
        )
        output = (proc.stdout + proc.stderr)[-4000:]
        passed = proc.returncode == 0
        return {
            **base,
            "status": "pass" if passed else "fail",
            "observed": {"exitCode": proc.returncode, "command": command, "tail": output},
            "message": (
                f"`{command}` exited 0"
                if passed
                else f"`{command}` exited {proc.returncode}"
            ),
        }
