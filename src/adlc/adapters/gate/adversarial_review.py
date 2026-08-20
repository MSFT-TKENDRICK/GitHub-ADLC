"""Adversarial code-review squad gate (L8).

Scores the verdict files written by ``.github/workflows/adlc-adversarial.md``
against the quorum declared in ``.adlc/squads.yaml``.

Two rules do all the work here, and both exist because an LLM verdict is
otherwise unfalsifiable:

* **Citation-or-discard.** A finding that does not cite ``path:Lstart-Lend`` is
  dropped before anything is counted. It cannot block a merge, because nobody
  can check it.
* **Quorum.** One member shouting is not a squad decision. ``quorum: "2/3"``
  means two independent members must each land a *cited*, high-or-critical
  finding before the gate fails.

This module also owns the small amount of parsing that the sibling
``evidence_review`` gate reuses (:func:`load_squads`, :func:`parse_review`,
:func:`iter_reviews`). Both gates are L8-owned, so the shared code lives here
rather than in a third module outside the workstream's exclusive paths.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.ports import GateResult

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = [
    "AdversarialReviewGate",
    "Finding",
    "Review",
    "SquadConfig",
    "count_quorum",
    "find_squads_file",
    "iter_reviews",
    "load_squads",
    "parse_review",
    "quorum_threshold",
]

#: Where a squad configuration may live, relative to the repo root, in order.
SQUADS_CANDIDATES: tuple[tuple[str, ...], ...] = (
    (".adlc", "squads.yaml"),
    ("templates", ".adlc", "squads.yaml"),
)

#: Used when a squads file exists but omits a squad, so the gate still has a
#: defensible rule instead of silently passing.
BUILTIN_SQUADS: dict[str, dict[str, Any]] = {
    "adversarial_review": {
        "blocking": True,
        "quorum": "2/3",
        "citation": "file-line",
        "members": [
            {"id": "security-adversary"},
            {"id": "performance-adversary"},
            {"id": "accessibility-adversary"},
        ],
    },
    "evidence_review": {
        "blocking": False,
        "quorum": "1/1",
        "citation": "artifact-sha256",
        "members": [{"id": "requirements-auditor"}],
    },
}

BUILTIN_DEFAULTS: dict[str, Any] = {
    "citationPolicy": "discard-uncited",
    "blockingSeverities": ["high", "critical"],
    "abstainCountsAsPass": False,
}

_FRONTMATTER_RE = re.compile(r"\A\ufeff?---\s*\n(?P<yaml>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_FINDING_RE = re.compile(r"^\s{0,3}#{2,4}\s*\[(?P<sev>[A-Za-z]+)\]\s*(?P<title>.+?)\s*$", re.MULTILINE)

#: ``src/app.ts:L12-L20``, ``src/app.ts:L12-20`` or ``src/app.ts:L12``.
#: A bare path is deliberately NOT a citation -- "this file is bad" is not
#: evidence, and accepting it would make the discard rule cosmetic.
FILE_LINE_CITATION_RE = re.compile(
    r"(?P<path>[\w./@+-]+\.[A-Za-z0-9_]{1,12}):L(?P<start>\d+)(?:\s*-\s*L?(?P<end>\d+))?"
)

#: A bare 64-hex digest, as produced by ``sha256``.
ARTIFACT_SHA_RE = re.compile(r"\b(?P<sha>[a-f0-9]{64})\b")

VALID_VERDICTS = ("block", "warn", "pass", "abstain")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One ``## [severity] title`` block inside a review body."""

    severity: str
    title: str
    body: str
    citations: tuple[str, ...]

    @property
    def cited(self) -> bool:
        return bool(self.citations)


@dataclass
class Review:
    """One squad member's verdict file."""

    path: str
    squad: str = ""
    member: str = ""
    verdict: str = "abstain"
    run_id: str = ""
    reviewed_sha: str = ""
    findings: list[Finding] = field(default_factory=list)
    parse_error: str = ""

    @property
    def cited_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.cited]

    @property
    def uncited_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.cited]

    def blocking_findings(self, blocking_severities: tuple[str, ...]) -> list[Finding]:
        """Cited findings whose severity counts as a blocking vote."""
        return [f for f in self.cited_findings if f.severity in blocking_severities]


def _safe_yaml_load(text: str) -> dict[str, Any]:
    try:
        import yaml

        loaded = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - malformed review must never crash the gate
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _citation_re(citation_kind: str) -> re.Pattern[str]:
    return ARTIFACT_SHA_RE if citation_kind == "artifact-sha256" else FILE_LINE_CITATION_RE


def parse_review(path: Path, citation: str = "file-line") -> Review:
    """Parse one ``reviews/*.md`` verdict file. Never raises.

    ``citation`` selects which citation shape counts as evidence:
    ``"file-line"`` for code review, ``"artifact-sha256"`` for evidence review.
    """
    review = Review(path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        review.parse_error = f"unreadable: {exc}"
        return review

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        review.parse_error = "no YAML frontmatter"
        return review

    meta = _safe_yaml_load(match.group("yaml"))
    review.squad = str(meta.get("squad", "") or "")
    review.member = str(meta.get("member", "") or "")
    review.run_id = str(meta.get("runId", "") or "")
    review.reviewed_sha = str(meta.get("reviewedSha", "") or "")

    verdict = str(meta.get("verdict", "") or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        review.parse_error = f"invalid verdict {verdict!r}"
        review.verdict = "abstain"
    else:
        review.verdict = verdict

    body = text[match.end():]
    pattern = _citation_re(citation)
    headings = list(_FINDING_RE.finditer(body))
    for idx, head in enumerate(headings):
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(body)
        block = body[head.end():end]
        review.findings.append(
            Finding(
                severity=head.group("sev").strip().lower(),
                title=head.group("title").strip(),
                body=block.strip(),
                citations=tuple(dict.fromkeys(m.group(0) for m in pattern.finditer(block))),
            )
        )
    return review


def iter_reviews(reviews_dir: Path, squad: str, citation: str = "file-line") -> list[Review]:
    """Parse every ``*.md`` under ``reviews_dir`` that belongs to ``squad``.

    A file whose frontmatter names a different squad is ignored, so both gates
    can share one directory.
    """
    if not reviews_dir.is_dir():
        return []
    out: list[Review] = []
    for path in sorted(reviews_dir.glob("*.md")):
        review = parse_review(path, citation=citation)
        if review.squad and review.squad != squad:
            continue
        if not review.squad and not path.name.startswith(f"{squad}."):
            continue
        out.append(review)
    return out


# ---------------------------------------------------------------------------
# Squad configuration
# ---------------------------------------------------------------------------


@dataclass
class SquadConfig:
    """Resolved configuration for one squad."""

    squad_id: str
    source: str
    blocking: bool
    quorum: str
    citation: str
    members: tuple[str, ...]
    blocking_severities: tuple[str, ...]
    abstain_counts_as_pass: bool
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def threshold(self) -> int:
        return quorum_threshold(self.quorum, len(self.members))


def find_squads_file(cfg: Config) -> Path | None:
    """Cheapest possible probe: two ``Path.is_file()`` calls. Never raises."""
    try:
        root = Path(cfg.root)
    except Exception:  # noqa: BLE001
        return None
    for parts in SQUADS_CANDIDATES:
        candidate = root.joinpath(*parts)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def quorum_threshold(quorum: Any, member_count: int) -> int:
    """Turn a quorum expression into an absolute number of blocking votes.

    * ``"2/3"`` with 3 members -> ``2`` (the common case, read literally).
    * ``"2/3"`` with 6 members -> ``4`` (scaled, so adding members does not
      quietly make the squad easier to satisfy).
    * ``2`` -> ``2``.
    * ``"all"`` -> ``member_count``; ``"any"`` -> ``1``.

    Always clamped to ``1..member_count`` so a misconfigured file can never
    produce an unreachable or a zero threshold.
    """
    count = max(member_count, 1)
    value: int
    if isinstance(quorum, bool):  # bool is an int subclass; reject it explicitly
        value = count
    elif isinstance(quorum, int):
        value = quorum
    else:
        text = str(quorum or "").strip().lower()
        if text in ("all", "unanimous"):
            value = count
        elif text in ("any", "one"):
            value = 1
        elif "/" in text:
            num, _, den = text.partition("/")
            try:
                numerator, denominator = int(num), int(den)
            except ValueError:
                return count
            if denominator <= 0:
                return count
            value = numerator if denominator == count else math.ceil(numerator / denominator * count)
        else:
            try:
                value = int(text)
            except ValueError:
                return count
    return max(1, min(value, count))


def load_squads(cfg: Config, squad_id: str) -> SquadConfig:
    """Resolve one squad's configuration. Never raises."""
    raw: dict[str, Any] = {}
    source = "built-in defaults"
    path = find_squads_file(cfg)
    if path is not None:
        try:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
                source = str(path)
        except Exception:  # noqa: BLE001 - a broken squads file falls back, never crashes
            source = f"{path} (unparseable; using built-in defaults)"

    defaults = {**BUILTIN_DEFAULTS, **(raw.get("defaults") or {})}
    squads = raw.get("squads") or {}
    spec = squads.get(squad_id) if isinstance(squads, dict) else None
    if not isinstance(spec, dict):
        spec = BUILTIN_SQUADS.get(squad_id, {})
        if path is not None:
            source = f"{source} (no `squads.{squad_id}`; using built-in defaults)"

    members: list[str] = []
    for entry in spec.get("members") or []:
        if isinstance(entry, dict):
            member_id = str(entry.get("id") or entry.get("name") or "").strip()
        else:
            member_id = str(entry).strip()
        if member_id:
            members.append(member_id)
    if not members:
        members = [str(m["id"]) for m in BUILTIN_SQUADS.get(squad_id, {}).get("members", [])]

    severities = defaults.get("blockingSeverities") or BUILTIN_DEFAULTS["blockingSeverities"]

    return SquadConfig(
        squad_id=squad_id,
        source=source,
        blocking=bool(spec.get("blocking", BUILTIN_SQUADS.get(squad_id, {}).get("blocking", False))),
        quorum=str(spec.get("quorum", BUILTIN_SQUADS.get(squad_id, {}).get("quorum", "all"))),
        citation=str(spec.get("citation", BUILTIN_SQUADS.get(squad_id, {}).get("citation", "file-line"))),
        members=tuple(members),
        blocking_severities=tuple(str(s).lower() for s in severities),
        abstain_counts_as_pass=bool(defaults.get("abstainCountsAsPass", False)),
        coverage=dict(spec.get("coverage") or {}),
    )


def count_quorum(reviews: list[Review], squad: SquadConfig) -> dict[str, Any]:
    """Apply citation-or-discard, then count blocking votes.

    A member only casts a blocking vote when it *both* declared
    ``verdict: block`` *and* filed at least one cited finding at a blocking
    severity. A ``block`` verdict backed by nothing checkable is downgraded to
    ``pass`` and recorded in ``discarded``.
    """
    by_member: dict[str, Review] = {}
    for review in reviews:
        key = review.member or Path(review.path).stem
        by_member.setdefault(key, review)

    blocking_votes: list[str] = []
    unsupported: list[str] = []
    discarded: list[dict[str, str]] = []
    abstained: list[str] = []
    parse_errors: list[dict[str, str]] = []

    for member, review in sorted(by_member.items()):
        if review.parse_error:
            parse_errors.append({"member": member, "path": review.path, "error": review.parse_error})
        for finding in review.uncited_findings:
            discarded.append(
                {"member": member, "severity": finding.severity, "title": finding.title,
                 "reason": f"no {squad.citation} citation"}
            )
        if review.verdict == "abstain":
            abstained.append(member)
            continue
        if review.verdict in ("block", "warn"):
            if review.blocking_findings(squad.blocking_severities):
                blocking_votes.append(member)
            else:
                unsupported.append(member)

    missing = [m for m in squad.members if m not in by_member]
    return {
        "reviewsFound": len(by_member),
        "membersConfigured": list(squad.members),
        "membersMissing": missing,
        "blockingVotes": blocking_votes,
        "unsupportedBlockVerdicts": unsupported,
        "abstained": abstained,
        "discardedFindings": discarded,
        "parseErrors": parse_errors,
        "quorum": squad.quorum,
        "quorumThreshold": squad.threshold,
        "quorumMet": len(blocking_votes) >= squad.threshold,
    }


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class AdversarialReviewGate:
    """Scores the adversarial code-review squad against its quorum."""

    id = "adversarial_review"
    name = "adversarial-review"
    kind = "gate"
    required_by_default = False

    squad_id = "adversarial_review"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        path = find_squads_file(cfg)
        if path is None:
            searched = ", ".join("/".join(p) for p in SQUADS_CANDIDATES)
            return False, f"no squad configuration found (searched {searched})"
        return True, f"squad configuration at {path}"

    def evaluate(self, run: dict[str, Any], cfg: Config) -> GateResult:
        required = cfg.is_required(self.id)
        run_id = str((run or {}).get("runId") or "")
        expected = {
            "quorum": "declared in squads.yaml",
            "citation": "every finding cites path:Lstart-Lend",
        }

        if not run_id:
            return self._not_run(required, expected, "run has no runId, so no reviews directory can be located")

        squad = load_squads(cfg, self.squad_id)
        expected = {
            "quorum": squad.quorum,
            "quorumThreshold": squad.threshold,
            "members": list(squad.members),
            "blockingSeverities": list(squad.blocking_severities),
            "citation": squad.citation,
            "source": squad.source,
        }

        reviews_dir = cfg.run_dir(run_id) / "reviews"
        reviews = iter_reviews(reviews_dir, self.squad_id, citation=squad.citation)
        if not reviews:
            return self._not_run(
                required,
                expected,
                f"no {self.squad_id} verdict files in {reviews_dir}; "
                "the adlc-adversarial workflow did not run or produced nothing",
                observed={"reviewsDir": str(reviews_dir), "reviewsFound": 0},
            )

        tally = count_quorum(reviews, squad)
        tally["reviewsDir"] = str(reviews_dir)
        tally["blocking"] = squad.blocking
        evidence = [str(Path(r.path).as_posix()) for r in reviews]

        if squad.blocking and tally["quorumMet"]:
            return {
                "id": self.id,
                "required": required,
                "status": "fail",
                "severity": "high",
                "observed": tally,
                "expected": expected,
                "message": (
                    f"adversarial squad quorum met: {len(tally['blockingVotes'])} of "
                    f"{squad.threshold} required blocking votes "
                    f"({', '.join(tally['blockingVotes'])}) filed cited "
                    f"{'/'.join(squad.blocking_severities)} findings"
                ),
                "evidence": evidence,
            }

        if tally["quorumMet"]:
            message = (
                f"adversarial squad quorum met ({len(tally['blockingVotes'])}/{squad.threshold}) but the "
                "squad is configured non-blocking; recorded as a warning"
            )
        else:
            parts = [
                f"{len(tally['blockingVotes'])} of {squad.threshold} required blocking votes",
            ]
            if tally["unsupportedBlockVerdicts"]:
                parts.append(
                    f"{len(tally['unsupportedBlockVerdicts'])} block verdict(s) downgraded for lack of a "
                    "cited high/critical finding"
                )
            if tally["discardedFindings"]:
                parts.append(f"{len(tally['discardedFindings'])} uncited finding(s) discarded")
            if tally["membersMissing"]:
                parts.append(f"no verdict from {', '.join(tally['membersMissing'])}")
            message = "adversarial squad does not block: " + "; ".join(parts)

        return {
            "id": self.id,
            "required": required,
            "status": "pass",
            "severity": "medium" if tally["quorumMet"] or tally["membersMissing"] else "low",
            "observed": tally,
            "expected": expected,
            "message": message,
            "evidence": evidence,
        }

    def _not_run(
        self,
        required: bool,
        expected: dict[str, Any],
        reason: str,
        observed: dict[str, Any] | None = None,
    ) -> GateResult:
        return {
            "id": self.id,
            "required": required,
            "status": "not_run",
            "severity": "medium",
            "observed": observed or {},
            "expected": expected,
            "message": reason,
            "evidence": [],
        }
