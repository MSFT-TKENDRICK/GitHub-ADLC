"""`sync()` and `update()` against an in-memory GitHub. No credentials, no network.

The two properties that matter most are exercised here:

* **id vs number** -- the sub-issues and dependencies endpoints take issue *ids*.
* **idempotency** -- ``sync()`` runs on every stage; a second call must not
  create, patch, or comment on anything.
"""

from __future__ import annotations

import pytest

from adlc.adapters.taskstore import github as gh

from .conftest import FakeGitHub, issue_id_for, issue_number, make_graph


def store(fake: FakeGitHub, cfg=None, **settings) -> gh.GitHubTaskStore:
    return gh.GitHubTaskStore(
        cfg, transport=fake, owner="acme", repo="widgets", settings=settings
    )


# ---------------------------------------------------------------------------
# Shape of a first sync
# ---------------------------------------------------------------------------


def test_sync_creates_one_parent_and_one_sub_issue_per_node(fake_github: FakeGitHub) -> None:
    graph = make_graph(3)
    mapping = store(fake_github).sync(graph)

    assert set(mapping) == {"T001", "T002", "T003"}
    assert len(fake_github.issues) == 4  # 1 parent + 3 tasks
    parent_number = next(iter(fake_github.sub_issues))
    assert len(fake_github.sub_issues[parent_number]) == 3


def test_sync_returns_self_describing_external_ids(fake_github: FakeGitHub) -> None:
    """Matches the spine SQLite store's `sqlite:<run>/<node>` convention."""
    mapping = store(fake_github).sync(make_graph(2))
    for node_id, external in mapping.items():
        assert external.startswith("acme/widgets#")
        issue = fake_github.issues[issue_number(external)]
        assert gh.parse_marker(issue["body"]) == ("2026-08-19-a1b2", "node", node_id)


def test_sub_issues_are_linked_by_id_not_number(fake_github: FakeGitHub) -> None:
    """The classic footgun: `sub_issue_id` is the database id, not the number."""
    st = store(fake_github)
    st.sync(make_graph(3))

    posts = [c for c in fake_github.calls if c[0] == "POST" and c[1].endswith("/sub_issues")]
    assert len(posts) == 3
    for _, _, body in posts:
        assert set(body) == {"sub_issue_id"}
        matched = fake_github.by_id(body["sub_issue_id"])
        assert matched is not None, "sub_issue_id did not resolve to an issue id"
        assert body["sub_issue_id"] != matched["number"]
        assert body["sub_issue_id"] == issue_id_for(matched["number"])


def test_issues_are_labelled_so_the_run_can_be_re_indexed(fake_github: FakeGitHub) -> None:
    store(fake_github).sync(make_graph(2))
    label = gh.run_label("2026-08-19-a1b2")
    assert all(label in issue["labels"] for issue in fake_github.issues.values())

    task_labels = next(
        issue["labels"] for issue in fake_github.issues.values() if "[T001]" in issue["title"]
    )
    assert "adlc-kind:implement" in task_labels
    assert "adlc-level:0" in task_labels


def test_cross_references_are_resolved_to_real_issue_numbers(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(3))
    body = fake_github.issues[issue_number(mapping["T002"])]["body"]

    assert f"Blocked by: #{issue_number(mapping['T001'])} (`T001`)" in body
    assert f"Blocks: #{issue_number(mapping['T003'])} (`T003`)" in body
    assert "_pending_" not in body
    assert "not yet synced" not in body


# ---------------------------------------------------------------------------
# Idempotency -- the property that makes sync() safe to call every stage
# ---------------------------------------------------------------------------


def test_second_sync_performs_no_mutations_at_all(fake_github: FakeGitHub) -> None:
    graph = make_graph(4)
    store(fake_github).sync(graph)
    before = dict(fake_github.issues)

    fake_github.calls.clear()
    mapping = store(fake_github).sync(graph)

    assert fake_github.mutations == []
    assert fake_github.issues == before
    assert mapping == {
        "T001": "acme/widgets#2", "T002": "acme/widgets#3",
        "T003": "acme/widgets#4", "T004": "acme/widgets#5",
    }


def test_resync_reuses_issues_even_with_a_fresh_store_instance(fake_github: FakeGitHub) -> None:
    graph = make_graph(3)
    first = store(fake_github).sync(graph)
    second = store(fake_github).sync(graph)
    assert first == second
    assert len(fake_github.issues) == 4


def test_unrelated_issues_are_never_adopted(fake_github: FakeGitHub) -> None:
    fake_github.request("POST", "/repos/acme/widgets/issues", {"title": "a real bug", "body": "hi"})
    other = make_graph(1, run_id="some-other-run")
    store(fake_github).sync(other)
    fake_github.calls.clear()

    store(fake_github).sync(make_graph(2))
    creations = [c for c in fake_github.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert len(creations) == 3  # 1 parent + 2 tasks, nothing adopted


def test_a_grown_graph_only_creates_the_new_nodes(fake_github: FakeGitHub) -> None:
    store(fake_github).sync(make_graph(2))
    fake_github.calls.clear()

    store(fake_github).sync(make_graph(3))
    creations = [c for c in fake_github.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert len(creations) == 1
    assert "[T003]" in creations[0][2]["title"]


# ---------------------------------------------------------------------------
# Dependency projection
# ---------------------------------------------------------------------------


def test_dependencies_post_the_blocker_id_to_the_blocked_issue(fake_github: FakeGitHub) -> None:
    mapping = store(fake_github).sync(make_graph(3))

    posts = [
        c for c in fake_github.calls
        if c[0] == "POST" and c[1].endswith("/dependencies/blocked_by")
    ]
    assert len(posts) == 2
    blocked = issue_number(mapping["T002"])
    blocked_path = f"/repos/acme/widgets/issues/{blocked}/dependencies/blocked_by"
    t002 = next(c for c in posts if c[1] == blocked_path)
    assert set(t002[2]) == {"issue_id"}
    assert t002[2]["issue_id"] == issue_id_for(issue_number(mapping["T001"]))
    assert fake_github.blocked_by[blocked] == [issue_id_for(issue_number(mapping["T001"]))]


def test_dependency_sync_can_be_switched_off(fake_github: FakeGitHub) -> None:
    store(fake_github, syncDependencies=False).sync(make_graph(3))
    assert fake_github.blocked_by == {}


def test_dependency_api_failure_degrades_to_a_warning(fake_github: FakeGitHub) -> None:
    """taskgraph.json is authoritative, so a missing API must not fail the sync."""
    fake_github.dependency_status = 403
    st = store(fake_github)
    mapping = st.sync(make_graph(3))

    assert len(mapping) == 3
    assert any("blocked by" in w for w in st.warnings)
    body = fake_github.issues[issue_number(mapping["T002"])]["body"]
    # the edge survives in the body even when the API refuses
    assert f"Blocked by: #{issue_number(mapping['T001'])}" in body


def test_dependency_edges_are_not_re_posted(fake_github: FakeGitHub) -> None:
    graph = make_graph(3)
    store(fake_github).sync(graph)
    fake_github.calls.clear()
    store(fake_github).sync(graph)
    assert not [c for c in fake_github.mutations if "dependencies" in c[1]]


# ---------------------------------------------------------------------------
# The 100-sub-issue limit
# ---------------------------------------------------------------------------


def test_large_graphs_are_chunked_under_a_root_issue(fake_github: FakeGitHub) -> None:
    st = store(fake_github, maxSubIssues=4)
    mapping = st.sync(make_graph(10))

    assert len(mapping) == 10
    assert len(st.parents) == 3          # 4 + 4 + 2
    assert st.root is not None
    for parent in st.parents:
        assert len(fake_github.sub_issues[parent["number"]]) <= 4
    assert sorted(fake_github.sub_issues[st.root["number"]]) == sorted(
        p["id"] for p in st.parents
    )


def test_chunked_sync_is_also_idempotent(fake_github: FakeGitHub) -> None:
    graph = make_graph(10)
    store(fake_github, maxSubIssues=4).sync(graph)
    fake_github.calls.clear()
    store(fake_github, maxSubIssues=4).sync(graph)
    assert fake_github.mutations == []


def test_a_graph_too_large_to_chunk_fails_with_a_clear_message(fake_github: FakeGitHub) -> None:
    with pytest.raises(gh.GitHubTaskStoreError, match="exceeds the 2 sub-issues-per-parent"):
        store(fake_github, maxSubIssues=2).sync(make_graph(9))


def test_reparenting_moves_a_node_between_chunks(fake_github: FakeGitHub) -> None:
    """Re-chunking must re-home task issues, not leave them under a stale parent."""
    graph = make_graph(6)
    store(fake_github, maxSubIssues=3).sync(graph)
    assert len(set(fake_github.parent_of.values())) == 3  # 2 parts + root

    st = store(fake_github, maxSubIssues=100)
    mapping = st.sync(graph)

    retries = [
        c for c in fake_github.calls
        if c[0] == "POST" and c[1].endswith("/sub_issues") and c[2].get("replace_parent")
    ]
    assert retries, "expected a replace_parent retry when re-chunking"
    assert st.warnings == []
    # Every task now hangs off the single remaining parent.
    parent = st.parents[0]["number"]
    assert {
        fake_github.parent_of[issue_id_for(issue_number(n))] for n in mapping.values()
    } == {parent}
    assert len(fake_github.sub_issues[parent]) == 6


def test_an_unattachable_sub_issue_warns_but_does_not_fail(fake_github: FakeGitHub) -> None:
    graph = make_graph(2)
    st = store(fake_github)

    real_request = fake_github.request

    def refuse(method, path, body=None):
        if method.upper() == "POST" and path.endswith("/sub_issues"):
            fake_github.calls.append(("POST", path, body))
            return 422, {"message": "nope"}
        return real_request(method, path, body)

    fake_github.request = refuse  # type: ignore[method-assign]
    mapping = st.sync(graph)

    assert len(mapping) == 2
    assert len(st.warnings) == 2
    assert "could not attach" in st.warnings[0]


# ---------------------------------------------------------------------------
# Replan: nodes that disappear from the graph
# ---------------------------------------------------------------------------


def test_a_removed_node_is_detached_and_closed(fake_github: FakeGitHub) -> None:
    """Otherwise the parent rollup counts it forever and a run never hits 100%."""
    st = store(fake_github)
    mapping = st.sync(make_graph(3))
    removed = issue_number(mapping["T003"])

    st2 = store(fake_github)
    st2.sync(make_graph(2))

    assert fake_github.issues[removed]["state"] == "closed"
    assert fake_github.issues[removed]["state_reason"] == "not_planned"
    assert issue_id_for(removed) not in fake_github.parent_of
    assert any("no longer in the task graph" in w for w in st2.warnings)


def test_progress_reaches_100_percent_after_a_replan(fake_github: FakeGitHub) -> None:
    store(fake_github).sync(make_graph(3))
    st = store(fake_github)
    st.sync(make_graph(2))
    st.update("T001", "ok")
    st.update("T002", "ok")

    assert st.progress()["subIssuesSummary"] == {
        "total": 2, "completed": 2, "percent_completed": 100,
    }


def test_retiring_a_node_is_idempotent(fake_github: FakeGitHub) -> None:
    store(fake_github).sync(make_graph(3))
    store(fake_github).sync(make_graph(2))
    fake_github.calls.clear()

    st = store(fake_github)
    st.sync(make_graph(2))
    assert fake_github.mutations == []
    assert st.warnings == []


# ---------------------------------------------------------------------------
# Labels track the graph rather than accumulating
# ---------------------------------------------------------------------------


def test_kind_and_level_labels_are_replaced_not_accumulated(fake_github: FakeGitHub) -> None:
    graph = make_graph(1)
    store(fake_github).sync(graph)

    graph["nodes"][0].update(kind="doc", level=5)
    mapping = store(fake_github).sync(graph)

    labels = fake_github.issues[issue_number(mapping["T001"])]["labels"]
    assert "adlc-kind:doc" in labels
    assert "adlc-kind:implement" not in labels
    assert "adlc-level:5" in labels
    assert "adlc-level:0" not in labels


def test_human_and_status_labels_survive_a_resync(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(1))
    number = issue_number(mapping["T001"])
    st.update("T001", "fail")
    fake_github.issues[number]["labels"].append("needs-triage")

    store(fake_github).sync(make_graph(1))
    labels = fake_github.issues[number]["labels"]
    assert "needs-triage" in labels
    assert "adlc-status:fail" in labels



# ---------------------------------------------------------------------------
# Surfacing sub_issues_summary
# ---------------------------------------------------------------------------


def test_progress_surfaces_the_native_sub_issues_summary(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    st.sync(make_graph(4))

    assert st.progress()["subIssuesSummary"] == {
        "total": 4, "completed": 0, "percent_completed": 0,
    }
    st.update("T001", "ok")
    st.update("T002", "ok")
    assert st.progress()["subIssuesSummary"] == {
        "total": 4, "completed": 2, "percent_completed": 50,
    }


def test_progress_does_not_double_count_the_root(fake_github: FakeGitHub) -> None:
    st = store(fake_github, maxSubIssues=3)
    st.sync(make_graph(7))
    assert st.progress()["subIssuesSummary"]["total"] == 7
    assert st.progress()["root"] is not None


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


def test_update_comments_labels_and_closes(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(2))
    number = issue_number(mapping["T001"])

    st.update("T001", "ok", note="patch applied cleanly")

    issue = fake_github.issues[number]
    assert issue["state"] == "closed"
    assert issue["state_reason"] == "completed"
    assert "adlc-status:ok" in issue["labels"]
    assert gh.run_label("2026-08-19-a1b2") in issue["labels"]
    assert "patch applied cleanly" in fake_github.comments[number][0]["body"]


@pytest.mark.parametrize(
    ("status", "state", "reason"),
    [
        ("ok", "closed", "completed"),
        ("skipped", "closed", "not_planned"),
        ("fail", "open", None),
        ("in_progress", "open", None),
    ],
)
def test_update_maps_task_outcomes_to_issue_state(
    fake_github: FakeGitHub, status, state, reason
) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(1))
    st.update("T001", status)

    issue = fake_github.issues[issue_number(mapping["T001"])]
    assert issue["state"] == state
    assert issue["state_reason"] == reason


def test_repeating_an_update_does_not_duplicate_the_comment(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(1))
    number = issue_number(mapping["T001"])

    st.update("T001", "fail", note="tests red")
    st.update("T001", "fail", note="tests red")
    assert len(fake_github.comments[number]) == 1

    st.update("T001", "fail", note="tests red again")
    assert len(fake_github.comments[number]) == 2


def test_a_genuine_repeat_transition_is_still_recorded(fake_github: FakeGitHub) -> None:
    """Retry history is evidence; only an immediate replay is deduplicated."""
    st = store(fake_github)
    mapping = st.sync(make_graph(1))
    number = issue_number(mapping["T001"])

    for status in ("running", "fail", "running", "fail", "ok"):
        st.update("T001", status)

    recorded = [c["body"].splitlines()[1] for c in fake_github.comments[number]]
    assert recorded == [
        "**Status → `running`**", "**Status → `fail`**",
        "**Status → `running`**", "**Status → `fail`**", "**Status → `ok`**",
    ]


def test_updates_for_sibling_nodes_do_not_deduplicate_each_other(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(2))
    st.update("T001", "ok")
    st.update("T002", "ok")

    assert len(fake_github.comments[issue_number(mapping["T001"])]) == 1
    assert len(fake_github.comments[issue_number(mapping["T002"])]) == 1


def test_update_replaces_rather_than_accumulates_status_labels(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    mapping = st.sync(make_graph(1))
    st.update("T001", "in_progress")
    st.update("T001", "ok")

    labels = fake_github.issues[issue_number(mapping["T001"])]["labels"]
    assert [lbl for lbl in labels if lbl.startswith("adlc-status:")] == ["adlc-status:ok"]


def test_update_rejects_an_empty_status(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    st.sync(make_graph(1))
    with pytest.raises(gh.GitHubTaskStoreError, match="non-empty status"):
        st.update("T001", "")


def test_update_before_sync_explains_what_is_missing(fake_github: FakeGitHub) -> None:
    with pytest.raises(gh.GitHubTaskStoreError, match="ADLC_RUN_ID"):
        store(fake_github).update("T001", "ok")


def test_update_relocates_an_issue_using_adlc_run_id(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later stage in a fresh process can still report progress."""
    mapping = store(fake_github).sync(make_graph(2))
    monkeypatch.setenv("ADLC_RUN_ID", "2026-08-19-a1b2")

    st = store(fake_github)
    st.update("T002", "ok")
    assert fake_github.issues[issue_number(mapping["T002"])]["state"] == "closed"


def test_relocating_indexes_the_whole_run_exactly_once(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N updates in a fresh process must not mean N full issue listings."""
    store(fake_github).sync(make_graph(8))
    monkeypatch.setenv("ADLC_RUN_ID", "2026-08-19-a1b2")

    st = store(fake_github)
    fake_github.calls.clear()
    for i in range(1, 9):
        st.update(f"T{i:03d}", "ok")

    listings = [c for c in fake_github.calls if c[1].startswith("/repos/acme/widgets/issues?")]
    assert len(listings) == 1


def test_sync_ignores_pull_requests_sharing_the_run_label(fake_github: FakeGitHub) -> None:
    """The issues list endpoint returns PRs too; they must never be adopted."""
    graph = make_graph(2)
    store(fake_github).sync(graph)
    fake_github.add_pull_request(labels=[gh.run_label(graph["runId"]), "adlc"])
    fake_github.calls.clear()

    st = store(fake_github)
    assert len(st.sync(graph)) == 2
    assert fake_github.mutations == []
    assert st.warnings == []


def test_update_of_an_unknown_node_fails_loudly(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store(fake_github).sync(make_graph(1))
    monkeypatch.setenv("ADLC_RUN_ID", "2026-08-19-a1b2")
    with pytest.raises(gh.GitHubTaskStoreError, match="no GitHub issue found for node 'T404'"):
        store(fake_github).update("T404", "ok")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_sync_requires_a_run_id(fake_github: FakeGitHub) -> None:
    with pytest.raises(gh.GitHubTaskStoreError, match="missing 'runId'"):
        store(fake_github).sync({"nodes": []})


def test_sync_rejects_a_run_id_that_breaks_the_marker(fake_github: FakeGitHub) -> None:
    """Whitespace would make every marker unparseable and duplicate the whole run."""
    with pytest.raises(gh.GitHubTaskStoreError, match="cannot be encoded in an issue marker"):
        store(fake_github).sync(make_graph(1, run_id="nightly run 7"))
    assert fake_github.issues == {}


def test_sync_rejects_a_run_id_containing_a_comment_terminator(fake_github: FakeGitHub) -> None:
    with pytest.raises(gh.GitHubTaskStoreError, match="cannot be encoded in an issue marker"):
        store(fake_github).sync(make_graph(1, run_id="r-->x"))


def test_sync_rejects_duplicate_node_ids(fake_github: FakeGitHub) -> None:
    graph = make_graph(2)
    graph["nodes"][1]["id"] = "T001"
    with pytest.raises(gh.GitHubTaskStoreError, match="duplicate node id 'T001'"):
        store(fake_github).sync(graph)


def test_sync_rejects_a_node_without_an_id(fake_github: FakeGitHub) -> None:
    graph = make_graph(1)
    del graph["nodes"][0]["id"]
    with pytest.raises(gh.GitHubTaskStoreError, match="without an 'id'"):
        store(fake_github).sync(graph)


def test_an_empty_graph_still_creates_the_run_parent(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    assert st.sync({"runId": "r-empty", "baseSha": "x", "nodes": []}) == {}
    assert len(st.parents) == 1


def test_an_unresolved_repository_fails_with_guidance(cfg) -> None:
    with pytest.raises(gh.GitHubTaskStoreError, match="GITHUB_REPOSITORY"):
        gh.GitHubTaskStore(cfg, transport=FakeGitHub()).sync(make_graph(1))


# ---------------------------------------------------------------------------
# Projects v2 -- optional, must never be load-bearing
# ---------------------------------------------------------------------------


def test_projects_v2_is_off_by_default(fake_github: FakeGitHub) -> None:
    st = store(fake_github)
    st.sync(make_graph(2))
    assert fake_github.graphql_calls == []
    assert st.warnings == []


def test_projects_v2_adds_items_and_sets_fields_when_enabled(fake_github: FakeGitHub) -> None:
    st = store(
        fake_github,
        enableProjects=True,
        projectId="PVT_1",
        projectFields={
            "level": "PVTF_level",
            "kind": "PVTSSF_kind",
            "kindOptions": {"implement": "opt_impl", "test": "opt_test"},
        },
    )
    st.sync(make_graph(2))

    adds = [v for q, v in fake_github.graphql_calls if "addProjectV2ItemById" in q]
    assert len(adds) == 2
    assert all(v["project"] == "PVT_1" and v["content"].startswith("I_kw") for v in adds)

    fields = [v for q, v in fake_github.graphql_calls if "updateProjectV2ItemFieldValue" in q]
    assert {"number": 0.0} in [v["value"] for v in fields]
    assert {"singleSelectOptionId": "opt_impl"} in [v["value"] for v in fields]


def test_projects_v2_without_a_project_id_only_warns(fake_github: FakeGitHub) -> None:
    st = store(fake_github, enableProjects=True)
    assert len(st.sync(make_graph(2))) == 2
    assert any("projectId" in w for w in st.warnings)


def test_a_projects_v2_failure_never_fails_the_sync(fake_github: FakeGitHub) -> None:
    def boom(query, variables):
        raise RuntimeError("project archived")

    fake_github.graphql = boom  # type: ignore[method-assign]
    st = store(fake_github, enableProjects=True, projectId="PVT_1")

    assert len(st.sync(make_graph(2))) == 2
    assert all("project archived" in w for w in st.warnings)
    assert len(st.warnings) == 2
