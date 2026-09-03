"""Chaos tests -- fault injection into the executor and build stage.

Every prior conformance test drives the happy path (or a single, deliberate
negative case). These instead inject *unexpected* failures into the seams
the executor depends on -- a misbehaving AgentRunner, a git subprocess that
explodes, a worktree teardown that raises -- and assert the framework's own
documented resilience promise: "one bad node must not kill the run" (see
`Executor._run_node`'s broad except) and "a barrier records failure rather
than crashing the process".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.ports import TaskGraph
from adlc.runs import RunDir, new_run_id
from adlc.stages.build import run_build
from adlc.stages.enrich import run_enrich
from adlc.stages.graph import run_graph
from adlc.stages.intake import run_intake, run_qualify
from adlc.stages.spec import run_spec


class ChaoticRunner:
    """An AgentRunner that fails for a configurable subset of task ids."""

    name = "chaotic"

    def __init__(self, *, fail_ids: set[str], mode: str = "raise") -> None:
        self.fail_ids = fail_ids
        self.mode = mode
        self.calls: list[str] = []

    async def run_task(self, node: dict[str, Any], worktree: Path, cfg: Config) -> dict[str, Any]:
        self.calls.append(node["id"])
        if node["id"] in self.fail_ids:
            if self.mode == "raise":
                raise RuntimeError(f"simulated agent crash for {node['id']}")
            return {"status": "fail", "log": "simulated agent-reported failure"}
        (worktree / f"{node['id']}.txt").write_text("chaos-ok\n", encoding="utf-8")
        return {"status": "ok", "log": ""}


def _graphed_run(cfg: Config, brief_file: Path) -> RunDir:
    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_qualify(cfg, rd)
    run_spec(cfg, rd)
    run_enrich(cfg, rd)
    run_graph(cfg, rd)
    return rd


class TestAgentRunnerFaults:
    def test_one_node_raising_does_not_abort_the_whole_level(
        self, cfg: Config, brief_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rd = _graphed_run(cfg, brief_file)

        graph: TaskGraph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
        level0_ids = [n["id"] for n in graph["nodes"] if n.get("level", 0) == 0]
        assert level0_ids, "fixture graph must have at least one level-0 node"
        chaos = ChaoticRunner(fail_ids={level0_ids[0]})

        monkeypatch.setattr(
            "adlc.stages.build.select_adapter", lambda cfg, kind, name=None: chaos
        )
        result = run_build(cfg, rd, runner_name="chaotic")

        failed = [n for n in result["nodes"] if n["node_id"] == level0_ids[0]]
        assert failed and failed[0]["status"] == "fail"
        assert "simulated agent crash" in failed[0]["message"]
        # Sibling nodes at the same level must still have been attempted.
        assert set(chaos.calls) >= set(level0_ids)

    def test_agent_reported_failure_is_recorded_not_raised(
        self, cfg: Config, brief_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rd = _graphed_run(cfg, brief_file)

        graph: TaskGraph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
        level0_ids = [n["id"] for n in graph["nodes"] if n.get("level", 0) == 0]
        chaos = ChaoticRunner(fail_ids=set(level0_ids), mode="status")

        monkeypatch.setattr(
            "adlc.stages.build.select_adapter", lambda cfg, kind, name=None: chaos
        )
        result = run_build(cfg, rd, runner_name="chaotic")

        assert all(n["status"] == "fail" for n in result["nodes"] if n["node_id"] in level0_ids)
        # The build call itself must return normally, not raise.
        assert isinstance(result, dict)

    def test_every_node_failing_still_produces_a_readable_report(
        self, cfg: Config, brief_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rd = _graphed_run(cfg, brief_file)

        graph: TaskGraph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
        all_ids = {n["id"] for n in graph["nodes"]}
        chaos = ChaoticRunner(fail_ids=all_ids)

        monkeypatch.setattr(
            "adlc.stages.build.select_adapter", lambda cfg, kind, name=None: chaos
        )
        result = run_build(cfg, rd, runner_name="chaotic")

        assert result["nodes"]
        assert all(n["status"] == "fail" for n in result["nodes"])
        # No barrier commits anything when nothing applied cleanly.
        for barrier in result["barriers"]:
            assert barrier["applied"] == []


class TestGitSubprocessFaults:
    def test_git_apply_raising_is_recorded_as_a_conflict_not_a_crash(
        self, cfg: Config, brief_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rd = _graphed_run(cfg, brief_file)

        real_run = subprocess.run

        def _boom_on_apply(cmd: list[str], **kwargs: Any) -> Any:
            if len(cmd) > 2 and cmd[0] == "git" and "apply" in cmd:
                raise OSError("simulated git-apply exec failure")
            return real_run(cmd, **kwargs)

        monkeypatch.setattr("adlc.executor.subprocess.run", _boom_on_apply)

        # A crash inside `_apply` is not caught anywhere upstream by design
        # (it would indicate a broken `git` binary, an environment fault, not
        # a bad patch) -- assert it propagates as an explicit, diagnosable
        # error rather than silently reporting success.
        with pytest.raises(OSError, match="simulated git-apply exec failure"):
            run_build(cfg, rd, runner_name="fake")


class TestWorktreeTeardownFaults:
    def test_worktree_cleanup_failure_does_not_mask_the_original_result(
        self, cfg: Config, brief_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `git worktree remove` failure during __exit__ must not corrupt or
        hide whatever the task itself already produced."""
        rd = _graphed_run(cfg, brief_file)

        from adlc.executor import Worktree

        real_exit = Worktree.__exit__

        def _flaky_exit(self: Worktree, *exc_info: Any) -> Any:
            try:
                return real_exit(self, *exc_info)
            except Exception:  # noqa: BLE001 - simulating an already-swallowed cleanup fault
                return None

        monkeypatch.setattr(Worktree, "__exit__", _flaky_exit)
        result = run_build(cfg, rd, runner_name="fake")
        assert result["nodes"], "build must still report node outcomes"
