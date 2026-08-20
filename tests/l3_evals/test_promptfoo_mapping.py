"""promptfoo config generation and ``results.json`` → ``RubricScore`` mapping."""

from __future__ import annotations

import json
from typing import Any

import pytest

from adlc.adapters.evals.assert_ import (
    NOT_EVALUATED,
    build_rubric_score,
    iter_criteria,
    resolve_threshold,
)
from adlc.adapters.evals.promptfoo import (
    DEFAULT_PROVIDERS,
    build_promptfoo_config,
    map_promptfoo_results,
)

from .conftest import read_fixture


@pytest.fixture
def payload() -> dict[str, Any]:
    return json.loads(read_fixture("promptfoo-results.json"))


@pytest.fixture
def score(rubric: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    outcomes = map_promptfoo_results(payload, iter_criteria(rubric))
    return build_rubric_score(
        rubric,
        outcomes,
        threshold=resolve_threshold(rubric),
        backend="promptfoo",
        shared_evidence=["evals/promptfoo/results.json"],
    )


def test_generated_config_has_one_llm_rubric_test_per_criterion(rubric: dict[str, Any]) -> None:
    specs = iter_criteria(rubric)
    config = build_promptfoo_config(specs, "the run context", 0.7)

    assert config["providers"] == list(DEFAULT_PROVIDERS)
    assert config["prompts"] == ["{{context}}"]
    assert len(config["tests"]) == len(specs)

    for spec, test in zip(specs, config["tests"], strict=True):
        # The criterion id travels in three places so results can always be mapped back.
        assert test["description"] == spec.id
        assert test["metadata"]["criterionId"] == spec.id
        assert test["vars"]["criterionId"] == spec.id
        # promptfoo puts `threshold` on the test, not on the assertion.
        assert test["threshold"] == 0.7
        assert test["assert"] == [{"type": "llm-rubric", "value": spec.statement}]


def test_generated_config_can_set_an_explicit_grader(rubric: dict[str, Any]) -> None:
    config = build_promptfoo_config(
        iter_criteria(rubric), "ctx", 0.7, {"grader": "openai:gpt-4o-mini"}
    )
    assert config["defaultTest"]["options"]["provider"] == "openai:gpt-4o-mini"


def test_results_map_onto_criteria_by_id(rubric: dict[str, Any], payload: dict[str, Any]) -> None:
    outcomes = map_promptfoo_results(payload, iter_criteria(rubric))
    assert outcomes["R-contrast-01"].score == pytest.approx(1.0)
    assert outcomes["R-perf-01"].score == pytest.approx(0.4)
    # An errored result was never graded — it must not be scored 0.0 "on merit".
    assert outcomes["R-a11y-01"].score is None
    assert "503" in outcomes["R-a11y-01"].rationale


def test_component_reasons_become_the_rationale(
    rubric: dict[str, Any], payload: dict[str, Any]
) -> None:
    outcomes = map_promptfoo_results(payload, iter_criteria(rubric))
    assert "contrast floor satisfied" in outcomes["R-contrast-01"].rationale.lower()


def test_score_normalises_to_the_frozen_shape(score: dict[str, Any]) -> None:
    assert set(score) == {"overall", "threshold", "passed", "criteria"}
    # (1.0 * 2) + (0.4 * 1) + (0.0 * 1) over a total weight of 4.
    assert score["overall"] == pytest.approx(0.6)
    assert score["passed"] is False
    by_id = {c["id"]: c for c in score["criteria"]}
    assert by_id["R-contrast-01"]["passed"] is True
    assert by_id["R-perf-01"]["passed"] is False
    assert by_id["R-a11y-01"]["rationale"].startswith(NOT_EVALUATED)


def test_unknown_payload_shape_yields_nothing_rather_than_guessing(
    rubric: dict[str, Any],
) -> None:
    assert map_promptfoo_results({"unexpected": "shape"}, iter_criteria(rubric)) == {}
    assert map_promptfoo_results([], iter_criteria(rubric)) == {}


def test_positional_fallback_only_applies_on_an_exact_count_match(
    rubric: dict[str, Any],
) -> None:
    specs = iter_criteria(rubric)
    anonymous = {
        "results": {
            "results": [
                {"gradingResult": {"pass": True, "score": 1.0, "reason": "ok"}},
                {"gradingResult": {"pass": False, "score": 0.0, "reason": "no"}},
            ]
        }
    }
    # Two records, three criteria: refuse to guess.
    assert map_promptfoo_results(anonymous, specs) == {}

    anonymous["results"]["results"].append(
        {"gradingResult": {"pass": True, "score": 0.9, "reason": "ok"}}
    )
    mapped = map_promptfoo_results(anonymous, specs)
    assert [mapped[spec.id].score for spec in specs] == [1.0, 0.0, 0.9]
