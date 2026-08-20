"""The ``evals`` gate: RubricScore in, GateResult out — fail closed.

The gate never evaluates anything itself. It reads whatever ``RubricScore`` the selected
runner produced and passes iff ``overall >= threshold``. No score at all is ``not_run``,
which a *required* gate turns into a build failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.evals.assert_ import NOT_EVALUATED
from adlc.adapters.gate.evals import EvalsGate, find_rubric_score
from adlc.config import Config
from adlc.ports import GATE_IDS

PASSING: dict[str, Any] = {
    "overall": 0.86,
    "threshold": 0.7,
    "passed": True,
    "criteria": [
        {
            "id": "R-contrast-01",
            "score": 1.0,
            "weight": 2.0,
            "passed": True,
            "rationale": "3/3 judged test cases passed without violation",
            "evidence": ["evals/assert-results.jsonl"],
        },
        {
            "id": "R-perf-01",
            "score": 0.58,
            "weight": 1.0,
            "passed": False,
            "rationale": "latency budget exceeded on 2 of 5 runs",
            "evidence": ["evals/assert-results.jsonl"],
        },
    ],
}

FAILING: dict[str, Any] = {
    **PASSING,
    "overall": 0.42,
    "passed": False,
}

PARTLY_UNEVALUATED: dict[str, Any] = {
    "overall": 0.5,
    "threshold": 0.7,
    "passed": False,
    "criteria": [
        PASSING["criteria"][0],
        {
            "id": "R-a11y-01",
            "score": 0.0,
            "weight": 1.0,
            "passed": False,
            "rationale": f"{NOT_EVALUATED} by ASSERT: no ASSERT record matched criterion",
            "evidence": [],
        },
    ],
}


def write_score(run_dir: Path, score: dict[str, Any], name: str = "score.json") -> None:
    (run_dir / "evals").mkdir(parents=True, exist_ok=True)
    (run_dir / "evals" / name).write_text(json.dumps(score), encoding="utf-8")


def test_gate_identity_matches_the_registry() -> None:
    gate = EvalsGate()
    assert gate.id == "evals"
    assert gate.id in GATE_IDS
    assert gate.kind == "gate"
    # Optional by default; the `full` profile is what promotes it to required.
    assert gate.required_by_default is False


def test_detect_is_free(cfg: Config) -> None:
    available, reason = EvalsGate.detect(cfg)
    assert available is True
    assert reason


def test_missing_score_is_not_run_not_pass(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "not_run"
    assert result["observed"]["score"] is None
    assert "adlc eval" in result["message"]
    assert result["evidence"] == []


def test_required_missing_score_is_high_severity(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    cfg.profile = "full"
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["required"] is True
    assert result["status"] == "not_run"
    assert result["severity"] == "high"


def test_pass_when_overall_meets_threshold(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, PASSING)
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "pass"
    assert result["observed"]["overall"] == 0.86
    assert result["observed"]["threshold"] == 0.7
    assert result["observed"]["failedCriteria"] == ["R-perf-01"]
    assert result["evidence"] == ["evals/score.json"]
    assert len(result["observed"]["criteria"]) == 2


def test_fail_when_overall_is_below_threshold(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, FAILING)
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "fail"
    assert "0.42" in result["message"]
    assert "R-perf-01" in result["message"]


def test_boundary_equal_to_threshold_passes(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, {**PASSING, "overall": 0.7})
    assert EvalsGate().evaluate(run_doc, cfg)["status"] == "pass"


def test_unevaluated_criteria_are_surfaced(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, PARTLY_UNEVALUATED)
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "fail"
    assert result["observed"]["unevaluatedCriteria"] == ["R-a11y-01"]
    assert NOT_EVALUATED in result["message"]


def test_score_with_no_criteria_is_not_run(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, {"overall": 1.0, "threshold": 0.7, "passed": True, "criteria": []})
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "not_run"
    assert result["observed"]["passed"] is False


def test_backend_specific_score_files_are_found(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, PASSING, name="promptfoo-score.json")
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "pass"
    assert result["evidence"] == ["evals/promptfoo-score.json"]


def test_stage_results_win_over_loose_files(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, FAILING)
    run_doc["stages"] = [
        {"stage": "eval", "attempt": 1, "status": "ok", "data": {"score": PASSING}}
    ]
    found = find_rubric_score(run_doc, cfg)
    assert found is not None
    score, source = found
    assert score["overall"] == 0.86
    assert source == "stages/eval.1.json"
    assert EvalsGate().evaluate(run_doc, cfg)["status"] == "pass"


def test_latest_eval_attempt_wins(cfg: Config, run_dir: Path, run_doc: dict[str, Any]) -> None:
    run_doc["stages"] = [
        {"stage": "eval", "attempt": 1, "status": "ok", "data": {"score": FAILING}},
        {"stage": "eval", "attempt": 2, "status": "ok", "data": {"score": PASSING}},
    ]
    result = EvalsGate().evaluate(run_doc, cfg)
    assert result["status"] == "pass"
    assert result["observed"]["source"] == "stages/eval.2.json"


def test_unrelated_json_in_evals_dir_is_ignored(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    (run_dir / "evals").mkdir(parents=True, exist_ok=True)
    (run_dir / "evals" / "assert-config.json").write_text('{"hello": "world"}', encoding="utf-8")
    (run_dir / "evals" / "broken.json").write_text("{not json", encoding="utf-8")
    assert EvalsGate().evaluate(run_doc, cfg)["status"] == "not_run"


def test_gate_result_has_the_frozen_shape(
    cfg: Config, run_dir: Path, run_doc: dict[str, Any]
) -> None:
    write_score(run_dir, PASSING)
    result = EvalsGate().evaluate(run_doc, cfg)
    assert set(result) == {
        "id", "required", "status", "severity", "observed", "expected", "message", "evidence",
    }
    assert result["status"] in {"pass", "fail", "not_run"}
    assert result["severity"] in {"low", "medium", "high", "critical"}
    assert isinstance(result["required"], bool)
    assert isinstance(result["evidence"], list)


@pytest.mark.parametrize("bad", [None, 17, "nope", {"runId": ""}])
def test_gate_never_crashes_on_a_malformed_run(cfg: Config, bad: Any) -> None:
    run = bad if isinstance(bad, dict) else {"runId": "missing-run"}
    result = EvalsGate().evaluate(run, cfg)
    assert result["status"] == "not_run"
