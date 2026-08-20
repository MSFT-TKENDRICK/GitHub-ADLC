"""Local evidence collector -- always available, never fabricates.

Playwright gives rich browser evidence but must be installed. On a bare runner
the framework still has to produce *real* evidence rather than either failing or
inventing artifacts. So this collector captures evidence of what actually
executed in this run:

* ``console.jsonl``  -- captured stdout/stderr of the configured test command
* ``run-manifest.json`` -- the exact commands, exit codes, timings, environment
* ``changed-files.json`` -- what the candidate actually modified, with blob SHAs
* ``replay.sh``      -- a script that reproduces this evidence run

It is deliberately modest. It proves the pipeline end-to-end with zero installs,
and Playwright takes over automatically the moment it is available.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import ArtifactRef, Run

_REDACT_KEYS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PAT", "CREDENTIAL")


class LocalEvidenceCollector:
    name = "local"
    kind = "evidence"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "built-in local collector (no browser or install required)"

    def collect(self, run: Run, variant: str, out: Path) -> list[ArtifactRef]:
        from adlc.runs import git, sha256_file  # local import avoids a cycle

        out.mkdir(parents=True, exist_ok=True)
        root = Path(os.environ.get("ADLC_ROOT", ".")).resolve()

        commands = {
            "test": os.environ.get("ADLC_TEST_COMMAND", ""),
        }
        console: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []

        for label, command in commands.items():
            if not command:
                console.append({
                    "type": "adlc-info",
                    "text": f"no '{label}' command configured; nothing executed for it",
                })
                continue
            started = time.time()
            proc = subprocess.run(
                command, cwd=str(root), shell=True,
                capture_output=True, text=True, check=False,
            )
            duration = round(time.time() - started, 3)
            for stream, text in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                for line in (text or "").splitlines()[-400:]:
                    console.append({"type": stream, "text": line})
            executions.append({
                "label": label, "command": command,
                "exitCode": proc.returncode, "durationSeconds": duration,
            })

        console_path = out / "console.jsonl"
        console_path.write_text(
            "\n".join(json.dumps(entry) for entry in console) + "\n", encoding="utf-8"
        )

        changed: list[dict[str, str]] = []
        names = git("diff", "--name-only", "HEAD~1", "HEAD", cwd=root, check=False)
        for rel in filter(None, (names or "").splitlines()):
            target = root / rel
            changed.append({
                "path": rel,
                "blobSha": git("hash-object", str(target), cwd=root, check=False)
                if target.is_file() else "",
                "exists": target.is_file(),
            })
        (out / "changed-files.json").write_text(
            json.dumps(changed, indent=2) + "\n", encoding="utf-8"
        )

        manifest = {
            "variant": variant,
            "runId": run.get("runId"),
            "candidateSha": run.get("headSha") or run.get("baseSha"),
            "collector": f"{self.name}/adlc",
            "executions": executions,
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "cwd": str(root),
            },
            "env": {
                key: ("[REDACTED]" if any(m in key.upper() for m in _REDACT_KEYS) else value)
                for key, value in sorted(os.environ.items())
                if key.startswith(("ADLC_", "GITHUB_", "CI"))
            },
        }
        (out / "run-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        measurements = [{
            "metricId": "console_errors",
            "value": sum(1 for e in console if e["type"] == "stderr"),
            "collector": self.name,
            "artifactSha256": sha256_file(console_path),
        }]
        (out / "local-measurements.json").write_text(
            json.dumps(measurements, indent=2) + "\n", encoding="utf-8"
        )

        replay = out / "replay.sh"
        replay.write_text(
            "#!/usr/bin/env bash\n"
            "# ADLC replay - reproduces this evidence run\n"
            "set -euo pipefail\n"
            + "".join(f"{e['command']}\n" for e in executions)
            + ("echo 'no commands were configured for this run'\n" if not executions else ""),
            encoding="utf-8",
        )

        refs: list[ArtifactRef] = []
        for path in sorted(out.rglob("*")):
            if path.is_file():
                refs.append({
                    "path": path.as_posix(),
                    "kind": _kind_for(path.name),
                    "mimeType": "application/json" if path.suffix == ".json" else "text/plain",
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                })
        return refs


def _kind_for(name: str) -> str:
    return {
        "console.jsonl": "console_log",
        "run-manifest.json": "run_manifest",
        "changed-files.json": "changed_files",
        "local-measurements.json": "measurements",
        "replay.sh": "replay_script",
    }.get(name, "file")
