"""Chaos/fault-injection tests for filesystem failures.

These tests deliberately break writes and corrupted inputs at the seam where a
real machine fails: after the data is prepared, but before the filesystem makes
it durable. The invariant is fail-closed and leave the previous durable state
intact.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adlc.config import Config
from adlc.runs import RunDir, new_run_id, write_json
from adlc.stages.evidence_diff import compute_diff


def test_write_json_replace_failure_preserves_the_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"version": 1}\n', encoding="utf-8")

    def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        assert Path(src).name == "state.json.tmp"
        assert Path(dst) == target
        raise OSError("simulated disk-full during atomic replace")

    monkeypatch.setattr("adlc.runs.os.replace", fail_replace)

    with pytest.raises(OSError, match="disk-full"):
        write_json(target, {"version": 2})

    assert target.read_text(encoding="utf-8") == '{"version": 1}\n'
    assert target.with_suffix(".json.tmp").read_text(encoding="utf-8") == '{\n  "version": 2\n}\n'


def test_corrupted_baseline_pack_produces_a_stated_diff_absence(tmp_path: Path) -> None:
    cfg = Config(root=tmp_path)
    baseline = RunDir(cfg, new_run_id())
    candidate = RunDir(cfg, new_run_id())
    baseline.create(profile="minimal", brief_text="# Baseline\n")
    candidate.create(profile="minimal", brief_text="# Candidate\n", references_run=baseline.run_id)

    baseline.review_pack.parent.mkdir(parents=True, exist_ok=True)
    baseline.review_pack.write_bytes(b'{"schemaVersion": "\xff')

    diff = compute_diff(cfg, candidate)

    assert diff["baselineRunId"] is None
    assert diff["measurements"] == []
    assert diff["coverage"] == []
    assert diff["screenshots"] == []
    assert "unreadable evidence-review-pack.json" in diff["reason"]

