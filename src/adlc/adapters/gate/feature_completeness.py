"""Feature-completeness gate (L8) -- the last question, asked from outside.

Every other gate checks something *about the change*: do the tests pass, is there
a hash behind each requirement, did an adversary find a hole. This one checks
something about the **run**: having done all of that, did we actually demonstrate
the thing that was asked for?

Three properties make that question worth asking separately.

**It is answered by someone who never saw the code.** The reviewer's only input is
``completeness-pack.json`` (see :mod:`adlc.stages.complete`), and the workflow that
runs it checks out no source. A reviewer that has read the implementation grades
the implementation; a reviewer that has only read the brief and the evidence
grades the evidence, which is the thing being claimed.

**It blocks.** This is a deliberate departure from :mod:`~adlc.adapters.gate.evidence_review`,
which is advisory because a deterministic hash check already sits underneath it
and owns the blocking decision. There is no deterministic check for "does this
evidence actually show what the brief asked for" -- it is a judgement, and if the
judgement cannot stop the run then it is a comment, not a gate. A blocking verdict
here routes the run back into the **outer loop**, where the design is revisited,
rather than the inner loop, where the code is patched: if the evidence does not
answer the brief, patching the code is guessing.

**It cannot block on a hunch.** The same falsifiability rules apply as everywhere
else in L8, plus one more:

* citation-or-discard -- a finding with no ``artifactSha256`` is dropped before
  the vote,
* fabrication screening -- a cited digest that does not appear in the pack is
  dropped too, because an invented hash is worse than no hash: it looks checkable,
* quorum -- one reviewer's opinion is not a verdict.

And it fails closed. If the pack is missing, or the squad never filed, the result
is ``not_run``, which the aggregate treats as a failure when the gate is required.
"We could not check whether we built the right thing" is not "we built the right
thing".
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from adlc.adapters.gate.adversarial_review import (
    SQUADS_CANDIDATES,
    SquadConfig,
    count_quorum,
    find_squads_file,
    iter_reviews,
    load_squads,
)

# Reused rather than re-implemented: the screening rule must be identical for
# both evidence-facing squads, or a claim that one gate discards would survive in
# the other and the guarantee would depend on which gate happened to read it.
from adlc.adapters.gate.evidence_review import _pack_hashes, _screen_citations
from adlc.ports import GateResult, Run
from adlc.runs import RunDir

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = ["PACK_NAME", "FeatureCompletenessGate"]

#: Written by the `complete` stage into the run directory root.
PACK_NAME = "completeness-pack.json"


class FeatureCompletenessGate:
    """Blocking review of the brief against the evidence, by a code-blind squad."""

    id = "feature_completeness"
    name = "feature-completeness"
    kind = "gate"
    required_by_default = False

    squad_id = "feature_completeness"

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
                "message": "run has no runId, so the completeness pack cannot be located",
            }

        squad: SquadConfig = load_squads(cfg, self.squad_id)
        rd = RunDir(cfg, run_id)
        pack_path = rd.path / PACK_NAME
        expected: dict[str, Any] = {
            "blocking": (
                f"quorum {squad.quorum} of the {self.squad_id} squad on cited findings "
                "=> FAIL, routed to the outer loop for redesign"
            ),
            "input": (
                f"{rd.rel(pack_path)} only -- no source, no diffs, no agent sessions, "
                "no raw traces"
            ),
            "members": list(squad.members),
            "citation": squad.citation,
            "source": squad.source,
        }

        pack, note = self._load_pack(pack_path, rd)
        observed: dict[str, Any] = {
            "pack": rd.rel(pack_path),
            "reviewsDir": rd.rel(rd.reviews_dir),
        }

        if pack is None:
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": {**observed, "packNote": note}, "expected": expected,
                "message": (
                    f"no completeness pack to review ({note}). Run `adlc complete` first -- "
                    "without it there is nothing for a code-blind reviewer to read."
                ),
            }

        counts = pack.get("counts") or {}
        observed.update({
            "requirements": counts.get("requirements", 0),
            "covered": counts.get("covered", 0),
            "uncovered": counts.get("uncovered", 0),
            "artifacts": counts.get("artifacts", 0),
            "personaRecords": counts.get("personaRecords", 0),
            "excludedFromReview": [str(e.get("what", "")) for e in (pack.get("excluded") or [])],
        })

        if not counts.get("requirements"):
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": observed, "expected": expected,
                "message": (
                    "the completeness pack declares no requirements, so there is no "
                    "statement of intent to review the evidence against"
                ),
            }

        pack_run_id = str(pack.get("runId") or "")
        if pack_run_id and pack_run_id != run_id:
            observed["packRunId"] = pack_run_id
            return {
                **base, "status": "fail", "severity": "high",
                "observed": observed, "expected": expected,
                "message": (
                    f"the completeness pack belongs to run {pack_run_id}, not {run_id} -- "
                    "a review of another run's evidence says nothing about this one"
                ),
            }

        reviews = iter_reviews(rd.reviews_dir, self.squad_id, citation=squad.citation)
        if not reviews:
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": observed, "expected": expected,
                "message": (
                    f"no {self.squad_id} verdict files in {rd.rel(rd.reviews_dir)} -- the "
                    "adlc-feature-completeness workflow did not run or produced nothing. "
                    "Nobody has confirmed the evidence answers the brief."
                ),
            }

        evidence = [*base["evidence"], rd.rel(pack_path), *(rd.rel(r.path_obj) for r in reviews)]
        screening = _screen_citations(reviews, _pack_hashes(pack))
        tally = count_quorum(reviews, squad)
        tally.update(screening)
        observed["review"] = tally

        if tally["quorumMet"]:
            voters = ", ".join(tally["blockingVotes"])
            titles = [
                f.title
                for review in reviews
                for f in review.blocking_findings(squad.blocking_severities)
            ][:5]
            return {
                **base, "status": "fail", "severity": "high",
                "observed": observed, "expected": expected, "evidence": evidence,
                "message": (
                    f"the evidence does not demonstrate the request: quorum "
                    f"({len(tally['blockingVotes'])}/{tally['quorumThreshold']}) from {voters} "
                    f"on cited findings -- {'; '.join(titles) or 'see review files'}. "
                    "This is an outer-loop failure: revisit the design and the evidence "
                    "plan, not just the implementation."
                ),
            }

        notes: list[str] = []
        if tally["membersMissing"]:
            notes.append(f"{len(tally['membersMissing'])} member(s) filed nothing")
        if tally["unsupportedBlockVerdicts"]:
            notes.append(
                f"{len(tally['unsupportedBlockVerdicts'])} verdict(s) downgraded for lack of a "
                "cited concern"
            )
        if tally["discardedFindings"]:
            notes.append(f"{len(tally['discardedFindings'])} uncited claim(s) discarded")
        if screening["fabricatedCitations"]:
            notes.append(
                f"{len(screening['fabricatedCitations'])} claim(s) cited a hash absent from "
                "the pack"
            )
        suffix = f" ({'; '.join(notes)})" if notes else ""

        # A missing member is not a pass. If the squad could not muster a quorum
        # because nobody showed up, say so as `not_run` rather than reporting a
        # clean bill of health nobody actually signed.
        if len(tally["membersMissing"]) >= max(1, squad.threshold):
            return {
                **base, "status": "not_run", "severity": "high",
                "observed": observed, "expected": expected, "evidence": evidence,
                "message": (
                    f"quorum is unreachable -- {len(tally['membersMissing'])} of "
                    f"{len(squad.members)} member(s) filed no verdict "
                    f"({', '.join(tally['membersMissing'])}){suffix}"
                ),
            }

        return {
            **base, "status": "pass", "severity": "low",
            "observed": observed, "expected": expected, "evidence": evidence,
            "message": (
                f"{counts.get('covered', 0)}/{counts.get('requirements', 0)} requirement(s) "
                f"backed by evidence; the code-blind squad raised no quorum concern that the "
                f"evidence fails to demonstrate the request{suffix}"
            ),
        }

    @staticmethod
    def _load_pack(pack_path: Any, rd: RunDir) -> tuple[dict[str, Any] | None, str]:
        """Load ``completeness-pack.json``. Never raises."""
        if not pack_path.is_file():
            return None, f"{rd.rel(pack_path)} not found"
        try:
            loaded = json.loads(pack_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"{rd.rel(pack_path)} is unreadable or not valid JSON: {exc}"
        if not isinstance(loaded, dict):
            return None, f"{rd.rel(pack_path)} does not contain a JSON object"
        return loaded, ""
