"""The L1 runners driven by the **real** spine executor.

`adlc.executor.Executor` extracts the canonical `patches/<task-id>.patch` by
diffing the worktree after `run_task` returns and ignores
`TaskOutcome["patchPath"]`. That makes "did the runner leave the right changes
in the worktree?" the property that actually matters, and it is not something
the unit tests in `test_runners.py` can observe. These tests run the genuine
executor — worktrees, level barriers, patch application, `baseSha` advance —
with the backends mocked.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.agents.agent_task import AgentTaskRunner
from adlc.adapters.agents.copilot_sdk import CopilotSdkRunner
from adlc.config import Config
from adlc.executor import Executor
from adlc.ports import TaskGraph
from adlc.runs import RunDir

from .conftest import git


@pytest.fixture
def spine(repo: Path, base_sha: str) -> tuple[Config, RunDir]:
    """A repo that is also a valid ADLC root, with a run directory."""
    cfg = Config(root=repo, limits={"taskTimeoutSeconds": 30, "pollSeconds": 1})
    run_dir = RunDir(cfg, "2026-08-19-a1b2")
    run_dir.patches_dir.mkdir(parents=True, exist_ok=True)
    return cfg, run_dir


def _graph(base_sha: str) -> TaskGraph:
    return {
        "runId": "2026-08-19-a1b2",
        "baseSha": base_sha,
        "nodes": [
            {
                "id": "T001",
                "title": "Add the theme module",
                "kind": "implement",
                "dependsOn": [],
                "writeSet": ["src/theme.ts"],
                "acceptance": ["US1-AC1"],
            },
            {
                "id": "T002",
                "title": "Add the toggle module",
                "kind": "implement",
                "dependsOn": [],
                "writeSet": ["src/toggle.ts"],
                "acceptance": ["US1-AC2"],
            },
        ],
    }


async def test_copilot_sdk_runner_drives_the_real_executor(
    spine: tuple[Config, RunDir], repo: Path, base_sha: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, run_dir = spine
    # Keep the runner's own patch out of the way of the canonical one the
    # executor writes, so both can be inspected independently.
    monkeypatch.setenv("ADLC_PATCH_DIR", str(tmp_path / "runner-patches"))
    monkeypatch.setattr(
        CopilotSdkRunner, "detect", staticmethod(lambda _cfg: (True, "mocked as available"))
    )
    runner = CopilotSdkRunner()

    async def fake_converse(prompt: str, worktree: Path) -> tuple[str, dict[str, Any]]:
        # One file per node, derived from the write set stated in the prompt.
        target = "src/theme.ts" if "src/theme.ts" in prompt else "src/toggle.ts"
        (worktree / target).write_text(f"export const x = '{target}';\n", encoding="utf-8")
        return "ok", {"tokensIn": 10, "tokensOut": 3}

    monkeypatch.setattr(runner, "_converse", fake_converse)

    report = await Executor(cfg, run_dir, runner, max_parallel=2).run(_graph(base_sha))

    assert [n.status for n in report.nodes] == ["ok", "ok"], [n.message for n in report.nodes]
    assert [n.node_id for n in report.nodes] == ["T001", "T002"]
    # The executor extracted the canonical patches from the worktrees...
    for node_id, path in (("T001", "src/theme.ts"), ("T002", "src/toggle.ts")):
        canonical = run_dir.patches_dir / f"{node_id}.patch"
        assert canonical.is_file()
        assert path in canonical.read_text(encoding="utf-8")
    # ...and the runner's own patch applies at the base SHA it claims.
    for node_id in ("T001", "T002"):
        assert _applies_at_base(repo, base_sha, tmp_path / "runner-patches" / f"{node_id}.patch")


def _applies_at_base(repo: Path, base_sha: str, patch: Path) -> bool:
    """Does the patch apply at ``base_sha``? Checked in a throwaway worktree.

    The main checkout has moved on by the time this runs — the level barrier
    commits and advances ``baseSha`` — so anchoring has to be checked against a
    fresh checkout of the base commit rather than against HEAD.
    """
    assert patch.is_file(), f"{patch} was not written"
    scratch = Path(tempfile.mkdtemp(prefix="adlc-anchor-"))
    target = scratch / "wt"
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), base_sha],
            capture_output=True, text=True, check=True,
        )
        proc = subprocess.run(
            ["git", "-C", str(target), "apply", "--check", str(patch)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return True
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
            capture_output=True, text=True, check=False,
        )
        shutil.rmtree(scratch, ignore_errors=True)


async def test_level_barrier_applies_the_patches(
    spine: tuple[Config, RunDir], repo: Path, base_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, run_dir = spine
    monkeypatch.setattr(
        CopilotSdkRunner, "detect", staticmethod(lambda _cfg: (True, "mocked as available"))
    )
    runner = CopilotSdkRunner()

    async def fake_converse(prompt: str, worktree: Path) -> tuple[str, dict[str, Any]]:
        target = "src/theme.ts" if "src/theme.ts" in prompt else "src/toggle.ts"
        (worktree / target).write_text(f"export const x = '{target}';\n", encoding="utf-8")
        return "ok", {}

    monkeypatch.setattr(runner, "_converse", fake_converse)

    report = await Executor(cfg, run_dir, runner, max_parallel=2).run(_graph(base_sha))
    barrier = report.barriers[0]

    assert barrier["applied"] == ["T001", "T002"], barrier["conflicts"]
    assert not barrier["conflicts"]
    assert report.base_sha != base_sha
    assert (repo / "src" / "theme.ts").is_file()
    assert (repo / "src" / "toggle.ts").is_file()


async def test_write_set_violation_fails_the_node_in_the_real_executor(
    spine: tuple[Config, RunDir], repo: Path, base_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, run_dir = spine
    monkeypatch.setattr(
        CopilotSdkRunner, "detect", staticmethod(lambda _cfg: (True, "mocked as available"))
    )
    runner = CopilotSdkRunner()

    async def rogue(prompt: str, worktree: Path) -> tuple[str, dict[str, Any]]:
        target = "src/theme.ts" if "src/theme.ts" in prompt else "src/toggle.ts"
        (worktree / target).write_text("ok\n", encoding="utf-8")
        (worktree / "README.md").write_text("SNEAKY\n", encoding="utf-8")
        return "ok", {}

    monkeypatch.setattr(runner, "_converse", rogue)

    report = await Executor(cfg, run_dir, runner, max_parallel=2).run(_graph(base_sha))

    assert {n.status for n in report.nodes} == {"fail"}
    assert all("README.md" in n.message for n in report.nodes)
    # Nothing was applied, so the repository is untouched at the base SHA.
    assert report.barriers[0]["applied"] == []
    assert (repo / "README.md").read_text(encoding="utf-8") == "# demo\n"
    assert git(repo, "rev-parse", "HEAD") == base_sha


async def test_out_of_band_result_reaches_the_executor(
    spine: tuple[Config, RunDir], repo: Path, base_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `agent-task` result is produced remotely — it must land in the worktree.

    The executor diffs the worktree; a runner that only wrote a patch file would
    be recorded as "no changes produced" and its work silently dropped.
    """
    cfg, run_dir = spine
    git(repo, "checkout", "-q", "-b", "copilot/task-1")
    (repo / "src" / "theme.ts").write_text("export const theme = 'dark';\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cloud agent work")
    git(repo, "checkout", "-q", "main")
    git(repo, "remote", "add", "origin", str(repo))

    monkeypatch.setattr(
        AgentTaskRunner, "detect", staticmethod(lambda _cfg: (True, "mocked as available"))
    )
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()

    async def create_task(*_args: Any, **_kwargs: Any) -> dict:
        return {"id": "42"}

    async def poll(*_args: Any, **_kwargs: Any) -> dict:
        return {"status": "completed", "branch": "copilot/task-1"}

    monkeypatch.setattr(runner, "create_task", create_task)
    monkeypatch.setattr(runner, "poll", poll)

    graph: TaskGraph = {
        "runId": "2026-08-19-a1b2",
        "baseSha": base_sha,
        "nodes": [
            {"id": "T001", "title": "Theme", "kind": "implement",
             "dependsOn": [], "writeSet": ["src/theme.ts"]}
        ],
    }
    report = await Executor(cfg, run_dir, runner, max_parallel=1).run(graph)

    # The point of this test: the executor diffs the worktree, so an out-of-band
    # result must have been materialised there.
    assert [n.status for n in report.nodes] == ["ok"], [n.message for n in report.nodes]
    assert report.nodes[0].message != "no changes produced"
    patch = run_dir.patches_dir / "T001.patch"
    assert patch.is_file()
    assert "src/theme.ts" in patch.read_text(encoding="utf-8")


async def test_patch_path_uses_the_run_dir_when_the_run_id_is_known(
    spine: tuple[Config, RunDir], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adlc.adapters.agents.copilot_sdk import resolve_patch_path

    cfg, run_dir = spine
    monkeypatch.setenv("ADLC_RUN_ID", run_dir.run_id)
    assert resolve_patch_path({"id": "T001"}, repo, cfg) == run_dir.patches_dir / "T001.patch"


async def test_patch_path_fallback_is_unique_per_worktree(
    cfg: Config, tmp_path: Path
) -> None:
    """Two concurrent runs must not collide on the same `<task-id>.patch`."""
    from adlc.adapters.agents.copilot_sdk import resolve_patch_path

    first = tmp_path / "adlc-T001-aaaa"
    second = tmp_path / "adlc-T001-bbbb"
    first.mkdir()
    second.mkdir()
    assert resolve_patch_path({"id": "T001"}, first, cfg) != resolve_patch_path(
        {"id": "T001"}, second, cfg
    )
    assert resolve_patch_path({"id": "T001"}, first, cfg).parent.parent == tmp_path
