"""End-to-end ``run_task`` behaviour for all three runners, driven by mocks.

No credentials, no network, no installed backend. The Copilot SDK session, the
Agent Tasks REST calls and every ``gh`` invocation are substituted; what is
*not* substituted is the git machinery, so the patch these tests assert on is a
real diff produced by real git against a real base SHA.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.agents import gh_aw as gh_aw_module
from adlc.adapters.agents.agent_task import AgentTaskRunner, ApiError, extract_result_ref
from adlc.adapters.agents.copilot_sdk import CopilotSdkRunner, collect_usage
from adlc.adapters.agents.gh_aw import GhAwRunner, GhResult, select_patch_file
from adlc.config import Config
from adlc.ports import TaskNode

from .conftest import git

# ---------------------------------------------------------------------------
# copilot-sdk
# ---------------------------------------------------------------------------


def _available(monkeypatch: pytest.MonkeyPatch, runner: type) -> None:
    monkeypatch.setattr(runner, "detect", staticmethod(lambda _cfg: (True, "mocked as available")))


async def test_copilot_sdk_refuses_to_run_when_unavailable(
    node: TaskNode, repo: Path, repo_cfg: Config
) -> None:
    outcome = await CopilotSdkRunner().run_task(node, repo, repo_cfg)
    assert outcome["status"] == "fail"
    assert "copilot-sdk unavailable" in outcome["log"]


async def test_copilot_sdk_produces_a_patch_and_reports_usage(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, CopilotSdkRunner)
    runner = CopilotSdkRunner()

    async def fake_converse(prompt: str, worktree: Path) -> tuple[str, dict[str, Any]]:
        assert "src/theme.ts" in prompt, "the prompt must state the write set"
        (worktree / "src" / "theme.ts").write_text("export default 'dark';\n", encoding="utf-8")
        return "done", {"tokensIn": 1200, "tokensOut": 340, "cost": 0.021}

    monkeypatch.setattr(runner, "_converse", fake_converse)
    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "ok", outcome["log"]
    patch = Path(outcome["patchPath"])
    assert patch.name == "T001.patch"
    assert "src/theme.ts" in patch.read_text(encoding="utf-8")
    assert base_sha[:12] in outcome["log"]
    assert outcome["tokensIn"] == 1200
    assert outcome["tokensOut"] == 340
    assert outcome["cost"] == pytest.approx(0.021)


async def test_copilot_sdk_refuses_an_out_of_write_set_edit(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, CopilotSdkRunner)
    runner = CopilotSdkRunner()

    async def rogue(_prompt: str, worktree: Path) -> tuple[str, dict[str, Any]]:
        (worktree / "src" / "theme.ts").write_text("ok\n", encoding="utf-8")
        (worktree / "README.md").write_text("SNEAKY\n", encoding="utf-8")
        return "done", {"tokensIn": 10}

    monkeypatch.setattr(runner, "_converse", rogue)
    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "README.md" in outcome["log"]
    assert "outside writeSet" in outcome["log"]
    # Accounting is still reported for work that was actually paid for.
    assert outcome["tokensIn"] == 10


async def test_copilot_sdk_never_leaks_a_session_exception(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, CopilotSdkRunner)
    runner = CopilotSdkRunner()

    async def explode(_prompt: str, _worktree: Path) -> tuple[str, dict[str, Any]]:
        raise RuntimeError("runtime binary missing")

    monkeypatch.setattr(runner, "_converse", explode)
    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "RuntimeError" in outcome["log"]
    assert "runtime binary missing" in outcome["log"]


async def test_copilot_sdk_times_out_within_the_configured_budget(
    node: TaskNode, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, CopilotSdkRunner)
    runner = CopilotSdkRunner()

    async def hang(_prompt: str, _worktree: Path) -> tuple[str, dict[str, Any]]:
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(runner, "_converse", hang)
    outcome = await runner.run_task(node, repo, Config(root=repo, limits={"taskTimeoutSeconds": 1}))

    assert outcome["status"] == "fail"
    assert "timed out" in outcome["log"]


async def test_send_prefers_send_and_wait() -> None:
    class Session:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def send_and_wait(self, prompt: str) -> dict[str, Any]:
            self.seen.append(prompt)
            return {"content": "hi", "usage": {"input_tokens": 5, "output_tokens": 7}}

    session = Session()
    response = await CopilotSdkRunner._send(session, "do the thing")

    assert session.seen == ["do the thing"]
    assert collect_usage(response) == {"tokensIn": 5, "tokensOut": 7}


async def test_send_falls_back_to_send_plus_idle_event() -> None:
    """SDK 1.x has no ``send_and_wait``; the documented surface is send + idle."""

    class Event:
        def __init__(self, type_: str, data: Any) -> None:
            self.type = type_
            self.data = data

    class Message:
        def __init__(self, content: str) -> None:
            self.content = content

    class Session:
        def __init__(self) -> None:
            self.handler: Any = None

        def on(self, handler: Any) -> None:
            self.handler = handler

        async def send(self, _prompt: str) -> None:
            self.handler(Event("assistant.message", Message("wrote the file")))
            self.handler(Event("session.idle", None))

    response = await CopilotSdkRunner._send(Session(), "go")
    assert response["content"] == "wrote the file"


def test_collect_usage_omits_what_the_backend_never_reported() -> None:
    assert collect_usage({"content": "hi"}) == {}
    assert collect_usage(None) == {}
    assert collect_usage({"usage": {"prompt_tokens": 9}}) == {"tokensIn": 9}


# ---------------------------------------------------------------------------
# agent-task
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_repo(repo: Path, base_sha: str) -> str:
    """Give the repo a branch of 'cloud agent' work reachable via `origin`.

    ``origin`` points at the repository itself, so the fetch in ``run_task`` is
    a real git fetch that needs no network.
    """
    git(repo, "checkout", "-q", "-b", "copilot/task-1")
    (repo / "src" / "theme.ts").write_text("export default 'dark';\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cloud agent work")
    git(repo, "checkout", "-q", "main")
    git(repo, "remote", "add", "origin", str(repo))
    return "copilot/task-1"


def _stub_api(
    monkeypatch: pytest.MonkeyPatch, runner: AgentTaskRunner, created: dict, final: dict
) -> list[dict]:
    calls: list[dict] = []

    async def create_task(slug: str, token: str, prompt: str, base_ref: str) -> dict:
        calls.append({"slug": slug, "prompt": prompt, "base_ref": base_ref})
        return created

    async def poll(*_args: Any, **_kwargs: Any) -> dict:
        return final

    monkeypatch.setattr(runner, "create_task", create_task)
    monkeypatch.setattr(runner, "poll", poll)
    return calls


async def test_agent_task_fetches_the_result_and_anchors_the_patch(
    node: TaskNode,
    repo: Path,
    repo_cfg: Config,
    base_sha: str,
    remote_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch, AgentTaskRunner)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()
    calls = _stub_api(
        monkeypatch,
        runner,
        {"id": "42"},
        {"status": "completed", "pull_request": {"number": 7, "head_ref": remote_repo}},
    )

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "ok", outcome["log"]
    assert calls[0]["slug"] == "octo/demo"
    assert "src/theme.ts" in calls[0]["prompt"]
    patch = Path(outcome["patchPath"])
    assert "src/theme.ts" in patch.read_text(encoding="utf-8")
    assert base_sha[:12] in outcome["log"]


async def test_agent_task_refuses_a_result_outside_the_write_set(
    node: TaskNode,
    repo: Path,
    repo_cfg: Config,
    base_sha: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git(repo, "checkout", "-q", "-b", "copilot/overreach")
    (repo / "src" / "theme.ts").write_text("ok\n", encoding="utf-8")
    (repo / "README.md").write_text("SNEAKY\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "overreach")
    git(repo, "checkout", "-q", "main")
    git(repo, "remote", "add", "origin", str(repo))

    _available(monkeypatch, AgentTaskRunner)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()
    _stub_api(
        monkeypatch, runner, {"id": "42"},
        {"status": "completed", "branch": "copilot/overreach"},
    )

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "outside writeSet" in outcome["log"]
    assert "README.md" in outcome["log"]


async def test_agent_task_fails_closed_on_a_non_completed_status(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, AgentTaskRunner)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()
    _stub_api(monkeypatch, runner, {"id": "42"}, {"status": "waiting_for_user"})

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "waiting_for_user" in outcome["log"]
    assert "unattended" in outcome["log"]


async def test_agent_task_reports_an_unrecognised_preview_response(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, AgentTaskRunner)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()
    _stub_api(monkeypatch, runner, {"unexpected": "shape"}, {"status": "completed"})

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "no task id" in outcome["log"]
    assert "public-preview response" in outcome["log"]


async def test_agent_task_surfaces_an_api_error(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, AgentTaskRunner)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()

    async def boom(*_args: Any, **_kwargs: Any) -> dict:
        raise ApiError(403, "Copilot coding agent is not enabled for this repository")

    monkeypatch.setattr(runner, "create_task", boom)
    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "HTTP 403" in outcome["log"]
    assert "not enabled" in outcome["log"]


async def test_agent_task_reports_a_completed_task_with_nothing_to_fetch(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, AgentTaskRunner)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = AgentTaskRunner()
    _stub_api(monkeypatch, runner, {"id": "42"}, {"status": "completed"})

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "no branch or pull request" in outcome["log"]


async def test_poll_gives_up_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = AgentTaskRunner()
    seen = 0

    async def always_queued(*_args: Any) -> dict:
        nonlocal seen
        seen += 1
        return {"status": "queued"}

    monkeypatch.setattr(runner, "get_task", always_queued)
    loop = asyncio.get_running_loop()
    task = await runner.poll("octo/demo", "t", "1", loop.time() + 0.05, interval=0.01)

    assert task["status"] == "timed_out"
    assert seen >= 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"pull_request": {"head_ref": "copilot/x"}}, "copilot/x"),
        ({"pull_request": {"head": {"ref": "copilot/y"}}}, "copilot/y"),
        ({"head_ref": "copilot/z"}, "copilot/z"),
        ({"branch": "copilot/b"}, "copilot/b"),
        ({"pull_request": {"number": 12}}, "refs/pull/12/head"),
        ({"status": "completed"}, None),
        ({"pull_request": {"head_ref": "   "}}, None),
    ],
)
def test_extract_result_ref_handles_the_preview_shapes(
    payload: dict, expected: str | None
) -> None:
    assert extract_result_ref(payload) == expected


# ---------------------------------------------------------------------------
# gh-aw
# ---------------------------------------------------------------------------

PATCH_TEXT = """diff --git a/src/theme.ts b/src/theme.ts
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/src/theme.ts
@@ -0,0 +1 @@
+export default 'dark';
"""

ROGUE_PATCH = PATCH_TEXT + """diff --git a/README.md b/README.md
index 2222222..3333333 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# demo
+# owned
"""


def _stub_gh(
    monkeypatch: pytest.MonkeyPatch,
    runner: GhAwRunner,
    patch_text: str | None,
    *,
    patch_name: str = "T001.patch",
    new_runs: list[dict[str, Any]] | None = None,
    baseline_fails: bool = False,
) -> None:
    state = {"dispatched": False}
    fresh = new_runs if new_runs is not None else [{"databaseId": 99, "displayTitle": "T001"}]

    async def list_runs(_slug: str, limit: int = 30) -> list[dict[str, Any]] | None:
        if baseline_fails and not state["dispatched"]:
            return None
        rows: list[dict[str, Any]] = [
            {"databaseId": 1, "displayTitle": "old"},
            {"databaseId": 2, "displayTitle": "older"},
        ]
        return rows + fresh if state["dispatched"] else rows

    async def dispatch(_slug: str, _inputs: dict[str, str]) -> GhResult:
        state["dispatched"] = True
        return GhResult(0, "", "")

    async def view_run(_slug: str, run_id: int) -> dict[str, Any]:
        return {"status": "completed", "conclusion": "success", "url": f"https://x/{run_id}"}

    async def fake_run_gh(*args: str, **_kwargs: Any) -> GhResult:
        assert args[:2] == ("run", "download")
        directory = Path(args[args.index("--dir") + 1])
        (directory / "adlc-patch").mkdir(parents=True, exist_ok=True)
        if patch_text is not None:
            # Bytes, not write_text: on Windows text mode turns LF into CRLF and
            # git then rejects the patch — the exact defect this suite guards.
            (directory / "adlc-patch" / patch_name).write_bytes(patch_text.encode("utf-8"))
        return GhResult(0, "", "")

    monkeypatch.setattr(runner, "list_runs", list_runs)
    monkeypatch.setattr(runner, "dispatch", dispatch)
    monkeypatch.setattr(runner, "view_run", view_run)
    monkeypatch.setattr(gh_aw_module, "run_gh", fake_run_gh)


async def test_gh_aw_refuses_to_run_when_unavailable(
    node: TaskNode, repo: Path, repo_cfg: Config
) -> None:
    outcome = await GhAwRunner().run_task(node, repo, repo_cfg)
    assert outcome["status"] == "fail"
    assert "gh-aw unavailable" in outcome["log"]


async def test_gh_aw_promotes_the_workflow_patch(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    _stub_gh(monkeypatch, runner, PATCH_TEXT)

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "ok", outcome["log"]
    patch = Path(outcome["patchPath"])
    assert patch.read_text(encoding="utf-8") == PATCH_TEXT
    assert "gh-aw run 99" in outcome["log"]
    assert "T001.patch" in outcome["log"]


async def test_gh_aw_refuses_a_patch_outside_the_write_set(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    _stub_gh(monkeypatch, runner, ROGUE_PATCH)

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "outside writeSet" in outcome["log"]
    assert "README.md" in outcome["log"]


async def test_gh_aw_reports_a_workflow_that_uploaded_no_patch(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    _stub_gh(monkeypatch, runner, None)

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "uploaded no" in outcome["log"]


async def test_gh_aw_reports_a_failed_dispatch(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()

    async def some_runs(_slug: str, limit: int = 30) -> list[dict[str, Any]]:
        return [{"databaseId": 1, "displayTitle": "old"}]

    async def refused(_slug: str, _inputs: dict[str, str]) -> GhResult:
        return GhResult(1, "", "could not find any workflows named adlc-task.lock.yml")

    monkeypatch.setattr(runner, "list_runs", some_runs)
    monkeypatch.setattr(runner, "dispatch", refused)

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "could not dispatch" in outcome["log"]
    assert "adlc-task.lock.yml" in outcome["log"]


async def test_gh_aw_refuses_to_dispatch_without_a_run_baseline(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed baseline query would make every pre-existing run look new."""
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    dispatched: list[str] = []

    async def broken(_slug: str, limit: int = 30) -> list[dict[str, Any]] | None:
        return None

    async def dispatch(_slug: str, _inputs: dict[str, str]) -> GhResult:
        dispatched.append("yes")
        return GhResult(0, "", "")

    monkeypatch.setattr(runner, "list_runs", broken)
    monkeypatch.setattr(runner, "dispatch", dispatch)

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "baseline" in outcome["log"]
    assert dispatched == [], "must not dispatch when the baseline is unknown"


async def test_gh_aw_refuses_to_guess_between_concurrent_runs(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling nodes at the same level dispatch the same workflow concurrently."""
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    _stub_gh(
        monkeypatch, runner, PATCH_TEXT,
        new_runs=[
            {"databaseId": 98, "displayTitle": "adlc-task"},
            {"databaseId": 99, "displayTitle": "adlc-task"},
        ],
    )

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "none is identifiable" in outcome["log"]
    assert "run-name" in outcome["log"]


async def test_gh_aw_picks_the_run_named_for_this_task(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    _stub_gh(
        monkeypatch, runner, PATCH_TEXT,
        new_runs=[
            {"databaseId": 98, "displayTitle": "T999"},
            {"databaseId": 42, "displayTitle": "T001"},
            {"databaseId": 97, "displayTitle": "T002"},
        ],
    )

    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "ok", outcome["log"]
    assert "gh-aw run 42" in outcome["log"]


async def test_gh_aw_fails_closed_on_an_unsuccessful_run(
    node: TaskNode, repo: Path, repo_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available(monkeypatch, GhAwRunner)
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    runner = GhAwRunner()
    _stub_gh(monkeypatch, runner, PATCH_TEXT)

    async def failed(_slug: str, run_id: int) -> dict[str, Any]:
        return {"status": "completed", "conclusion": "failure", "url": "https://x"}

    monkeypatch.setattr(runner, "view_run", failed)
    outcome = await runner.run_task(node, repo, repo_cfg)

    assert outcome["status"] == "fail"
    assert "did not succeed" in outcome["log"]


def test_select_patch_file_prefers_the_exact_task_id(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "other.patch").write_text("x", encoding="utf-8")
    (tmp_path / "a" / "T001.patch").write_text("y", encoding="utf-8")
    assert select_patch_file(tmp_path, "T001").name == "T001.patch"


def test_select_patch_file_returns_none_when_there_is_nothing(tmp_path: Path) -> None:
    assert select_patch_file(tmp_path, "T001") is None
