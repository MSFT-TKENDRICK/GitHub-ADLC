"""`detect()` must be cheap, non-raising, network-free, and specific.

These are the tests that guarantee the credential-free spine keeps working: with
no token the GitHub store must decline politely so ``sqlite`` takes over.
"""

from __future__ import annotations

import urllib.request
from typing import ClassVar

import pytest

from adlc.adapters.taskstore import github as gh
from adlc.config import Config, load_adapters


@pytest.fixture(autouse=True)
def _explode_on_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any network call from detect() is a contract violation, not a slow test."""

    def boom(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("detect() must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(urllib.request, "build_opener", boom)


def _write_git_config(root, url: str) -> None:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*\n', encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The path the conformance suite exercises: no credentials at all.
# ---------------------------------------------------------------------------


def test_detect_without_token_declines_with_specific_reason(cfg: Config) -> None:
    available, reason = gh.GitHubTaskStore.detect(cfg)
    assert available is False
    assert reason == "GITHUB_TOKEN not set — falling back to sqlite task store"


def test_detect_without_token_ignores_a_present_repo(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    available, reason = gh.GitHubTaskStore.detect(cfg)
    assert available is False
    assert "GITHUB_TOKEN" in reason


def test_detect_with_token_but_no_repo_declines(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    available, reason = gh.GitHubTaskStore.detect(cfg)
    assert available is False
    assert "GITHUB_REPOSITORY" in reason
    assert "sqlite" in reason


def test_detect_never_raises_on_a_broken_config() -> None:
    class Exploding:
        raw: ClassVar[dict] = {}

        @property
        def root(self):  # pragma: no cover - exercised via detect()
            raise OSError("disk on fire")

    available, reason = gh.GitHubTaskStore.detect(Exploding())  # type: ignore[arg-type]
    assert available is False
    assert isinstance(reason, str) and reason


@pytest.mark.parametrize("token_var", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_detect_accepts_either_token_variable(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, token_var: str
) -> None:
    monkeypatch.setenv(token_var, "ghp_example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    available, reason = gh.GitHubTaskStore.detect(cfg)
    assert available is True
    assert "acme/widgets" in reason


def test_detect_ignores_a_blank_token(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    assert gh.GitHubTaskStore.detect(cfg)[0] is False


# ---------------------------------------------------------------------------
# Repo resolution from a git remote (still no network, just a file read).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widgets.git",
        "https://github.com/acme/widgets",
        "git@github.com:acme/widgets.git",
        "ssh://git@github.com/acme/widgets.git",
    ],
)
def test_detect_resolves_repo_from_git_remote(
    tmp_path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    _write_git_config(tmp_path, url)
    available, reason = gh.GitHubTaskStore.detect(Config(root=tmp_path))
    assert available is True, reason
    assert "acme/widgets" in reason


def test_detect_ignores_non_github_remotes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    _write_git_config(tmp_path, "https://gitlab.com/acme/widgets.git")
    available, _ = gh.GitHubTaskStore.detect(Config(root=tmp_path))
    assert available is False


def test_resolve_repo_follows_a_worktree_gitdir_pointer(tmp_path) -> None:
    """A worktree's ``.git`` is a file; the config lives in the common dir."""
    common = tmp_path / "main" / ".git"
    (common / "worktrees" / "wt").mkdir(parents=True)
    (common / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widgets.git\n', encoding="utf-8"
    )
    (common / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {common / 'worktrees' / 'wt'}\n", encoding="utf-8")

    assert gh.resolve_repo(Config(root=worktree)) == ("acme", "widgets")


def test_resolve_repo_prefers_explicit_config_over_remote(tmp_path) -> None:
    _write_git_config(tmp_path, "https://github.com/acme/widgets.git")
    cfg = Config(root=tmp_path, raw={"taskstore": {"github": {"repo": "other/thing"}}})
    assert gh.resolve_repo(cfg, gh._settings_from_cfg(cfg)) == ("other", "thing")


def test_resolve_repo_handles_a_missing_git_directory(tmp_path) -> None:
    assert gh.resolve_repo(Config(root=tmp_path)) is None


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_entry_point_is_discoverable_and_does_not_displace_the_default() -> None:
    adapters = load_adapters("taskstore")
    assert adapters.get("github") is gh.GitHubTaskStore
    assert gh.GitHubTaskStore.name == "github"
    assert gh.GitHubTaskStore.kind == "taskstore"


def test_uncredentialed_construction_does_not_raise(cfg: Config) -> None:
    """`select_adapter` instantiates adapters eagerly; that must be inert."""
    store = gh.GitHubTaskStore(cfg)
    assert store.node_records == {}
    with pytest.raises(gh.GitHubTaskStoreError, match="GITHUB_TOKEN not set"):
        _ = store.transport
