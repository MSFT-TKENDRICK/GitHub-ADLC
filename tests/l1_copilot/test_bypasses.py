"""Regression tests for the write-set bypasses found in code review.

Each of these reproduced a real escape before the enforcement path was moved off
hand-written diff parsing and onto git itself. They use real git and real patch
bytes, because that is the only way the bypasses are visible: the shapes
involved (C-quoted headers, `rename from`/`rename to`) are exactly the ones a
regex scanner cannot see but `git apply` honours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.agents import copilot_sdk
from adlc.adapters.agents import gh_aw as gh_aw_module
from adlc.adapters.agents.copilot_sdk import (
    GitResult,
    changed_paths,
    enumerate_patch_paths,
    finalize_patch,
    patch_from_range,
    paths_in_patch,
    violating_paths,
)
from adlc.adapters.agents.gh_aw import GhAwRunner, GhResult, select_patch_file
from adlc.config import Config
from adlc.ports import TaskNode

from .conftest import git

# ---------------------------------------------------------------------------
# Finding 1 — a failing `git status` must not read as "nothing changed"
# ---------------------------------------------------------------------------


async def test_changed_paths_reports_failure_as_none(tmp_path: Path) -> None:
    assert await changed_paths(tmp_path / "not-a-repo") is None


async def test_finalize_patch_fails_closed_when_the_status_probe_fails(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git status` is the only write-set check; if it fails, nothing may be emitted."""
    (repo / "src" / "theme.ts").write_text("ok\n", encoding="utf-8")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "pwn.yml").write_text("on: push\n", encoding="utf-8")

    real_run_git = copilot_sdk.run_git

    async def flaky(worktree: Path, *args: str, **kwargs: Any) -> GitResult:
        if args and args[0] == "status":
            return GitResult(124, b"", "git status timed out")
        return await real_run_git(worktree, *args, **kwargs)

    monkeypatch.setattr(copilot_sdk, "run_git", flaky)
    result = await finalize_patch(node, repo, repo_cfg, base_sha)

    assert not result.ok
    assert "git status failed" in result.reason
    assert result.patch_path is None


# ---------------------------------------------------------------------------
# Finding 2 — quoted headers and rename records defeat a regex parser
# ---------------------------------------------------------------------------

QUOTED_PATCH = (
    'diff --git "a/.github/workflows/pwn\\303\\251.yml" "b/.github/workflows/pwn\\303\\251.yml"\n'
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    '+++ "b/.github/workflows/pwn\\303\\251.yml"\n'
    "@@ -0,0 +1 @@\n"
    "+on: push\n"
)

RENAME_PATCH = (
    "diff --git a/src/theme.ts b/src/theme.ts\n"
    "similarity index 100%\n"
    "rename from docs/decisions/0001-adr.md\n"
    "rename to .github/workflows/pwn.yml\n"
)


@pytest.fixture
def repo_with_adr(repo: Path) -> Path:
    (repo / "docs" / "decisions").mkdir(parents=True)
    (repo / "docs" / "decisions" / "0001-adr.md").write_text("# adr\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add adr")
    return repo


@pytest.mark.parametrize("patch_text", [QUOTED_PATCH, RENAME_PATCH])
def test_the_regex_scanner_is_blind_to_these_shapes(patch_text: str) -> None:
    """Documents *why* enforcement may not use `paths_in_patch`."""
    assert violating_paths(paths_in_patch(patch_text), ["src/**"]) == []


async def test_git_sees_the_quoted_protected_path(repo: Path, tmp_path: Path) -> None:
    patch = tmp_path / "quoted.patch"
    patch.write_bytes(QUOTED_PATCH.encode("utf-8"))

    declared = await enumerate_patch_paths(repo, patch)

    assert declared == [".github/workflows/pwné.yml"]
    assert violating_paths(declared, ["src/**"]) == [".github/workflows/pwné.yml"]


async def test_git_sees_both_sides_of_a_rename(repo_with_adr: Path, tmp_path: Path) -> None:
    patch = tmp_path / "rename.patch"
    patch.write_bytes(RENAME_PATCH.encode("utf-8"))

    declared = await enumerate_patch_paths(repo_with_adr, patch)

    assert ".github/workflows/pwn.yml" in declared
    assert "docs/decisions/0001-adr.md" in declared, "the rename source must not be invisible"
    assert violating_paths(declared, ["src/**", "docs/**"]) == [
        ".github/workflows/pwn.yml",
        "docs/decisions/0001-adr.md",
    ]


async def test_enumerate_patch_paths_rejects_an_unparseable_patch(
    repo: Path, tmp_path: Path
) -> None:
    patch = tmp_path / "junk.patch"
    patch.write_bytes(b"this is not a patch\n")
    assert await enumerate_patch_paths(repo, patch) is None


def _stub_download(
    monkeypatch: pytest.MonkeyPatch, runner: GhAwRunner, patch_text: str
) -> None:
    async def list_runs(_slug: str, limit: int = 30) -> list[dict[str, Any]]:
        return (
            [{"databaseId": 1, "displayTitle": "old"}]
            if not getattr(list_runs, "gone", False)
            else [{"databaseId": 1, "displayTitle": "old"},
                  {"databaseId": 9, "displayTitle": "T001"}]
        )

    async def dispatch(_slug: str, _inputs: dict[str, str]) -> GhResult:
        list_runs.gone = True
        return GhResult(0, "", "")

    async def view_run(_slug: str, run_id: int) -> dict[str, Any]:
        return {"status": "completed", "conclusion": "success", "url": "https://x"}

    async def fake_run_gh(*args: str, **_kwargs: Any) -> GhResult:
        directory = Path(args[args.index("--dir") + 1])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "T001.patch").write_bytes(patch_text.encode("utf-8"))
        return GhResult(0, "", "")

    monkeypatch.setattr(runner, "list_runs", list_runs)
    monkeypatch.setattr(runner, "dispatch", dispatch)
    monkeypatch.setattr(runner, "view_run", view_run)
    monkeypatch.setattr(gh_aw_module, "run_gh", fake_run_gh)


@pytest.mark.parametrize(
    ("patch_text", "expected"),
    [(QUOTED_PATCH, "pwné.yml"), (RENAME_PATCH, "0001-adr.md")],
)
async def test_gh_aw_refuses_a_patch_that_hides_a_protected_path(
    repo_with_adr: Path, tmp_path: Path, patch_text: str, expected: str,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gh-aw artifact is attacker-controlled; git must be the parser."""
    cfg = Config(root=repo_with_adr, limits={"taskTimeoutSeconds": 30, "pollSeconds": 1})
    monkeypatch.setattr(
        GhAwRunner, "detect", staticmethod(lambda _cfg: (True, "mocked as available"))
    )
    monkeypatch.setenv("ADLC_REPO", "octo/demo")
    monkeypatch.setenv("ADLC_PATCH_DIR", str(tmp_path / "patches"))
    runner = GhAwRunner()
    _stub_download(monkeypatch, runner, patch_text)

    node: TaskNode = {"id": "T001", "title": "t", "kind": "implement", "writeSet": ["src/**"]}
    outcome = await runner.run_task(node, repo_with_adr, cfg)

    assert outcome["status"] == "fail"
    assert "outside writeSet" in outcome["log"]
    assert expected in outcome["log"]
    # Nothing was applied and nothing was promoted.
    assert not (tmp_path / "patches" / "T001.patch").exists()
    assert not (repo_with_adr / ".github" / "workflows").exists()
    assert (repo_with_adr / "docs" / "decisions" / "0001-adr.md").is_file()


# ---------------------------------------------------------------------------
# Finding 3 — a rename in a remote result hides its source path
# ---------------------------------------------------------------------------


async def test_patch_from_range_catches_a_rename_out_of_a_protected_path(
    repo_with_adr: Path, tmp_path: Path
) -> None:
    cfg = Config(root=repo_with_adr, limits={"taskTimeoutSeconds": 30})
    base_sha = git(repo_with_adr, "rev-parse", "HEAD")
    git(repo_with_adr, "checkout", "-q", "-b", "agent/work")
    git(repo_with_adr, "mv", "docs/decisions/0001-adr.md", "docs/guide.md")
    git(repo_with_adr, "commit", "-qm", "sneaky rename")
    git(repo_with_adr, "checkout", "-q", "main")

    node: TaskNode = {"id": "T001", "writeSet": ["docs/*.md"]}
    result = await patch_from_range(node, repo_with_adr, cfg, base_sha, "agent/work")

    assert not result.ok
    assert "docs/decisions/0001-adr.md" in result.violations


# ---------------------------------------------------------------------------
# Finding 5 — never adopt an unrelated run's patch
# ---------------------------------------------------------------------------


def test_select_patch_file_refuses_a_foreign_patch(tmp_path: Path) -> None:
    (tmp_path / "T999.patch").write_text("x", encoding="utf-8")
    assert select_patch_file(tmp_path, "T001") is None


def test_select_patch_file_refuses_when_the_task_id_is_empty(tmp_path: Path) -> None:
    (tmp_path / "anything.patch").write_text("x", encoding="utf-8")
    assert select_patch_file(tmp_path, "") is None
