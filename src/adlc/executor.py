"""Topological DAG executor with worktree isolation and patch barriers.

Why a built-in executor rather than an agent framework: it must run *identically*
from the CLI and inside a GitHub Actions job, with no credentials and no network,
so that the conformance suite genuinely proves the concept. It is ~200 lines.

Execution model
---------------
Nodes are grouped into topological *levels*. Every node in a level runs
concurrently in its own git worktree checked out at the current ``baseSha``.
Each node emits a patch anchored to that exact SHA. At the **level barrier** the
patches are applied in id order, tests run, a commit is made, ``baseSha``
advances, and context capsules are regenerated for the next level.

Two correctness rules that a naive implementation gets wrong:

* **Write-set conflicts are a compile-time graph error**, detected before any
  agent runs -- not discovered at merge time when work is already wasted.
* **A stale ``blobSha`` in a context capsule fails the node** rather than letting
  an agent edit against content that has since changed underneath it.
"""

from __future__ import annotations

import asyncio
import fnmatch
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import PROTECTED_PATHS, AgentRunner, TaskGraph, TaskNode, TaskOutcome
from adlc.runs import RunDir, git, utcnow


class GraphError(Exception):
    """The task graph is structurally invalid. Raised before execution starts."""


@dataclass
class NodeRun:
    node_id: str
    level: int
    status: str = "pending"
    started_at: str = ""
    ended_at: str = ""
    patch: str | None = None
    message: str = ""


@dataclass
class ExecutionReport:
    levels: int = 0
    nodes: list[NodeRun] = field(default_factory=list)
    barriers: list[dict[str, Any]] = field(default_factory=list)
    base_sha: str = ""
    completed_levels: int = 0

    def to_data(self) -> dict[str, Any]:
        return {
            "levels": self.levels,
            "completedLevels": self.completed_levels,
            "baseSha": self.base_sha,
            "nodes": [n.__dict__ for n in self.nodes],
            "barriers": self.barriers,
        }


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------


def assign_levels(graph: TaskGraph) -> dict[str, int]:
    """Kahn's algorithm. Raises :class:`GraphError` on a cycle."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    indegree = {nid: 0 for nid in nodes}
    dependents: dict[str, list[str]] = defaultdict(list)

    for nid, node in nodes.items():
        for dep in node.get("dependsOn") or []:
            if dep not in nodes:
                raise GraphError(f"node '{nid}' depends on unknown node '{dep}'")
            indegree[nid] += 1
            dependents[dep].append(nid)

    levels: dict[str, int] = {}
    frontier = sorted(nid for nid, deg in indegree.items() if deg == 0)
    level = 0
    resolved = 0
    while frontier:
        nxt: list[str] = []
        for nid in frontier:
            levels[nid] = level
            resolved += 1
            for child in dependents[nid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        frontier = sorted(nxt)
        level += 1

    if resolved != len(nodes):
        stuck = sorted(set(nodes) - set(levels))
        raise GraphError(f"cycle detected in task graph involving: {', '.join(stuck)}")
    return levels


def check_write_sets(graph: TaskGraph, levels: dict[str, int]) -> None:
    """Reject overlapping write-sets within a level -- before anything runs."""
    by_level: dict[int, list[TaskNode]] = defaultdict(list)
    for node in graph.get("nodes", []):
        by_level[levels[node["id"]]].append(node)

    for level, nodes in sorted(by_level.items()):
        owner: dict[str, str] = {}
        for node in sorted(nodes, key=lambda n: n["id"]):
            for path in node.get("writeSet") or []:
                if path in owner:
                    raise GraphError(
                        f"write-set conflict at level {level}: "
                        f"'{path}' claimed by both '{owner[path]}' and '{node['id']}'. "
                        "Split the task or add a dependency edge."
                    )
                owner[path] = node["id"]


def check_protected_paths(graph: TaskGraph) -> None:
    for node in graph.get("nodes", []):
        for path in node.get("writeSet") or []:
            for pattern in PROTECTED_PATHS:
                if fnmatch.fnmatch(path, pattern):
                    raise GraphError(
                        f"node '{node['id']}' declares a write to protected path "
                        f"'{path}' (matches '{pattern}')"
                    )


def validate_graph(graph: TaskGraph) -> dict[str, int]:
    """Full pre-flight. Returns the level assignment."""
    if not graph.get("nodes"):
        raise GraphError("task graph has no nodes")
    levels = assign_levels(graph)
    for node in graph.get("nodes", []):
        declared = node.get("level")
        if declared is not None and declared != levels[node["id"]]:
            node["level"] = levels[node["id"]]
    check_write_sets(graph, levels)
    check_protected_paths(graph)
    return levels


def verify_capsule(node: TaskNode, root: Path) -> list[str]:
    """Return a list of stale refs (blobSha no longer matches the working tree)."""
    stale: list[str] = []
    for ref in (node.get("context") or {}).get("refs") or []:
        path, expected = ref.get("path"), ref.get("blobSha")
        if not path or not expected:
            continue
        target = root / path
        if not target.is_file():
            stale.append(f"{path} (missing)")
            continue
        actual = git("hash-object", str(target), cwd=root, check=False)
        if actual and actual != expected:
            stale.append(f"{path} (expected {expected[:8]}, found {actual[:8]})")
    return stale


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


class Worktree:
    """A disposable git worktree pinned to an exact SHA."""

    def __init__(self, root: Path, sha: str, name: str) -> None:
        self.root = root
        self.sha = sha
        self.name = name
        self.path = Path(tempfile.mkdtemp(prefix=f"adlc-{name}-"))
        self._added = False

    def __enter__(self) -> Worktree:
        git("worktree", "add", "--detach", str(self.path), self.sha, cwd=self.root)
        self._added = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self._added:
            git("worktree", "remove", "--force", str(self.path), cwd=self.root, check=False)
        shutil.rmtree(self.path, ignore_errors=True)

    def diff(self) -> str:
        """Patch of everything changed in this worktree, including new files."""
        git("add", "-A", cwd=self.path, check=False)
        return git("diff", "--cached", "--binary", cwd=self.path, check=False)


def violated_write_set(patch_text: str, write_set: list[str]) -> list[str]:
    """Paths touched by a patch that were not declared in the node's write-set."""
    touched: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            touched.add(line[6:].strip())
        elif line.startswith("--- a/") and "/dev/null" not in line:
            touched.add(line[6:].strip())
    allowed = set(write_set or [])
    return sorted(
        path
        for path in touched
        if path not in allowed
        and not any(fnmatch.fnmatch(path, pattern) for pattern in allowed)
    )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    def __init__(
        self,
        cfg: Config,
        rd: RunDir,
        runner: AgentRunner,
        *,
        max_parallel: int = 4,
    ) -> None:
        self.cfg = cfg
        self.rd = rd
        self.runner = runner
        self.max_parallel = max(1, max_parallel)

    async def _run_node(
        self, node: TaskNode, base_sha: str, sem: asyncio.Semaphore
    ) -> NodeRun:
        record = NodeRun(node_id=node["id"], level=node.get("level", 0), started_at=utcnow())
        async with sem:
            try:
                stale = verify_capsule(node, self.cfg.root)
                if stale:
                    record.status = "fail"
                    record.message = "stale context capsule: " + "; ".join(stale)
                    record.ended_at = utcnow()
                    return record

                with Worktree(self.cfg.root, base_sha, node["id"]) as wt:
                    outcome: TaskOutcome = await self.runner.run_task(node, wt.path, self.cfg)
                    patch_text = wt.diff()

                if outcome.get("status") == "fail":
                    record.status = "fail"
                    record.message = outcome.get("log", "agent reported failure")
                elif not patch_text.strip():
                    record.status = "ok"
                    record.message = "no changes produced"
                else:
                    violations = violated_write_set(patch_text, node.get("writeSet") or [])
                    if violations:
                        record.status = "fail"
                        record.message = (
                            "patch touches undeclared paths: " + ", ".join(violations)
                        )
                    else:
                        patch_path = self.rd.patches_dir / f"{node['id']}.patch"
                        patch_path.parent.mkdir(parents=True, exist_ok=True)
                        patch_path.write_text(patch_text, encoding="utf-8")
                        record.patch = self.rd.rel(patch_path)
                        record.status = "ok"
            except Exception as exc:  # noqa: BLE001 - one bad node must not kill the run
                record.status = "fail"
                record.message = f"{type(exc).__name__}: {exc}"
        record.ended_at = utcnow()
        return record

    def _barrier(self, level: int, records: list[NodeRun], base_sha: str) -> dict[str, Any]:
        """Apply this level's patches, run tests, commit, advance baseSha."""
        applied, conflicts = [], []
        for record in sorted(records, key=lambda r: r.node_id):
            if record.status != "ok" or not record.patch:
                continue
            patch_path = self.rd.path / record.patch
            proc = subprocess.run(  # noqa: S603
                ["git", "apply", "--index", "--3way", str(patch_path)],
                cwd=str(self.cfg.root), capture_output=True, text=True, check=False,
            )
            if proc.returncode == 0:
                applied.append(record.node_id)
            else:
                conflicts.append({"node": record.node_id, "error": proc.stderr.strip()})

        test_ok, test_output = True, "no test command configured"
        command = (self.cfg.raw.get("commands") or {}).get("test")
        if command and applied:
            proc = subprocess.run(  # noqa: S603,S602
                command, cwd=str(self.cfg.root), shell=True,
                capture_output=True, text=True, check=False,
            )
            test_ok = proc.returncode == 0
            test_output = (proc.stdout + proc.stderr)[-4000:]

        new_sha = base_sha
        if applied and not conflicts and test_ok:
            git("commit", "-m", f"adlc: level {level} ({', '.join(applied)})",
                "--no-verify", cwd=self.cfg.root, check=False)
            new_sha = git("rev-parse", "HEAD", cwd=self.cfg.root, check=False) or base_sha

        return {
            "level": level, "applied": applied, "conflicts": conflicts,
            "testsPassed": test_ok, "testOutput": test_output,
            "baseShaBefore": base_sha, "baseShaAfter": new_sha, "at": utcnow(),
        }

    async def run(self, graph: TaskGraph, *, resume_from: int = 0) -> ExecutionReport:
        levels = validate_graph(graph)
        by_level: dict[int, list[TaskNode]] = defaultdict(list)
        for node in graph.get("nodes", []):
            by_level[levels[node["id"]]].append(node)

        report = ExecutionReport(levels=len(by_level))
        base_sha = graph.get("baseSha") or git("rev-parse", "HEAD", cwd=self.cfg.root, check=False)
        report.base_sha = base_sha
        sem = asyncio.Semaphore(self.max_parallel)

        for level in sorted(by_level):
            if level < resume_from:
                report.completed_levels = level + 1
                continue
            nodes = by_level[level]
            records = await asyncio.gather(
                *(self._run_node(node, base_sha, sem) for node in nodes)
            )
            report.nodes.extend(records)

            barrier = self._barrier(level, list(records), base_sha)
            report.barriers.append(barrier)
            base_sha = barrier["baseShaAfter"]
            report.base_sha = base_sha

            if barrier["conflicts"] or not barrier["testsPassed"]:
                break
            report.completed_levels = level + 1

        return report
