"""Builders shared by the L8 squad tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "SQUADS_YAML",
    "make_pack",
    "make_run",
    "sha",
    "write_pack",
    "write_precondition",
    "write_review",
    "write_squads",
]


def sha(seed: str) -> str:
    """A deterministic, well-formed 64-hex digest for fixtures."""
    return hashlib.sha256(seed.encode()).hexdigest()


SQUADS_YAML = """
version: 1
defaults:
  citationPolicy: discard-uncited
  blockingSeverities: [high, critical]
  abstainCountsAsPass: false
squads:
  adversarial_review:
    blocking: true
    quorum: "2/3"
    citation: file-line
    members:
      - id: security-adversary
      - id: performance-adversary
      - id: accessibility-adversary
  evidence_review:
    blocking: false
    quorum: "1/1"
    citation: artifact-sha256
    members:
      - id: requirements-auditor
    coverage:
      minArtifactsPerRequirement: 1
      requireShaMatch: true
      requireHashVerification: true
"""


def write_squads(repo: Path, text: str, *, location: str = ".adlc") -> Path:
    """Write a squads config to the vendored (`.adlc`) or template location."""
    if location == ".adlc":
        path = repo / ".adlc" / "squads.yaml"
    else:
        path = repo / "templates" / ".adlc" / "squads.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_review(
    run_dir: Path,
    member: str,
    verdict: str,
    findings: list[tuple[str, str, str]] | None = None,
    *,
    squad: str = "adversarial_review",
    reviewed_sha: str = "deadbeef",
    run_id: str = "2026-08-19-t3st",
) -> Path:
    """Write one verdict file.

    ``findings`` is a list of ``(severity, title, citation_or_empty)``.
    """
    body = [
        "---",
        f"squad: {squad}",
        f"member: {member}",
        f"verdict: {verdict}",
        f"runId: {run_id}",
        f"reviewedSha: {reviewed_sha}",
        "---",
        "",
    ]
    for severity, title, citation in findings or []:
        body.append(f"## [{severity}] {title}")
        if citation:
            body.append(f"`{citation}`")
        body.append("")
        body.append("Prose describing the failure mode.")
        body.append("")
    path = run_dir / "reviews" / f"{squad}.{member}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def make_run(
    *,
    run_id: str = "2026-08-19-t3st",
    head_sha: str = "cafebabe",
    artifact_hashes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "adlc-run/v1",
        "runId": run_id,
        "repo": "owner/name",
        "baseSha": "0" * 40,
        "headSha": head_sha,
        "status": "gated",
        "profile": "full",
        "artifacts": [
            {
                "path": f"evidence/candidate-a/{i}.bin",
                "kind": "playwright_trace",
                "sha256": h,
                "bytes": 1,
            }
            for i, h in enumerate(artifact_hashes or [])
        ],
    }


def make_pack(
    *,
    run_id: str = "2026-08-19-t3st",
    candidate_sha: str = "cafebabe",
    collector: str = "adlc/0.1.0",
    requirements: list[str] | None = None,
    coverage: list[dict[str, Any]] | None = None,
    measurements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reqs = requirements if requirements is not None else ["US1-AC1", "US1-AC2"]
    pack: dict[str, Any] = {
        "runId": run_id,
        "candidateSha": candidate_sha,
        "workflowRunId": "1234",
        "collector": collector,
        "requirements": [
            {"id": r, "text": f"requirement {r}", "source": "spec.md#L1"} for r in reqs
        ],
        "coverage": coverage
        if coverage is not None
        else [
            {
                "requirementId": r,
                "evidenceKinds": ["playwright_trace"],
                "artifactSha256": [sha(r)],
                "present": True,
            }
            for r in reqs
        ],
    }
    if measurements is not None:
        pack["measurements"] = measurements
    return pack


def write_pack(run_dir: Path, pack: dict[str, Any]) -> Path:
    path = run_dir / "evidence-review-pack.json"
    path.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return path


#: The message the spine's gate emits on success, reproduced verbatim so the
#: fixture stays recognisable in failure output.
PRECONDITION_PASS_MESSAGE = "all 2 requirement(s) backed by hash-verified evidence"


def write_precondition(
    run_dir: Path,
    status: str = "pass",
    message: str | None = None,
    observed: dict[str, Any] | None = None,
) -> Path:
    """Write the `evidence_completeness` gate result that EvidenceReviewGate reads.

    That gate is spine-owned (`adlc.adapters.gate.evidence_completeness`) and is
    the *only* thing allowed to block on coverage. These tests fixture its
    recorded verdict rather than reimplementing it, which is precisely the
    boundary the L8 gate is supposed to respect.
    """
    default_message = {
        "pass": PRECONDITION_PASS_MESSAGE,
        "fail": "1 requirement(s) without evidence; 0 referenced hash(es) not found on disk",
        "not_run": "evidence-review-pack.json not found - run `adlc evidence` first.",
    }.get(status, "")
    payload = {
        "id": "evidence_completeness",
        "required": True,
        "status": status,
        "severity": "high",
        "observed": observed or {},
        "expected": {"uncovered": [], "unverifiedHashes": []},
        "message": message if message is not None else default_message,
        "evidence": ["gates/evidence_completeness.json"],
    }
    path = run_dir / "gates" / "evidence_completeness.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
