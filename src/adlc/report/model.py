"""The report's data model -- everything the page needs, computed once, in Python.

The page is a viewer, not a compiler. Every number, summary, diff and layout
coordinate is resolved here and shipped as one JSON blob, so opening the report
costs a parse and a render rather than a pipeline of client-side work. That is
what keeps a 60-file, 200-artifact run instant on a laptop with no network.

The centrepiece is the **task graph laid out as a gitgraph**: nodes assigned to
lanes, edges resolved to concrete coordinates. Levels become columns because a
level *is* a parallel wave -- everything in one ran concurrently in isolated
worktrees, and everything in the next waited for the barrier. Drawing it any
other way would misrepresent how the run actually executed. Each node carries its
own <=150-char summary plus the gates, ADRs, artifacts and diff that belong to
it, so clicking a node is a lookup, not a search.
"""

from __future__ import annotations

import json
import re
from typing import Any

from adlc.config import Config
from adlc.reduce import aggregate_passed, load_run
from adlc.report.adr import build_adrs
from adlc.report.diff import collect_diffs
from adlc.report.media import build_media
from adlc.runs import RunDir, read_json, utcnow
from adlc.summarize import (
    artifact_tldr,
    gate_tldr,
    humanise_bytes,
    node_tldr,
    requirement_tldr,
)

__all__ = ["build_model", "graph_mermaid", "to_embedded_json"]

#: Horizontal spacing between waves, in SVG units. Chosen so a 12-level graph
#: fits a laptop viewport without scrolling.
_COL = 190
#: Vertical spacing between lanes.
_ROW = 62
_MARGIN = 34

_STATUS_CLASS = {"pass": "ok", "fail": "bad", "not_run": "warn"}
_STAGE_CLASS = {"ok": "ok", "fail": "bad", "skipped": "warn"}


def _lane_layout(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """Place nodes on a lane grid: column = wave, row = lane within the wave.

    Lanes are assigned by trying to keep a node in the same lane as its first
    dependency. A chain of work then draws as a straight horizontal line, which
    is the shape a reader can follow, and forks visibly diverge rather than the
    whole graph zig-zagging.
    """
    by_level: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        by_level.setdefault(int(node.get("level", 0) or 0), []).append(node)

    lane_of: dict[str, int] = {}
    placed: list[dict[str, Any]] = []
    for level in sorted(by_level):
        taken: set[int] = set()
        wave = sorted(by_level[level], key=lambda n: str(n.get("id", "")))
        # First pass: inherit a lane from the first dependency where possible.
        preferred: dict[str, int | None] = {}
        for node in wave:
            deps = [d for d in (node.get("dependsOn") or []) if d in lane_of]
            preferred[node["id"]] = lane_of[deps[0]] if deps else None
        for node in wave:
            want = preferred[node["id"]]
            if want is None or want in taken:
                want = next(lane for lane in range(len(wave) + len(taken) + 1)
                            if lane not in taken)
            taken.add(want)
            lane_of[node["id"]] = want
        for node in wave:
            lane = lane_of[node["id"]]
            placed.append({
                **node,
                "lane": lane,
                "x": _MARGIN + level * _COL,
                "y": _MARGIN + lane * _ROW,
            })

    width = _MARGIN * 2 + (max(by_level) if by_level else 0) * _COL + 150
    height = _MARGIN * 2 + (max(lane_of.values()) if lane_of else 0) * _ROW + 40
    return placed, width, height


def _edges(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    position = {n["id"]: n for n in nodes}
    out: list[dict[str, Any]] = []
    for node in nodes:
        for dep in node.get("dependsOn") or []:
            source = position.get(dep)
            if not source:
                continue
            out.append({
                "from": dep,
                "to": node["id"],
                "x1": source["x"], "y1": source["y"],
                "x2": node["x"], "y2": node["y"],
                "straight": source["lane"] == node["lane"],
            })
    return out


def graph_mermaid(graph: dict[str, Any] | None) -> str:
    """Mermaid source for the diagram fallback.

    The interactive gitgraph is an SVG built from the same model, but the Mermaid
    text is kept because it is the form a human can paste elsewhere, diff in a
    pull request, or read when JavaScript is unavailable.
    """
    if not graph or not graph.get("nodes"):
        return "flowchart LR\n  none[\"No task graph\"]"
    lines = ["flowchart LR"]
    levels: dict[int, list[dict[str, Any]]] = {}
    for node in graph["nodes"]:
        levels.setdefault(int(node.get("level", 0) or 0), []).append(node)
    for level in sorted(levels):
        lines.append(f'  subgraph L{level}["level {level}"]')
        for node in levels[level]:
            title = str(node.get("title", ""))[:44].replace('"', "'")
            lines.append(f'    {node["id"]}["{node["id"]}<br/>{title}"]')
        lines.append("  end")
    for node in graph["nodes"]:
        for dep in node.get("dependsOn") or []:
            lines.append(f"  {dep} --> {node['id']}")
    return "\n".join(lines)


def _artifact_paths(artifacts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Case-fold every artifact path once per render, not once per node.

    ``_node_artifacts`` runs for every node, so folding the case inside it
    re-lowercased the entire artifact list once per node: the same
    once-per-pair rework ``media._pair_shots`` was rewritten to avoid, one file
    over. Hoisting it makes the string work proportional to the artifacts alone.
    """
    return [(a["sha256"], str(a.get("path", "")).lower()) for a in artifacts]


def _node_artifacts(node: dict[str, Any], paths: list[tuple[str, str]]) -> list[str]:
    """Artifacts whose path mentions this node's id. Cheap, and precise enough."""
    node_id = str(node.get("id") or "").lower()
    if not node_id:
        return []
    return [sha for sha, path in paths if node_id in path]


def _requirements(pack: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not pack:
        return []
    coverage = {c.get("requirementId"): c for c in pack.get("coverage") or []}
    out: list[dict[str, Any]] = []
    for requirement in pack.get("requirements") or []:
        cover = coverage.get(requirement.get("id"), {})
        kinds = list(cover.get("evidenceKinds") or [])
        covered = bool(cover.get("present"))
        out.append({
            "id": requirement.get("id", ""),
            "text": requirement.get("text", ""),
            "source": requirement.get("source", ""),
            "covered": covered,
            "evidenceKinds": kinds,
            "artifactSha256": list(cover.get("artifactSha256") or []),
            "tldr": requirement_tldr(requirement.get("text", ""), covered, kinds),
        })
    return out


def _personas(rd: RunDir) -> list[dict[str, Any]]:
    try:
        from adlc.stages.persona_feedback import load_feedback
    except ImportError:  # pragma: no cover - defensive
        return []
    try:
        return load_feedback(rd)
    except Exception:  # noqa: BLE001 - a bad record must not lose the report
        return []


def build_model(cfg: Config, rd: RunDir) -> dict[str, Any]:
    """Assemble the complete report model."""
    run = load_run(rd)
    gates = list(run.get("gates") or [])
    passed, failures = aggregate_passed(gates)
    artifacts = list(run.get("artifacts") or [])
    stages = list(run.get("stages") or [])

    graph = read_json(rd.taskgraph) if rd.taskgraph.is_file() else None
    pack = read_json(rd.review_pack) if rd.review_pack.is_file() else None
    score = (
        read_json(rd.evals_dir / "rubric-score.json")
        if (rd.evals_dir / "rubric-score.json").is_file() else None
    )
    qualification = (
        read_json(rd.path / "qualification.json")
        if (rd.path / "qualification.json").is_file() else None
    )
    completeness = (
        read_json(rd.path / "completeness-pack.json")
        if (rd.path / "completeness-pack.json").is_file() else None
    )

    adrs = build_adrs(cfg, graph)
    diffs = collect_diffs(rd.patches_dir)
    diff_by_task = {d["taskId"]: d for d in diffs}
    media = build_media(rd, artifacts)
    personas = _personas(rd)

    raw_nodes = list((graph or {}).get("nodes") or [])
    placed, width, height = _lane_layout(raw_nodes)

    adr_by_number = {a["number"]: a for a in adrs}
    # Decisions name their tasks (see ``stages.adr``), so the node's own view of
    # which ADRs govern it is the union of both directions.
    adrs_by_task: dict[str, list[str]] = {}
    for adr in adrs:
        for linked in adr.get("nodes") or []:
            adrs_by_task.setdefault(linked.get("id", ""), []).append(adr["number"])

    nodes: list[dict[str, Any]] = []
    artifact_paths = _artifact_paths(artifacts)
    for node in placed:
        diff = diff_by_task.get(node.get("id", ""), {})
        refs = [re.sub(r"\D", "", str(r)).zfill(4) for r in (node.get("adrRefs") or [])]
        refs += adrs_by_task.get(node.get("id", ""), [])
        nodes.append({
            "id": node.get("id", ""),
            "title": node.get("title", ""),
            # `tldr` is generated by the graph stage, but a graph compiled before
            # this field existed must still render, so fall back rather than
            # showing an empty card.
            "tldr": node.get("tldr") or node_tldr(node),
            "kind": node.get("kind", "implement"),
            "level": int(node.get("level", 0) or 0),
            "lane": node["lane"],
            "x": node["x"], "y": node["y"],
            "dependsOn": list(node.get("dependsOn") or []),
            "writeSet": list(node.get("writeSet") or []),
            "acceptance": list(node.get("acceptance") or []),
            "adrRefs": list(dict.fromkeys(r for r in refs if r in adr_by_number)),
            "artifactSha256": _node_artifacts(node, artifact_paths),
            "stats": diff.get("stats") or {"files": 0, "additions": 0, "deletions": 0},
            "hasDiff": bool(diff.get("files")),
        })

    gate_view = [{
        "id": gate.get("id", ""),
        "status": gate.get("status", "not_run"),
        "cls": _STATUS_CLASS.get(gate.get("status", ""), "warn"),
        "required": bool(gate.get("required")),
        "message": gate.get("message", ""),
        "tldr": gate_tldr(gate),
        "observed": gate.get("observed"),
        "expected": gate.get("expected"),
    } for gate in gates]

    artifact_view = [{
        "path": a.get("path", ""),
        "kind": a.get("kind", "file"),
        "mimeType": a.get("mimeType", ""),
        "bytes": a.get("bytes", 0),
        "human": humanise_bytes(a.get("bytes", 0)),
        "sha256": a.get("sha256", ""),
        "tldr": artifact_tldr(a),
    } for a in artifacts]

    stage_view = [{
        "stage": s.get("stage", ""),
        "attempt": s.get("attempt", 1),
        "status": s.get("status", "ok"),
        "cls": _STAGE_CLASS.get(s.get("status", "ok"), "warn"),
        "startedAt": s.get("startedAt", ""),
        "endedAt": s.get("endedAt", ""),
        "message": s.get("message", ""),
        "outputs": list(s.get("outputs") or []),
    } for s in stages]

    required_total = sum(1 for g in gates if g.get("required"))
    required_pass = sum(1 for g in gates if g.get("required") and g.get("status") == "pass")

    return {
        "runId": rd.run_id,
        "repo": run.get("repo", ""),
        "prNumber": run.get("prNumber"),
        "profile": run.get("profile", ""),
        "status": run.get("status", ""),
        "baseSha": run.get("baseSha", ""),
        "headSha": run.get("headSha", ""),
        "generated": utcnow(),
        "passed": passed,
        "failures": failures,
        "requiredPass": required_pass,
        "requiredTotal": required_total,
        "qualification": qualification,
        "gates": gate_view,
        "artifacts": artifact_view,
        "stages": stage_view,
        "graph": {
            "nodes": nodes,
            "edges": _edges(placed),
            "width": width,
            "height": height,
            "levels": sorted({n["level"] for n in nodes}),
        },
        "diffs": diffs,
        "media": media,
        "adrs": adrs,
        "personas": personas,
        "requirements": _requirements(pack),
        "rubric": score,
        "completeness": completeness,
        "run": run,
    }


def to_embedded_json(model: dict[str, Any]) -> str:
    """Serialise the model for an inline ``<script type="application/json">``.

    ``<`` and ``>`` are escaped so the string ``</script`` can never appear in
    the payload and terminate the block early. This is the one place where a
    stray byte in captured evidence could break the whole document, so it is
    handled by construction rather than by hoping the data is well behaved.
    """
    text = json.dumps(model, ensure_ascii=False, default=str, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
