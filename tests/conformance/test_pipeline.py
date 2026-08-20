"""End-to-end conformance: the credential-free proof of the whole ADLC concept.

Maps 1:1 to the acceptance criteria in ``docs/PLAN.md`` section 8.1. Each test
names the criterion it proves.
"""

from __future__ import annotations

import json
from datetime import datetime

from adlc.config import Config
from adlc.reduce import aggregate_passed, load_run
from adlc.runs import RunDir
from adlc.schemas import is_valid
from adlc.stages.intake import run_qualify

# -- criterion 1 ------------------------------------------------------------

def test_run_json_validates_against_schema(completed) -> None:
    """C1: run.json validates against adlc-run/v1."""
    _, rd = completed
    valid, errors = is_valid("adlc-run", load_run(rd))
    assert valid, errors


def test_stage_history_is_append_only(cfg: Config, brief_file) -> None:
    """C1: a forced re-run appends an attempt and never edits history.

    Uses its own run because it deliberately mutates state.
    """
    from adlc.reduce import reduce_run
    from adlc.runs import new_run_id
    from adlc.stages.intake import run_intake

    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_qualify(cfg, rd)
    reduce_run(cfg, rd)

    before = [dict(s) for s in load_run(rd)["stages"]]
    qualify_before = [s for s in before if s["stage"] == "qualify"]

    run_qualify(cfg, rd)
    reduce_run(cfg, rd)
    after = [dict(s) for s in load_run(rd)["stages"]]
    qualify_after = [s for s in after if s["stage"] == "qualify"]

    assert len(qualify_after) == len(qualify_before) + 1
    assert qualify_after[-1]["attempt"] == qualify_before[-1]["attempt"] + 1
    # Every prior record survives byte-identically.
    for original in before:
        assert original in after


# -- criterion 2 ------------------------------------------------------------

def test_graph_is_acyclic_with_real_parallel_width(completed) -> None:
    """C2: the DAG is acyclic and at least two nodes share a level."""
    _, rd = completed
    graph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
    valid, errors = is_valid("taskgraph", graph)
    assert valid, errors

    levels: dict[int, int] = {}
    for node in graph["nodes"]:
        levels[node["level"]] = levels.get(node["level"], 0) + 1
    assert max(levels.values()) >= 2, f"no parallelism in the graph: {levels}"


def test_same_level_nodes_actually_ran_concurrently(completed) -> None:
    """C2: concurrency is real, proven by overlapping execution windows."""
    _, rd = completed
    build = [s for s in load_run(rd)["stages"] if s["stage"] == "build"][-1]
    nodes = build["data"]["nodes"]
    level_zero = [n for n in nodes if n["level"] == 0]
    assert len(level_zero) >= 2

    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value)

    windows = [(parse(n["started_at"]), parse(n["ended_at"])) for n in level_zero]
    latest_start = max(start for start, _ in windows)
    earliest_end = min(end for _, end in windows)
    assert latest_start <= earliest_end, (
        f"level-0 nodes did not overlap in time: {windows}"
    )


# -- criterion 5 ------------------------------------------------------------

def test_every_artifact_hash_verifies(completed) -> None:
    """C5: recorded artifact hashes match the bytes on disk."""
    from adlc.runs import sha256_file

    _, rd = completed
    artifacts = load_run(rd)["artifacts"]
    assert artifacts, "no artifacts were recorded"
    for artifact in artifacts:
        path = rd.path / artifact["path"]
        assert path.is_file(), f"missing artifact {artifact['path']}"
        assert sha256_file(path) == artifact["sha256"], artifact["path"]


def test_replayable_evidence_exists(completed) -> None:
    """C5: the evidence bundle is replayable, not just descriptive."""
    _, rd = completed
    evidence = rd.evidence_dir / "candidate-a"
    assert (evidence / "console.jsonl").is_file()
    assert (evidence / "run-manifest.json").is_file()
    replay = list(evidence.glob("replay.*"))
    assert replay, "no replay script was generated"


# -- criterion 7 ------------------------------------------------------------

def test_review_pack_leaks_no_raw_evidence(completed) -> None:
    """C7: the sanitised pack carries no raw HAR/trace/console/replay content."""
    _, rd = completed
    pack = json.loads(rd.review_pack.read_text(encoding="utf-8"))
    valid, errors = is_valid("evidence-review-pack", pack)
    assert valid, errors

    blob = json.dumps(pack)
    for forbidden in ("<html", "Set-Cookie", "Authorization", '"headers"', "await page.", "#!/usr/bin/env"):
        assert forbidden not in blob, f"review pack leaked {forbidden!r}"

    # Structural: only the allowlisted top-level keys may appear.
    assert set(pack) <= {
        "runId", "candidateSha", "workflowRunId", "collector",
        "requirements", "measurements", "coverage", "screenshots",
    }


def test_review_pack_covers_every_requirement(completed) -> None:
    """C7: coverage is asserted per requirement, with artifact hashes."""
    _, rd = completed
    pack = json.loads(rd.review_pack.read_text(encoding="utf-8"))
    assert pack["requirements"], "no requirements were extracted from the spec"
    covered = {c["requirementId"] for c in pack["coverage"] if c["present"]}
    assert {r["id"] for r in pack["requirements"]} == covered


# -- criterion 8 ------------------------------------------------------------

def test_report_is_self_contained(completed) -> None:
    """C8: report.html opens standalone and shows the substance."""
    _, rd = completed
    html = rd.report.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    for fragment in ("ADLC run", "Gates", "Evidence", "Decisions", "Task graph", "flowchart"):
        assert fragment in html, f"report is missing the {fragment!r} section"
    # No local asset references: it must survive being emailed as one file.
    assert 'src="./' not in html and 'href="./' not in html


# -- criterion 10 -----------------------------------------------------------

def test_gates_are_recorded_with_enforcement(completed) -> None:
    """The aggregate reflects every required gate."""
    cfg, rd = completed
    gates = load_run(rd)["gates"]
    ids = {g["id"] for g in gates}
    assert set(cfg.required_gates()) <= ids
    for gate in gates:
        assert gate["status"] in {"pass", "fail", "not_run"}
        assert gate.get("message"), f"gate {gate['id']} gave no explanation"


def test_full_pipeline_reaches_a_green_aggregate(completed) -> None:
    """The credential-free happy path actually passes."""
    _, rd = completed
    run = load_run(rd)
    passed, failures = aggregate_passed(run["gates"])
    assert passed, f"required gates failed: {failures}"
    assert run["status"] in {"gated", "reported", "decided"}


def test_build_stage_actually_succeeded(completed) -> None:
    """Guard against a green aggregate masking a broken build.

    The gate set does not include build health, so without this the suite could
    pass while every patch failed to apply.
    """
    _, rd = completed
    build = [s for s in load_run(rd)["stages"] if s["stage"] == "build"][-1]
    assert build["status"] == "ok", build["message"]

    data = build["data"]
    assert data["completedLevels"] == data["levels"], data["barriers"]
    assert not data["failedNodes"], data["failedNodes"]
    for barrier in data["barriers"]:
        assert not barrier["conflicts"], barrier["conflicts"]
        assert barrier["testsPassed"], barrier["testOutput"]
        assert barrier["applied"], f"level {barrier['level']} applied no patches"


def test_candidate_commits_advanced_past_the_base(completed) -> None:
    """Patch barriers really commit -- the candidate is a build at a commit."""
    _, rd = completed
    run = load_run(rd)
    variants = {v["key"]: v for v in run["variants"]}
    assert "candidate-a" in variants
    assert variants["candidate-a"]["commit"], "candidate has no commit"
    assert variants["candidate-a"]["commit"] != run["baseSha"], (
        "candidate commit did not advance past the base SHA"
    )
