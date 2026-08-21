"""Feature-completeness stage -- build the pack the final reviewer may see.

Every gate before this one asks a mechanical question: did the tests pass, is
there a hash for each requirement, did the squad reach quorum. None of them asks
the question the person who filed the request actually cares about:

    *Does the evidence we collected show the thing I asked for?*

That question cannot be answered by anyone who has been reading the code, because
knowing how something was built makes it very hard not to grade the build. So the
reviewer for this stage is given a deliberately impoverished view: the original
brief, the requirements derived from it, and *summaries* of the evidence -- kind,
size, digest, caption. No source. No diffs. No agent transcripts or internal
reasoning. No raw traces, HAR or console text.

That restriction is enforced structurally rather than by asking nicely:

* :func:`build_pack` constructs the pack from an allowlist of fields. Nothing is
  copied wholesale, so a field added upstream cannot leak in by accident.
* ``schemas/completeness-pack.schema.json`` sets ``additionalProperties: false``
  at every level, so an unexpected key fails validation instead of shipping.
* :func:`assert_sanitised` scans the serialised pack for the fingerprints of the
  things that must never be in it -- diff headers, shebangs, HTML, Playwright
  calls, headers carrying credentials -- and refuses to write on a hit.
* The workflow that runs the reviewer checks out no source at all, so the code is
  *absent* rather than merely off-limits.

A reviewer that can only see evidence can only judge evidence. That is the point:
its verdict is about whether the run proved what it set out to prove, and it
cannot be argued out of that by an implementation it never saw.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from adlc.config import Config
from adlc.reduce import load_run
from adlc.report.adr import build_adrs
from adlc.runs import RunDir, new_run_id, read_json, utcnow, write_json
from adlc.schemas import is_valid
from adlc.stages.evidence import collect_measurements, extract_requirements
from adlc.stages.persona_feedback import load_feedback
from adlc.summarize import clamp, gate_tldr, requirement_tldr

__all__ = [
    "EXCLUSIONS",
    "LEAK_MARKERS",
    "SanitisationError",
    "assert_sanitised",
    "build_pack",
    "iterate_on_feedback",
    "run_complete",
]

SCHEMA = "completeness-pack"

#: The brief is the reviewer's fixed point. Long enough to carry a real request,
#: bounded so a pasted design doc cannot smuggle a code listing through.
MAX_BRIEF_CHARS = 20_000

#: Declared to the reviewer inside the pack itself. A reviewer that knows the
#: shape of its blindfold can say "I cannot judge that from here" instead of
#: guessing, which is the failure mode of a restricted reviewer that was never
#: told it was restricted.
EXCLUSIONS: tuple[dict[str, str], ...] = (
    {
        "what": "Source code and diffs",
        "why": (
            "Reading the implementation makes it near-impossible to judge the evidence on "
            "its own terms. This review asks whether the run proved what was asked for, "
            "not whether the code looks reasonable."
        ),
    },
    {
        "what": "Agent sessions, transcripts and internal reasoning",
        "why": (
            "An agent's account of its own work is not evidence that the work happened. "
            "Admitting it here would let a persuasive narrative substitute for an artifact."
        ),
    },
    {
        "what": "Raw traces, HAR, console logs and replay scripts",
        "why": (
            "These carry source fragments, URLs and credentials, and they are "
            "attacker-controlled: a prompt-injection payload in a page under test would "
            "otherwise reach the reviewer. Digests and captions carry the same proof value."
        ),
    },
    {
        "what": "Other gates' internal observations",
        "why": (
            "Gate verdicts are included as pass/fail so the reviewer knows what has already "
            "been checked, but not their internals -- this review must not become an appeal "
            "court for checks that already have owners."
        ),
    },
)

#: Fingerprints of content that must never appear in the pack. Matched against
#: the serialised JSON, so a leak anywhere in the structure is caught.
LEAK_MARKERS: tuple[tuple[str, str], ...] = (
    ("diff --git", "a unified diff header"),
    ("@@ -", "a diff hunk header"),
    ("#!/usr/bin/env", "a script shebang"),
    ("<html", "raw HTML"),
    ("<!doctype", "a raw HTML document"),
    ("await page.", "Playwright replay source"),
    ("Set-Cookie:", "an HTTP response header carrying a cookie"),
    ("Authorization:", "an HTTP header carrying credentials"),
    ("-----BEGIN ", "a PEM-encoded key block"),
    ("data:image/", "an inlined binary blob"),
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SanitisationError(RuntimeError):
    """Raised when the pack contains something the reviewer must never see."""


def assert_sanitised(pack: dict[str, Any]) -> None:
    """Fail loudly if the pack carries anything from the exclusion list.

    This is a belt-and-braces check over the allowlist construction in
    :func:`build_pack`. It exists because the cost of the two mechanisms
    disagreeing is that a reviewer silently sees the code, which would
    invalidate every verdict it ever produces without anyone noticing.
    """
    blob = json.dumps(pack, ensure_ascii=False, default=str)
    lowered = blob.lower()
    hits = [
        f"{description} ({marker!r})"
        for marker, description in LEAK_MARKERS
        if marker.lower() in lowered
    ]
    if hits:
        raise SanitisationError(
            "completeness pack contains content the reviewer must never see: "
            + "; ".join(hits)
        )


def _brief(rd: RunDir) -> dict[str, Any]:
    """The original request, verbatim and hashed."""
    if not rd.brief.is_file():
        return {"text": "", "source": "brief.md", "sha256": "", "truncated": False}
    raw = rd.brief.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    truncated = len(raw) > MAX_BRIEF_CHARS
    return {
        "text": raw[:MAX_BRIEF_CHARS],
        "source": "brief.md",
        "sha256": digest,
        "truncated": truncated,
    }


def _caption(path: str, kind: str) -> str:
    """A caption describing the artifact without quoting its contents."""
    name = path.rsplit("/", 1)[-1]
    return clamp(f"{kind.replace('_', ' ')} captured as {name}", 300)


def build_pack(cfg: Config, rd: RunDir, variant: str = "candidate-a") -> dict[str, Any]:
    """Assemble the completeness pack from an allowlist of fields."""
    run = load_run(rd)
    requirements = extract_requirements(rd)
    artifacts = list(run.get("artifacts") or [])
    review_pack = read_json(rd.review_pack) if rd.review_pack.is_file() else {}
    coverage = {c.get("requirementId"): c for c in (review_pack.get("coverage") or [])}

    evidence: list[dict[str, Any]] = []
    for artifact in artifacts:
        digest = str(artifact.get("sha256") or "")
        if not _SHA256.match(digest):
            continue
        kind = str(artifact.get("kind") or "file")
        evidence.append({
            "artifactSha256": digest,
            "kind": kind,
            "mimeType": str(artifact.get("mimeType") or ""),
            "bytes": int(artifact.get("bytes") or 0),
            "caption": _caption(str(artifact.get("path") or ""), kind),
            "redacted": True,
        })

    requirement_view: list[dict[str, Any]] = []
    uncovered: list[str] = []
    for requirement in requirements:
        cover = coverage.get(requirement["id"], {})
        kinds = [str(k) for k in (cover.get("evidenceKinds") or [])]
        hashes = [h for h in (cover.get("artifactSha256") or []) if _SHA256.match(str(h))]
        covered = bool(cover.get("present")) and bool(hashes)
        if not covered:
            uncovered.append(requirement["id"])
        requirement_view.append({
            "id": requirement["id"],
            "text": clamp(requirement["text"], 2000),
            "source": requirement.get("source", ""),
            "tldr": requirement_tldr(requirement["text"], covered, kinds),
            "covered": covered,
            "evidenceKinds": kinds,
            "artifactSha256": hashes,
        })

    personas: list[dict[str, Any]] = []
    for record in load_feedback(rd):
        if record.get("_invalid") or not record.get("verdict"):
            continue
        personas.append({
            "name": str(record.get("name") or ""),
            "role": str(record.get("role") or ""),
            "scenarioId": str(record.get("scenarioId") or ""),
            "verdict": str(record.get("verdict")),
            "simulated": bool(record.get("simulated", True)),
            "tldr": clamp(str(record.get("tldr") or ""), 150),
            # Only the summary of each friction point -- never the step trace,
            # which quotes observations that can carry page content.
            "friction": [
                clamp(str(f.get("summary") or ""), 400)
                for f in (record.get("friction") or [])
            ][:10],
            "artifactSha256": [
                h for h in (record.get("artifactSha256") or []) if _SHA256.match(str(h))
            ][:10],
        })

    decisions = [{
        "number": adr.get("number", ""),
        "title": clamp(adr.get("title", ""), 300),
        "status": adr.get("status", ""),
        "tldr": clamp(adr.get("tldr", ""), 150),
        "chosen": clamp(adr.get("chosen", ""), 300),
        "citationCount": len(adr.get("citations") or []),
    } for adr in build_adrs(cfg)]

    gates = [{
        "id": str(gate.get("id") or ""),
        "status": str(gate.get("status") or "not_run"),
        "required": bool(gate.get("required")),
        "tldr": gate_tldr(gate),
    } for gate in (run.get("gates") or []) if gate.get("id") != "feature_completeness"]

    measurements = [{
        "metricId": m["metricId"],
        "value": m.get("value"),
        "budget": m.get("budget"),
        "passed": bool(m.get("passed")),
        "collector": str(m.get("collector") or ""),
        "artifactSha256": str(m.get("artifactSha256") or ""),
    } for m in collect_measurements(rd, variant)]

    return {
        "runId": rd.run_id,
        "candidateSha": str(run.get("headSha") or run.get("baseSha") or ""),
        "profile": str(run.get("profile") or ""),
        "generatedAt": utcnow(),
        "collector": "adlc.stages.complete",
        "brief": _brief(rd),
        "requirements": requirement_view,
        "evidence": evidence,
        "measurements": measurements,
        "personaFeedback": personas,
        "decisions": decisions,
        "gates": gates,
        "uncovered": uncovered,
        "counts": {
            "requirements": len(requirement_view),
            "covered": len(requirement_view) - len(uncovered),
            "uncovered": len(uncovered),
            "artifacts": len(evidence),
            "personaRecords": len(personas),
            "personaBlocked": sum(1 for p in personas if p["verdict"] == "blocked"),
            "decisions": len(decisions),
        },
        "excluded": [dict(item) for item in EXCLUSIONS],
    }


def run_complete(cfg: Config, rd: RunDir, variant: str = "candidate-a") -> dict[str, Any]:
    """Build, sanitise, validate and write ``completeness-pack.json``.

    Refuses to write on a sanitisation failure. A pack that leaks is worse than
    no pack: the review would still run, still return a verdict, and that verdict
    would carry an independence it no longer has.
    """
    started = utcnow()
    pack = build_pack(cfg, rd, variant)

    leak = ""
    try:
        assert_sanitised(pack)
    except SanitisationError as exc:
        leak = str(exc)

    valid, errors = is_valid(SCHEMA, pack)
    path = rd.path / "completeness-pack.json"

    if leak:
        rd.write_stage(
            "complete", status="fail", outputs=[],
            message=f"refused to write the completeness pack -- {leak}",
            data={"sanitised": False, "packValid": valid, "packErrors": errors[:5]},
            started_at=started,
        )
        return {"pack": None, "valid": False, "sanitised": False, "message": leak}

    write_json(path, pack)
    counts = pack["counts"]
    status = "ok" if valid and counts["requirements"] else "fail"
    if not counts["requirements"]:
        message = (
            "no requirements could be extracted from the spec, so there is nothing "
            "to review completeness against"
        )
    else:
        message = (
            f"{counts['covered']}/{counts['requirements']} requirement(s) backed by "
            f"evidence; {counts['artifacts']} artifact summary(ies), "
            f"{counts['personaRecords']} persona record(s)"
        )
    if not valid:
        message += f"; pack invalid: {errors[:3]}"

    rd.write_stage(
        "complete", status=status, outputs=[rd.rel(path)], message=message,
        data={
            "sanitised": True,
            "packValid": valid,
            "packErrors": errors[:5],
            "uncovered": pack["uncovered"][:20],
            **counts,
        },
        started_at=started,
    )
    return {"pack": pack, "valid": valid, "sanitised": True, "message": message}


# ---------------------------------------------------------------------------
# Outer loop
# ---------------------------------------------------------------------------

#: The gate whose verdict routes this run back into redesign.
GATE_ID = "feature_completeness"

#: What the successor brief says when the gate blocked but nothing survived
#: screening. Naming *why* the section is empty matters: a redesign prompted by
#: "no reason given" should be treated very differently from one prompted by a
#: cited finding, and the next run can only make that distinction if it is told.
_NO_ADMISSIBLE_FINDINGS = (
    "(The gate blocked, but no finding survived citation screening -- every claim "
    "was either uncited or cited a digest absent from the evidence pack. Re-examine "
    "the gate verdict below before redesigning: there may be nothing here to act on.)"
)

#: Any 64-hex token in reviewer prose. Screening removes a fabricated digest from
#: a finding's parsed ``citations``, but the reviewer usually also wrote it into
#: the surrounding sentence, and that copy is quoted verbatim into the successor
#: brief. An invented hash is dangerous because it *looks* checkable, and it looks
#: exactly as checkable in prose as in a citation field -- so both are scrubbed.
_DIGEST_IN_PROSE = re.compile(r"\b[0-9a-fA-F]{64}\b")

#: Kept deliberately conspicuous. A silent deletion would leave a sentence that
#: reads as though it cited something, with the citation quietly missing.
_REDACTED = "[unverifiable digest removed]"


def _redact_unverifiable(text: str, pack_hashes: set[str]) -> str:
    """Replace every 64-hex token in ``text`` that is not a digest in the pack."""
    return _DIGEST_IN_PROSE.sub(
        lambda m: m.group(0) if m.group(0).lower() in pack_hashes else _REDACTED, text
    )


def _gate_result(rd: RunDir) -> dict[str, Any] | None:
    """The recorded ``feature_completeness`` verdict, freshest first."""
    path = rd.gates_dir / f"{GATE_ID}.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            return loaded
    try:
        run = load_run(rd)
    except Exception:  # noqa: BLE001 - a missing run.json is not an error here
        return None
    for gate in run.get("gates") or []:
        if isinstance(gate, dict) and gate.get("id") == GATE_ID:
            return gate
    return None


def _feedback_digest(rd: RunDir) -> tuple[str, list[str], dict[str, Any]]:
    """Readable feedback for the successor brief, the members who filed it, and
    a record of what was screened out.

    Only findings the gate itself found admissible reach the brief, and
    admissibility is decided by re-applying the same two rules the gate applied:

    * An uncited claim was discarded before the vote.
    * A claim citing a digest that is absent from the pack was discarded as
      *fabricated* -- an invented hash is worse than none, because it looks
      checkable.

    Re-applying the second rule here is load-bearing. The gate screens its own
    in-memory ``Review`` objects and that mutation leaves no trace on disk, so
    this function -- which re-reads the reviews from disk -- would otherwise copy
    a fabricated finding straight into the next run's brief. It would then shape
    the redesign as an amendment to the request, having already been ruled
    inadmissible for the vote. That is exactly the influence the screening exists
    to deny it, arriving one run later through a side door.

    A pack that cannot be read means no citation can be checked. The findings are
    still carried, because dropping them silently would tell the successor "the
    gate blocked for no stated reason", but they are carried under an explicit
    warning: a labelled unverified claim is honest, an unlabelled one is not.
    """
    from adlc.adapters.gate.adversarial_review import iter_reviews
    from adlc.adapters.gate.evidence_review import _pack_hashes, _screen_citations

    reviews = iter_reviews(rd.reviews_dir, GATE_ID, citation="artifact-sha256")

    pack: Any = None
    try:
        pack = json.loads((rd.path / "completeness-pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pack = None

    screening: dict[str, Any] = {"packVerified": isinstance(pack, dict), "fabricatedCitations": []}
    pack_hashes: set[str] = set()
    if isinstance(pack, dict):
        pack_hashes = _pack_hashes(pack)
        screening["fabricatedCitations"] = _screen_citations(
            reviews, pack_hashes
        )["fabricatedCitations"]

    lines: list[str] = []
    members: list[str] = []
    if not screening["packVerified"]:
        lines.append(
            "> **Citations unverified.** The completeness pack could not be read, so "
            "the digests below were not checked against it. Treat them as claims, not "
            "as confirmed references."
        )
        lines.append("")

    for review in reviews:
        cited = review.cited_findings
        if not cited:
            continue
        member = review.member or review.path_obj.stem
        members.append(member)
        lines.append(f"### {member} (verdict: {review.verdict})")
        for finding in cited:
            # Only scrub when the pack was readable. With no pack every digest is
            # equally uncheckable, so redacting them all would destroy the
            # findings rather than qualify them -- the warning above does that.
            title = finding.title
            body = " ".join(finding.body.split())
            if screening["packVerified"]:
                title = _redact_unverifiable(title, pack_hashes)
                body = _redact_unverifiable(body, pack_hashes)
            lines.append(f"- **[{finding.severity}] {title}**")
            lines.append(f"  - evidence cited: {', '.join(finding.citations[:4])}")
            if body:
                lines.append(f"  - {body[:600]}")
        lines.append("")

    text = "\n".join(lines).strip()
    return ("" if not members else text), members, screening


def iterate_on_feedback(
    cfg: Config, rd: RunDir, *, iterate: bool = True
) -> dict[str, Any]:
    """Route a failed completeness review back into the **outer** loop.

    The inner loop patches code against a fixed plan. That is the wrong repair
    for this failure: if the evidence does not demonstrate what was asked for,
    the plan itself is what was wrong, and another pass of patching would produce
    more evidence for the same wrong thing. So the successor is a fresh run
    carrying the original brief *plus the reviewers' cited findings*, which is
    the outer loop by definition -- spec, enrich and graph all run again.

    Returns without creating anything when the gate passed, did not run, or when
    ``iterate`` is ``False`` (the caller wants the verdict recorded but not acted
    on -- useful in CI, where creating runs is the orchestrator's job).
    """
    started = utcnow()
    gate = _gate_result(rd)
    status = str((gate or {}).get("status") or "not_run")

    if gate is None or status != "fail":
        rd.write_stage(
            "complete", status="ok", outputs=[],
            message=(
                f"no outer-loop iteration: gate `{GATE_ID}` is {status}"
                if gate is not None
                else f"no outer-loop iteration: gate `{GATE_ID}` has not been evaluated"
            ),
            data={"gateStatus": status, "successorRun": None, "iterated": False},
            started_at=started,
        )
        return {"iterated": False, "successorRun": None, "gateStatus": status}

    feedback, members, screening = _feedback_digest(rd)
    if not iterate:
        rd.write_stage(
            "complete", status="fail", outputs=[],
            message=(
                f"gate `{GATE_ID}` failed and iteration is disabled; the run needs a "
                "redesign pass before it can pass completeness review"
            ),
            data={"gateStatus": status, "successorRun": None, "iterated": False,
                  "members": members, **screening},
            started_at=started,
        )
        return {"iterated": False, "successorRun": None, "gateStatus": status,
                "feedback": feedback, **screening}

    brief = rd.brief.read_text(encoding="utf-8") if rd.brief.is_file() else ""
    successor_id = new_run_id()
    successor = RunDir(cfg, successor_id)
    successor.create(
        profile=cfg.profile,
        brief_text=(
            f"{brief}\n\n---\n\n"
            f"## Feature-completeness review feedback (run {rd.run_id})\n\n"
            "A code-blind reviewer squad compared this brief against the evidence the "
            "run collected and concluded the evidence does not demonstrate the request. "
            "Treat the findings below as amendments to the brief, not as bugs to patch: "
            "the previous run's plan is what failed.\n\n"
            f"{feedback or _NO_ADMISSIBLE_FINDINGS}\n\n"
            f"> Gate verdict: {gate.get('message', '')}\n"
        ),
        references_run=rd.run_id,
    )

    rd.write_stage(
        "complete", status="fail",
        outputs=[f"gates/{GATE_ID}.json"],
        message=(
            f"gate `{GATE_ID}` failed; created successor run {successor_id} "
            f"(route=outer) carrying feedback from {', '.join(members) or 'the squad'}"
        ),
        data={
            "gateStatus": status, "successorRun": successor_id, "iterated": True,
            "route": "outer", "members": members, **screening,
        },
        started_at=started,
    )
    return {
        "iterated": True, "successorRun": successor_id, "gateStatus": status,
        "route": "outer", "feedback": feedback, "members": members, **screening,
    }
