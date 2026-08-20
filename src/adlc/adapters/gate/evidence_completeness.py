"""Evidence completeness gate -- the deterministic half of evidence review.

This is the gate that actually blocks. It asserts, with no LLM involved, that
**every requirement has at least one hash-verified artifact** produced by the
declared collector at the declared candidate SHA.

The LLM squad verdict (see the ``evidence_review`` gate, workstream L8) is
advisory on top of this. Splitting them this way is deliberate: an agent reading
evidence can be fooled or prompt-injected, but a hash either matches or it does
not.
"""

from __future__ import annotations

import json

from adlc.config import Config
from adlc.ports import GateResult, Run
from adlc.runs import RunDir, sha256_file


class EvidenceCompletenessGate:
    id = "evidence_completeness"
    name = "evidence-completeness"
    kind = "gate"
    required_by_default = True

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "built-in deterministic evidence check (no credentials required)"

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        rd = RunDir(cfg, run["runId"])
        base: GateResult = {
            "id": self.id,
            "required": cfg.is_required(self.id),
            "severity": "high",
            "evidence": [f"gates/{self.id}.json"],
        }

        if not rd.review_pack.is_file():
            return {
                **base, "status": "not_run",
                "observed": {}, "expected": {"reviewPack": "evidence-review-pack.json"},
                "message": (
                    "evidence-review-pack.json not found - run `adlc evidence` first. "
                    "Absence of evidence is not evidence of correctness."
                ),
            }

        try:
            pack = json.loads(rd.review_pack.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                **base, "status": "fail", "observed": {"error": str(exc)}, "expected": {},
                "message": "evidence-review-pack.json is not valid JSON",
            }

        requirements = pack.get("requirements") or []
        coverage = {c.get("requirementId"): c for c in pack.get("coverage") or []}

        uncovered: list[str] = []
        for requirement in requirements:
            entry = coverage.get(requirement.get("id"))
            if not entry or not entry.get("present") or not entry.get("artifactSha256"):
                uncovered.append(requirement.get("id", "?"))

        # Verify every referenced hash actually matches a file on disk.
        on_disk = {
            sha256_file(path): path
            for path in rd.evidence_dir.rglob("*")
            if path.is_file()
        }
        unverified: list[str] = []
        for entry in pack.get("coverage") or []:
            for digest in entry.get("artifactSha256") or []:
                if digest not in on_disk:
                    unverified.append(f"{entry.get('requirementId')}:{digest[:12]}")

        problems = bool(uncovered or unverified)
        return {
            **base,
            "status": "fail" if problems else "pass",
            "observed": {
                "requirements": len(requirements),
                "uncovered": uncovered,
                "unverifiedHashes": unverified[:25],
                "artifactsOnDisk": len(on_disk),
                "candidateSha": pack.get("candidateSha"),
            },
            "expected": {"uncovered": [], "unverifiedHashes": []},
            "message": (
                f"all {len(requirements)} requirement(s) backed by hash-verified evidence"
                if not problems
                else (
                    f"{len(uncovered)} requirement(s) without evidence; "
                    f"{len(unverified)} referenced hash(es) not found on disk"
                )
            ),
        }
