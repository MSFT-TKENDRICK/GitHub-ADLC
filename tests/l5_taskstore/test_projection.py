"""The graph -> issue projection. Pure functions, no client, no credentials."""

from __future__ import annotations

import pytest

from adlc.adapters.taskstore import github as gh

from .conftest import make_graph


def test_markers_are_stable_and_parseable() -> None:
    body = f"{gh.node_marker('r1', 'T003')}\nsome text"
    assert gh.parse_marker(body) == ("r1", "node", "T003")
    assert gh.parse_marker(gh.parent_marker("r1", 2)) == ("r1", "parent", "2")
    assert gh.parse_marker(gh.root_marker("r1")) == ("r1", "root", "1")


def test_parse_marker_ignores_foreign_bodies() -> None:
    assert gh.parse_marker(None) is None
    assert gh.parse_marker("") is None
    assert gh.parse_marker("a normal issue someone filed") is None
    assert gh.parse_marker("<!-- something:else -->") is None


def test_run_label_is_clamped_to_the_api_limit() -> None:
    assert gh.run_label("2026-08-19-a1b2") == "adlc-run:2026-08-19-a1b2"
    long_label = gh.run_label("x" * 200)
    assert len(long_label) == gh.LABEL_MAX
    assert long_label.startswith(gh.RUN_LABEL_PREFIX)


def test_node_body_carries_what_an_agent_needs_to_act() -> None:
    graph = make_graph(3)
    node = graph["nodes"][1]
    body = gh.render_node_body(graph, node, {"T001": 11, "T002": 12, "T003": 13})

    assert body.startswith(gh.node_marker(graph["runId"], "T002"))
    assert "Task number 2" in body
    assert "`test`" in body                      # kind
    assert "level `1`" in body                   # level
    assert "src/mod2.py" in body                 # writeSet
    assert "US1-AC2" in body                     # acceptance criteria id
    assert ".adlc/runs/2026-08-19-a1b2/" in body  # link back to the run
    assert "patches/T002.patch" in body


def test_node_body_renders_dependency_edges_and_a_mermaid_neighbourhood() -> None:
    graph = make_graph(3)
    body = gh.render_node_body(graph, graph["nodes"][1], {"T001": 11, "T002": 12, "T003": 13})

    assert "Blocked by: #11 (`T001`)" in body
    assert "Blocks: #13 (`T003`)" in body
    assert "```mermaid" in body
    assert 'T001["T001 #11"] --> T002["T002 #12"]' in body
    assert 'T002["T002 #12"] --> T003["T003 #13"]' in body
    assert "`taskgraph.json` is the authoritative source for dependencies" in body


def test_node_body_tolerates_unresolved_dependencies() -> None:
    graph = make_graph(2)
    body = gh.render_node_body(graph, graph["nodes"][1], {})
    assert "`T001` (not yet synced)" in body
    assert "Blocks: _none_" in body


def test_isolated_node_still_renders_a_valid_mermaid_block() -> None:
    graph = {"runId": "r1", "baseSha": "abc", "nodes": [
        {"id": "T001", "title": "solo", "kind": "doc", "dependsOn": [], "level": 0,
         "writeSet": ["docs/x.md"]},
    ]}
    body = gh.render_node_body(graph, graph["nodes"][0], {"T001": 5})
    assert "```mermaid\nflowchart LR\n    T001[\"T001 #5\"]\n```" in body


def test_context_capsule_is_referenced_not_inlined() -> None:
    """Capsules are bounded on purpose; issue bodies are not a capsule sink."""
    secret_excerpt = "SUPER-SPECIFIC-CAPSULE-CONTENT"
    graph = make_graph(1)
    graph["nodes"][0]["context"] = {
        "refs": [{"path": "src/app.ts", "blobSha": "deadbeef", "excerpt": secret_excerpt}],
        "interfaces": secret_excerpt,
        "conventions": secret_excerpt,
    }
    body = gh.render_node_body(graph, graph["nodes"][0], {"T001": 1})

    assert secret_excerpt not in body
    assert "not inlined" in body
    assert "taskgraph.json" in body


def test_bodies_stay_within_the_github_issue_limit() -> None:
    graph = make_graph(1)
    graph["nodes"][0]["writeSet"] = [f"src/generated/file_{i:05d}.py" for i in range(9000)]
    body = gh.render_node_body(graph, graph["nodes"][0], {"T001": 1})

    assert len(body) <= gh.BODY_BUDGET
    assert body.endswith("_…truncated to fit the GitHub issue body limit._")


def test_parent_body_lists_every_task_in_its_chunk() -> None:
    graph = make_graph(3)
    body = gh.render_parent_body(graph, 1, 1, graph["nodes"], {"T001": 1, "T002": 2, "T003": 3})

    assert body.startswith(gh.parent_marker(graph["runId"], 1))
    assert "sha256:deadbeef" in body
    for node_id, number in (("T001", 1), ("T002", 2), ("T003", 3)):
        assert f"`{node_id}`" in body
        assert f"#{number}" in body


def test_parent_body_marks_unsynced_tasks_as_pending() -> None:
    graph = make_graph(2)
    body = gh.render_parent_body(graph, 1, 1, graph["nodes"], {})
    assert body.count("_pending_") == 2


def test_root_body_links_every_part() -> None:
    graph = make_graph(2)
    body = gh.render_root_body(graph, [1, 2], {"__part1": 7, "__part2": 8})
    assert body.startswith(gh.root_marker(graph["runId"]))
    assert "- Part 1: #7" in body
    assert "- Part 2: #8" in body


# ---------------------------------------------------------------------------
# Chunking and ordering
# ---------------------------------------------------------------------------


def test_sub_issue_limit_matches_the_documented_github_limit() -> None:
    assert gh.SUB_ISSUE_LIMIT == 100
    assert gh.NESTING_DEPTH_LIMIT == 8


@pytest.mark.parametrize(
    ("count", "limit", "expected"),
    [(0, 100, [0]), (1, 100, [1]), (100, 100, [100]), (101, 100, [100, 1]), (5, 2, [2, 2, 1])],
)
def test_plan_chunks_never_exceeds_the_limit(count, limit, expected) -> None:
    nodes = [{"id": f"T{i:03d}"} for i in range(count)]
    chunks = gh.plan_chunks(nodes, limit)
    assert [len(c) for c in chunks] == expected
    assert sum(len(c) for c in chunks) == count
    assert all(len(c) <= limit for c in chunks)


def test_plan_chunks_rejects_a_nonsense_limit() -> None:
    with pytest.raises(gh.GitHubTaskStoreError, match="must be >= 1"):
        gh.plan_chunks([{"id": "T001"}], 0)


def test_order_nodes_is_deterministic_by_level_then_id() -> None:
    nodes = [
        {"id": "T009", "level": 2}, {"id": "T002", "level": 0},
        {"id": "T001", "level": 1}, {"id": "T003", "level": 0},
    ]
    assert [n["id"] for n in gh.order_nodes(nodes)] == ["T002", "T003", "T001", "T009"]
    assert [n["id"] for n in gh.order_nodes(reversed(nodes))] == ["T002", "T003", "T001", "T009"]


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------


def test_next_link_parses_the_github_link_header() -> None:
    header = (
        '<https://api.github.com/repos/a/b/issues?page=2>; rel="next", '
        '<https://api.github.com/repos/a/b/issues?page=9>; rel="last"'
    )
    assert gh._next_link(header) == "https://api.github.com/repos/a/b/issues?page=2"
    assert gh._next_link('<https://x>; rel="last"') is None
    assert gh._next_link("") is None


def test_error_messages_are_flattened_for_humans() -> None:
    assert "Validation Failed" in gh._message(
        {"message": "Validation Failed", "errors": [{"message": "already exists"}]}
    )
    assert "already exists" in gh._message(
        {"message": "Validation Failed", "errors": [{"message": "already exists"}]}
    )
