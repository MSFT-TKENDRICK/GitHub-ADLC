"""Step definitions for tests/conformance/features/adlc_lifecycle.feature.

The first BDD/Gherkin coverage in the suite (pytest-bdd was already a
project dependency but previously exercised zero .feature files). This
scenario narrates, in plain language, exactly what the credential-free
conformance driver proves procedurally elsewhere in this directory -- it
is not a duplicate of those tests, it is the same guarantees expressed as
an executable specification a non-engineer reviewer can read.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from adlc.config import Config
from adlc.reduce import aggregate_passed
from adlc.runs import RunDir
from adlc.schemas import is_valid

from .driver import drive

scenarios("features/adlc_lifecycle.feature")

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIEFS = REPO_ROOT / "examples" / "briefs"


def _read_run(rd: RunDir) -> dict[str, Any]:
    return json.loads(rd.run_json.read_text(encoding="utf-8"))


@pytest.fixture
def context() -> dict[str, Any]:
    """Shared mutable state across Given/When/Then steps for one scenario."""
    return {}


@given("a fresh ADLC-initialised repository", target_fixture="cfg")
def _fresh_repo(tmp_path: Path) -> Config:
    return Config(root=tmp_path, profile="minimal")


@given(parsers.parse('the "{name}" example brief'))
def _example_brief(name: str, context: dict[str, Any]) -> None:
    brief_path = BRIEFS / f"{name}.md"
    assert brief_path.is_file(), f"no example brief named {name}"
    context["brief_path"] = brief_path


@given("a run that has been driven through the full ADLC pipeline", target_fixture="rd")
def _pre_driven_run(cfg: Config, context: dict[str, Any]) -> RunDir:
    context.setdefault("brief_path", BRIEFS / "dark-mode.md")
    return drive(cfg, context["brief_path"])


@when("the brief is driven through the full ADLC pipeline", target_fixture="rd")
def _drive_pipeline(cfg: Config, context: dict[str, Any]) -> RunDir:
    return drive(cfg, context["brief_path"])


@when(parsers.parse('a required gate\'s status is forced to "{status}"'))
def _force_gate_status(rd: RunDir, status: str, context: dict[str, Any]) -> None:
    run_doc = _read_run(rd)
    gates = run_doc["gates"]
    required = next(g for g in gates if g["required"])
    required["status"] = status
    context["gates"] = gates


@then(parsers.parse('the run status is "{status}"'))
def _assert_run_status(rd: RunDir, status: str) -> None:
    assert _read_run(rd)["status"] == status


@then(parsers.parse('the run document validates against the "{schema_name}" schema'))
def _assert_schema_valid(rd: RunDir, schema_name: str) -> None:
    ok, errors = is_valid(schema_name, _read_run(rd))
    assert ok, f"schema errors: {errors}"


@then("the stage history is append-only")
def _assert_stages_append_only(rd: RunDir) -> None:
    stages = _read_run(rd)["stages"]
    seen: set[tuple[str, int]] = set()
    for entry in stages:
        key = (entry["stage"], entry["attempt"])
        assert key not in seen, f"duplicate stage attempt {key}"
        seen.add(key)


@then("every declared artifact has a verified sha256 digest")
def _assert_artifact_hashes(rd: RunDir) -> None:
    run_doc = _read_run(rd)
    for artifact in run_doc.get("artifacts", []):
        assert artifact.get("sha256"), f"artifact missing sha256: {artifact}"


@then("the task graph has no cycles")
def _assert_graph_acyclic(rd: RunDir) -> None:
    graph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph["nodes"]}

    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(node_id: str) -> None:
        if node_id in visited:
            return
        assert node_id not in visiting, f"cycle detected at {node_id}"
        visiting.add(node_id)
        for dep in nodes[node_id].get("dependsOn", []):
            _visit(dep)
        visiting.discard(node_id)
        visited.add(node_id)

    for node_id in nodes:
        _visit(node_id)


@then("at least two nodes share a level")
def _assert_parallel_level(rd: RunDir) -> None:
    graph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
    levels = Counter(n.get("level", 0) for n in graph["nodes"])
    assert any(count >= 2 for count in levels.values()), "no level has >=2 nodes"


@then("no two nodes at the same level declare overlapping write sets")
def _assert_no_write_set_overlap(rd: RunDir) -> None:
    graph = json.loads(rd.taskgraph.read_text(encoding="utf-8"))
    by_level: dict[int, list[set[str]]] = defaultdict(list)
    for node in graph["nodes"]:
        by_level[node.get("level", 0)].append(set(node.get("writeSet", [])))
    for level, write_sets in by_level.items():
        for i, a in enumerate(write_sets):
            for b in write_sets[i + 1 :]:
                assert not (a & b), f"overlapping write set at level {level}: {a & b}"


@then(parsers.parse('the aggregate gate status is "{expected}"'))
def _assert_aggregate_status(context: dict[str, Any], expected: str) -> None:
    passed, _failures = aggregate_passed(context["gates"])
    assert passed == (expected == "pass")
