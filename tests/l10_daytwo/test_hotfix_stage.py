"""``adlc hotfix`` — the narrow day-2 lap.

The invariants under test are the ones from ``docs/PLAN.md`` §4.2: stage
results are append-only, only ``adlc reduce`` writes ``run.json``, and a run
whose gates did not run is never reported as green.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from adlc.adapters.daytwo.sre_agent import SreAgentReceiver
from adlc.config import Config
from adlc.stages import hotfix as hotfix_stage
from adlc.stages.hotfix import (
    FALLBACK_WRITE_SET,
    HOTFIX_GATE_IDS,
    build_hotfix_graph,
    resolve_write_set,
    run_hotfix,
)
from tests.l10_daytwo.conftest import FIXTURES, REPO_ROOT, load_fixture

RUN_ID = "2026-08-19-a1b2"


class RecordingExecutor:
    """Stands in for the ``adlc`` CLI so tests never shell out."""

    def __init__(self, *, fail: set[str] | None = None, run_id: str = RUN_ID) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()
        self.run_id = run_id

    def __call__(self, argv, cwd):
        argv = list(argv)
        self.calls.append(argv)
        step = argv[1] if len(argv) > 1 else ""
        if step in self.fail:
            return {"argv": argv, "returncode": 1, "stdout": "", "stderr": f"{step} exploded",
                    "ok": False}
        stdout = json.dumps({"runId": self.run_id}) if argv[1:3] == ["run", "new"] else ""
        return {"argv": argv, "returncode": 0, "stdout": stdout, "stderr": "", "ok": True}


@pytest.fixture
def incident() -> dict:
    return SreAgentReceiver().parse(load_fixture("repository_dispatch.json"))


# -- the happy path ---------------------------------------------------------


def test_hotfix_produces_its_own_outputs(cfg: Config, incident: dict) -> None:
    executor = RecordingExecutor()
    result = run_hotfix(cfg=cfg, incident=incident, executor=executor)

    assert result["stage"] == "hotfix"
    assert result["status"] == "ok"
    assert result["attempt"] == 1
    assert set(result["outputs"]) == {"brief.md", "incident.json", "taskgraph.json"}
    assert result["digest"].startswith("sha256:")

    run_dir = cfg.run_dir(RUN_ID)
    for name in result["outputs"]:
        assert (run_dir / name).is_file(), f"{name} was not written"


def test_hotfix_never_writes_run_json(cfg: Config, incident: dict) -> None:
    """PLAN §4.2: only ``adlc reduce`` may write run.json."""
    run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor())
    assert not (cfg.run_dir(RUN_ID) / "run.json").exists()
    assert list(cfg.runs_dir.rglob("run.json")) == []


def test_hotfix_enters_through_the_day_one_front_door(cfg: Config, incident: dict) -> None:
    """The reuse claim: the run is created by ``adlc run new --brief``."""
    executor = RecordingExecutor()
    run_hotfix(cfg=cfg, incident=incident, executor=executor)

    run_new = next(c for c in executor.calls if c[1:3] == ["run", "new"])
    assert "--brief" in run_new
    brief_path = Path(run_new[run_new.index("--brief") + 1])
    assert brief_path.name == "brief.md"
    assert "standard day-1 intake path" in brief_path.read_text(encoding="utf-8")


def test_downstream_commands_are_the_frozen_cli_surface(cfg: Config, incident: dict) -> None:
    executor = RecordingExecutor()
    run_hotfix(cfg=cfg, incident=incident, executor=executor)

    steps = [c[1] for c in executor.calls]
    assert steps == ["run", "build", "evidence", "gate", "reduce", "report"]
    # A hotfix skips spec/enrich on purpose - that is what makes it narrow.
    assert "spec" not in steps and "enrich" not in steps

    gate = next(c for c in executor.calls if c[1] == "gate")
    assert gate[gate.index("--ids") + 1] == ",".join(HOTFIX_GATE_IDS)
    assert gate[gate.index("--profile") + 1] == "minimal"

    evidence = next(c for c in executor.calls if c[1] == "evidence")
    assert evidence[evidence.index("--variant") + 1] == "hotfix"


def test_stage_result_records_gate_evaluation(cfg: Config, incident: dict) -> None:
    result = run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor())
    assert result["data"]["gatesEvaluated"] is True
    assert result["data"]["gateIds"] == list(HOTFIX_GATE_IDS)
    assert result["data"]["runCreatedByCli"] is True


# -- append-only ------------------------------------------------------------


def test_stage_results_are_append_only(cfg: Config, incident: dict) -> None:
    """PLAN §4.2: a re-run appends ``attempt: n+1`` and never edits."""
    first = run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor())
    stages = cfg.run_dir(RUN_ID) / "stages"
    before = (stages / "hotfix.1.json").read_bytes()

    second = run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor())

    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert (stages / "hotfix.1.json").read_bytes() == before, "attempt 1 was mutated"
    assert (stages / "hotfix.2.json").is_file()


def test_stage_result_matches_the_ports_stageresult_shape(cfg: Config, incident: dict) -> None:
    result = run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor())
    for key in ("stage", "attempt", "status", "startedAt", "endedAt",
                "outputs", "digest", "message", "data"):
        assert key in result, f"StageResult is missing {key}"
    assert result["status"] in {"ok", "fail", "skipped"}

    stored = json.loads(
        (cfg.run_dir(RUN_ID) / "stages" / "hotfix.1.json").read_text(encoding="utf-8")
    )
    assert stored["stage"] == "hotfix"


# -- failing closed ---------------------------------------------------------


def test_a_failed_step_fails_the_stage(cfg: Config, incident: dict) -> None:
    result = run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor(fail={"build"}))
    assert result["status"] == "fail"
    assert "failed step(s): build" in result["message"]


def test_ungated_run_is_never_described_as_green(cfg: Config, incident: dict) -> None:
    result = run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor(fail={"gate"}))
    assert result["data"]["gatesEvaluated"] is False
    assert result["status"] == "fail"


def test_plan_only_executes_nothing(cfg: Config, incident: dict) -> None:
    executor = RecordingExecutor()
    result = run_hotfix(cfg=cfg, incident=incident, executor=executor, plan_only=True,
                        run_id=RUN_ID)

    assert executor.calls == []
    assert result["data"]["planOnly"] is True
    assert result["data"]["gatesEvaluated"] is False
    assert all(step["status"] == "skipped" for step in result["data"]["steps"])
    assert "plan-only" in result["message"]


def test_cli_exit_code_is_nonzero_when_gates_did_not_run(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hotfix_stage, "subprocess_executor", RecordingExecutor(fail={"gate"}))
    code = hotfix_stage.main(["--incident", str(FIXTURES / "repository_dispatch.json")])
    assert code == 1


def test_cli_plan_only_exits_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code = hotfix_stage.main(
        ["--incident", str(FIXTURES / "repository_dispatch.json"), "--plan-only", "--json"]
    )
    assert code == 0


def test_cli_reports_a_bad_incident_without_a_traceback(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.json"
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
    """PLAN §4.4: overlapping write sets at one level is a graph error."""
    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    level_one = [set(n["writeSet"]) for n in graph["nodes"] if n["level"] == 1]
    assert len(level_one) == 2
    assert level_one[0].isdisjoint(level_one[1])


def test_capsules_never_allow_protected_paths(incident: dict) -> None:
    graph, _ = build_hotfix_graph(incident, run_id=RUN_ID, base_sha="a" * 40)
    for node in graph["nodes"]:
        do_not_touch = node["context"]["doNotTouch"]
        for protected in (".github/**", ".adlc/**", "schemas/**", "docs/decisions/**"):
            assert protected in do_not_touch


def test_write_set_provenance_is_honest(incident: dict, tmp_path) -> None:
    # 1. taken from the incident when it carries a hint
    paths, source = resolve_write_set(incident, None)
    assert source == "incident"
    assert paths == ["src/checkout/handler.py", "src/checkout/inventory.py"]

    # 2. from config when the incident says nothing
    bare = SreAgentReceiver().parse({"title": "no hint"})
    configured = Config(root=tmp_path, raw={"hotfix": {"writeSet": ["src/api/"]}})
    assert resolve_write_set(bare, configured) == (["src/api/"], "config")

    # 3. otherwise a placeholder, labelled as one
    assert resolve_write_set(bare, Config(root=tmp_path)) == (list(FALLBACK_WRITE_SET), "fallback")


def test_fallback_write_set_is_announced_loudly(cfg: Config) -> None:
    bare = SreAgentReceiver().parse({"title": "no hint anywhere"})
    result = run_hotfix(cfg=cfg, incident=bare, executor=RecordingExecutor())
    assert result["data"]["writeSetSource"] == "fallback"
    assert "placeholder" in result["message"]
    assert "refine it" in result["message"]


def test_graph_is_written_to_the_run_directory(cfg: Config, incident: dict) -> None:
    run_hotfix(cfg=cfg, incident=incident, executor=RecordingExecutor())
    graph = json.loads((cfg.run_dir(RUN_ID) / "taskgraph.json").read_text(encoding="utf-8"))
    assert graph["runId"] == RUN_ID
    assert graph["specDigest"].startswith("sha256:")
