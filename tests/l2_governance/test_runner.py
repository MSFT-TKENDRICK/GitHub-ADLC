"""``MafGovernedRunner``: write-set enforcement, patching, prompts, tools.

No MAF, no AGT, no credentials, no model call. What is exercised here is the
part of the runner that upholds the frozen ``AgentRunner`` contract: a patch
anchored to the base SHA, and nothing written outside ``node['writeSet']``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adlc.adapters.agents import maf_governed
from adlc.adapters.agents.maf_governed import (
    MafGovernedRunner,
    _compile_glob,
    _resolve_run_dir,
    _write_set_violations,
    build_prompt,
    build_worktree_tools,
    changed_paths,
)
from adlc.config import Config
from adlc.ports import PROTECTED_PATHS


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit, standing in for a task worktree."""
    root = tmp_path / "worktree"
    root.mkdir()
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    git("config", "user.email", "adlc@example.invalid")
    git("config", "user.name", "ADLC Test")
    git("config", "commit.gpgsign", "false")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return root


def head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Glob semantics
# ---------------------------------------------------------------------------


class TestGlobCompilation:
    @pytest.mark.parametrize(
        ("pattern", "path", "matches"),
        [
            ("src/theme.ts", "src/theme.ts", True),
            ("src/theme.ts", "src/theme.test.ts", False),
            ("src/*.ts", "src/theme.ts", True),
            # The bug fnmatch would introduce: `*` must not cross a separator.
            ("src/*.ts", "src/nested/theme.ts", False),
            ("src/**/*.ts", "src/nested/deep/theme.ts", True),
            ("src/**", "src/nested/theme.ts", True),
            (".github/**", ".github/workflows/ci.yml", True),
            (".github/**", ".github", True),
            ("docs/decisions/**", "docs/decisions/0001-x.md", True),
            ("docs/decisions/**", "docs/other.md", False),
            ("pyproject.toml", "pyproject.toml", True),
            ("pyproject.toml", "sub/pyproject.toml", False),
            # An exact entry must NOT authorize descendants: nothing stops an
            # agent creating `theme` as a directory and hiding files under it.
            ("src/theme", "src/theme/index.ts", False),
            ("src/theme", "src/theme", True),
            ("src/theme/", "src/theme/index.ts", True),
            ("src/theme/**", "src/theme/index.ts", True),
            ("src/new.py", "src/new.py/extra.txt", False),
        ],
    )
    def test_matching(self, pattern: str, path: str, matches: bool) -> None:
        assert bool(_compile_glob(pattern).match(path)) is matches

    def test_every_protected_path_compiles(self) -> None:
        for pattern in PROTECTED_PATHS:
            assert _compile_glob(pattern) is not None


# ---------------------------------------------------------------------------
# Write-set enforcement
# ---------------------------------------------------------------------------


class TestWriteSetEnforcement:
    def test_clean_worktree_has_no_violations(self, repo: Path) -> None:
        assert _write_set_violations(repo, ["src/app.py"]) == set()

    def test_allowed_edit_is_not_a_violation(self, repo: Path) -> None:
        (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
        assert _write_set_violations(repo, ["src/app.py"]) == set()

    def test_write_outside_the_set_is_a_violation(self, repo: Path) -> None:
        (repo / "src" / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
        assert _write_set_violations(repo, ["src/app.py"]) == {"src/sneaky.py"}

    def test_untracked_files_are_seen(self, repo: Path) -> None:
        (repo / "notes.md").write_text("hi\n", encoding="utf-8")
        assert "notes.md" in _write_set_violations(repo, ["src/app.py"])

    def test_deletion_is_seen(self, repo: Path) -> None:
        (repo / "src" / "app.py").unlink()
        assert _write_set_violations(repo, ["docs/x.md"]) == {"src/app.py"}

    def test_protected_paths_violate_even_when_in_the_write_set(self, repo: Path) -> None:
        """A malformed graph must not be able to authorize `.github/**`."""
        (repo / ".github").mkdir()
        (repo / ".github" / "evil.yml").write_text("on: push\n", encoding="utf-8")
        violations = _write_set_violations(repo, [".github/**", "src/app.py"])
        assert ".github/evil.yml" in violations

    def test_pyproject_is_protected(self, repo: Path) -> None:
        (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        assert "pyproject.toml" in _write_set_violations(repo, ["pyproject.toml"])

    def test_changed_paths_on_a_non_repo_is_empty(self, tmp_path: Path) -> None:
        assert changed_paths(tmp_path / "not-a-repo") == set()


# ---------------------------------------------------------------------------
# Patch production
# ---------------------------------------------------------------------------


class TestPatchProductionIsTheSpinesJob:
    """The runner must not stage, diff, reset or write patches.

    ``executor.Worktree.diff()`` does that immediately after ``run_task``
    returns, with ``core.autocrlf=false``. A ``git add`` here would populate the
    index with translated line endings and reintroduce "corrupt patch".
    """

    def test_runner_module_never_stages_or_resets(self) -> None:
        source = Path(maf_governed.__file__).read_text(encoding="utf-8")
        for forbidden in ('"add"', '"reset"', '"clean"', '"commit"', '"checkout"'):
            assert forbidden not in source, f"runner must not run git {forbidden}"

    @pytest.mark.asyncio
    async def test_worktree_is_left_untouched_by_a_write_set_violation(
        self, monkeypatch, repo: Path, cfg: Config
    ) -> None:
        """A violation must not reset the worktree — that would destroy evidence."""
        monkeypatch.setattr(MafGovernedRunner, "detect", staticmethod(lambda cfg: (True, "ok")))

        async def _invoke(self, node, worktree, cfg, engine, write_set):
            (worktree / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
            return "did a bad thing", {}

        monkeypatch.setattr(MafGovernedRunner, "_invoke", _invoke)
        monkeypatch.setattr(
            maf_governed.PolicyEngine, "load", classmethod(lambda cls, *a, **k: _NullEngine())
        )

        outcome = await MafGovernedRunner().run_task(
            {"id": "T1", "writeSet": ["src/app.py"]}, repo, cfg
        )
        assert outcome["status"] == "fail"
        assert "outside writeSet" in outcome["log"]
        assert (repo / "sneaky.py").is_file(), "the runner reset the worktree"

    @pytest.mark.asyncio
    async def test_index_is_left_clean_for_the_executor(
        self, monkeypatch, repo: Path, cfg: Config
    ) -> None:
        monkeypatch.setattr(MafGovernedRunner, "detect", staticmethod(lambda cfg: (True, "ok")))

        async def _invoke(self, node, worktree, cfg, engine, write_set):
            (worktree / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
            return "ok", {}

        monkeypatch.setattr(MafGovernedRunner, "_invoke", _invoke)
        monkeypatch.setattr(
            maf_governed.PolicyEngine, "load", classmethod(lambda cls, *a, **k: _NullEngine())
        )

        outcome = await MafGovernedRunner().run_task(
            {"id": "T1", "writeSet": ["src/app.py"]}, repo, cfg
        )
        assert outcome["status"] == "ok"
        staged = subprocess.run(  # noqa: ASYNC221 - a fast, bounded git read
            ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert staged == "", "the runner staged files; the executor must own the index"


class _NullEngine:
    """A PolicyEngine stand-in that permits nothing and records nothing."""

    records: tuple = ()

    def evidence(self) -> dict:
        return {"engine": "null", "policy": "", "total": 0, "denied": 0, "decisions": []}

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Worktree-scoped tools (defence in depth behind the policy)
# ---------------------------------------------------------------------------


class TestWorktreeTools:
    def tools(self, repo: Path, write_set: list[str]):
        read_file, write_file, list_files = build_worktree_tools(repo, write_set)
        return read_file, write_file, list_files

    def test_read_within_the_worktree(self, repo: Path) -> None:
        read_file, _, _ = self.tools(repo, ["src/app.py"])
        assert "hello" in read_file("src/app.py")

    def test_read_cannot_escape_the_worktree(self, repo: Path) -> None:
        read_file, _, _ = self.tools(repo, ["src/app.py"])
        assert read_file("../../../etc/passwd").startswith("ERROR")

    def test_read_refuses_protected_paths(self, repo: Path) -> None:
        (repo / ".adlc").mkdir()
        (repo / ".adlc" / "config.yaml").write_text("profile: full\n", encoding="utf-8")
        read_file, _, _ = self.tools(repo, ["src/app.py"])
        assert "protected" in read_file(".adlc/config.yaml")

    def test_write_inside_the_write_set(self, repo: Path) -> None:
        _, write_file, _ = self.tools(repo, ["src/new.py"])
        assert write_file("src/new.py", "X = 1\n").startswith("wrote")
        assert (repo / "src" / "new.py").read_text(encoding="utf-8") == "X = 1\n"

    def test_write_outside_the_write_set_is_refused(self, repo: Path) -> None:
        _, write_file, _ = self.tools(repo, ["src/new.py"])
        assert "not in this task's writeSet" in write_file("src/other.py", "X = 1\n")
        assert not (repo / "src" / "other.py").exists()

    def test_write_to_a_protected_path_is_refused(self, repo: Path) -> None:
        _, write_file, _ = self.tools(repo, [".github/**"])
        assert "protected" in write_file(".github/workflows/evil.yml", "on: push\n")
        assert not (repo / ".github" / "workflows" / "evil.yml").exists()

    def test_write_cannot_traverse_out(self, repo: Path, tmp_path: Path) -> None:
        _, write_file, _ = self.tools(repo, ["**"])
        assert write_file("../escaped.py", "X = 1\n").startswith("ERROR")
        assert not (tmp_path / "escaped.py").exists()

    def test_list_files_excludes_git(self, repo: Path) -> None:
        _, _, list_files = self.tools(repo, ["src/app.py"])
        listing = list_files("**/*")
        assert "src/app.py" in listing
        assert ".git/" not in listing

    def test_list_files_refuses_traversal_patterns(self, repo: Path) -> None:
        """`../**/*` would enumerate sibling worktrees."""
        _, _, list_files = self.tools(repo, ["src/app.py"])
        assert list_files("../**/*").startswith("ERROR")
        assert list_files("/etc/*").startswith("ERROR")


class TestGitDirectoryIsUnreachable:
    """`.git` is never reachable, whatever the write set says.

    The spine runs `git add -A` on this worktree the instant run_task returns,
    so a writable `.git/config` is arbitrary code execution via clean filters
    or an external diff command.
    """

    def tools(self, repo: Path, write_set: list[str]):
        return build_worktree_tools(repo, write_set)

    def test_write_into_git_is_refused_even_with_a_wildcard_write_set(
        self, repo: Path
    ) -> None:
        _, write_file, _ = self.tools(repo, ["**"])
        result = write_file(".git/config", "[core]\n\tfsmonitor = calc.exe\n")
        assert result.startswith("ERROR")
        # A real .git/config always has a [core] section; assert the *injected*
        # setting never landed. fsmonitor is arbitrary command execution.
        assert "fsmonitor" not in (repo / ".git" / "config").read_text(encoding="utf-8")

    def test_write_into_git_hooks_is_refused(self, repo: Path) -> None:
        _, write_file, _ = self.tools(repo, [".git/**"])
        assert write_file(".git/hooks/pre-commit", "#!/bin/sh\n").startswith("ERROR")

    def test_reading_git_config_is_refused(self, repo: Path) -> None:
        read_file, _, _ = self.tools(repo, ["**"])
        assert read_file(".git/config").startswith("ERROR")

    def test_case_variants_are_refused_too(self, repo: Path) -> None:
        _, write_file, _ = self.tools(repo, ["**"])
        assert write_file(".GIT/config", "x").startswith("ERROR")

    def test_git_changes_count_as_violations(self, repo: Path) -> None:
        (repo / ".git" / "adlc-marker").write_text("x\n", encoding="utf-8")
        # git status never reports administrative files, so assert the guard
        # directly rather than relying on porcelain output.
        assert maf_governed._has_git_component(".git/config") is True
        assert maf_governed._has_git_component("src/.git/config") is True
        assert maf_governed._has_git_component("src/app.py") is False


class TestSensitiveFilesAndSymlinks:
    def tools(self, repo: Path, write_set: list[str]):
        return build_worktree_tools(repo, write_set)

    @pytest.mark.parametrize(
        "path",
        [".env", ".env.production", "sub/.npmrc", ".ssh/id_rsa", "deploy/.aws/credentials"],
    )
    def test_credential_files_are_refused(self, repo: Path, path: str) -> None:
        read_file, _, _ = self.tools(repo, ["**"])
        assert read_file(path).startswith("ERROR")
        assert maf_governed._is_sensitive(path) is True

    def test_ordinary_files_are_not_flagged_sensitive(self) -> None:
        for path in ("src/app.py", "docs/env.md", "environment.yml"):
            assert maf_governed._is_sensitive(path) is False

    def test_symlink_to_a_sensitive_file_is_refused(self, repo: Path) -> None:
        """Policy sees `docs/notes.txt`; only the filesystem knows it is `.env`."""
        (repo / ".env").write_text("TOKEN=hunter2\n", encoding="utf-8")
        (repo / "docs").mkdir()
        try:
            (repo / "docs" / "notes.txt").symlink_to(repo / ".env")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this machine")

        read_file, _, _ = self.tools(repo, ["**"])
        result = read_file("docs/notes.txt")
        assert result.startswith("ERROR")
        assert "hunter2" not in result

    def test_symlinked_directory_is_refused(self, repo: Path) -> None:
        (repo / "secrets").mkdir()
        (repo / "secrets" / "key.txt").write_text("k\n", encoding="utf-8")
        try:
            (repo / "link").symlink_to(repo / "secrets", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this machine")

        read_file, _, _ = self.tools(repo, ["**"])
        assert read_file("link/key.txt").startswith("ERROR")

    def test_symlink_guard_is_consulted_even_without_real_symlinks(
        self, repo: Path, monkeypatch
    ) -> None:
        """Windows often forbids symlink creation, so assert the wiring directly."""
        (repo / "docs").mkdir()
        (repo / "docs" / "notes.txt").write_text("plain\n", encoding="utf-8")

        real_is_symlink = Path.is_symlink

        def _fake(self: Path) -> bool:
            if self.name == "notes.txt":
                return True
            return real_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", _fake)
        read_file, _, _ = self.tools(repo, ["**"])
        result = read_file("docs/notes.txt")
        assert result.startswith("ERROR")
        assert "symlink" in result
        assert "plain" not in result

    def test_symlink_guard_walks_every_ancestor(self, repo: Path, monkeypatch) -> None:
        real_is_symlink = Path.is_symlink

        def _fake(self: Path) -> bool:
            if self.name == "link":
                return True
            return real_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", _fake)
        assert maf_governed._traverses_symlink(repo, Path("link/deep/file.txt")) is True
        assert maf_governed._traverses_symlink(repo, Path("src/app.py")) is False

    def test_parent_traversal_is_refused_lexically(self, repo: Path) -> None:
        read_file, write_file, _ = self.tools(repo, ["**"])
        assert read_file("../outside.txt").startswith("ERROR")
        assert write_file("../outside.txt", "x").startswith("ERROR")

    def test_absolute_paths_are_refused(self, repo: Path) -> None:
        read_file, _, _ = self.tools(repo, ["**"])
        assert read_file(str(Path(repo).anchor or "/")).startswith("ERROR")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_includes_the_write_set_and_acceptance(self) -> None:
        node = {
            "id": "T003",
            "title": "Add a theme",
            "kind": "implement",
            "writeSet": ["src/theme.ts"],
            "acceptance": ["US1-AC2"],
            "context": {
                "interfaces": "export function mount(): void",
                "commands": {"test": "npm test"},
                "refs": [{"path": "src/app.ts", "symbols": ["mount"], "excerpt": "…"}],
            },
        }
        prompt = build_prompt(node, node["writeSet"])
        assert "T003" in prompt
        assert "src/theme.ts" in prompt
        assert "US1-AC2" in prompt
        assert "npm test" in prompt
        assert "mount" in prompt

    def test_always_states_the_protected_paths(self) -> None:
        prompt = build_prompt({"id": "T1", "title": "x"}, ["src/a.py"])
        assert ".github/**" in prompt
        assert "docs/decisions/**" in prompt


# ---------------------------------------------------------------------------
# run_task guard rails
# ---------------------------------------------------------------------------


class TestRunTaskGuards:
    @pytest.mark.asyncio
    async def test_empty_write_set_is_rejected(self, monkeypatch, repo: Path, cfg: Config) -> None:
        monkeypatch.setattr(MafGovernedRunner, "detect", staticmethod(lambda cfg: (True, "ok")))
        outcome = await MafGovernedRunner().run_task({"id": "T1", "writeSet": []}, repo, cfg)
        assert outcome["status"] == "fail"
        assert "empty writeSet" in outcome["log"]

    @pytest.mark.asyncio
    async def test_non_git_worktree_is_rejected(
        self, monkeypatch, tmp_path: Path, cfg: Config
    ) -> None:
        monkeypatch.setattr(MafGovernedRunner, "detect", staticmethod(lambda cfg: (True, "ok")))
        outcome = await MafGovernedRunner().run_task(
            {"id": "T1", "writeSet": ["a.py"]}, tmp_path / "nope", cfg
        )
        assert outcome["status"] == "fail"
        assert "not a git worktree" in outcome["log"]

    @pytest.mark.asyncio
    async def test_outcome_matches_the_frozen_contract(
        self, no_optional_deps, repo: Path, cfg: Config
    ) -> None:
        outcome = await MafGovernedRunner().run_task(
            {"id": "T1", "writeSet": ["src/app.py"]}, repo, cfg
        )
        assert set(outcome) <= {"status", "patchPath", "log", "tokensIn", "tokensOut", "cost"}
        assert outcome["status"] in {"ok", "fail", "skipped"}
        assert isinstance(outcome["tokensIn"], int)
        assert isinstance(outcome["cost"], float)


class TestRunDirResolution:
    def test_env_run_dir_wins(self, monkeypatch, cfg: Config, tmp_path: Path) -> None:
        monkeypatch.setenv("ADLC_RUN_DIR", str(tmp_path / "explicit"))
        assert _resolve_run_dir(cfg) == tmp_path / "explicit"

    def test_run_id_maps_through_config(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.delenv("ADLC_RUN_DIR", raising=False)
        monkeypatch.setenv("ADLC_RUN_ID", "2026-08-19-a1b2")
        assert _resolve_run_dir(cfg) == cfg.run_dir("2026-08-19-a1b2")

    def test_fallback_is_inside_the_runs_dir(self, monkeypatch, cfg: Config) -> None:
        monkeypatch.delenv("ADLC_RUN_DIR", raising=False)
        monkeypatch.delenv("ADLC_RUN_ID", raising=False)
        assert _resolve_run_dir(cfg).parent == cfg.runs_dir


class TestChatClientResolution:
    def test_no_env_means_unavailable(self, monkeypatch) -> None:
        for env in (
            "ADLC_MAF_CHAT_CLIENT",
            "AZURE_AI_PROJECT_ENDPOINT",
            "AZURE_OPENAI_ENDPOINT",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(env, raising=False)
        assert maf_governed._chat_client_env() is None

    def test_factory_hook_is_honoured(self, monkeypatch) -> None:
        monkeypatch.setenv("ADLC_MAF_CHAT_CLIENT", "adlc.ports:PROTECTED_PATHS")
        assert maf_governed._chat_client_env() == "ADLC_MAF_CHAT_CLIENT"
