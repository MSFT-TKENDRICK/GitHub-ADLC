"""Persona feedback -- what the personas did, and what they were thinking.

``enrich_personas`` mines *who* the users are from the spec. This stage records
*what happened when they tried it*, one JSON record per persona per scenario,
under ``evidence/personas/`` so :meth:`RunDir.scan_artifacts` hashes each one as
a first-class artifact. That placement is the whole design: persona feedback is
evidence, so it must be hash-addressable, citable by a reviewer, and impossible
to edit after the fact without the digest changing.

Two things make these records honest rather than decorative:

* **Every record carries ``simulated``.** A deterministic walkthrough derived
  from the spec and the captured evidence is genuinely useful -- it is a
  structured reading of what the evidence does and does not show -- but it is not
  a human sitting in front of the product. Conflating the two would be the single
  most damaging thing this module could do, so the flag is required by the schema
  and set from the code path that produced the record, never from a caller's
  argument.
* **Verdicts are derived from signals, not invented.** "Blocked" means no
  artifact covers the requirement. "Partial" means a measurement tied to it
  missed its budget. "Confused" means evidence exists but nothing visual proves
  the user could see the outcome. Each is a fact about the run restated in the
  persona's voice, and each names the signal it came from.

Real feedback -- from moderated testing, a research tool, or an agent driving the
UI -- is first class here too: drop a conforming JSON file into
``evidence/personas/`` with ``simulated: false`` and :func:`load_feedback` reads
it alongside the generated ones. Generation never overwrites an ingested record.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.runs import RunDir, utcnow, write_json
from adlc.schemas import is_valid
from adlc.stages.enrich_personas import extract_personas
from adlc.stages.evidence import collect_measurements, extract_requirements
from adlc.summarize import clamp, persona_tldr

log = logging.getLogger(__name__)

__all__ = [
    "SCHEMA",
    "build_feedback",
    "load_feedback",
    "personas_dir",
    "run_persona_feedback",
]

SCHEMA = "persona-feedback"

#: Kinds that let a persona actually *see* an outcome. Everything else proves
#: the machine did something, not that a human could tell.
_VISUAL_KINDS = ("screenshot", "video")

_SLUG = re.compile(r"[^a-z0-9]+")


def personas_dir(rd: RunDir) -> Path:
    return rd.evidence_dir / "personas"


def _slug(text: str) -> str:
    return _SLUG.sub("-", str(text or "").lower()).strip("-") or "persona"


def _first_sentence(text: str, limit: int = 160) -> str:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    parts = re.split(r"(?<=[.!?])\s", body)
    return clamp(parts[0] if parts else body, limit)


def _goal_for(persona: dict[str, Any], requirement: dict[str, str]) -> str:
    """The persona's own stated goal, if any of them mention this requirement."""
    words = {w for w in re.findall(r"[a-z]{5,}", requirement["text"].lower())}
    best, score = "", 0
    for goal in persona.get("goals") or []:
        overlap = len(words & set(re.findall(r"[a-z]{5,}", goal.lower())))
        if overlap > score:
            best, score = goal, overlap
    return best if score >= 2 else (persona.get("goals") or [""])[0]


def _walkthrough(
    persona: dict[str, Any],
    requirement: dict[str, str],
    covering: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    kinds: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str, float, list[dict[str, Any]]]:
    """Build the step trace, verdict, sentiment and friction for one scenario.

    The trace is written from the persona's point of view but every claim in it
    is a restatement of something on disk: which artifact kinds exist, which
    measurement missed its budget, which accessibility need the persona declared.

    ``kinds`` is the coverage map's vocabulary for this requirement, which is what
    the evidence gate scores and what a reviewer reads. It is preferred over the
    file-extension kinds from ``scan_artifacts`` so that the persona pane and the
    evidence pane cannot describe the same artifact differently -- an internal
    contradiction reads as carelessness even when both statements are true.
    """
    kinds = sorted(set(kinds or [])) or sorted({str(a.get("kind") or "file") for a in covering})
    visual = [k for k in kinds if k in _VISUAL_KINDS]
    goal = _goal_for(persona, requirement)
    a11y = (persona.get("accessibility") or [""])[0]
    friction: list[dict[str, Any]] = []

    steps: list[dict[str, Any]] = [
        {
            "index": 0,
            "observation": f"Arrived wanting to {_first_sentence(goal or requirement['text'])}",
            "thought": (
                f"This is the outcome {requirement['id']} promises me. "
                f"I am judging it on that, not on how it was built."
            ),
            "action": f"Started the flow described by {requirement['id']}.",
            "outcome": (
                f"The run captured {len(covering)} artifact(s) for this step."
                if covering
                else "Nothing was captured for this step."
            ),
            "confidence": 0.9,
        }
    ]

    if not covering:
        verdict, sentiment = "blocked", -0.7
        steps.append({
            "index": 1,
            "observation": f"No evidence at all was captured for {requirement['id']}.",
            "thought": (
                "If nobody recorded me doing this, I have no way to show it worked. "
                "An untested promise is a promise I cannot rely on."
            ),
            "action": "Stopped and reported it rather than assuming success.",
            "outcome": "The requirement is unproven for this run.",
            "confidence": 1.0,
        })
        friction.append({
            "summary": (
                f"{requirement['id']} has no artifact behind it, so this persona's "
                f"journey cannot be shown to end successfully."
            ),
            "severity": "high",
            "requirementId": requirement["id"],
        })
    elif failed:
        verdict, sentiment = "partial", -0.3
        worst = failed[0]
        steps.append({
            "index": 1,
            "observation": (
                f"Reached the end, but {worst['metricId']} measured "
                f"{worst.get('value')} against a budget of {worst.get('budget')}."
            ),
            "thought": (
                "It finished, so technically I got what I asked for -- but slow enough "
                "or heavy enough that I noticed. That is the part I would complain about."
            ),
            "action": "Completed the task and made a note of where it dragged.",
            "outcome": f"Goal met; {len(failed)} measurement(s) outside budget.",
            "confidence": 0.8,
        })
        for measurement in failed[:3]:
            friction.append({
                "summary": (
                    f"{measurement['metricId']} came in at {measurement.get('value')} "
                    f"against a budget of {measurement.get('budget')}."
                ),
                "severity": "medium",
                "stepIndex": 1,
                "requirementId": requirement["id"],
            })
    elif not visual:
        verdict, sentiment = "confused", -0.1
        steps.append({
            "index": 1,
            "observation": (
                f"Evidence exists ({', '.join(kinds)}), but nothing shows me the screen."
            ),
            "thought": (
                "Logs tell me the system did something. They do not tell me whether I "
                "could see that it worked. I would have to guess."
            ),
            "action": "Carried on, unsure whether the result was the intended one.",
            "outcome": "Completed without visual confirmation.",
            "confidence": 0.5,
        })
        friction.append({
            "summary": (
                f"{requirement['id']} is covered only by non-visual evidence "
                f"({', '.join(kinds)}), so nothing proves the outcome was legible to a user."
            ),
            "severity": "medium",
            "stepIndex": 1,
            "requirementId": requirement["id"],
        })
    else:
        verdict, sentiment = "satisfied", 0.6
        steps.append({
            "index": 1,
            "observation": f"Saw the result confirmed on screen via {', '.join(visual)}.",
            "thought": (
                "I can see the outcome I was promised, so I would trust this and move on."
            ),
            "action": "Finished the task.",
            "outcome": "Goal met, with a visual record of it.",
            "confidence": 0.9,
        })

    if a11y:
        steps.append({
            "index": len(steps),
            "observation": f"My stated access need: {_first_sentence(a11y, 200)}",
            "thought": (
                "Whether that need was met is not something the captured evidence "
                "answers on its own -- check it against the accessibility gate."
            ),
            "action": "Flagged it for the accessibility reviewer.",
            "outcome": "Deferred to the accessibility gate.",
            "confidence": 0.4,
        })

    return steps, verdict, sentiment, friction


def _coverage_map(rd: RunDir) -> dict[str, dict[str, Any]]:
    """The review pack's requirement -> evidence mapping, keyed by requirement id.

    Deliberately reused rather than re-derived. The pack's coverage heuristic is
    coarse, but it is the one the ``evidence_completeness`` gate scores and the
    one a reviewer will cite. A second, differently-coarse heuristic here would
    let a persona record claim evidence the gate does not agree it has -- which is
    exactly the ungrounded-claim failure the review squad exists to catch.
    """
    if not rd.review_pack.is_file():
        return {}
    try:
        pack = json.loads(rd.review_pack.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(pack, dict):
        return {}
    return {
        str(entry.get("requirementId")): entry
        for entry in (pack.get("coverage") or [])
        if isinstance(entry, dict) and entry.get("requirementId")
    }


def build_feedback(rd: RunDir, variant: str = "candidate-a") -> list[dict[str, Any]]:
    """Generate one record per persona per requirement they own.

    Personas are matched to requirements by the acceptance-criteria ids their own
    story block declared, which is how ``enrich_personas`` already grounds them.
    A persona that owns no criteria walks every requirement instead -- they are
    still a user of the feature, and a persona with nothing to say is a sign the
    spec forgot to connect them to an outcome, not a reason to drop them.
    """
    spec = rd.spec_dir / "spec.md"
    spec_text = spec.read_text(encoding="utf-8", errors="replace") if spec.is_file() else ""
    personas, _ = extract_personas(spec_text)
    if not personas:
        return []

    requirements = extract_requirements(rd)
    if not requirements:
        return []

    coverage = _coverage_map(rd)
    by_sha = {
        a["sha256"]: a
        for a in rd.scan_artifacts()
        if a.get("kind") != "persona_feedback"
    }
    measurements = collect_measurements(rd, variant)
    failed_measurements = [m for m in measurements if not m.get("passed")]

    records: list[dict[str, Any]] = []
    for persona in personas:
        owned = [r for r in requirements if r["id"] in (persona.get("criteria") or [])]
        for requirement in owned or requirements:
            entry = coverage.get(requirement["id"], {})
            covering = [
                by_sha[sha] for sha in (entry.get("artifactSha256") or []) if sha in by_sha
            ]
            steps, verdict, sentiment, friction = _walkthrough(
                persona, requirement, covering, failed_measurements,
                kinds=[str(k) for k in (entry.get("evidenceKinds") or [])],
            )
            record = {
                "personaId": _slug(persona["role"]),
                "name": persona["name"],
                "role": persona["role"],
                "proficiency": _first_sentence(persona.get("proficiency", ""), 200),
                "scenarioId": requirement["id"],
                "scenarioText": _first_sentence(requirement["text"], 400),
                "tldr": persona_tldr(
                    persona["name"], persona["role"], verdict, len(friction),
                    scenario=requirement["id"],
                ),
                "verdict": verdict,
                "sentiment": sentiment,
                "simulated": True,
                "source": "adlc.stages.persona_feedback (deterministic, offline)",
                "recordedAt": utcnow(),
                "steps": steps,
                "friction": friction,
                "quotes": [q[:400] for q in (persona.get("quotes") or [])[:3]],
                "accessibility": (persona.get("accessibility") or [])[:3],
                "artifactSha256": [
                    a["sha256"] for a in covering[:10]
                    if re.fullmatch(r"[a-f0-9]{64}", a["sha256"])
                ],
            }
            records.append(record)
    return records


def load_feedback(rd: RunDir) -> list[dict[str, Any]]:
    """Read every persona record on disk, newest schema check applied.

    An invalid record is surfaced with its errors rather than dropped. A reader
    who sees nothing cannot tell "no personas ran" from "the records were
    malformed", and those call for opposite responses.
    """
    out: list[dict[str, Any]] = []
    directory = personas_dir(rd)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out.append({
                "personaId": path.stem, "name": path.stem, "role": "", "scenarioId": "",
                "verdict": "blocked", "simulated": True, "steps": [],
                "_invalid": [f"unreadable: {exc}"], "_path": rd.rel(path),
            })
            continue
        valid, errors = is_valid(SCHEMA, payload)
        payload["_path"] = rd.rel(path)
        if not valid:
            payload["_invalid"] = errors[:5]
        out.append(payload)
    return out


def run_persona_feedback(
    cfg: Config, rd: RunDir, variant: str = "candidate-a"
) -> dict[str, Any]:
    """Write generated persona records, then report on everything on disk.

    Ingested (``simulated: false``) records are never overwritten: a real human's
    session outranks anything this module can derive, and losing one to a rerun
    would be unrecoverable.
    """
    started = utcnow()
    directory = personas_dir(rd)
    directory.mkdir(parents=True, exist_ok=True)

    protected = {
        path.name
        for path in directory.glob("*.json")
        if _is_real(path)
    }

    written: list[str] = []
    invalid: list[str] = []
    for record in build_feedback(rd, variant):
        name = f"{record['personaId']}--{_slug(record['scenarioId'])}.json"
        if name in protected:
            continue
        valid, errors = is_valid(SCHEMA, record)
        if not valid:
            invalid.append(f"{name}: {errors[:2]}")
            continue
        write_json(directory / name, record)
        written.append(rd.rel(directory / name))

    everything = load_feedback(rd)
    verdicts: dict[str, int] = {}
    friction_total = 0
    for record in everything:
        verdicts[str(record.get("verdict"))] = verdicts.get(str(record.get("verdict")), 0) + 1
        friction_total += len(record.get("friction") or [])

    real = sum(1 for r in everything if r.get("simulated") is False)
    status = "ok" if everything and not invalid else ("skipped" if not everything else "fail")
    message = (
        f"{len(everything)} persona record(s) ({real} from real sessions, "
        f"{len(everything) - real} simulated); {friction_total} friction point(s)"
        if everything
        else "no personas could be grounded in the spec - none written"
    )
    if invalid:
        message += f"; {len(invalid)} invalid record(s)"

    rd.write_stage(
        "persona_feedback",
        status=status,
        outputs=written,
        message=message,
        data={
            "variant": variant,
            "written": len(written),
            "onDisk": len(everything),
            "realSessions": real,
            "verdicts": verdicts,
            "frictionPoints": friction_total,
            "invalid": invalid[:5],
        },
        started_at=started,
    )
    return {"records": everything, "written": written, "invalid": invalid}


def _is_real(path: Path) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("simulated") is False
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
