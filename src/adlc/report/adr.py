"""ADR parsing for the report's decision detail views.

A decision record is only useful if you can see *what informed it*. A list of
titles tells a reader that a choice was made; it does not let them check whether
the choice was reasonable. So this module pulls two things out of each MADR
document:

* **The sections** -- context, drivers, options, outcome, consequences,
  confirmation -- so a decision can be read in place rather than by opening a
  separate file that may not have shipped with the report.
* **A classified citation index** -- every link, ADR cross-reference, file path,
  artifact digest, requirement id and run id the record mentions. These become
  the links/citations pane: the set of things a reader can follow to audit the
  decision. Classifying them (rather than dumping a list of URLs) is what lets
  the UI turn an artifact digest into a jump to that artifact and a requirement
  id into a jump to that requirement, instead of rendering dead text.

Citations are extracted, never invented. If a record cites nothing, the pane says
so -- an unsourced decision is a finding, and hiding that behind an empty section
would be the wrong kind of tidy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.stages.adr import list_adrs
from adlc.summarize import adr_tldr, clamp

__all__ = ["build_adrs", "parse_adr", "parse_citations"]

_FRONT_MATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^(?P<key>[A-Za-z][\w-]*):\s*(?P<value>.*)$", re.MULTILINE)
_SECTION = re.compile(r"^#{2,4}\s+(?P<title>.+?)\s*$", re.MULTILINE)
_CHOSEN = re.compile(
    r"Chosen option:\s*[\"“']?(?P<chosen>[^\"”'\n]+)[\"”']?\s*(?:,\s*because\s*(?P<why>.+))?",
    re.IGNORECASE,
)

_MD_LINK = re.compile(r"\[(?P<text>[^\]]{1,120})\]\((?P<href>[^)\s]{1,400})\)")
_BARE_URL = re.compile(r"(?<![(\[<])\bhttps?://[^\s<>)\]]{4,400}")
_ADR_REF = re.compile(r"\bADR[- ]?(\d{3,4})\b", re.IGNORECASE)
_DECISION_PATH = re.compile(r"docs/decisions/(\d{4})-[\w.-]+\.md")
_SHA256 = re.compile(r"\b[a-f0-9]{64}\b")
_REQUIREMENT = re.compile(r"\b(?:US\d{1,3}-)?(?:AC|FR|NFR|SC|TR|UC)-?\d{1,3}\b")
_RUN_REF = re.compile(r"`?\.adlc/runs/([\w.-]+)`?")
_CODE_PATH = re.compile(r"`(?P<path>(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,6})(?::L?\d+(?:-L?\d+)?)?`")

#: Sections we surface as first-class fields. Anything else is kept under
#: ``other`` rather than dropped -- a maintainer who added a section meant it.
_KNOWN_SECTIONS = {
    "context and problem statement": "context",
    "decision drivers": "drivers",
    "considered options": "options",
    "decision outcome": "outcome",
    "consequences": "consequences",
    "confirmation": "confirmation",
    "more information": "moreInfo",
    "pros and cons of the options": "prosAndCons",
}


def _front_matter(text: str) -> dict[str, str]:
    match = _FRONT_MATTER.search(text)
    if not match:
        return {}
    return {
        m.group("key").strip().lower(): m.group("value").strip()
        for m in _FIELD.finditer(match.group("body"))
    }


def _sections(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    body = _FRONT_MATTER.sub("", text)
    marks = list(_SECTION.finditer(body))
    known: dict[str, str] = {}
    other: list[dict[str, str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        title = mark.group("title").strip()
        content = body[mark.end():end].strip()
        key = _KNOWN_SECTIONS.get(title.lower())
        if key:
            known[key] = content
        elif content:
            other.append({"title": title, "body": content})
    return known, other


def _bullets(block: str) -> list[str]:
    out: list[str] = []
    for line in (block or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("* ", "- ", "+ ")):
            item = stripped[2:].strip()
        elif re.match(r"^\d+[.)]\s+", stripped):
            item = re.sub(r"^\d+[.)]\s+", "", stripped)
        else:
            continue
        if item and item.strip("_* ") not in {"To be completed.", "None."}:
            out.append(item)
    return out


def _prose(block: str) -> str:
    """The non-list prose of a section, with list items removed.

    Bullets are surfaced separately so they render as a list rather than as one
    run-on paragraph; what is left here is the sentences around them.
    """
    kept = [
        line.strip()
        for line in (block or "").splitlines()
        if line.strip() and not line.strip().startswith(("* ", "- ", "+ "))
        and not re.match(r"^\d+[.)]\s+", line.strip())
    ]
    return " ".join(kept).strip()


def parse_citations(text: str, *, adr_numbers: set[str] | None = None) -> list[dict[str, str]]:
    """Everything this record points at, classified and de-duplicated.

    Ordered by kind so the pane groups naturally, and de-duplicated on
    ``(kind, ref)`` because the same URL cited twice is still one source.
    """
    found: dict[tuple[str, str], dict[str, str]] = {}

    def add(kind: str, ref: str, label: str = "") -> None:
        ref = ref.strip().rstrip(".,;)")
        if not ref:
            return
        found.setdefault((kind, ref), {
            "kind": kind, "ref": ref, "label": clamp(label or ref, 120),
        })

    for match in _MD_LINK.finditer(text):
        href, label = match.group("href"), match.group("text")
        if href.startswith(("http://", "https://")):
            add("web", href, label)
        elif href.startswith("#"):
            add("anchor", href, label)
        else:
            add("file", href.split("#", 1)[0], label)

    for match in _BARE_URL.finditer(text):
        add("web", match.group(0))
    for match in _DECISION_PATH.finditer(text):
        add("adr", match.group(1), f"ADR {match.group(1)}")
    for match in _ADR_REF.finditer(text):
        number = match.group(1).zfill(4)
        if adr_numbers is None or number in adr_numbers:
            add("adr", number, f"ADR {number}")
    for match in _SHA256.finditer(text):
        add("artifact", match.group(0), f"artifact {match.group(0)[:12]}...")
    for match in _REQUIREMENT.finditer(text):
        add("requirement", match.group(0))
    for match in _RUN_REF.finditer(text):
        add("run", match.group(1))
    for match in _CODE_PATH.finditer(text):
        add("file", match.group("path"))

    order = {
        "requirement": 0, "artifact": 1, "adr": 2, "file": 3, "web": 4, "run": 5, "anchor": 6,
    }
    return sorted(found.values(), key=lambda c: (order.get(c["kind"], 9), c["ref"]))


def _task_refs(value: str) -> list[str]:
    """Task ids from an ``adlc-tasks`` front-matter field.

    Tolerant of the two shapes a human or a generator will produce: a bare
    comma-separated list and a YAML flow sequence.
    """
    cleaned = (value or "").strip().strip("[]")
    if cleaned.lower() in {"", "n/a", "none"}:
        return []
    out: list[str] = []
    for part in re.split(r"[,\s]+", cleaned):
        item = part.strip().strip("\"'")
        if item and item not in out:
            out.append(item)
    return out


def parse_adr(path: Path, text: str, *, adr_numbers: set[str] | None = None) -> dict[str, Any]:
    """Turn one MADR document into the detail view's data."""
    meta = _front_matter(text)
    known, other = _sections(text)
    title_match = re.search(r"^#\s+(.+)$", _FRONT_MATTER.sub("", text), re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem

    outcome = known.get("outcome", "")
    chosen_match = _CHOSEN.search(outcome)
    chosen = (chosen_match.group("chosen").strip() if chosen_match else "")
    why = (chosen_match.group("why") or "").strip() if chosen_match else ""

    status = meta.get("status", "unknown")
    return {
        "number": path.name[:4],
        "slug": path.stem,
        "path": f"docs/decisions/{path.name}",
        "title": title,
        "status": status,
        "tldr": adr_tldr(title, status, chosen),
        "date": meta.get("date", ""),
        "decisionMakers": meta.get("decision-makers", ""),
        "consulted": meta.get("consulted", ""),
        "informed": meta.get("informed", ""),
        "runId": meta.get("adlc-run", ""),
        "reviewSha": meta.get("adlc-review-sha", ""),
        "taskRefs": _task_refs(meta.get("adlc-tasks", "")),
        "context": known.get("context", ""),
        "drivers": _bullets(known.get("drivers", "")),
        "options": _bullets(known.get("options", "")),
        "chosen": chosen,
        "justification": why,
        "outcome": outcome,
        "consequences": _bullets(known.get("consequences", "")),
        "confirmation": known.get("confirmation", ""),
        "moreInfo": known.get("moreInfo", ""),
        "sections": [
            item for item in (
                {
                    "title": section["title"],
                    "body": _prose(section["body"]),
                    "bullets": _bullets(section["body"]),
                }
                for section in other
            ) if item["body"] or item["bullets"]
        ],
        "citations": parse_citations(text, adr_numbers=adr_numbers),
    }


def build_adrs(cfg: Config, graph: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every ADR, parsed, with the task nodes that reference it attached.

    The node linkage is what answers "where was this decided?" -- a decision
    detached from the work it governs is trivia. It is resolved from *both*
    directions, because either side may be the one that knows: a graph node can
    carry ``adrRefs``, and a record authored after the graph carries
    ``adlc-tasks``. In practice the second is the common case, since the plan is
    drawn before the decision is taken.
    """
    records = list_adrs(cfg)
    numbers = {adr.number for adr in records}

    node_titles: dict[str, str] = {}
    by_adr: dict[str, list[dict[str, str]]] = {}
    for node in (graph or {}).get("nodes") or []:
        node_id = str(node.get("id", ""))
        node_titles[node_id] = node.get("title", "")
        for ref in node.get("adrRefs") or []:
            key = re.sub(r"\D", "", str(ref)).zfill(4)
            by_adr.setdefault(key, []).append({"id": node_id, "title": node.get("title", "")})

    out: list[dict[str, Any]] = []
    for adr in records:
        try:
            text = adr.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append({
                "number": adr.number, "slug": adr.path.stem,
                "path": f"docs/decisions/{adr.path.name}", "title": adr.title,
                "status": adr.status, "tldr": f"Unreadable on disk: {exc}",
                "citations": [], "nodes": [], "taskRefs": [], "error": str(exc),
            })
            continue
        parsed = parse_adr(adr.path, text, adr_numbers=numbers)

        linked = list(by_adr.get(adr.number, []))
        seen = {n["id"] for n in linked}
        # A record may name a task that is not in this run's graph -- a decision
        # can outlive the plan that prompted it. Keep it, flagged, rather than
        # dropping a reference the author deliberately wrote down.
        for task_id in parsed["taskRefs"]:
            if task_id in seen:
                continue
            seen.add(task_id)
            linked.append({
                "id": task_id,
                "title": node_titles.get(task_id, ""),
                "inGraph": task_id in node_titles,
            })
        for node in linked:
            node.setdefault("inGraph", True)

        parsed["nodes"] = linked
        out.append(parsed)
    return out
