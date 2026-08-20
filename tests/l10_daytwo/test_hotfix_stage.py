"""``adlc hotfix`` -- the narrow day-2 lap, driven through the spine's own stages.

The invariants under test are from ``docs/PLAN.md`` section 4.2: stage results
are append-only, only ``adlc reduce`` writes ``run.json``, and a run whose gates
did not pass is never reported as green.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from adlc.adapters.daytwo.sre_agent import SreAgentReceiver
from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages import hotfix as hotfix_stage
from adlc.stages.hotfix import (
    FALLBACK_WRITE_SET,
    HOTFIX_GATE_IDS,
    HOTFIX_VARIANT,
    build_hotfix_graph,
    resolve_write_set,
    run_hotfix,
)
from tests.l10_daytwo.conftest import FIXTURES, REPO_ROOT, load_fixture

RUN_ID = "2026-08-19-a1b2"


@pytest.fixture
def incident() -> dict:
    return SreAgentReceiver().parse(load_fixture("repository_dispatch.json"))


@pytest.fixture
def planned(repo_cfg: Config, incident: dict):
    """A plan-only hotfix run: real run dir, no build/evidence/gate/reduce."""
    result = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID, plan_only=True)
    return result, RunDir(repo_cfg, RUN_ID)


# -- the day-1 front door ---------------------------------------------------


def test_hotfix_creates_a_real_run_through_the_day_one_path(planned) -> None:
    result, rd = planned

    assert result["stage"] == "hotfix"
    assert rd.brief.is_file()
    assert rd.taskgraph.is_file()
    assert (rd.path / "incident.json").is_file()

    # intake and qualify are the spine's own stages, not day-2 copies.
    steps = {s["step"]: s for s in result["data"]["steps"]}
    assert steps["intake"]["status"] == "ok"
    assert steps["qualify"]["status"] == "ok"
    assert rd.latest_stage("intake") is not None
    assert rd.latest_stage("qualify") is not None


def test_the_brief_qualifies_under_the_ordinary_scorer(planned) -> None:
    """A day-2 brief must clear the same bar a human-authored brief does."""
    result, rd = planned
    qualification = json.loads((rd.path / "qualification.json").read_text(encoding="utf-8"))
    assert qualification["qualified"] is True
    assert result["data"]["qualified"] is True


@pytest.mark.parametrize("payload", [
    {"title": "Disk full on worker"},
    {"title": "Queue depth climbing", "summary": "Backlog growing."},
    {},
], ids=["title-only", "no-signals", "empty"])
def test_a_terse_incident_still_qualifies(repo_cfg: Config, payload: dict) -> None:
    """The regression that matters: sparse incidents must not be silently parked."""
    from adlc.stages.intake import qualify_text

    brief = SreAgentReceiver().to_brief(SreAgentReceiver().parse(payload))
    result = qualify_text(brief)
    threshold = int((repo_cfg.raw.get("qualify") or {}).get("minScore", 50))
    assert result["score"] >= threshold, (
        f"a terse incident scored {result['score']} < {threshold} and would be parked; "
        f"missing: {result['missing']}"
    )


def test_hotfix_halts_when_the_brief_does_not_qualify(repo_cfg: Config, incident: dict) -> None:
    repo_cfg.raw["qualify"] = {"minScore": 101}     # nothing can score 101
    result = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID)

    assert result["status"] == "fail"
    assert result["data"]["qualified"] is False
    assert "did not qualify" in result["data"]["halted"]
    assert result["data"]["gatesEvaluated"] is False
    # It stopped before building, rather than burning agent time on a bad brief.
    assert not any(s["step"] == "build" for s in result["data"]["steps"])


def test_allow_unqualified_overrides_the_halt(repo_cfg: Config, incident: dict) -> None:
    repo_cfg.raw["qualify"] = {"minScore": 101}
    result = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID,
                        plan_only=True, allow_unqualified=True)
    assert result["data"]["halted"] is None


# -- run.json and append-only ------------------------------------------------


def test_hotfix_never_writes_run_json(planned) -> None:
    """PLAN section 4.2: only ``adlc reduce`` may write run.json."""
    _, rd = planned
    assert not rd.run_json.exists(), "plan-only must not produce run.json"


def test_stage_results_are_append_only(repo_cfg: Config, incident: dict) -> None:
    first = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID, plan_only=True)
    rd = RunDir(repo_cfg, RUN_ID)
    before = (rd.stages_dir / "hotfix.1.json").read_bytes()

    second = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID, plan_only=True)

    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert (rd.stages_dir / "hotfix.1.json").read_bytes() == before, "attempt 1 was mutated"
    assert (rd.stages_dir / "hotfix.2.json").is_file()


def test_stage_result_uses_the_spine_writer_shape(planned) -> None:
    result, rd = planned
    for key in ("stage", "attempt", "status", "startedAt", "endedAt",
                "outputs", "digest", "message", "data"):
        assert key in result, f"StageResult is missing {key}"
    assert result["digest"].startswith("sha256:")
    assert result["status"] in {"ok", "fail", "skipped"}

    stored = json.loads((rd.stages_dir / "hotfix.1.json").read_text(encoding="utf-8"))
    assert stored["stage"] == "hotfix"
    assert stored["digest"] == result["digest"]


# -- plan-only and fail-closed ----------------------------------------------


def test_plan_only_skips_build_evidence_gates_and_reduce(planned) -> None:
    result, _ = planned
    steps = {s["step"] for s in result["data"]["steps"]}
    assert steps == {"intake", "qualify", "graph"}
    assert result["data"]["planOnly"] is True
    assert result["data"]["gatesEvaluated"] is False
    assert "plan-only" in result["message"]


def test_hotfix_uses_the_spine_variant_not_an_invented_one() -> None:
    """Reusing `candidate-a` keeps the report and the evidence pack in agreement."""
    assert HOTFIX_VARIANT == "candidate-a"


def test_hotfix_requires_the_same_gates_as_any_other_change(repo_cfg: Config) -> None:
    assert set(HOTFIX_GATE_IDS) == set(repo_cfg.required_gates())


def test_a_failing_step_fails_the_stage(repo_cfg: Config, incident: dict, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr(hotfix_stage, "_build", explode)
    monkeypatch.setattr(hotfix_stage, "run_evidence", explode)
    monkeypatch.setattr(hotfix_stage, "run_gates", explode)

    result = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID)
    assert result["status"] == "fail"
    assert "failed step(s)" in result["message"]
    assert result["data"]["gatesEvaluated"] is False


def test_ungated_run_is_never_described_as_green(
    repo_cfg: Config, incident: dict, monkeypatch
) -> None:
    monkeypatch.setattr(hotfix_stage, "_build", lambda *a, **k: {"failedNodes": []})
    monkeypatch.setattr(hotfix_stage, "run_evidence", lambda *a, **k: {"artifacts": []})
    monkeypatch.setattr(hotfix_stage, "run_gates",
                        lambda *a, **k: {"passed": False, "failures": ["tests: not_run"]})

    result = run_hotfix(cfg=repo_cfg, incident=incident, run_id=RUN_ID)
    assert result["status"] == "fail"
    assert result["data"]["gatesPassed"] is False
    assert result["data"]["gateFailures"] == ["tests: not_run"]
    assert "aggregate FAILED" in result["message"]


def test_cli_plan_only_exits_zero(repo_cfg: Config, monkeypatch) -> None:
    monkeypatch.chdir(repo_cfg.root)
    assert hotfix_stage.main(
        ["--incident", str(FIXTURES / "repository_dispatch.json"), "--plan-only"]
    ) == 0


def test_cli_exit_is_nonzero_when_gates_fail(repo_cfg: Config, monkeypatch) -> None:
    monkeypatch.chdir(repo_cfg.root)
    monkeypatch.setattr(hotfix_stage, "_build", lambda *a, **k: {"failedNodes": []})
    monkeypatch.setattr(hotfix_stage, "run_evidence", lambda *a, **k: {"artifacts": []})
    monkeypatch.setattr(hotfix_stage, "run_gates",
                        lambda *a, **k: {"passed": False, "failures": ["tests: fail"]})
    assert hotfix_stage.main(["--incident", str(FIXTURES / "repository_dispatch.json")]) == 1


def test_cli_reports_a_bad_incident_without_a_traceback(
    repo_cfg: Config, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(repo_cfg.root)
    bad = repo_cfg.root / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    assert hotfix_stage.main(["--incident", str(bad)]) == 1
    assert "adlc hotfix:" in capsys.readouterr().err


# -- the task graph ---------------------------------------------------------


def test_hotfix_graph_validates_against_the_frozen_schema(incident: dict) -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas" / "taskgraph.schema.json").read_text(encoding="utf-8")
    )
    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    jsonschema.validate(graph, schema)


def test_hotfix_graph_passes_the_executors_own_validation(incident: dict) -> None:
    """The strongest check available: the same validator `adlc build` runs.

    Covers cycles, level assignment, same-level write-set overlap and protected
    paths in one call.
    """
    from adlc.executor import validate_graph

    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    levels = validate_graph(graph)
    assert levels["T001"] == 0
    assert levels["T002"] == levels["T003"] == 1


def test_hotfix_graph_is_narrow_and_correctly_levelled(incident: dict) -> None:
    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    nodes = {n["id"]: n for n in graph["nodes"]}

    assert list(nodes) == ["T001", "T002", "T003"]
    assert nodes["T001"]["kind"] == "test" and nodes["T001"]["level"] == 0
    assert nodes["T002"]["kind"] == "implement" and nodes["T002"]["level"] == 1
    assert nodes["T003"]["kind"] == "doc" and nodes["T003"]["level"] == 1
    # The test must come first: a hotfix with no failing test is a guess.
    assert nodes["T002"]["dependsOn"] == ["T001"]


def test_same_level_write_sets_do_not_overlap(incident: dict) -> None:
    """PLAN section 4.4: overlapping write sets at one level is a graph error."""
    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    level_one = [set(n["writeSet"]) for n in graph["nodes"] if n["level"] == 1]
    assert len(level_one) == 2
    assert level_one[0].isdisjoint(level_one[1])


def test_capsules_forbid_the_protected_paths(incident: dict) -> None:
    from adlc.ports import PROTECTED_PATHS

    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    for node in graph["nodes"]:
        for protected in PROTECTED_PATHS:
            assert protected in node["context"]["doNotTouch"]


def test_write_set_provenance_is_honest(incident: dict, repo_cfg: Config) -> None:
    # 1. taken from the incident when it carries a hint
    paths, source = resolve_write_set(incident, None)
    assert source == "incident"
    assert paths == ["src/checkout/handler.py", "src/checkout/inventory.py"]

    # 2. from config when the incident says nothing
    bare = SreAgentReceiver().parse({"title": "no hint"})
    configured = Config(root=repo_cfg.root, raw={"hotfix": {"writeSet": ["src/api/"]}})
    assert resolve_write_set(bare, configured) == (["src/api/"], "config")

    # 3. otherwise a placeholder, labelled as one
    assert resolve_write_set(bare, Config(root=repo_cfg.root)) == (
        list(FALLBACK_WRITE_SET), "fallback"
    )


def test_fallback_write_set_is_announced_loudly(repo_cfg: Config) -> None:
    bare = SreAgentReceiver().parse({"title": "no hint anywhere"})
    result = run_hotfix(cfg=repo_cfg, incident=bare, run_id=RUN_ID, plan_only=True)
    assert result["data"]["writeSetSource"] == "fallback"
    assert "placeholder" in result["message"]
    assert "refine it" in result["message"]
