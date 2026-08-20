"""Run directory management and immutable stage results.

The central invariant of ADLC: **stages never mutate shared state**. Each stage
writes a brand-new ``stages/<stage>.<attempt>.json`` file. Only
:mod:`adlc.reduce` folds those into ``run.json``.

That is what makes parallel GitHub Actions jobs safe -- jobs share no
filesystem, so any design that appends to one canonical document loses writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adlc import RUN_SCHEMA_VERSION
from adlc.config import Config
from adlc.ports import ArtifactRef, Run, StageResult

_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{4}$")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y-%m-%d}-{secrets.token_hex(2)}"


def is_run_id(value: str) -> bool:
    return bool(_RUN_ID_RE.match(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: Any) -> Path:
    """Atomic JSON write -- temp file then replace.

    A half-written stage result is worse than a missing one, because the
    reducer would silently produce a corrupt run record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def current_sha(root: Path) -> str:
    try:
        return git("rev-parse", "HEAD", cwd=root)
    except RuntimeError:
        return ""


def detect_repo(root: Path) -> str:
    if env := os.environ.get("GITHUB_REPOSITORY"):
        return env
    try:
        url = git("remote", "get-url", "origin", cwd=root, check=False)
    except RuntimeError:
        return ""
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url or "")
    return match.group(1) if match else ""


class RunDir:
    """Typed accessor for one run's directory."""

    def __init__(self, cfg: Config, run_id: str) -> None:
        self.cfg = cfg
        self.run_id = run_id
        self.path = cfg.run_dir(run_id)

    # -- well-known locations --------------------------------------------
    @property
    def stages_dir(self) -> Path:
        return self.path / "stages"

    @property
    def run_json(self) -> Path:
        return self.path / "run.json"

    @property
    def spec_dir(self) -> Path:
        return self.path / "spec"

    @property
    def enrichment_dir(self) -> Path:
        return self.path / "enrichment"

    @property
    def patches_dir(self) -> Path:
        return self.path / "patches"

    @property
    def evidence_dir(self) -> Path:
        return self.path / "evidence"

    @property
    def evals_dir(self) -> Path:
        return self.path / "evals"

    @property
    def gates_dir(self) -> Path:
        return self.path / "gates"

    @property
    def reviews_dir(self) -> Path:
        return self.path / "reviews"

    @property
    def taskgraph(self) -> Path:
        return self.path / "taskgraph.json"

    @property
    def brief(self) -> Path:
        return self.path / "brief.md"

    @property
    def report(self) -> Path:
        return self.path / "report.html"

    @property
    def review_pack(self) -> Path:
        return self.path / "evidence-review-pack.json"

    def rel(self, path: Path) -> str:
        """Path relative to the run dir, POSIX-style, for storing in JSON."""
        try:
            return path.resolve().relative_to(self.path.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    # -- lifecycle --------------------------------------------------------
    def create(self, *, profile: str, brief_text: str, references_run: str | None = None) -> Run:
        for directory in (
            self.stages_dir, self.spec_dir, self.enrichment_dir, self.patches_dir,
            self.evidence_dir, self.evals_dir, self.gates_dir, self.reviews_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.brief.write_text(brief_text, encoding="utf-8")

        seed: Run = {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "runId": self.run_id,
            "createdAt": utcnow(),
            "referencesRun": references_run,
            "repo": detect_repo(self.cfg.root),
            "baseSha": current_sha(self.cfg.root),
            "headSha": current_sha(self.cfg.root),
            "prNumber": None,
            "status": "draft",
            "profile": profile,
            "capabilities": {},
            "stages": [],
            "variants": [],
            "gates": [],
            "artifacts": [],
            "decision": None,
            "experimentRef": None,
        }
        write_json(self.path / "seed.json", seed)
        return seed

    def exists(self) -> bool:
        return self.path.is_dir()

    # -- immutable stage results ------------------------------------------
    def next_attempt(self, stage: str) -> int:
        existing = list(self.stages_dir.glob(f"{stage}.*.json"))
        attempts = []
        for item in existing:
            try:
                attempts.append(int(item.name.split(".")[-2]))
            except (IndexError, ValueError):
                continue
        return (max(attempts) + 1) if attempts else 1

    def write_stage(
        self,
        stage: str,
        *,
        status: str = "ok",
        outputs: list[str] | None = None,
        message: str = "",
        data: dict[str, Any] | None = None,
        started_at: str | None = None,
    ) -> StageResult:
        """Append a NEW immutable stage result. Never overwrites."""
        attempt = self.next_attempt(stage)
        payload: StageResult = {
            "stage": stage,
            "attempt": attempt,
            "status": status,  # type: ignore[typeddict-item]
            "startedAt": started_at or utcnow(),
            "endedAt": utcnow(),
            "outputs": outputs or [],
            "digest": "",
            "message": message,
            "data": data or {},
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        payload["digest"] = f"sha256:{sha256_bytes(body)}"
        write_json(self.stages_dir / f"{stage}.{attempt}.json", payload)
        return payload

    def stage_results(self) -> list[StageResult]:
        """All stage results, ordered by (startedAt, stage, attempt)."""
        results: list[StageResult] = []
        for item in sorted(self.stages_dir.glob("*.json")):
            try:
                results.append(read_json(item))
            except (json.JSONDecodeError, OSError):
                continue
        results.sort(key=lambda r: (r.get("startedAt", ""), r.get("stage", ""), r.get("attempt", 0)))
        return results

    def latest_stage(self, stage: str) -> StageResult | None:
        matching = [r for r in self.stage_results() if r.get("stage") == stage]
        return matching[-1] if matching else None

    # -- artifacts ---------------------------------------------------------
    def artifact_ref(self, path: Path, kind: str, mime: str = "application/octet-stream") -> ArtifactRef:
        return {
            "path": self.rel(path),
            "kind": kind,
            "mimeType": mime,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    def scan_artifacts(self) -> list[ArtifactRef]:
        """Hash every evidence/report artifact currently on disk."""
        kinds = {
            ".zip": ("playwright_trace", "application/zip"),
            ".har": ("har", "application/json"),
            ".webm": ("video", "video/webm"),
            ".png": ("screenshot", "image/png"),
            ".jsonl": ("jsonl", "application/x-ndjson"),
            ".html": ("html_report", "text/html"),
            ".json": ("json", "application/json"),
            ".ts": ("replay_script", "text/plain"),
        }
        refs: list[ArtifactRef] = []
        roots = [self.evidence_dir, self.evals_dir]
        for root in roots:
            if not root.is_dir():
                continue
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    kind, mime = kinds.get(item.suffix, ("file", "application/octet-stream"))
                    refs.append(self.artifact_ref(item, kind, mime))
        if self.report.is_file():
            refs.append(self.artifact_ref(self.report, "html_report", "text/html"))
        return refs


def resolve_run(cfg: Config, run_id: str | None = None) -> RunDir:
    """Resolve a run id, defaulting to the most recently created run."""
    if run_id and run_id != "latest":
        rd = RunDir(cfg, run_id)
        if not rd.exists():
            raise FileNotFoundError(f"run '{run_id}' not found under {cfg.runs_dir}")
        return rd
    if not cfg.runs_dir.is_dir():
        raise FileNotFoundError(f"no runs yet under {cfg.runs_dir}")
    candidates = sorted((d for d in cfg.runs_dir.iterdir() if d.is_dir()), key=lambda d: d.name)
    if not candidates:
        raise FileNotFoundError(f"no runs yet under {cfg.runs_dir}")
    return RunDir(cfg, candidates[-1].name)
