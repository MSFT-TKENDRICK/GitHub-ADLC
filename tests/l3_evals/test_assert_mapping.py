"""JSONL → ``RubricScore`` mapping for the ASSERT backend.

This is the load-bearing test of L3: whatever ASSERT emits, the gate and the report only
ever see the frozen ``RubricScore`` shape. Driven by a checked-in fixture that mirrors the
real ``scores.jsonl`` record shape from ``responsibleai/ASSERT``
(``assert_ai/stages/judge.py``): a verdict is *violation booleans*, not a score float.
"""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.evals.assert_ import (
    NOT_EVALUATED,
    REQUIRES_JUDGE,
    CriterionSpec,
    build_rubric_score,
    coerce_score,
    iter_criteria,
    iter_jsonl,
    map_records_to_outcomes,
    render_eval_config,
    resolve_threshold,
    slugify,
    verdict_score,
)

from .conftest import read_fixture


@pytest.fixture
def outcomes(rubric: dict[str, Any]) -> dict[str, Any]:
    records = list(iter_jsonl(read_fixture("assert-scores.jsonl")))
    return map_records_to_outcomes(records, iter_criteria(rubric))


@pytest.fixture
def score(rubric: dict[str, Any], outcomes: dict[str, Any]) -> dict[str, Any]:
    return build_rubric_score(
        rubric,
        outcomes,
        threshold=resolve_threshold(rubric),
        backend="ASSERT",
        shared_evidence=["evals/assert-results.jsonl"],
    )


def test_fixture_parses_as_jsonl() -> None:
    records = list(iter_jsonl(read_fixture("assert-scores.jsonl")))
    assert len(records) == 6
    assert {r["behavior"] for r in records} == {
        "r_contrast_01",
        "r_perf_01",
        "r_unknown_behaviour",
    }


def test_verdict_score_inverts_violation_booleans() -> None:
    clean = {"judge_status": "ok", "verdict": {"dimensions": {"policy_violation": False}}}
    violated = {"judge_status": "ok", "verdict": {"dimensions": {"policy_violation": True}}}
    overrefused = {
        "judge_status": "ok",
        "verdict": {"dimensions": {"policy_violation": False, "overrefusal": True}},
    }
    assert verdict_score(clean) == 1.0
    assert verdict_score(violated) == 0.0
    assert verdict_score(overrefused) == 0.0


def test_unjudged_rows_are_excluded_not_counted_as_passes() -> None:
    failed = {
        "judge_status": "judge_failed",
        "judge_error": "timeout",
        "verdict": {"dimensions": {}},
    }
    assert verdict_score(failed) is None


def test_criterion_score_is_the_share_of_clean_judged_rows(outcomes: dict[str, Any]) -> None:
    # r_contrast_01: three judged rows, one violation → 2/3.
    assert outcomes["R-contrast-01"].score == pytest.approx(2 / 3)
    # r_perf_01: one clean judged row, one judge_failed row that must not count as a pass.
    assert outcomes["R-perf-01"].score == pytest.approx(1.0)
    assert "not judged" in outcomes["R-perf-01"].rationale
    # R-a11y-01 has no rows at all.
    assert "R-a11y-01" not in outcomes


def test_records_for_unknown_behaviours_are_ignored(outcomes: dict[str, Any]) -> None:
    assert set(outcomes) <= {"R-contrast-01", "R-perf-01", "R-a11y-01"}


def test_rubric_score_has_the_frozen_shape(score: dict[str, Any]) -> None:
    assert set(score) == {"overall", "threshold", "passed", "criteria"}
    assert isinstance(score["overall"], float)
    assert isinstance(score["passed"], bool)
    for criterion in score["criteria"]:
        assert set(criterion) == {"id", "score", "weight", "passed", "rationale", "evidence"}
        assert 0.0 <= criterion["score"] <= 1.0
        assert isinstance(criterion["passed"], bool)
        assert isinstance(criterion["evidence"], list)
        assert all(isinstance(ref, str) for ref in criterion["evidence"])


def test_overall_is_weight_aware(score: dict[str, Any]) -> None:
    # (2/3 * 2) + (1.0 * 1) + (0.0 * 1), over a total weight of 4.
    assert score["overall"] == pytest.approx(round(((2 / 3) * 2 + 1.0) / 4, 4))
    assert score["threshold"] == 0.7
    assert score["passed"] is False


def test_unevaluated_criterion_fails_closed_and_says_so(score: dict[str, Any]) -> None:
    by_id = {c["id"]: c for c in score["criteria"]}
    a11y = by_id["R-a11y-01"]
    assert a11y["score"] == 0.0
    assert a11y["passed"] is False
    assert a11y["rationale"].startswith(NOT_EVALUATED)
    assert "R-a11y-01" in a11y["rationale"]
    # Carries the spine's marker, so `adlc.stages.evals` counts it in data.unevaluated
    # and `adlc.stages.autoresearch` can aggregate it across runs.
    assert REQUIRES_JUDGE in a11y["rationale"]


def test_every_criterion_cites_the_raw_jsonl(score: dict[str, Any]) -> None:
    for criterion in score["criteria"]:
        assert "evals/assert-results.jsonl" in criterion["evidence"]


def test_judge_evidence_and_rationale_survive_normalisation(score: dict[str, Any]) -> None:
    contrast = next(c for c in score["criteria"] if c["id"] == "R-contrast-01")
    assert "2/3 judged test cases passed without violation" in contrast["rationale"]
    assert "4.5:1" in contrast["rationale"]
    assert any("span-0a1b" in ref for ref in contrast["evidence"])


def test_no_criteria_yields_a_zero_score_not_a_crash() -> None:
    empty = build_rubric_score({"id": "x", "criteria": []}, {}, threshold=0.7, backend="ASSERT")
    assert empty == {"overall": 0.0, "threshold": 0.7, "passed": False, "criteria": []}


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        (True, 1.0, 1.0),
        (False, 1.0, 0.0),
        ("pass", 1.0, 1.0),
        ("FAIL", 1.0, 0.0),
        (0.42, 1.0, 0.42),
        (5, 5.0, 1.0),
        (1, 5.0, 0.0),
        (3, 5.0, 0.5),
        (2.0, 1.0, 1.0),
        (-1.0, 1.0, 0.0),
        ("not a score", 1.0, None),
        (None, 1.0, None),
    ],
)
def test_coerce_score(value: Any, scale: float, expected: float | None) -> None:
    assert coerce_score(value, scale=scale) == expected


def test_slug_matching_bridges_criterion_ids_and_assert_behaviour_names() -> None:
    spec = CriterionSpec(id="R-contrast-01", statement="…", weight=1.0)
    assert spec.slug == "r_contrast_01"
    assert slugify("R contrast 01") == "r_contrast_01"


def test_rendered_eval_config_matches_the_assert_schema(rubric: dict[str, Any]) -> None:
    yaml = pytest.importorskip("yaml")
    spec = iter_criteria(rubric)[0]
    text = render_eval_config(
        spec,
        suite="adlc-2026-08-19-a1b2-r_contrast_01",
        run_id="2026-08-19-a1b2",
        context="# Dark mode\n\nUsers can switch to a dark theme.\n",
        settings={"target": {"callable": "demo.app:chat"}, "model": "azure/gpt-4o"},
    )
    payload = yaml.safe_load(text)
    assert payload["suite"] == "adlc-2026-08-19-a1b2-r_contrast_01"
    assert payload["run"] == "2026-08-19-a1b2"
    assert payload["behavior"]["name"] == "r_contrast_01"
    assert payload["behavior"]["description"] == spec.statement
    assert "dark theme" in payload["context"]
    assert payload["default_model"]["name"] == "azure/gpt-4o"
    assert set(payload["pipeline"]) == {"systematize", "test_set", "inference", "judge"}
    assert payload["pipeline"]["inference"]["target"] == {"callable": "demo.app:chat"}
    assert payload["pipeline"]["judge"]["model"]["name"] == "azure/gpt-4o"
