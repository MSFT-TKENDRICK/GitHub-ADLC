"""Evidence-review squad gate (L8) -- the **advisory** half of evidence review.

The deterministic half already exists and already blocks: the spine's
``evidence_completeness`` gate verifies that every requirement has at least one
hash-verified artifact from the declared collector at the declared candidate
SHA. That logic is **not** duplicated here. This gate *delegates* to it by
reading its recorded result, and layers the LLM squad verdict on top.

The split is the whole point, and the direction of the arrow matters:

* ``evidence_completeness`` is a hash comparison. It either matches or it does
  not, and no amount of clever text can change that. It blocks.
* This gate is a language model reading a sanitised pack. It can be wrong, and
  it can be argued with. So its power is capped: it may downgrade a passing
  deterministic result to a **warning**, and nothing more.

  - It can never turn green red, because an LLM judgement is not a fact.
  - It can never turn red green, because the deterministic precondition is read
    first and this gate returns before the squad verdicts are even loaded.

Claims citing an ``artifactSha256`` that does not appear in the pack are
discarded before the verdict is counted -- a fabricated digest is worse than no
citation, because it looks checkable.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from adlc.adapters.gate.adversarial_review import (
    SQUADS_CANDIDATES,
    Review,
    SquadConfig,
    count_quorum,
    find_squads_file,
    iter_reviews,
    load_squads,
)
from adlc.ports import GateResult, Run
from adlc.runs import RunDir

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = ["DETERMINISTIC_GATE_ID", "EvidenceReviewGate", "read_precondition"]

#: The spine gate that owns the deterministic coverage check. Single source of
#: truth -- see ``adlc.adapters.gate.evidence_completeness``.
DETERMINISTIC_GATE_ID = "evidence_completeness"


def read_precondition(rd: RunDir, run: Run) -> GateResult | None:
    """Return the recorded ``evidence_completeness`` result, or ``None``.

    Prefers the gate file on disk (freshest -- written by ``run_gates`` in the
    same invocation) and falls back to the reduced ``run.json``. Never raises.
    """
    path = rd.gates_dir / f"{DETERMINISTIC_GATE_ID}.json"
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded  # type: ignore[return-value]
    except (OSError, ValueError):
        pass
    for gate in (run or {}).get("gates") or []:
        if isinstance(gate, dict) and gate.get("id") == DETERMINISTIC_GATE_ID:
            return gate  # type: ignore[return-value]
    return None


def _pack_hashes(pack: dict[str, Any]) -> set[str]:
    """Every ``artifactSha256`` that legitimately appears anywhere in the pack."""
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
    """Drop cited hashes that are not in the pack.

    A hallucinated 64-hex digest is *worse* than no citation, because it looks
    checkable. A finding survives only if at least one of its citations is a
    digest that genuinely appears in the pack.
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
                        "member": review.member or "?",
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
    """Advisory squad verdict layered on the deterministic coverage check."""

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

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        required = cfg.is_required(self.id)
        run_id = str((run or {}).get("runId") or "")
        base: dict[str, Any] = {
            "id": self.id,
            "required": required,
            "evidence": [f"gates/{self.id}.json"],
        }

        if not run_id:
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": {}, "expected": {"runId": "required to locate the run directory"},
                "message": "run has no runId, so the review directory cannot be located",
            }

        squad: SquadConfig = load_squads(cfg, self.squad_id)
        rd = RunDir(cfg, run_id)
        expected: dict[str, Any] = {
            "blocking": (
                f"delegated to gate `{DETERMINISTIC_GATE_ID}`: every requirement has >= 1 "
                "hash-verified artifact from the declared collector at the declared SHA"
            ),
            "advisory": f"LLM squad quorum {squad.quorum} may downgrade to warn, never to fail",
            "members": list(squad.members),
            "citation": squad.citation,
            "source": squad.source,
        }

        # -- blocking half: delegated, never recomputed ---------------------
        precondition = read_precondition(rd, run)
        if precondition is None:
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": {"precondition": DETERMINISTIC_GATE_ID, "preconditionStatus": None},
                "expected": expected,
                "message": (
                    f"deterministic evidence coverage has not been evaluated -- no "
                    f"gates/{DETERMINISTIC_GATE_ID}.json. Run the `{DETERMINISTIC_GATE_ID}` "
                    "gate first; an advisory review of unverified evidence means nothing."
                ),
            }

        precondition_status = precondition.get("status")
        observed: dict[str, Any] = {
            "precondition": DETERMINISTIC_GATE_ID,
            "preconditionStatus": precondition_status,
            "preconditionMessage": precondition.get("message", ""),
            "preconditionObserved": precondition.get("observed", {}),
            "reviewsDir": rd.rel(rd.reviews_dir),
        }

        if precondition_status == "not_run":
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": observed, "expected": expected,
                "message": (
                    f"deterministic evidence coverage did not run (gate "
                    f"`{DETERMINISTIC_GATE_ID}`: {precondition.get('message', 'no reason given')})"
                ),
            }

        if precondition_status != "pass":
            # Fail closed, and point at the gate that actually owns the check so
            # nobody debugs this one instead.
            return {
                **base, "status": "fail", "severity": "high",
                "observed": observed, "expected": expected,
                "message": (
                    f"deterministic evidence coverage failed -- see gate "
                    f"`{DETERMINISTIC_GATE_ID}`: {precondition.get('message', 'no reason given')}. "
                    "Advisory review is not meaningful without hash-verified evidence."
                ),
            }

        # -- advisory half: capped at `warn` --------------------------------
        reviews = iter_reviews(rd.reviews_dir, self.squad_id, citation=squad.citation)
        if not reviews:
            observed["advisory"] = {
                "verdict": "not_run",
                "reason": (
                    f"no {self.squad_id} verdict files in {rd.rel(rd.reviews_dir)}; "
                    "the adlc-evidence-review workflow did not run or produced nothing"
                ),
            }
            return {
                **base, "status": "pass", "severity": "low",
                "observed": observed, "expected": expected,
                "message": (
                    f"deterministic evidence coverage passed (gate `{DETERMINISTIC_GATE_ID}`); "
                    f"advisory squad did not run (no verdict files in {rd.rel(rd.reviews_dir)})"
                ),
            }

        evidence = [*base["evidence"], *(rd.rel(r.path_obj) for r in reviews)]

        pack, pack_note = self._load_pack(rd)
        if pack is None:
            observed["packNote"] = pack_note
            observed["advisory"] = {
                "verdict": "not_run",
                "reason": f"citations cannot be screened: {pack_note}",
            }
            return {
                **base, "status": "pass", "severity": "medium",
                "observed": observed, "expected": expected, "evidence": evidence,
                "message": (
                    "deterministic evidence coverage passed, but the advisory verdict was "
                    f"discarded because its citations could not be screened ({pack_note})"
                ),
            }

        screening = _screen_citations(reviews, _pack_hashes(pack))
        tally = count_quorum(reviews, squad)
        tally.update(screening)
        observed["advisory"] = tally

        if tally["quorumMet"]:
            return {
                **base, "status": "pass", "severity": "medium",
                "observed": observed, "expected": expected, "evidence": evidence,
                "message": (
                    "WARN: deterministic evidence coverage passed, but the advisory squad "
                    f"reached quorum ({len(tally['blockingVotes'])}/{tally['quorumThreshold']}) "
                    f"on cited concerns from {', '.join(tally['blockingVotes'])}. Advisory only "
                    f"-- gate `{DETERMINISTIC_GATE_ID}` is the blocking check."
                ),
            }

        notes = []
        if tally["unsupportedBlockVerdicts"]:
            notes.append(
                f"{len(tally['unsupportedBlockVerdicts'])} verdict(s) downgraded for lack of a "
                "cited concern"
            )
        if tally["discardedFindings"]:
            notes.append(f"{len(tally['discardedFindings'])} uncited claim(s) discarded")
        if screening["fabricatedCitations"]:
            notes.append(
                f"{len(screening['fabricatedCitations'])} claim(s) cited a hash absent from the pack"
            )
        suffix = f" ({'; '.join(notes)})" if notes else ""
        return {
            **base, "status": "pass", "severity": "low",
            "observed": observed, "expected": expected, "evidence": evidence,
            "message": (
                f"deterministic evidence coverage passed (gate `{DETERMINISTIC_GATE_ID}`); "
                f"advisory squad raised no quorum concern{suffix}"
            ),
        }

    @staticmethod
    def _load_pack(rd: RunDir) -> tuple[dict[str, Any] | None, str]:
        """Load the pack for **citation screening only**. Never raises.

        This is not the coverage check -- that belongs to
        ``evidence_completeness``. The pack is read here purely to confirm that
        the digests a reviewer cited actually exist.
        """
        if not rd.review_pack.is_file():
            return None, f"{rd.rel(rd.review_pack)} not found"
        try:
            loaded = json.loads(rd.review_pack.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"{rd.rel(rd.review_pack)} is unreadable or not valid JSON: {exc}"
        if not isinstance(loaded, dict):
            return None, f"{rd.rel(rd.review_pack)} does not contain a JSON object"
        return loaded, ""
