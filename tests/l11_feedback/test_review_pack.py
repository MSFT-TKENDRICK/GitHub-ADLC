"""The CI authority path: a feedback pack applied under a native PR review.

A pack is a file, and a file carries no permission. These tests pin the rule
that makes it safe to honour one on a shared runner: the authority comes from a
`pull_request_review` event, and the pack must describe the very commit that
review authorised.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from adlc.config import Config
from adlc.reduce import load_run
from adlc.runs import RunDir, read_json, sha256_bytes, write_json
from adlc.stages.feedback import apply_pack_with_review
from adlc.stages.review import apply_review
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run

OTHER_SHA = "9" * 40


def _event(state: str = "changes_requested", sha: str = CANDIDATE_SHA) -> dict[str, Any]:
    return {
        "review": {
            "state": state,
            "commit_id": sha,
            "user": {"login": "maintainer"},
            "body": "please revise",
        },
        "pull_request": {"head": {"sha": sha}, "labels": []},
    }


@pytest.fixture
def run(cfg: Config) -> RunDir:
    return make_run(
        cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA,
        screenshots={"home.png": (10, 20, 30)},
    )


@pytest.fixture
def pack(run: RunDir, valid_pack: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(valid_pack)
    doc["runId"] = run.run_id
    doc["candidateSha"] = CANDIDATE_SHA
    shot = run.evidence_dir / "candidate-a" / "home.png"
    doc["annotations"][0]["artifactSha256"] = sha256_bytes(shot.read_bytes())
    doc["annotations"][0]["artifactPath"] = run.rel(shot)
    seed = read_json(run.path / "seed.json")
    seed["artifacts"] = run.scan_artifacts()
    write_json(run.run_json, seed)
    return doc


# ---------------------------------------------------------------------------
# One act, one outcome
# ---------------------------------------------------------------------------


def test_pack_and_review_apply_as_one_act(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    result = apply_pack_with_review(cfg, run, _event(), pack, retrigger=False)

    assert result["applied"] is True
    assert result["reviewApplied"] is True
    assert result["authorisedBy"] == "maintainer"
    assert run.latest_stage("feedback")["status"] != "fail"
    assert run.latest_stage("review")["status"] != "fail"


def test_one_successor_not_two(cfg: Config, run: RunDir, pack: dict[str, Any]) -> None:
    """The forked-lineage guard: both halves want a successor, only one is made."""
    before = {p.name for p in run.path.parent.iterdir()}

    result = apply_pack_with_review(cfg, run, _event(), pack, retrigger=False)
    created = {p.name for p in run.path.parent.iterdir()} - before

    assert result["successorRun"] is not None
    assert created == {result["successorRun"]}, "a second successor forks the lineage"
    assert result["review"]["successorRun"] == result["successorRun"]
    assert run.latest_stage("review")["data"]["adoptedSuccessor"] is True


def test_successor_carries_the_outer_route(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    result = apply_pack_with_review(cfg, run, _event(), pack, retrigger=False)

    successor = load_run(RunDir(cfg, result["successorRun"]))
    assert successor["route"] == "outer"
    assert successor["referencesRun"] == run.run_id


# ---------------------------------------------------------------------------
# Binding: the pack may only borrow a permission granted for the same commit
# ---------------------------------------------------------------------------


def test_pack_for_a_different_commit_is_refused(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    result = apply_pack_with_review(cfg, run, _event(sha=OTHER_SHA), pack)

    assert result["applied"] is False
    assert "refusing to borrow that permission" in result["reason"]
    assert run.latest_stage("review") is None
    assert run.latest_stage("feedback")["status"] == "fail"


def test_unbound_review_is_refused(cfg: Config, run: RunDir, pack: dict[str, Any]) -> None:
    event = _event()
    event["review"]["commit_id"] = ""
    result = apply_pack_with_review(cfg, run, event, pack)

    assert result["applied"] is False
    assert "unbound review" in result["reason"]
    assert run.latest_stage("review") is None


def test_unknown_review_state_is_refused_not_raised(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    result = apply_pack_with_review(cfg, run, _event(state="exploded"), pack)

    assert result["applied"] is False
    assert "unsupported review state" in result["reason"]


def test_review_state_is_flattened_into_the_refusal(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    """An attacker-controlled state string must not inject lines into the record."""
    result = apply_pack_with_review(cfg, run, _event(state="x\n## injected"), pack)

    assert result["applied"] is False
    assert "\n" not in result["reason"]


def test_non_dict_pack_is_refused(cfg: Config, run: RunDir) -> None:
    result = apply_pack_with_review(cfg, run, _event(), ["not", "a", "pack"])

    assert result["applied"] is False
    assert "not a JSON object" in result["reason"]


# ---------------------------------------------------------------------------
# Failure and replay semantics
# ---------------------------------------------------------------------------


def test_a_refused_pack_leaves_the_review_unapplied(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    """Half-applying a composite decision is a worse surprise than refusing it."""
    pack["packDigest"] = "0" * 64

    result = apply_pack_with_review(cfg, run, _event(), pack)

    assert result["applied"] is False
    assert result["reviewApplied"] is False
    assert run.latest_stage("review") is None


def test_replay_does_not_move_the_adr_twice(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    first = apply_pack_with_review(cfg, run, _event(), pack, retrigger=False)
    assert first["reviewApplied"] is True

    second = apply_pack_with_review(cfg, run, _event(), pack, retrigger=False)

    assert second["applied"] is True
    assert second["replay"] is True
    assert second["reviewApplied"] is False
    assert second["successorRun"] == first["successorRun"]


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


def test_plain_review_is_unchanged_without_a_pack(cfg: Config, run: RunDir) -> None:
    """The default path must behave exactly as it did before this layer."""
    result = apply_review(cfg, run, _event())

    assert result["applied"] is True
    assert result["successorRun"] is not None
    assert run.latest_stage("review")["data"]["adoptedSuccessor"] is False


def test_approved_review_with_a_pack_creates_no_successor(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    pack["verdict"] = "accept"
    pack["annotations"][0]["severity"] = "minor"
    pack["diffDecisions"][0]["decision"] = "accept"

    result = apply_pack_with_review(cfg, run, _event(state="approved"), pack, retrigger=False)

    assert result["applied"] is True
    assert result["reviewApplied"] is True
    assert result["successorRun"] is None
    assert result["review"]["successorRun"] is None
