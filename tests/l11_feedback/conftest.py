"""Shared fixtures for the L11 human-feedback workstream.

Everything here works offline with no credentials and no image library: PNGs are
synthesised with :mod:`zlib` so the tests exercise *real* decodable bytes and
real SHA-256 digests rather than opaque blobs.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
SRC = REPO_ROOT / "src"

if str(SRC) not in sys.path:  # importable without an editable install
    sys.path.insert(0, str(SRC))

from adlc.config import Config
from adlc.runs import RunDir, read_json, sha256_bytes, write_json

CANDIDATE_SHA = "a" * 40
BASELINE_SHA = "b" * 40


@pytest.fixture
def targets_doc() -> dict[str, Any]:
    """A minimal but structurally real ``adlc-feedback-targets/v1`` document.

    Lives here rather than in one test module because several suites need it,
    and because the ``submission`` block is built by the *real*
    :func:`submission_contract` -- a hand-written copy would happily disagree
    with the shipped manifest and hide exactly the drift these tests exist to
    catch. It did, once: a hand-built copy masked a GUI reading ``enums`` under
    a key the manifest has never emitted.
    """
    from adlc.stages.feedback_targets import SCHEMA_VERSION, submission_contract

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": "2024-01-01T00:00:00Z",
        "run": {
            "runId": "20240101-000000-abcdef",
            "candidateSha": CANDIDATE_SHA,
            "baselineRunId": None,
            "reportDigest": "sha256:" + "c" * 64,
            "profile": "minimal",
            "title": None,
            "passed": False,
        },
        "requirements": [{"id": "AC-1", "text": "it works", "source": "spec/spec.md"}],
        "artifacts": [
            {
                "id": "art-1",
                "path": "evidence/candidate-a/home.png",
                "sha256": "b" * 64,
                "kind": "screenshot",
                "mediaType": "image/png",
                "bytes": 100,
                "width": 800,
                "height": 600,
                "annotatable": True,
                "inline": None,
                "inlineOmittedReason": "not inlined: test fixture",
            }
        ],
        "reasoning": [
            {
                "id": "rsn-1",
                "targetKind": "squad_finding",
                "targetRef": "reviews/security.md#finding-1",
                "targetTitle": "unescaped input",
                "sourceDigest": "sha256:" + "d" * 64,
                "author": "security-adversary",
                "text": "the slug is interpolated raw",
                "severity": "high",
                "confidence": "high",
                "citations": ["src/adlc/x.py:12"],
            }
        ],
        "diff": {
            "baselineRunId": None,
            "measurements": [
                {
                    "targetKind": "measurement",
                    "targetId": "lcp",
                    "label": "lcp",
                    "change": "changed",
                    "value": 2.5,
                    "baselineValue": 1.5,
                    "delta": 1.0,
                    "budget": 2.0,
                    "passed": False,
                    "baselinePassed": True,
                    "budgetCrossed": "entered_breach",
                    "collector": "lighthouse",
                    "regression": True,
                }
            ],
            "coverage": [],
            "screenshots": [],
        },
        "submission": submission_contract(),
        "budgets": {
            "perArtifactBytes": 1,
            "totalBytes": 1,
            "inlinedBytes": 0,
            "inlinedCount": 0,
            "omittedCount": 1,
        },
    }


# ---------------------------------------------------------------------------
# Tiny PNG writer -- a real, decodable image with no third-party dependency
# ---------------------------------------------------------------------------


def png_bytes(width: int = 8, height: int = 6, rgb: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """A valid solid-colour RGB PNG."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, **kwargs: Any) -> str:
    """Write a PNG and return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = png_bytes(**kwargs)
    path.write_bytes(data)
    return sha256_bytes(data)


# ---------------------------------------------------------------------------
# Run construction
# ---------------------------------------------------------------------------


def make_run(
    cfg: Config,
    run_id: str,
    *,
    head_sha: str,
    references_run: str | None = None,
    measurements: list[dict[str, Any]] | None = None,
    coverage: list[dict[str, Any]] | None = None,
    screenshots: dict[str, tuple[int, int, int]] | None = None,
    variant: str = "candidate-a",
) -> RunDir:
    """Build a run directory carrying a review pack and screenshot evidence."""
    rd = RunDir(cfg, run_id)
    rd.create(
        profile=cfg.profile, brief_text="# Brief\n\nA change.\n", references_run=references_run
    )

    seed_path = rd.path / "seed.json"
    seed = read_json(seed_path)
    seed["headSha"] = head_sha
    seed["baseSha"] = head_sha
    seed["referencesRun"] = references_run
    write_json(seed_path, seed)

    out = rd.evidence_dir / variant
    out.mkdir(parents=True, exist_ok=True)
    for name, rgb in (screenshots or {}).items():
        write_png(out / name, rgb=rgb)

    write_json(
        rd.review_pack,
        {
            "schemaVersion": "adlc-evidence-review-pack/v1",
            "runId": run_id,
            "candidateSha": head_sha,
            "workflowRunId": None,
            "collector": "adlc/0.1.0",
            "requirements": [
                {"id": "US1-AC1", "text": "A theme toggle exists.", "source": "spec/spec.md"},
                {"id": "US1-AC2", "text": "Theme applies immediately.", "source": "spec/spec.md"},
            ],
            "measurements": measurements if measurements is not None else [],
            "coverage": coverage if coverage is not None else [],
            "screenshots": [],
        },
    )
    return rd


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    root = tmp_path / "repo"
    (root / ".adlc").mkdir(parents=True)
    return Config(root=root, profile="minimal")


@pytest.fixture
def valid_pack() -> dict[str, Any]:
    """A minimal-but-representative pack that must validate."""
    return {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "2026-08-20-c0de",
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00Z",
        "verdict": "revise",
        "route": "outer",
        "summary": "The toggle is unreachable by keyboard.",
        "annotations": [
            {
                "id": "an-1",
                "artifactSha256": "c" * 64,
                "artifactPath": "evidence/candidate-a/home.png",
                "artifactKind": "screenshot",
                "shape": "rect",
                "geometry": {"points": [[0.1, 0.1], [0.4, 0.35]]},
                "severity": "major",
                "comment": "No visible focus ring on the toggle.",
                "requirementIds": ["US1-AC1"],
            }
        ],
        "critiques": [
            {
                "id": "cr-1",
                "targetKind": "squad_finding",
                "targetRef": "reviews/adversarial_review.security-adversary.md#finding-1",
                "sourceDigest": "sha256:" + "d" * 64,
                "stance": "disagree",
                "comment": "That path is unreachable; the guard runs first.",
            }
        ],
        "diffDecisions": [
            {
                "id": "dd-1",
                "targetKind": "measurement",
                "targetId": "lcp",
                "decision": "reject",
                "comment": "A 400 ms regression is not acceptable.",
                "annotationIds": ["an-1"],
            }
        ],
    }


@pytest.fixture
def valid_diff() -> dict[str, Any]:
    """A minimal evidence diff that must validate."""
    return {
        "schemaVersion": "adlc-evidence-diff/v1",
        "runId": "2026-08-20-c0de",
        "baselineRunId": "2026-08-19-a1b2",
        "generatedAt": "2026-08-20T12:00:00Z",
        "measurements": [
            {
                "metricId": "lcp_ms",
                "change": "changed",
                "value": 2200.0,
                "baselineValue": 1800.0,
                "delta": 400.0,
                "budget": 2500.0,
                "passed": True,
                "baselinePassed": True,
                "budgetCrossed": "none",
                "collector": "lighthouse",
            }
        ],
        "coverage": [
            {
                "requirementId": "US1-AC1",
                "change": "unchanged",
                "present": True,
                "baselinePresent": True,
                "evidenceKinds": ["screenshot"],
                "baselineEvidenceKinds": ["screenshot"],
            }
        ],
        "screenshots": [
            {
                "path": "home.png",
                "change": "changed",
                "sha256": "f" * 64,
                "baselineSha256": "e" * 64,
                "bytes": 128,
                "baselineBytes": 120,
            }
        ],
        "summary": {"measurementsChanged": 1, "screenshotsChanged": 1},
    }
