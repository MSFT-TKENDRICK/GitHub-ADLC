"""Patch production against a real (local, throwaway) git repository.

These prove the two contract clauses in ``docs/PLAN.md`` §4.4 that matter most:
the patch is anchored to the worktree's exact base SHA, and a write outside
``node['writeSet']`` is refused rather than silently trimmed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from adlc.adapters.agents.copilot_sdk import (
    changed_paths,
    finalize_patch,
    patch_from_range,
    resolve_patch_path,
    sha_of,
)
from adlc.config import Config
from adlc.ports import TaskNode

from .conftest import git


def _applies_at(repo: Path, base: str, patch: Path) -> bool:
    """Reset to ``base`` and check the patch applies there — the anchoring proof."""
    git(repo, "reset", "--hard", "--quiet", base)
    git(repo, "clean", "-fdq")
    result = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


async def test_sha_of_resolves_head(repo: Path, base_sha: str) -> None:
    assert await sha_of(repo) == base_sha
    assert await sha_of(repo, "does-not-exist") is None


async def test_sha_of_on_a_non_repo(tmp_path: Path) -> None:
    assert await sha_of(tmp_path / "nowhere") is None


async def test_changed_paths_reports_modified_and_untracked(repo: Path) -> None:
    (repo / "src" / "app.ts").write_text("changed\n", encoding="utf-8")
    (repo / "src" / "theme.ts").write_text("new\n", encoding="utf-8")
    assert set(await changed_paths(repo)) == {"src/app.ts", "src/theme.ts"}


async def test_changed_paths_ignores_gitignored_output(repo: Path) -> None:
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore build")
    (repo / "build").mkdir()
    (repo / "build" / "bundle.js").write_text("noise\n", encoding="utf-8")
    assert await changed_paths(repo) == []


async def test_patch_is_written_and_applies_at_the_base_sha(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    (repo / "src" / "app.ts").write_text("export const mount = () => 1;\n", encoding="utf-8")
    (repo / "src" / "theme.ts").write_text("export default 'dark';\n", encoding="utf-8")

    result = await finalize_patch(node, repo, repo_cfg, base_sha)

    assert result.ok, result.reason
    assert result.patch_path is not None
    assert result.patch_path.name == "T001.patch"
    assert result.patch_path.parent.name == "patches"
    assert result.patch_path.parent.parent == repo.parent  # never inside the worktree
    assert base_sha[:12] in result.reason

    text = result.patch_path.read_text(encoding="utf-8")
    assert "diff --git a/src/app.ts b/src/app.ts" in text
    assert "src/theme.ts" in text
    assert _applies_at(repo, base_sha, result.patch_path)


async def test_out_of_write_set_edit_is_refused_and_reverted(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    (repo / "src" / "app.ts").write_text("legit\n", encoding="utf-8")
    (repo / "README.md").write_text("SNEAKY\n", encoding="utf-8")
    (repo / "secrets.env").write_text("TOKEN=hunter2\n", encoding="utf-8")

    result = await finalize_patch(node, repo, repo_cfg, base_sha)

    assert not result.ok
    assert "outside writeSet" in result.reason
    assert result.violations == ["README.md", "secrets.env"]
    # "Refuse" means the edits are actually undone, not merely excluded.
    assert (repo / "README.md").read_text(encoding="utf-8") == "# demo\n"
    assert not (repo / "secrets.env").exists()
    # ...and nothing was written to patches/.
    assert not resolve_patch_path(node, repo, repo_cfg).exists()


async def test_protected_path_is_refused_even_when_declared(
    repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    node: TaskNode = {"id": "T002", "writeSet": ["src/app.ts", ".github/**"]}
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "evil.yml").write_text("on: push\n", encoding="utf-8")

    result = await finalize_patch(node, repo, repo_cfg, base_sha)

    assert not result.ok
    assert ".github/workflows/evil.yml" in result.violations
    assert not (repo / ".github" / "workflows" / "evil.yml").exists()


async def test_no_changes_is_a_failure_not_a_silent_pass(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    result = await finalize_patch(node, repo, repo_cfg, base_sha)
    assert not result.ok
    assert "no file changes" in result.reason


async def test_empty_write_set_is_a_failure(repo: Path, repo_cfg: Config) -> None:
    result = await finalize_patch({"id": "T003", "writeSet": []}, repo, repo_cfg)
    assert not result.ok
    assert "empty writeSet" in result.reason


async def test_non_repo_worktree_fails_cleanly(
    node: TaskNode, cfg: Config, tmp_path: Path
) -> None:
    result = await finalize_patch(node, tmp_path / "not-a-repo", cfg)
    assert not result.ok
    assert "not a git worktree" in result.reason


async def test_patch_path_honours_the_explicit_override(
    node: TaskNode, repo: Path, repo_cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADLC_PATCH_DIR", str(tmp_path / "explicit"))
    assert resolve_patch_path(node, repo, repo_cfg) == tmp_path / "explicit" / "T001.patch"


async def test_patch_path_finds_the_run_directory(node: TaskNode, tmp_path: Path) -> None:
    run_dir = tmp_path / "repo" / ".adlc" / "runs" / "2026-08-19-a1b2"
    worktree = run_dir / "worktrees" / "T001"
    worktree.mkdir(parents=True)
    (run_dir / "taskgraph.json").write_text("{}", encoding="utf-8")
    local_cfg = Config(root=tmp_path / "repo")
    assert resolve_patch_path(node, worktree, local_cfg) == run_dir / "patches" / "T001.patch"


async def test_task_id_is_sanitized_for_the_filename(repo: Path, repo_cfg: Config) -> None:
    path = resolve_patch_path({"id": "T 1/../evil"}, repo, repo_cfg)
    assert path.name == "T_1_.._evil.patch"
    assert path.parent == repo.parent / "patches"


# ---------------------------------------------------------------------------
# patch_from_range — work produced on another ref (cloud agent / gh-aw)
# ---------------------------------------------------------------------------


async def test_patch_from_range_anchors_to_the_base_sha(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    git(repo, "checkout", "-q", "-b", "agent/work")
    (repo / "src" / "theme.ts").write_text("export default 'dark';\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "agent work")
    git(repo, "checkout", "-q", "main")

    result = await patch_from_range(node, repo, repo_cfg, base_sha, "agent/work")

    assert result.ok, result.reason
    assert result.patch_path is not None
    assert "src/theme.ts" in result.patch_path.read_text(encoding="utf-8")
    assert _applies_at(repo, base_sha, result.patch_path)


async def test_patch_from_range_refuses_out_of_write_set_results(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    git(repo, "checkout", "-q", "-b", "agent/work")
    (repo / "src" / "theme.ts").write_text("ok\n", encoding="utf-8")
    (repo / "README.md").write_text("SNEAKY\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "agent overreach")
    git(repo, "checkout", "-q", "main")

    result = await patch_from_range(node, repo, repo_cfg, base_sha, "agent/work")

    assert not result.ok
    assert result.violations == ["README.md"]
    assert not resolve_patch_path(node, repo, repo_cfg).exists()


async def test_patch_from_range_on_an_identical_ref(
    node: TaskNode, repo: Path, repo_cfg: Config, base_sha: str
) -> None:
    result = await patch_from_range(node, repo, repo_cfg, base_sha, "HEAD")
    assert not result.ok
    assert "identical to base" in result.reason
