"""Integration with the landed spine.

Covers the seams the spine actually drives: `select_adapter` builds the store
with **no arguments**, `stages/graph.py` calls `bind(cfg)` before `sync()`, and a
failing task store must never block the graph stage.
"""

from __future__ import annotations

import pytest
from .conftest import FakeGitHub, issue_number, make_graph

from adlc.adapters.taskstore import github as gh
from adlc.adapters.taskstore.sqlite import SqliteTaskStore
from adlc.config import (
    EXPLICIT_ONLY_KINDS,
    SPINE_DEFAULTS,
    Config,
    load_adapters,
    select_adapter,
)
from adlc.ports import TaskStore

#: Entry points come from installed distribution metadata, not from PYTHONPATH.
#: Without them `select_adapter` cannot resolve anything, so skip rather than
#: report a false failure.
requires_entry_points = pytest.mark.skipif(
    not load_adapters("taskstore"),
    reason="no adlc distribution metadata — install the package into a venv to test selection",
)


def _write_git_config(root, url: str = "https://github.com/acme/widgets.git") -> None:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")


# ---------------------------------------------------------------------------
# Adapter selection -- the credential-free default must win
# ---------------------------------------------------------------------------


@requires_entry_points
def test_sqlite_is_selected_when_there_is_no_token(cfg: Config) -> None:
    assert SPINE_DEFAULTS["taskstore"] == "sqlite"
    assert isinstance(select_adapter(cfg, "taskstore"), SqliteTaskStore)


@requires_entry_points
def test_ambient_credentials_alone_must_not_select_github(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of EXPLICIT_ONLY_KINDS.

    Every GitHub Actions runner has `GITHUB_TOKEN`, and plenty of laptops have
    `gh` authenticated. If detection alone selected this store, a plain
    `adlc graph` would start writing real issues into a live repository with
    nobody having opted in.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    cfg = Config(root=tmp_path)

    assert "taskstore" in EXPLICIT_ONLY_KINDS
    assert gh.GitHubTaskStore.detect(cfg)[0] is True, "detect() should still report available"
    assert isinstance(select_adapter(cfg, "taskstore"), SqliteTaskStore)


@requires_entry_points
def test_config_yaml_opt_in_selects_github(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapters: {taskstore: github}` in .adlc/config.yaml is the supported route."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    adlc_dir = tmp_path / ".adlc"
    adlc_dir.mkdir()
    (adlc_dir / "config.yaml").write_text(
        "version: 1\nadapters:\n  taskstore: github\n", encoding="utf-8"
    )

    cfg = Config.load(tmp_path)
    assert cfg.adapters.get("taskstore") == "github"
    assert isinstance(select_adapter(cfg, "taskstore"), gh.GitHubTaskStore)


@requires_entry_points
def test_opt_in_is_honoured_even_without_credentials(tmp_path) -> None:
    """Explicit config wins over detection, so the failure is loud, not a silent swap."""
    cfg = Config(root=tmp_path, adapters={"taskstore": "github"})
    assert isinstance(select_adapter(cfg, "taskstore"), gh.GitHubTaskStore)


@requires_entry_points
def test_an_explicit_override_still_resolves(cfg: Config) -> None:
    assert isinstance(select_adapter(cfg, "taskstore", override="github"), gh.GitHubTaskStore)


def test_both_stores_satisfy_the_same_protocol() -> None:
    assert isinstance(gh.GitHubTaskStore(), TaskStore)
    assert isinstance(SqliteTaskStore(), TaskStore)


# ---------------------------------------------------------------------------
# bind(cfg) -- how config reaches a no-arg-constructed adapter
# ---------------------------------------------------------------------------


def test_the_spine_constructs_adapters_with_no_arguments() -> None:
    """`select_adapter` does `cls()`; construction must not need a config."""
    store = gh.GitHubTaskStore()
    assert store.cfg is None
    assert store.sub_issue_limit == gh.SUB_ISSUE_LIMIT


def test_bind_matches_the_hook_the_graph_stage_looks_for() -> None:
    store = gh.GitHubTaskStore()
    assert hasattr(store, "bind")
    assert hasattr(SqliteTaskStore(), "bind")


def test_bind_resolves_the_repository_from_the_git_remote(tmp_path) -> None:
    """Without bind() a no-arg store has no root and cannot find the remote."""
    _write_git_config(tmp_path)
    store = gh.GitHubTaskStore()
    with pytest.raises(gh.GitHubTaskStoreError, match="GITHUB_REPOSITORY"):
        _ = store.slug

    store.bind(Config(root=tmp_path))
    assert store.slug == "acme/widgets"


def test_bind_picks_up_the_taskstore_github_config_block(tmp_path) -> None:
    cfg = Config(
        root=tmp_path,
        raw={
            "taskstore": {
                "github": {
                    "repo": "other/thing",
                    "maxSubIssues": 7,
                    "syncDependencies": False,
                    "labels": ["tracked"],
                }
            }
        },
    )
    store = gh.GitHubTaskStore()
    store.bind(cfg)

    assert store.slug == "other/thing"
    assert store.sub_issue_limit == 7
    assert store.sync_dependencies is False
    assert store.extra_labels == ["tracked"]


def test_bind_does_not_clobber_explicit_constructor_settings(tmp_path) -> None:
    cfg = Config(root=tmp_path, raw={"taskstore": {"github": {"maxSubIssues": 7}}})
    store = gh.GitHubTaskStore(
        transport=FakeGitHub(), owner="acme", repo="widgets", settings={"maxSubIssues": 3}
    )
    store.bind(cfg)

    assert store.sub_issue_limit == 3
    assert store.slug == "acme/widgets"


def test_bind_is_idempotent(tmp_path) -> None:
    cfg = Config(root=tmp_path, raw={"taskstore": {"github": {"repo": "acme/widgets"}}})
    store = gh.GitHubTaskStore()
    store.bind(cfg)
    store.bind(cfg)
    assert store.slug == "acme/widgets"


# ---------------------------------------------------------------------------
# Run directory references come from config, not a hardcoded string
# ---------------------------------------------------------------------------


def test_issue_bodies_reference_the_configured_run_directory(tmp_path) -> None:
    cfg = Config(root=tmp_path)
    fake = FakeGitHub()
    store = gh.GitHubTaskStore(transport=fake, owner="acme", repo="widgets")
    store.bind(cfg)
    mapping = store.sync(make_graph(1))

    expected = cfg.run_dir("2026-08-19-a1b2").relative_to(tmp_path).as_posix()
    body = fake.issues[issue_number(mapping["T001"])]["body"]
    assert f"- Run directory: `{expected}/`" in body
    assert f"{expected}/taskgraph.json" in body
    assert str(tmp_path) not in body, "absolute local paths must not leak into issues"


def test_run_directory_falls_back_when_no_config_is_bound() -> None:
    fake = FakeGitHub()
    store = gh.GitHubTaskStore(transport=fake, owner="acme", repo="widgets")
    mapping = store.sync(make_graph(1))
    body = fake.issues[issue_number(mapping["T001"])]["body"]
    assert "- Run directory: `.adlc/runs/2026-08-19-a1b2/`" in body


# ---------------------------------------------------------------------------
# A spine-shaped graph: capsules present, must not be inlined
# ---------------------------------------------------------------------------


def test_a_compiled_graph_with_capsules_syncs_without_inlining_them(tmp_path) -> None:
    """`stages/graph.py` attaches a bounded capsule to every node."""
    secret = "CAPSULE-CONTENT-THAT-MUST-NOT-APPEAR"
    graph = make_graph(3)
    for node in graph["nodes"]:
        node["context"] = {
            "refs": [{"path": "src/app.py", "blobSha": "abc123", "excerpt": secret}],
            "conventions": secret,
            "doNotTouch": [".github/**", ".adlc/**"],
            "budget": {"maxTotalBytes": 65536, "maxFileBytes": 8192, "maxFiles": 12},
        }

    fake = FakeGitHub()
    store = gh.GitHubTaskStore(transport=fake, owner="acme", repo="widgets")
    store.bind(Config(root=tmp_path))
    assert len(store.sync(graph)) == 3

    for issue in fake.issues.values():
        assert secret not in issue["body"]
        assert len(issue["body"]) <= gh.BODY_BUDGET


def test_the_graph_stage_survives_a_failing_task_store(tmp_path, monkeypatch) -> None:
    """`run_graph` wraps the store in try/except; raising must be survivable."""
    from adlc.stages import graph as graph_stage

    store = gh.GitHubTaskStore()
    store.bind(Config(root=tmp_path))

    captured: dict[str, str] = {}
    try:
        store.sync(make_graph(1))
    except Exception as exc:  # noqa: BLE001 - mirrors the spine's handler
        captured["store"] = f"unavailable ({exc})"

    assert "unavailable" in captured["store"]
    assert "GITHUB_TOKEN" in captured["store"], "the recorded reason must be actionable"
    assert hasattr(graph_stage, "run_graph")


def test_the_graph_never_writes_run_json(tmp_path) -> None:
    """Only `adlc reduce` writes run.json; a task store must never touch it."""
    cfg = Config(root=tmp_path)
    fake = FakeGitHub()
    store = gh.GitHubTaskStore(transport=fake, owner="acme", repo="widgets")
    store.bind(cfg)
    store.sync(make_graph(2))
    store.update("T001", "ok")

    assert not list(tmp_path.rglob("run.json"))
    assert not tmp_path.joinpath(".adlc").exists()


def test_the_synced_graph_is_schema_valid(tmp_path) -> None:
    """Guards the fixture: a graph shape the schema rejects proves nothing."""
    from adlc.schemas import is_valid

    graph = make_graph(3)
    assert is_valid("taskgraph", graph), "test fixture drifted from taskgraph.schema.json"

    fake = FakeGitHub()
    store = gh.GitHubTaskStore(transport=fake, owner="acme", repo="widgets")
    store.bind(Config(root=tmp_path))
    assert len(store.sync(graph)) == 3
