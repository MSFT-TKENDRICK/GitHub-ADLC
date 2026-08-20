"""Negative conformance tests -- the guarantees that matter most.

A framework like this is only trustworthy if its *refusals* work. These tests
prove the failure paths, not the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adlc.config import Config
from adlc.executor import GraphError, validate_graph, violated_write_set
from adlc.ports import TaskGraph
from adlc.reduce import aggregate_passed, collect_gates, reduce_run
from adlc.runs import RunDir, new_run_id, write_json
from adlc.stages.graph import compile_graph, parse_tasks_md


# -- criterion 6: fail closed ----------------------------------------------

def test_required_gate_not_run_fails_the_aggregate() -> None:
    """C6: 'we could not check' must never read as 'it is fine'."""
    gates = [
        {"id": "tests", "required": True, "status": "pass"},
        {"id": "secrets_local", "required": True, "status": "not_run",
         "message": "scanner unavailable"},
    ]
    passed, failures = aggregate_passed(gates)
    assert passed is False
    assert any("NOT_RUN" in f for f in failures)


def test_optional_gate_not_run_does_not_fail_the_aggregate() -> None:
    """Optional gates degrade quietly; only required ones block."""
    gates = [
        {"id": "tests", "required": True, "status": "pass"},
        {"id": "code_quality", "required": False, "status": "not_run"},
    ]
    passed, failures = aggregate_passed(gates)
    assert passed is True
    assert failures == []


def test_missing_required_gate_is_synthesised_as_not_run(cfg: Config) -> None:
    """Silence must not be mistaken for success."""
    rd = RunDir(cfg, new_run_id())
    rd.create(profile="minimal", brief_text="# Empty\n")
    gates = collect_gates(rd, cfg)

    ids = {g["id"] for g in gates}
    assert set(cfg.required_gates()) <= ids
    assert all(g["status"] == "not_run" for g in gates)
    passed, _ = aggregate_passed(gates)
    assert passed is False


# -- criterion 3: write-set conflicts are a compile-time error --------------

def test_overlapping_write_sets_at_same_level_are_rejected() -> None:
    """C3: detected before any agent runs, not discovered at merge time."""
    graph: TaskGraph = {
        "runId": "r", "baseSha": "0" * 40,
        "nodes": [
            {"id": "T001", "title": "a", "kind": "implement", "dependsOn": [],
             "level": 0, "writeSet": ["src/theme.ts"]},
            {"id": "T002", "title": "b", "kind": "implement", "dependsOn": [],
             "level": 0, "writeSet": ["src/theme.ts"]},
        ],
    }
    with pytest.raises(GraphError, match="write-set conflict"):
        validate_graph(graph)


def test_same_paths_on_different_levels_are_allowed() -> None:
    """Sequencing resolves a conflict; only same-level overlap is an error."""
    graph: TaskGraph = {
        "runId": "r", "baseSha": "0" * 40,
        "nodes": [
            {"id": "T001", "title": "a", "kind": "implement", "dependsOn": [],
             "level": 0, "writeSet": ["src/theme.ts"]},
            {"id": "T002", "title": "b", "kind": "implement", "dependsOn": ["T001"],
             "level": 1, "writeSet": ["src/theme.ts"]},
        ],
    }
    levels = validate_graph(graph)
    assert levels["T001"] == 0
    assert levels["T002"] == 1


def test_cycles_are_rejected() -> None:
    graph: TaskGraph = {
        "runId": "r", "baseSha": "0" * 40,
        "nodes": [
            {"id": "T001", "title": "a", "kind": "implement", "dependsOn": ["T002"],
             "level": 0, "writeSet": ["a.txt"]},
            {"id": "T002", "title": "b", "kind": "implement", "dependsOn": ["T001"],
             "level": 0, "writeSet": ["b.txt"]},
        ],
    }
    with pytest.raises(GraphError, match="cycle"):
        validate_graph(graph)


def test_protected_paths_cannot_be_declared() -> None:
    """An agent must never be able to rewrite CI, schemas, config or ADRs."""
    graph: TaskGraph = {
        "runId": "r", "baseSha": "0" * 40,
        "nodes": [
            {"id": "T001", "title": "sneaky", "kind": "implement", "dependsOn": [],
             "level": 0, "writeSet": [".github/workflows/adlc.yml"]},
        ],
    }
    with pytest.raises(GraphError, match="protected path"):
        validate_graph(graph)


def test_patch_touching_undeclared_path_is_detected() -> None:
    """The executor rejects a patch that strays outside its declared write-set."""
    patch = (
        "diff --git a/src/allowed.py b/src/allowed.py\n"
        "--- a/src/allowed.py\n+++ b/src/allowed.py\n"
        "diff --git a/src/sneaky.py b/src/sneaky.py\n"
        "--- /dev/null\n+++ b/src/sneaky.py\n"
    )
    violations = violated_write_set(patch, ["src/allowed.py"])
    assert violations == ["src/sneaky.py"]


# -- criterion 4: stale capsules --------------------------------------------

def test_stale_blob_sha_fails_the_node(cfg: Config) -> None:
    """C4: an agent must never edit against content that has since changed."""
    from adlc.executor import verify_capsule

    target = cfg.root / "src" / "app.py"
    node = {
        "id": "T001", "writeSet": ["src/other.py"],
        "context": {"refs": [{"path": "src/app.py", "blobSha": "0" * 40}]},
    }
    stale = verify_capsule(node, cfg.root)
    assert stale and "src/app.py" in stale[0]

    # And the honest case: a matching SHA is not stale.
    from adlc.runs import git

    node["context"]["refs"][0]["blobSha"] = git("hash-object", str(target), cwd=cfg.root)
    assert verify_capsule(node, cfg.root) == []


# -- capsule budgets --------------------------------------------------------

def test_context_capsules_respect_their_budget(cfg: Config, brief_file: Path) -> None:
    """Unbounded inlining is a scalability and secrecy bug; budgets are enforced."""
    from adlc.ports import CAPSULE_MAX_FILE_BYTES, CAPSULE_MAX_FILES, CAPSULE_MAX_TOTAL_BYTES
    from adlc.stages.enrich import run_enrich
    from adlc.stages.intake import run_intake
    from adlc.stages.spec import run_spec

    # A file far larger than the per-file cap must not be inlined whole.
    (cfg.root / "src" / "huge.py").write_text("# pad\n" * 20_000, encoding="utf-8")

    rd = RunDir(cfg, new_run_id())
    rd.create(profile="minimal", brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, "test")
    run_spec(cfg, rd)
    run_enrich(cfg, rd)
    graph = compile_graph(cfg, rd)

    for node in graph["nodes"]:
        capsule = node["context"]
        assert len(capsule["refs"]) <= CAPSULE_MAX_FILES
        total = 0
        for ref in capsule["refs"]:
            excerpt = ref.get("excerpt", "")
            assert len(excerpt.encode("utf-8")) <= CAPSULE_MAX_FILE_BYTES
            total += len(excerpt.encode("utf-8"))
            assert ref["blobSha"], "every ref must carry a blob SHA for staleness checks"
        assert total <= CAPSULE_MAX_TOTAL_BYTES


# -- tasks.md parsing --------------------------------------------------------

def test_parallel_marker_and_dependencies_are_parsed() -> None:
    """Spec Kit's `[P]` and `(depends on ...)` conventions drive the DAG."""
    tasks = parse_tasks_md(
        "- [ ] T001 [P] [US1] Create model in src/models/user.py\n"
        "- [ ] T002 [P] [US1] Create test in tests/test_user.py\n"
        "- [ ] T003 [US1] Wire service in src/service.py (depends on T001, T002)\n"
    )
    assert [t["id"] for t in tasks] == ["T001", "T002", "T003"]
    assert tasks[0]["parallel"] and tasks[1]["parallel"]
    assert tasks[2]["dependsOn"] == ["T001", "T002"]
    assert "src/models/user.py" in tasks[0]["paths"]
    assert "(depends on" not in tasks[2]["description"]


# -- schema validation -------------------------------------------------------

def test_malformed_run_is_rejected_by_the_schema() -> None:
    from adlc.schemas import is_valid

    valid, errors = is_valid("adlc-run", {"schemaVersion": "adlc-run/v1"})
    assert valid is False
    assert errors
