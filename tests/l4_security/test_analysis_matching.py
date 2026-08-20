"""Exact-head-SHA analysis matching — the anti-stale-green core.

The fixture is deliberately adversarial: the *newest* analysis in the recorded
response is for a different commit on the default branch. A gate that reads
``analyses[0]``, or that queries alerts without pinning to a commit, evaluates
that analysis instead and reports a false green for the PR.
"""

from __future__ import annotations

from typing import Any

import pytest

from adlc.adapters.gate.codeql import find_matching_analysis

from .conftest import HEAD_SHA, PR_REF, STALE_SHA, load_fixture


@pytest.fixture
def analyses() -> list[dict[str, Any]]:
    return load_fixture("code_scanning_analyses.json")["analyses"]


def test_fixture_is_actually_adversarial(analyses: list[dict[str, Any]]) -> None:
    """Guard the guard: the newest analysis must NOT be the one we want."""
    assert analyses[0]["commit_sha"] == STALE_SHA
    assert analyses[0]["commit_sha"] != HEAD_SHA


def test_matches_the_exact_head_sha_not_the_newest(analyses: list[dict[str, Any]]) -> None:
    match = find_matching_analysis(analyses, sha=HEAD_SHA)
    assert match is not None
    assert match["commit_sha"] == HEAD_SHA
    assert match["id"] == 900299
    assert match["id"] != analyses[0]["id"], "must not fall back to the newest analysis"


def test_returns_none_when_the_sha_is_absent(analyses: list[dict[str, Any]]) -> None:
    """No match must mean None -- which the caller turns into not_run."""
    assert find_matching_analysis(analyses, sha="f" * 40) is None


def test_matching_is_case_insensitive(analyses: list[dict[str, Any]]) -> None:
    assert find_matching_analysis(analyses, sha=HEAD_SHA.upper()) is not None


def test_abbreviated_sha_does_not_match(analyses: list[dict[str, Any]]) -> None:
    """Prefix matching would let a short/ambiguous SHA satisfy the gate."""
    assert find_matching_analysis(analyses, sha=HEAD_SHA[:12]) is None


def test_empty_sha_never_matches(analyses: list[dict[str, Any]]) -> None:
    for empty in ("", "   ", None):
        assert find_matching_analysis(analyses, sha=empty) is None  # type: ignore[arg-type]


def test_ref_pins_the_match(analyses: list[dict[str, Any]]) -> None:
    assert find_matching_analysis(analyses, sha=HEAD_SHA, ref=PR_REF) is not None
    # Same commit, wrong ref => no match.
    assert find_matching_analysis(analyses, sha=HEAD_SHA, ref="refs/heads/main") is None


def test_category_pins_the_match(analyses: list[dict[str, Any]]) -> None:
    good = ".github/workflows/codeql.yml:analyze/language:python"
    assert find_matching_analysis(analyses, sha=HEAD_SHA, category=good) is not None
    assert find_matching_analysis(analyses, sha=HEAD_SHA, category="other/language:go") is None


def test_analysis_key_pins_the_workflow(analyses: list[dict[str, Any]]) -> None:
    """A different workflow's analysis of the same commit must not satisfy us."""
    assert find_matching_analysis(
        analyses, sha=HEAD_SHA, analysis_key=".github/workflows/codeql.yml"
    ) is not None
    assert find_matching_analysis(
        analyses, sha=HEAD_SHA, analysis_key=".github/workflows/other.yml"
    ) is None


def test_ignores_malformed_entries() -> None:
    junk: list[Any] = [None, "nonsense", 42, [], {"commit_sha": None}, {"no_commit": True}]
    assert find_matching_analysis(junk, sha=HEAD_SHA) is None
    assert find_matching_analysis([*junk, {"commit_sha": HEAD_SHA, "id": 1}], sha=HEAD_SHA) == {
        "commit_sha": HEAD_SHA,
        "id": 1,
    }


def test_empty_response_is_not_a_match() -> None:
    assert find_matching_analysis([], sha=HEAD_SHA) is None
