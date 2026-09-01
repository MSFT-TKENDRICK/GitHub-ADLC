"""Task graph compiler: Spec Kit ``tasks.md`` -> ``taskgraph.json``.

Parses Spec Kit's task format -- ``[ID] [P?] [Story] Description`` where ``[P]``
means parallelisable and ``(depends on T001, T002)`` declares edges -- and
compiles it into an executable DAG with **bounded context capsules**.

Capsules are the answer to "give subagents everything so they don't need
discovery" without the failure mode of unbounded inlining: they carry blob SHAs,
symbol names and line ranges by default, full content only for small files, and
are hard-capped (64 KiB total / 8 KiB per file / 12 files). They are regenerated
after every level barrier, and a blob-SHA mismatch fails the node rather than
letting an agent edit against stale content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.executor import validate_graph
from adlc.ports import (
    CAPSULE_MAX_FILE_BYTES,
    CAPSULE_MAX_FILES,
    CAPSULE_MAX_TOTAL_BYTES,
    PROTECTED_PATHS,
    ContextRef,
    TaskGraph,
    TaskNode,
)
from adlc.runs import RunDir, git, utcnow, write_json
from adlc.summarize import node_tldr

_TASK_RE = re.compile(
    r"^\s*-\s*\[[ xX]\]\s*(?P<id>T\d{3,})\s*(?P<parallel>\[P\])?\s*"
    r"(?:\[(?P<story>US\d+)\])?\s*(?P<desc>.+?)\s*$"
)
_DEPENDS_RE = re.compile(r"\(depends on\s+(?P<deps>[^)]+)\)", re.IGNORECASE)
_PATH_RE = re.compile(r"(?<![\w/.])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,6})(?![\w/])")
_SYMBOL_RE = re.compile(
    r"^\s*(?:def|class|function|const|let|var|export\s+(?:const|function|class)|async\s+def)\s+"
    r"([A-Za-z_][\w]*)",
    re.MULTILINE,
)

_KIND_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("test", re.compile(r"\btest|spec\b", re.IGNORECASE)),
    ("doc", re.compile(r"\bdoc|readme|guide\b", re.IGNORECASE)),
    ("infra", re.compile(r"\bci\b|workflow|pipeline|docker|infra", re.IGNORECASE)),
)

_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webm", ".zip", ".pdf", ".woff", ".woff2",
    ".ico", ".so", ".dll", ".dylib", ".exe", ".har",
}
_EXCLUDED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".adlc", "vendor", ".next", "coverage",
}


def _kind_for(description: str) -> str:
    for kind, pattern in _KIND_HINTS:
        if pattern.search(description):
            return kind
    return "implement"


def parse_tasks_md(text: str) -> list[dict[str, Any]]:
    """Extract task rows from a Spec Kit ``tasks.md``."""
    tasks: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _TASK_RE.match(line)
        if not match:
            continue
        description = match.group("desc")
        deps: list[str] = []
        if dep_match := _DEPENDS_RE.search(description):
            deps = [d.strip() for d in re.split(r"[,\s]+", dep_match.group("deps")) if d.strip()]
            description = _DEPENDS_RE.sub("", description).strip()
        tasks.append({
            "id": match.group("id"),
            "parallel": bool(match.group("parallel")),
            "story": match.group("story"),
            "description": description,
            "dependsOn": deps,
            "paths": _PATH_RE.findall(description),
        })
    return tasks


def _is_protected(path: str) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(path, pattern) for pattern in PROTECTED_PATHS)


def _write_set(task: dict[str, Any]) -> list[str]:
    paths = [p for p in task["paths"] if not _is_protected(p)]
    if paths:
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(paths))
    slug = re.sub(r"[^a-z0-9]+", "_", task["description"].lower())[:40].strip("_") or task["id"].lower()
    return [f"src/generated/{task['id'].lower()}_{slug}.txt"]


def _infer_deps(tasks: list[dict[str, Any]]) -> None:
    """Serialise non-``[P]`` tasks behind everything declared before them.

    Spec Kit expresses "these can run together" with ``[P]``; anything without
    it is sequential within its phase. Making that explicit here is what lets
    the executor safely run a level concurrently.
    """
    seen: list[str] = []
    for task in tasks:
        if not task["parallel"] and not task["dependsOn"] and seen:
            task["dependsOn"] = list(seen)
        seen.append(task["id"])


# ---------------------------------------------------------------------------
# Context capsules
# ---------------------------------------------------------------------------


def _candidate_files(root: Path, write_set: list[str], limit: int) -> list[Path]:
    """Files most likely relevant: siblings of the write-set, then key configs."""
    picked: list[Path] = []
    seen: set[Path] = set()

    def consider(path: Path) -> None:
        if len(picked) >= limit or path in seen:
            return
        seen.add(path)
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            return
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(root).parts):
            return
        picked.append(path)

    for rel in write_set:
        directory = (root / rel).parent
        if directory.is_dir():
            for sibling in sorted(directory.iterdir()):
                consider(sibling)

    for name in ("AGENTS.md", "README.md", "pyproject.toml", "package.json", "CONTRIBUTING.md"):
        candidate = root / name
        if candidate.is_file():
            consider(candidate)

    return picked[:limit]


def build_capsule(cfg: Config, node_write_set: list[str]) -> dict[str, Any]:
    """Assemble a bounded capsule. Budgets are enforced, not advisory."""
    root = cfg.root
    refs: list[ContextRef] = []
    total = 0

    for path in _candidate_files(root, node_write_set, CAPSULE_MAX_FILES):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        blob_sha = git("hash-object", str(path), cwd=root, check=False)
        ref: ContextRef = {"path": rel, "blobSha": blob_sha}

        text = raw.decode("utf-8", errors="replace")
        symbols = _SYMBOL_RE.findall(text)[:20]
        if symbols:
            ref["symbols"] = symbols

        # Full content only for small files, and only while inside the budget.
        if len(raw) <= CAPSULE_MAX_FILE_BYTES and total + len(raw) <= CAPSULE_MAX_TOTAL_BYTES:
            ref["excerpt"] = text
            ref["lines"] = [[1, text.count("\n") + 1]]
            total += len(raw)
        else:
            head = "\n".join(text.splitlines()[:40])
            snippet = head[: min(CAPSULE_MAX_FILE_BYTES, max(0, CAPSULE_MAX_TOTAL_BYTES - total))]
            if snippet:
                ref["excerpt"] = snippet
                ref["lines"] = [[1, snippet.count("\n") + 1]]
                total += len(snippet.encode("utf-8"))

        refs.append(ref)
        if total >= CAPSULE_MAX_TOTAL_BYTES:
            break

    commands = cfg.raw.get("commands") or {}
    conventions = ""
    for name in ("AGENTS.md", ".github/copilot-instructions.md"):
        candidate = root / name
        if candidate.is_file():
            conventions = candidate.read_text(encoding="utf-8", errors="replace")[:2000]
            break

    return {
        "refs": refs,
        "interfaces": "",
        "conventions": conventions,
        "commands": {
            "test": commands.get("test", ""),
            "lint": commands.get("lint", ""),
            "build": commands.get("build", ""),
        },
        "doNotTouch": list(PROTECTED_PATHS),
        "budget": {
            "maxTotalBytes": CAPSULE_MAX_TOTAL_BYTES,
            "maxFileBytes": CAPSULE_MAX_FILE_BYTES,
            "maxFiles": CAPSULE_MAX_FILES,
        },
    }


def regenerate_capsules(cfg: Config, graph: TaskGraph, level: int) -> TaskGraph:
    """Refresh capsules for nodes at or after ``level``.

    Called at every barrier so downstream nodes never see pre-merge content.
    """
    for node in graph.get("nodes", []):
        if node.get("level", 0) >= level:
            node["context"] = build_capsule(cfg, node.get("writeSet") or [])
    graph["baseSha"] = git("rev-parse", "HEAD", cwd=cfg.root, check=False)
    return graph


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def compile_graph(cfg: Config, rd: RunDir) -> TaskGraph:
    tasks_md = rd.spec_dir / "tasks.md"
    if not tasks_md.is_file():
        raise FileNotFoundError(f"{tasks_md} not found - run `adlc spec` first")

    text = tasks_md.read_text(encoding="utf-8")
    parsed = parse_tasks_md(text)
    if not parsed:
        raise ValueError(f"no tasks parsed from {tasks_md}")
    _infer_deps(parsed)

    rubric_ids: list[str] = []
    rubric_path = rd.enrichment_dir / "rubric.yaml"
    if rubric_path.is_file():
        rubric_ids = re.findall(r"^\s*-\s*id:\s*(\S+)", rubric_path.read_text(encoding="utf-8"), re.MULTILINE)

    nodes: list[TaskNode] = []
    for task in parsed:
        write_set = _write_set(task)
        node: TaskNode = {
            "id": task["id"],
            "title": task["description"],
            "kind": _kind_for(task["description"]),  # type: ignore[typeddict-item]
            "dependsOn": task["dependsOn"],
            "level": 0,
            "writeSet": write_set,
            "acceptance": [f"{task['story']}-AC1"] if task["story"] else [],
            "rubricIds": rubric_ids,
            "adrRefs": [],
            "context": build_capsule(cfg, write_set),
        }
        nodes.append(node)

    graph: TaskGraph = {
        "runId": rd.run_id,
        "baseSha": git("rev-parse", "HEAD", cwd=cfg.root, check=False),
        "specDigest": f"sha256:{__import__('hashlib').sha256(text.encode()).hexdigest()}",
        "nodes": nodes,
    }
    validate_graph(graph)  # raises GraphError on cycles / write-set conflicts
    # Summaries are generated after validation because `validate_graph` is what
    # assigns levels, and a node's summary describes where it sits in the wave
    # order. Generating before would describe a graph that does not exist yet.
    for node in graph["nodes"]:
        node["tldr"] = node_tldr(dict(node))
    return graph


def run_graph(cfg: Config, rd: RunDir) -> TaskGraph:
    from adlc.config import select_adapter

    started = utcnow()
    graph = compile_graph(cfg, rd)
    write_json(rd.taskgraph, graph)

    levels = sorted({node.get("level", 0) for node in graph["nodes"]})
    widest = max(
        (sum(1 for n in graph["nodes"] if n.get("level") == lvl) for lvl in levels), default=0
    )

    store_name = "none"
    try:
        store = select_adapter(cfg, "taskstore")
        if hasattr(store, "bind"):
            store.bind(cfg)
        store.sync(graph)
        store_name = getattr(store, "name", type(store).__name__)
    except Exception as exc:  # noqa: BLE001 - a task store must never block the graph
        store_name = f"unavailable ({exc})"

    rd.write_stage(
        "graph",
        outputs=[rd.rel(rd.taskgraph)],
        message=(
            f"{len(graph['nodes'])} task(s) across {len(levels)} level(s); "
            f"max parallel width {widest}; task store: {store_name}"
        ),
        data={
            "nodes": len(graph["nodes"]),
            "levels": len(levels),
            "maxWidth": widest,
            "taskStore": store_name,
        },
        started_at=started,
    )
    return graph
