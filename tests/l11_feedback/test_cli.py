"""L11 -- the CLI surface for feedback and the loopback server.

`adlc feedback apply` is the single code path both egress routes converge on:
the page can download a pack and a human runs this, or the loopback server POSTs
the same bytes into the same function. These tests pin the contract that makes
that true -- most importantly that a refusal exits non-zero, because in CI a
silently-zero refusal is indistinguishable from success.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adlc.cli import app
from adlc.config import Config
from adlc.runs import RunDir, read_json, sha256_bytes, write_json
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run


@pytest.fixture
def repo(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> RunDir:
    """A run the CLI can resolve from the current working directory."""
    (cfg.root / ".git").mkdir(exist_ok=True)  # so find_repo_root() stops here
    rd = make_run(
        cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA, screenshots={"home.png": (10, 20, 30)}
    )
    seed = read_json(rd.path / "seed.json")
    seed["artifacts"] = rd.scan_artifacts()
    write_json(rd.run_json, seed)
    monkeypatch.chdir(cfg.root)
    return rd


@pytest.fixture
def pack_file(repo: RunDir, valid_pack: dict[str, Any], tmp_path: Path) -> Path:
    doc = copy.deepcopy(valid_pack)
    doc["runId"] = repo.run_id
    doc["candidateSha"] = CANDIDATE_SHA
    shot = repo.evidence_dir / "candidate-a" / "home.png"
    doc["annotations"][0]["artifactSha256"] = sha256_bytes(shot.read_bytes())
    path = tmp_path / "feedback.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _invoke(*args: str):
    return CliRunner().invoke(app, list(args))


def test_feedback_apply_creates_a_successor(repo: RunDir, pack_file: Path) -> None:
    result = _invoke("feedback", "apply", str(pack_file), repo.run_id, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert payload["successorRun"]


def test_feedback_apply_exits_nonzero_on_refusal(
    repo: RunDir, pack_file: Path, tmp_path: Path
) -> None:
    """In CI, a refusal that exits 0 is indistinguishable from success."""
    doc = json.loads(pack_file.read_text(encoding="utf-8"))
    doc["candidateSha"] = "f" * 40
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(doc), encoding="utf-8")

    result = _invoke("feedback", "apply", str(stale), repo.run_id)
    assert result.exit_code == 1
    assert "refused" in result.output


def test_feedback_apply_reports_a_missing_pack(repo: RunDir, tmp_path: Path) -> None:
    result = _invoke("feedback", "apply", str(tmp_path / "nope.json"), repo.run_id)
    assert result.exit_code == 2


def test_feedback_route_override(repo: RunDir, pack_file: Path) -> None:
    result = _invoke(
        "feedback", "apply", str(pack_file), repo.run_id, "--route", "inner", "--json"
    )
    assert json.loads(result.output)["route"] == "inner"


def test_feedback_validate_accepts_a_good_pack(repo: RunDir, pack_file: Path) -> None:
    result = _invoke("feedback", "validate", str(pack_file))
    assert result.exit_code == 0
    assert "valid" in result.output


def test_feedback_validate_rejects_a_bad_pack(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schemaVersion": "adlc-human-feedback/v1"}), encoding="utf-8")

    result = _invoke("feedback", "validate", str(bad))
    assert result.exit_code == 1
    assert "INVALID" in result.output


def test_feedback_validate_never_writes(repo: RunDir, pack_file: Path) -> None:
    _invoke("feedback", "validate", str(pack_file))
    assert repo.latest_stage("feedback") is None


def test_report_serve_refuses_without_a_report(repo: RunDir) -> None:
    result = _invoke("report-serve", repo.run_id, "--no-open")
    assert result.exit_code == 2
    assert "run `adlc report` first" in result.output


# ---------------------------------------------------------------------------
# Ordinary bad input is a message and an exit code, never a traceback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["apply", "validate"])
def test_malformed_json_exits_cleanly(
    repo: RunDir, tmp_path: Path, command: str
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"schemaVersion": "adlc-human-feedback/v1",', encoding="utf-8")

    result = _invoke("feedback", command, str(broken), "--json")

    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not valid JSON" in result.output


@pytest.mark.parametrize("command", ["apply", "validate"])
def test_undecodable_pack_exits_cleanly(
    repo: RunDir, tmp_path: Path, command: str
) -> None:
    broken = tmp_path / "latin1.json"
    broken.write_bytes(b'{"summary": "caf\xe9"}')

    result = _invoke("feedback", command, str(broken), "--json")

    assert result.exit_code == 2, result.output
    assert "cannot read" in result.output or "not valid JSON" in result.output


def test_an_out_of_enum_route_is_rejected_by_the_cli(
    repo: RunDir, pack_file: Path
) -> None:
    result = _invoke(
        "feedback", "apply", str(pack_file), repo.run_id, "--route", "Outer", "--json"
    )

    assert result.exit_code == 2, result.output
    assert "--route must be one of" in result.output


def test_review_apply_with_a_feedback_pack(repo: RunDir, pack_file: Path, tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "review": {
                    "state": "changes_requested",
                    "commit_id": CANDIDATE_SHA,
                    "user": {"login": "maintainer"},
                    "body": "revise please",
                },
                "pull_request": {"head": {"sha": CANDIDATE_SHA}, "labels": []},
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(
        "review", "apply", repo.run_id, "--event", str(event),
        "--feedback-pack", str(pack_file), "--no-retrigger", "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert payload["reviewApplied"] is True
    assert payload["authorisedBy"] == "maintainer"


def test_review_apply_refuses_a_pack_for_another_commit(
    repo: RunDir, pack_file: Path, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "review": {
                    "state": "changes_requested",
                    "commit_id": "9" * 40,
                    "user": {"login": "maintainer"},
                },
                "pull_request": {"head": {"sha": "9" * 40}, "labels": []},
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(
        "review", "apply", repo.run_id, "--event", str(event),
        "--feedback-pack", str(pack_file), "--json",
    )

    assert result.exit_code == 1, result.output
    assert "borrow that permission" in result.output


def test_review_apply_missing_event_exits_two(repo: RunDir, tmp_path: Path) -> None:
    result = _invoke("review", "apply", repo.run_id, "--event", str(tmp_path / "nope.json"))

    assert result.exit_code == 2, result.output
    assert "no such event" in result.output
