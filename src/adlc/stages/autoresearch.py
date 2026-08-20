"""Autoresearch -- propose the next piece of work from repository signals.

Deliberately heuristic and deterministic. It reads signals that already exist
rather than inventing an opinion:

* **Run history** -- gates that repeatedly fail, and criteria the deterministic
  runner could not evaluate.
* **Human feedback** -- reviews that requested changes.
* **Repository state** -- TODO/FIXME density, missing conventional files.

The output is a brief, not a change. It is filed as an issue labelled
``adlc:brief``, which re-enters the ordinary day-1 intake path -- the same path
an SRE incident or a human idea uses.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.runs import read_json

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".adlc"}
_TEXT_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".md"}


def _run_signals(cfg: Config) -> dict[str, Any]:
    failing: Counter[str] = Counter()
    unevaluated: Counter[str] = Counter()
    revisions = 0
    runs = 0

    if cfg.runs_dir.is_dir():
        for directory in sorted(d for d in cfg.runs_dir.iterdir() if d.is_dir()):
            run_json = directory / "run.json"
            if not run_json.is_file():
                continue
            runs += 1
            try:
                run = read_json(run_json)
            except Exception:  # noqa: BLE001
                continue
            for gate in run.get("gates") or []:
                if gate.get("status") in {"fail", "not_run"} and gate.get("required"):
                    failing[gate["id"]] += 1
            for stage in run.get("stages") or []:
                if stage.get("stage") == "eval":
                    for crit in (stage.get("data") or {}).get("unevaluated") or []:
                        unevaluated[crit] += 1
                if stage.get("stage") == "review" and (stage.get("data") or {}).get("outcome") == "iterate":
                    revisions += 1
    return {
        "runs": runs,
        "failingGates": failing.most_common(5),
        "unevaluatedCriteria": unevaluated.most_common(5),
        "revisions": revisions,
    }


def _repo_signals(cfg: Config) -> dict[str, Any]:
    todos: Counter[str] = Counter()
    scanned = 0
    for path in cfg.root.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(cfg.root).parts):
            continue
        scanned += 1
        if scanned > 4000:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        count = len(_TODO_RE.findall(text))
        if count:
            todos[path.relative_to(cfg.root).as_posix()] = count

    missing = [
        name for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md", "docs/decisions")
        if not (cfg.root / name).exists()
    ]
    return {"filesScanned": scanned, "todoHotspots": todos.most_common(5), "missingFiles": missing}


def propose(cfg: Config) -> dict[str, Any]:
    """Produce a single, highest-signal brief."""
    runs = _run_signals(cfg)
    repo = _repo_signals(cfg)

    candidates: list[tuple[int, str, str]] = []

    def add(score: int, title: str, body: str) -> None:
        candidates.append((score, title, body))

    for gate_id, count in runs["failingGates"]:
        add(
            100 + count * 10,
            f"Make the '{gate_id}' gate pass reliably",
            (
                f"The required gate `{gate_id}` failed or did not run in {count} previous "
                f"run(s). A required gate that cannot run fails the build by design, so this "
                f"blocks delivery.\n\n"
                f"**Problem**: `{gate_id}` is not producing a trustworthy signal.\n"
                f"**Outcome**: every run yields a definitive pass or fail for `{gate_id}`.\n"
                f"**Acceptance criteria**: the gate returns `pass` or `fail` (never `not_run`) "
                f"on three consecutive runs, and its message names the evidence it used."
            ),
        )

    for criterion, count in runs["unevaluatedCriteria"]:
        add(
            80 + count * 5,
            f"Make rubric criterion '{criterion}' machine-checkable",
            (
                f"Criterion `{criterion}` needed an LLM judge in {count} run(s), so it was "
                f"scored 0 by the deterministic runner.\n\n"
                f"**Problem**: a criterion we cannot check cheaply drags every score down.\n"
                f"**Outcome**: the criterion is evaluated deterministically, or is explicitly "
                f"delegated to a configured judge.\n"
                f"**Acceptance criteria**: `adlc eval` reports a non-zero score for "
                f"`{criterion}` with a rationale naming the check performed."
            ),
        )

    for path, count in repo["todoHotspots"]:
        add(
            40 + count,
            f"Resolve {count} deferred item(s) in {path}",
            (
                f"`{path}` carries {count} TODO/FIXME marker(s).\n\n"
                f"**Problem**: deferred work in `{path}` is invisible to planning.\n"
                f"**Outcome**: each marker is resolved or promoted to a tracked task.\n"
                f"**Acceptance criteria**: no TODO/FIXME remains in `{path}`, and any deferred "
                f"item exists as an issue."
            ),
        )

    for name in repo["missingFiles"]:
        add(
            30,
            f"Add {name}",
            (
                f"`{name}` is absent, which weakens the context available to both humans and "
                f"agents.\n\n"
                f"**Problem**: contributors and agents lack `{name}`.\n"
                f"**Outcome**: `{name}` exists and is accurate.\n"
                f"**Acceptance criteria**: `{name}` is present and referenced from the README."
            ),
        )

    if not candidates:
        return {
            "proposal": None,
            "summary": "no actionable signal found",
            "signals": {"runs": runs, "repo": repo},
        }

    candidates.sort(key=lambda item: item[0], reverse=True)
    score, title, body = candidates[0]
    brief = f"# {title}\n\n{body}\n\n---\n\n_Proposed by ADLC autoresearch (signal score {score})._\n"

    return {
        "proposal": {"title": title, "body": brief, "score": score, "labels": ["adlc:brief"]},
        "summary": f"proposed: {title} (score {score})",
        "alternatives": [{"title": t, "score": s} for s, t, _ in candidates[1:4]],
        "signals": {"runs": runs, "repo": repo},
    }


def write_brief(cfg: Config, out: Path) -> Path | None:
    result = propose(cfg)
    if not result.get("proposal"):
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result["proposal"]["body"], encoding="utf-8")
    return out
