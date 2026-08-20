"""Intake and qualification stages.

Intake normalises whatever arrived (a file, a GitHub issue, an SRE incident)
into ``brief.md``. Qualification scores it deterministically so that low-value
or under-specified work is parked before any agent spends money on it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from adlc.config import Config
from adlc.runs import RunDir, utcnow, write_json

#: Signals we look for in a brief. Deterministic, explainable, cheap.
_SIGNALS: dict[str, tuple[re.Pattern[str], int, str]] = {
    "problem": (
        re.compile(r"\b(problem|issue|pain|broken|fails?|bug|regress)", re.IGNORECASE),
        20, "states a problem",
    ),
    "outcome": (
        re.compile(r"\b(so that|in order to|outcome|goal|value|benefit|impact)", re.IGNORECASE),
        20, "states a desired outcome",
    ),
    "acceptance": (
        re.compile(r"\b(acceptance|criteri|given\s+.*\bwhen\b|definition of done|must)", re.IGNORECASE),
        25, "has acceptance criteria",
    ),
    "scope": (
        re.compile(r"\b(scope|out of scope|non-goal|constraint|limit)", re.IGNORECASE),
        15, "bounds the scope",
    ),
    "audience": (
        re.compile(r"\b(user|customer|persona|developer|operator|admin)", re.IGNORECASE),
        10, "identifies an audience",
    ),
    "measurable": (
        re.compile(r"\b(\d+\s*(ms|s|%|rps|mb|kb)|p9[59]|latency|throughput|score)", re.IGNORECASE),
        10, "includes a measurable target",
    ),
}

_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bug", re.compile(r"\b(bug|defect|broken|regress|crash|error)\b", re.IGNORECASE)),
    ("security", re.compile(r"\b(security|vuln|cve|exploit|auth|secret)\b", re.IGNORECASE)),
    ("performance", re.compile(r"\b(perf|latency|slow|throughput|memory|p9[59])\b", re.IGNORECASE)),
    ("accessibility", re.compile(r"\b(a11y|accessib|screen reader|wcag|contrast)\b", re.IGNORECASE)),
    ("docs", re.compile(r"\b(docs?|documentation|readme|guide)\b", re.IGNORECASE)),
    ("infra", re.compile(r"\b(ci|pipeline|deploy|infra|workflow|runner)\b", re.IGNORECASE)),
    ("feature", re.compile(r"\b(add|new|support|introduce|enable|feature)\b", re.IGNORECASE)),
)


def brief_from_issue(number: int) -> str:
    """Fetch an issue via `gh` and render it as a brief."""
    proc = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "number,title,body,labels,url"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not read issue #{number}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    labels = ", ".join(label["name"] for label in data.get("labels", []))
    return (
        f"# {data['title']}\n\n"
        f"> Source: {data.get('url', '')}\n"
        f"> Labels: {labels or 'none'}\n\n"
        f"{data.get('body') or ''}\n"
    )


def run_intake(cfg: Config, rd: RunDir, source: str) -> dict[str, Any]:
    started = utcnow()
    data = {
        "source": source,
        "briefBytes": rd.brief.stat().st_size if rd.brief.is_file() else 0,
        "actor": os.environ.get("GITHUB_ACTOR", os.environ.get("USERNAME", "local")),
    }
    rd.write_stage("intake", outputs=["brief.md"], message=f"brief from {source}",
                   data=data, started_at=started)
    return data


def categorize(text: str) -> str:
    for name, pattern in _CATEGORIES:
        if pattern.search(text):
            return name
    return "feature"


def qualify_text(text: str) -> dict[str, Any]:
    """Deterministic 0-100 readiness score with a full explanation."""
    signals: list[dict[str, Any]] = []
    score = 0
    for key, (pattern, weight, description) in _SIGNALS.items():
        present = bool(pattern.search(text))
        if present:
            score += weight
        signals.append({
            "signal": key, "present": present,
            "weight": weight, "description": description,
        })

    words = len(text.split())
    if words < 30:
        length_penalty = 25
    elif words < 80:
        length_penalty = 10
    else:
        length_penalty = 0
    score = max(0, min(100, score - length_penalty))

    missing = [s["description"] for s in signals if not s["present"]]
    return {
        "score": score,
        "category": categorize(text),
        "words": words,
        "lengthPenalty": length_penalty,
        "signals": signals,
        "missing": missing,
        "risk": "high" if score < 40 else "medium" if score < 70 else "low",
    }


def run_qualify(cfg: Config, rd: RunDir) -> dict[str, Any]:
    started = utcnow()
    text = rd.brief.read_text(encoding="utf-8") if rd.brief.is_file() else ""
    result = qualify_text(text)
    threshold = int((cfg.raw.get("qualify") or {}).get("minScore", 50))
    result["threshold"] = threshold
    result["qualified"] = result["score"] >= threshold

    write_json(rd.path / "qualification.json", result)
    rd.write_stage(
        "qualify",
        status="ok" if result["qualified"] else "fail",
        outputs=["qualification.json"],
        message=(
            f"score {result['score']}/100 (threshold {threshold}), "
            f"category={result['category']}, risk={result['risk']}"
            + ("" if result["qualified"] else f"; missing: {', '.join(result['missing'])}")
        ),
        data=result,
        started_at=started,
    )
    return result
