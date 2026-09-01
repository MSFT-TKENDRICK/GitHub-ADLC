"""The dry run: what ingestion would do, said out loud before it does it.

Schema validity is not the contract. A pack can validate cleanly and still be
refused for a stale ``candidateSha``, or -- worse -- be *applied* with half its
annotations silently discarded for citing an artifact the run does not have. A
GUI author who only has ``adlc feedback validate`` finds that out after a human
has filled in the form.

So the refusal rules are extracted into :func:`plan_feedback`, and both
``apply_feedback`` and ``adlc feedback validate --run`` consume that one
function. These tests exist to hold that seam: a second implementation of
"what would happen" would be free to drift from what actually happens, and the
drift would surface as a reviewer's work being thrown away for no stated reason.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from typer.testing import CliRunner

from adlc.config import Config
from adlc.reduce import load_run
from adlc.runs import RunDir, read_json, sha256_bytes, write_json
from adlc.stages import feedback as fb
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run

runner = CliRunner()


@pytest.fixture
def run(cfg: Config) -> RunDir:
    return make_run(
        cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA,
        screenshots={"home.png": (10, 20, 30)},
    )


@pytest.fixture
def pack(run: RunDir, valid_pack: dict[str, Any]) -> dict[str, Any]:
    """A pack whose annotation cites an artifact the run actually has."""
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


def plan(run: RunDir, doc: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return fb.plan_feedback(doc, load_run(run), run.run_id, **kw)


# ---------------------------------------------------------------------------
# The planner agrees with the applier -- the whole point
# ---------------------------------------------------------------------------


def test_a_plan_that_says_apply_does_apply(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    assert plan(run, pack)["refusal"] is None
    result = fb.apply_feedback(cfg, run, pack, retrigger=False)
    assert result["applied"] is True


@pytest.mark.parametrize(
    "mutate,expect",
    [
        (lambda d: d.__setitem__("candidateSha", "f" * 40), "stale"),
        (lambda d: d.__setitem__("runId", "some-other-run"), "packRunId"),
        (lambda d: d.__setitem__("packDigest", "sha256:" + "0" * 64), "computedDigest"),
        (lambda d: d.__setitem__("verdict", "not-a-verdict"), "errors"),
    ],
)
def test_every_refusal_the_planner_predicts_is_a_refusal_in_practice(
    cfg: Config, run: RunDir, pack: dict[str, Any], mutate: Any, expect: str
) -> None:
    """If these two ever disagree the dry run becomes a lie."""
    mutate(pack)
    predicted = plan(run, pack)
    assert predicted["refusal"] is not None, f"planner accepted a pack it should refuse ({expect})"
    assert expect in predicted["refusal"]["data"] or expect in predicted["refusal"]["reason"]

    actual = fb.apply_feedback(cfg, run, pack, retrigger=False)
    assert actual["applied"] is False
    assert actual["reason"] == predicted["refusal"]["reason"]


def test_a_blocking_conflict_is_predicted_not_discovered(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    """Accepting while carrying a blocker is the contradiction a GUI must catch."""
    pack["verdict"] = "accept"
    pack["annotations"][0]["severity"] = "blocker"
    pack.pop("packDigest", None)
    predicted = plan(run, pack)
    assert predicted["refusal"] is not None
    assert "blocking" in predicted["refusal"]["data"]
    assert fb.apply_feedback(cfg, run, pack, retrigger=False)["applied"] is False


def test_an_unknown_route_is_refused_before_anything_is_written(
    cfg: Config, run: RunDir, pack: dict[str, Any]
) -> None:
    predicted = plan(run, pack, route="sideways")
    assert predicted["refusal"] is not None
    assert predicted["refusal"]["data"]["route"] == "sideways"


# ---------------------------------------------------------------------------
# Discards are not refusals, which is exactly why they must be surfaced
# ---------------------------------------------------------------------------


def test_uncited_annotations_are_reported_as_discarded_not_refused(
    run: RunDir, pack: dict[str, Any]
) -> None:
    """The quiet failure mode: the pack applies and the work vanishes."""
    pack["annotations"][0]["artifactSha256"] = "e" * 64
    pack.pop("packDigest", None)
    predicted = plan(run, pack)
    assert predicted["refusal"] is None, "an uncited annotation is a discard, not a refusal"
    assert len(predicted["discarded"]) == 1
    assert predicted["pack"]["annotations"] == []


def test_the_planner_leaves_the_run_untouched(
    run: RunDir, pack: dict[str, Any]
) -> None:
    """A dry run that wrote a stage result would not be a dry run."""
    before = sorted(p.name for p in run.path.rglob("*"))
    plan(run, pack)
    plan(run, {"nonsense": True})
    assert sorted(p.name for p in run.path.rglob("*")) == before
    assert run.latest_stage("feedback") is None


# ---------------------------------------------------------------------------
# The CLI surface a GUI author actually uses
# ---------------------------------------------------------------------------


def cli(*args: str) -> Any:
    from adlc.cli import app

    return runner.invoke(app, list(args))


@pytest.fixture
def in_repo(cfg: Config, run: RunDir, monkeypatch: pytest.MonkeyPatch) -> RunDir:
    """The CLI resolves the run from the working directory, not from ``cfg``."""
    monkeypatch.chdir(cfg.root)
    return run


def test_validate_without_a_run_still_only_checks_the_schema(
    tmp_path: Any, pack: dict[str, Any]
) -> None:
    """Backwards compatible: the old behaviour is unchanged when --run is absent."""
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = cli("feedback", "validate", str(path))
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_validate_with_a_run_predicts_a_refusal(
    cfg: Config, in_repo: RunDir, pack: dict[str, Any], tmp_path: Any
) -> None:
    run = in_repo
    pack["candidateSha"] = "f" * 40
    pack.pop("packDigest", None)
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = cli("feedback", "validate", str(path), "--run", run.run_id, "--json")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["wouldApply"] is False
    assert payload["refusal"]["data"]["stale"] is True


def test_validate_with_a_run_warns_about_silent_discards(
    cfg: Config, in_repo: RunDir, pack: dict[str, Any], tmp_path: Any
) -> None:
    """A pack that applies but loses annotations must not look like success."""
    run = in_repo
    pack["annotations"][0]["artifactSha256"] = "e" * 64
    pack.pop("packDigest", None)
    path = tmp_path / "uncited.json"
    path.write_text(json.dumps(pack), encoding="utf-8")

    result = cli("feedback", "validate", str(path), "--run", run.run_id)
    assert result.exit_code == 0, result.output
    assert "DISCARDED" in result.output

    as_json = cli("feedback", "validate", str(path), "--run", run.run_id, "--json")
    payload = json.loads(as_json.output)
    assert payload["wouldApply"] is True
    assert len(payload["discardedAnnotations"]) == 1


def test_validate_with_a_run_reports_a_clean_pack(
    cfg: Config, in_repo: RunDir, pack: dict[str, Any], tmp_path: Any
) -> None:
    run = in_repo
    path = tmp_path / "good.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = cli("feedback", "validate", str(path), "--run", run.run_id, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["wouldApply"] is True
    assert payload["discardedAnnotations"] == []
    assert payload["route"] in fb.VALID_ROUTES
    assert payload["citationCheck"] == "verified"


def test_the_dry_run_did_not_apply_anything(
    cfg: Config, in_repo: RunDir, pack: dict[str, Any], tmp_path: Any
) -> None:
    run = in_repo
    path = tmp_path / "good.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    cli("feedback", "validate", str(path), "--run", run.run_id)
    assert run.latest_stage("feedback") is None, "validate must never write a stage"
    assert not fb.feedback_dir(run).exists() or not list(fb.feedback_dir(run).glob("*.json"))
