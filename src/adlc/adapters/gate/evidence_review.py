"""Evidence-review squad gate (L8).

This gate has two halves, and only one of them is allowed to fail a build.

**The blocking half is deterministic.** Every requirement in
``evidence-review-pack.json`` must have at least one artifact hash that
(a) is declared present in ``coverage[]``, (b) matches a ``sha256`` actually
recorded in ``run.json``'s ``artifacts[]``, and (c) belongs to a pack whose
``candidateSha`` equals the run's ``headSha`` and whose ``collector`` is
declared. No language model is involved in that decision, so it is reproducible
and arguable.

**The advisory half is the LLM squad**, whose verdicts arrive as
``runs/<run>/reviews/evidence_review.*.md``. Its power is deliberately capped:
a squad verdict can downgrade a passing coverage result to a *warning*, and
nothing more. It can never turn a green build red, because an LLM judgement is
not a fact — and it can never turn a red build green, because coverage is
evaluated first and independently.

Claims that cite no ``artifactSha256`` present in the pack are discarded before
the verdict is counted, exactly as in the adversarial squad.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.adapters.gate.adversarial_review import (
    ARTIFACT_SHA_RE,
    SQUADS_CANDIDATES,
    Review,
    SquadConfig,
    count_quorum,
    find_squads_file,
    iter_reviews,
    load_squads,
)
from adlc.ports import GateResult

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = ["CoverageReport", "EvidenceReviewGate", "check_coverage", "load_pack"]

PACK_FILENAME = "evidence-review-pack.json"


class CoverageReport(dict):
    """Deterministic coverage result. A plain dict so it drops into ``observed``."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


def load_pack(run_dir: Path) -> tuple[dict[str, Any] | None, str]:
    """Read ``evidence-review-pack.json``. Returns ``(pack, reason)``; never raises."""
    path = run_dir / PACK_FILENAME
    if not path.is_file():
        return None, f"{path} not found"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable or not valid JSON: {exc}"
    if not isinstance(loaded, dict):
        return None, f"{path} does not contain a JSON object"
    return loaded, str(path)


def check_coverage(
    pack: dict[str, Any],
    run: dict[str, Any],
    rules: dict[str, Any] | None = None,
) -> CoverageReport:
    """The blocking half. Deterministic, offline, no LLM.

    Every requirement must be covered by at least ``minArtifactsPerRequirement``
    artifact hashes that are hash-verified against ``run['artifacts']``.
    """
    rules = rules or {}
    min_artifacts = int(rules.get("minArtifactsPerRequirement", 1) or 1)
    require_sha_match = bool(rules.get("requireShaMatch", True))
    require_hash_verification = bool(rules.get("requireHashVerification", True))

    known_hashes = {
        str(a.get("sha256", "")).lower()
        for a in (run.get("artifacts") or [])
        if isinstance(a, dict) and a.get("sha256")
    }

    requirements = [r for r in (pack.get("requirements") or []) if isinstance(r, dict)]
    coverage_rows = [c for c in (pack.get("coverage") or []) if isinstance(c, dict)]
    by_requirement: dict[str, dict[str, Any]] = {}
    for row in coverage_rows:
        req_id = str(row.get("requirementId") or "")
        if req_id:
            by_requirement.setdefault(req_id, row)

    collector = str(pack.get("collector") or "").strip()
    candidate_sha = str(pack.get("candidateSha") or "").strip()
    head_sha = str(run.get("headSha") or "").strip()

    problems: list[dict[str, str]] = []
    satisfied: list[str] = []

    if not collector:
        problems.append({"scope": "pack", "reason": "pack declares no collector"})
    if require_sha_match and head_sha and candidate_sha and candidate_sha != head_sha:
        problems.append(
            {
                "scope": "pack",
                "reason": f"pack candidateSha {candidate_sha} does not match run headSha {head_sha}",
            }
        )
    if require_sha_match and not candidate_sha:
        problems.append({"scope": "pack", "reason": "pack declares no candidateSha"})

    for requirement in requirements:
        req_id = str(requirement.get("id") or "")
        if not req_id:
            problems.append({"scope": "requirement", "reason": "requirement with no id"})
            continue
        row = by_requirement.get(req_id)
        if row is None:
            problems.append({"scope": req_id, "reason": "no coverage entry"})
            continue
        if not row.get("present"):
            problems.append({"scope": req_id, "reason": "coverage entry marked present: false"})
            continue

        hashes = [str(h).lower() for h in (row.get("artifactSha256") or []) if isinstance(h, str)]
        malformed = [h for h in hashes if not ARTIFACT_SHA_RE.fullmatch(h)]
        if malformed:
            problems.append(
                {"scope": req_id, "reason": f"malformed artifactSha256: {', '.join(sorted(malformed))}"}
            )
        well_formed = [h for h in hashes if h not in malformed]

        if require_hash_verification:
            verified = [h for h in well_formed if h in known_hashes]
            unverified = [h for h in well_formed if h not in known_hashes]
            if unverified:
                problems.append(
                    {
                        "scope": req_id,
                        "reason": (
                            "artifactSha256 not present in run.json artifacts[]: "
                            + ", ".join(sorted(unverified))
                        ),
                    }
                )
        else:
            verified = well_formed

        if len(verified) < min_artifacts:
            problems.append(
                {
                    "scope": req_id,
                    "reason": (
                        f"{len(verified)} hash-verified artifact(s), {min_artifacts} required"
                    ),
                }
            )
            continue
        satisfied.append(req_id)

    orphans = sorted(set(by_requirement) - {str(r.get("id") or "") for r in requirements})
    for orphan in orphans:
        problems.append({"scope": orphan, "reason": "coverage entry for an unknown requirementId"})

    return CoverageReport(
        ok=not problems and bool(requirements),
        collector=collector,
        candidateSha=candidate_sha,
        headSha=head_sha,
        requirements=len(requirements),
        requirementsSatisfied=sorted(satisfied),
        requirementsFailed=sorted({p["scope"] for p in problems if p["scope"] not in ("pack", "requirement")}),
        minArtifactsPerRequirement=min_artifacts,
        knownArtifactHashes=len(known_hashes),
        problems=problems,
    )


def _pack_hashes(pack: dict[str, Any]) -> set[str]:
    """Every artifactSha256 that legitimately appears anywhere in the pack."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "artifactSha256":
                    if isinstance(value, str):
                        found.add(value.lower())
                    elif isinstance(value, list):
                        found.update(str(v).lower() for v in value if isinstance(v, str))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(pack)
    return found


def _screen_citations(reviews: list[Review], pack_hashes: set[str]) -> dict[str, Any]:
    """Drop findings whose cited hashes are not in the pack.

    A hallucinated 64-hex digest is *worse* than no citation, because it looks
    checkable. So a finding survives only if at least one of its citations is a
    hash that genuinely appears in the pack.
    """
    fabricated: list[dict[str, str]] = []
    for review in reviews:
        kept = []
        for finding in review.findings:
            real = tuple(c for c in finding.citations if c.lower() in pack_hashes)
            invented = [c for c in finding.citations if c.lower() not in pack_hashes]
            if invented:
                fabricated.append(
                    {
                        "member": review.member or Path(review.path).stem,
                        "title": finding.title,
                        "hashes": ", ".join(sorted(invented)),
                    }
                )
            kept.append(
                type(finding)(
                    severity=finding.severity,
                    title=finding.title,
                    body=finding.body,
                    citations=real,
                )
            )
        review.findings = kept
    return {"fabricatedCitations": fabricated}


class EvidenceReviewGate:
    """Deterministic coverage blocks; the LLM squad can only warn."""

    id = "evidence_review"
    name = "evidence-review"
    kind = "gate"
    required_by_default = False

    squad_id = "evidence_review"

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

        if not run_id:
            return self._result(
                required,
                "not_run",
                "high",
                {},
                {"pack": PACK_FILENAME},
                "run has no runId, so the evidence review pack cannot be located",
            )

        squad: SquadConfig = load_squads(cfg, self.squad_id)
        rules = squad.coverage or {}
        run_dir = cfg.run_dir(run_id)
        expected: dict[str, Any] = {
            "blocking": "every requirement has >= 1 hash-verified artifact from the declared collector at the declared SHA",
            "advisory": f"LLM squad quorum {squad.quorum} may downgrade to warn, never to fail",
            "members": list(squad.members),
            "citation": squad.citation,
            "source": squad.source,
            "minArtifactsPerRequirement": int(rules.get("minArtifactsPerRequirement", 1) or 1),
        }

        pack, pack_reason = load_pack(run_dir)
        if pack is None:
            return self._result(
                required,
                "not_run",
                "high",
                {"packPath": str(run_dir / PACK_FILENAME)},
                expected,
                f"evidence review pack unavailable: {pack_reason}",
            )

        coverage = check_coverage(pack, run or {}, rules)

        # --- blocking half -------------------------------------------------
        if not coverage.ok:
            reasons = "; ".join(f"{p['scope']}: {p['reason']}" for p in coverage["problems"][:6])
            more = len(coverage["problems"]) - 6
            if more > 0:
                reasons += f"; (+{more} more)"
            if not coverage["requirements"]:
                reasons = "pack declares no requirements, so nothing could be verified"
            return self._result(
                required,
                "fail",
                "high",
                dict(coverage),
                expected,
                f"deterministic evidence coverage failed -- {reasons}",
                evidence=[str((run_dir / PACK_FILENAME).as_posix())],
            )

        # --- advisory half -------------------------------------------------
        reviews_dir = run_dir / "reviews"
        reviews = iter_reviews(reviews_dir, self.squad_id, citation=squad.citation)
        observed: dict[str, Any] = dict(coverage)
        observed["reviewsDir"] = str(reviews_dir)
        evidence = [str((run_dir / PACK_FILENAME).as_posix())]

        if not reviews:
            observed["advisory"] = {
                "verdict": "not_run",
                "reason": (
                    f"no {self.squad_id} verdict files in {reviews_dir}; "
                    "the adlc-evidence-review workflow did not run or produced nothing"
                ),
            }
            return self._result(
                required,
                "pass",
                "low",
                observed,
                expected,
                (
                    f"evidence coverage verified for {len(coverage['requirementsSatisfied'])} requirement(s) "
                    f"from collector {coverage['collector']}; advisory squad did not run "
                    f"(no verdict files in {reviews_dir})"
                ),
                evidence=evidence,
            )

        evidence.extend(str(Path(r.path).as_posix()) for r in reviews)
        screening = _screen_citations(reviews, _pack_hashes(pack))
        tally = count_quorum(reviews, squad)
        tally.update(screening)
        observed["advisory"] = tally

        if tally["quorumMet"]:
            return self._result(
                required,
                "pass",
                "medium",
                observed,
                expected,
                (
                    f"WARN: evidence coverage is complete, but the advisory squad reached quorum "
                    f"({len(tally['blockingVotes'])}/{tally['quorumThreshold']}) on cited concerns from "
                    f"{', '.join(tally['blockingVotes'])}. Advisory only -- coverage is the blocking check."
                ),
                evidence=evidence,
            )

        notes = []
        if tally["unsupportedBlockVerdicts"]:
            notes.append(
                f"{len(tally['unsupportedBlockVerdicts'])} verdict(s) downgraded for lack of a cited concern"
            )
        if tally["discardedFindings"]:
            notes.append(f"{len(tally['discardedFindings'])} uncited claim(s) discarded")
        if screening["fabricatedCitations"]:
            notes.append(
                f"{len(screening['fabricatedCitations'])} claim(s) cited a hash absent from the pack"
            )
        suffix = f" ({'; '.join(notes)})" if notes else ""
        return self._result(
            required,
            "pass",
            "low",
            observed,
            expected,
            (
                f"evidence coverage verified for {len(coverage['requirementsSatisfied'])} requirement(s) "
                f"from collector {coverage['collector']}; advisory squad raised no quorum concern{suffix}"
            ),
            evidence=evidence,
        )

    def _result(
        self,
        required: bool,
        status: str,
        severity: str,
        observed: dict[str, Any],
        expected: dict[str, Any],
        message: str,
        evidence: list[str] | None = None,
    ) -> GateResult:
        return {
            "id": self.id,
            "required": required,
            "status": status,  # type: ignore[typeddict-item]
            "severity": severity,  # type: ignore[typeddict-item]
            "observed": observed,
            "expected": expected,
            "message": message,
            "evidence": evidence or [],
        }
