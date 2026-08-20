"""Persona enrichment (leaf L9).

Renders ``<run_dir>/enrichment/personas.md`` from
``src/adlc/templates/personas.md.j2``.

Personas are mined from the spec's own user stories — the ``As a <role>`` clause
gives the role, ``I want … so that …`` gives the goals, and the acceptance
criteria ids inside the same story block are the ones that persona owns. That
grounding is the whole point: a downstream subagent reading ``personas.md``
must be reading the spec back, not generic marketing copy.

Deterministic, offline, no LLM. ``generate()`` never raises.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config

log = logging.getLogger(__name__)

#: Marks this generator in ``.adlc/config.yaml`` -> ``enrich.skip``.
FACET = "personas"

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "personas.md.j2"
GENERATED_BY = "adlc enrich (L9 enrich_personas)"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_STORY_HEADING = re.compile(r"^#{2,6}\s+.*\b(user\s+stor(?:y|ies)|scenario|story)\b", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_AS_A = re.compile(
    r"\bAs an?\s+(?P<role>[A-Za-z][A-Za-z0-9 \-/]{2,45}?)\s*(?=,|\s+I\s+(?:want|need|would))",
    re.IGNORECASE,
)
_I_WANT = re.compile(
    r"\bI\s+(?:want|need|would like)\s+(?:to\s+)?(?P<want>.+?)"
    r"(?:\s+so\s+that\s+(?P<so>.+?))?\s*$",
    re.IGNORECASE,
)
_AC_ID = re.compile(r"\b((?:US\d{1,3}-)?(?:AC|FR|NFR|SC|TR|UC)-?\d{1,3})\b")
_TRAILING_AC_REF = re.compile(
    r"[\s.,;:]*\(\s*(?:US\d{1,3}-)?(?:AC|FR|NFR|SC|TR|UC)-?\d{1,3}\s*\)\s*$",
    re.IGNORECASE,
)
_PAIN = re.compile(
    r"\b(cannot|can't|cant|unable to|no way to|there is no|today|currently|manual|manually"
    r"|workaround|error[- ]prone|slow|confus\w*|frustrat\w*|tedious|risk\w*|fails?|broken"
    r"|inconsistent|duplicate\w*|lost|missing)\b",
    re.IGNORECASE,
)
_PROBLEM_HEADING = re.compile(
    r"\b(problem|why|background|context|motivation|current)\b", re.IGNORECASE
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

#: Deterministic, culturally-neutral given names. Index chosen by digest of the
#: role so the same spec always renders the same persona names.
_NAMES: tuple[str, ...] = (
    "Amara", "Bruno", "Chidi", "Dalia", "Elias", "Farida", "Goran", "Hana",
    "Idris", "Jun", "Kaia", "Lucia", "Mikkel", "Nadia", "Omar", "Priya",
    "Quinn", "Rafael", "Sofia", "Tomas", "Ulla", "Viktor", "Wren", "Yara",
)

_PROFICIENCY: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(developer|engineer|sre|devops|architect|programmer|maintainer"
            r"|admin\w*|operator|ops)\b",
            re.IGNORECASE,
        ),
        (
            "High — comfortable in a terminal, reads API docs and logs directly, "
            "expects keyboard shortcuts and scriptable paths."
        ),
    ),
    (
        re.compile(
            r"\b(analyst|data scientist|designer|tester|qa|researcher|security)\b",
            re.IGNORECASE,
        ),
        (
            "Medium-high — fluent with specialist tooling in their own domain, "
            "expects export/filter affordances but not a CLI."
        ),
    ),
    (
        re.compile(
            r"\b(manager|lead|owner|editor|author|moderator|coordinator|reviewer"
            r"|stakeholder)\b",
            re.IGNORECASE,
        ),
        (
            "Medium — confident with mainstream SaaS UIs, will not debug anything; "
            "needs clear status and a reliable undo."
        ),
    ),
)
_PROFICIENCY_DEFAULT = (
    "Mixed — assume the low end of the range. Must succeed on first use with no "
    "training, no documentation and no support contact."
)

_A11Y_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(screen ?reader|aria|nvda|jaws|voiceover|talkback|semantic)\b",
            re.IGNORECASE,
        ),
        (
            "Screen-reader user: needs semantic landmarks, labelled controls and "
            "announced state changes (`aria-live`) rather than visual-only feedback."
        ),
    ),
    (
        re.compile(r"\b(keyboard|tab order|focus|shortcut)\b", re.IGNORECASE),
        (
            "Keyboard-only operation: every control reachable in a logical tab order "
            "with a visible focus indicator and no keyboard traps."
        ),
    ),
    (
        re.compile(
            r"\b(contrast|colou?r ?blind|colou?r-?blind|palette|theme|dark mode"
            r"|light mode)\b",
            re.IGNORECASE,
        ),
        (
            "Contrast and colour: WCAG 2.2 AA (4.5:1 body text, 3:1 UI components); "
            "colour is never the only carrier of meaning."
        ),
    ),
    (
        re.compile(
            r"\b(animation|motion|transition|parallax|autoplay|carousel)\b",
            re.IGNORECASE,
        ),
        (
            "Motion sensitivity: honour `prefers-reduced-motion`; no autoplaying or "
            "looping movement."
        ),
    ),
    (
        re.compile(r"\b(video|audio|caption|transcript|voice|sound)\b", re.IGNORECASE),
        "Time-based media: captions and a text transcript for any audio or video.",
    ),
    (
        re.compile(
            r"\b(zoom|magnif\w*|font size|text size|reflow|responsive|mobile)\b",
            re.IGNORECASE,
        ),
        (
            "Reflow at 200% zoom / 320 CSS px without horizontal scrolling or "
            "clipped content."
        ),
    ),
    (
        re.compile(
            r"\b(form|input|validation|error message|required field)\b",
            re.IGNORECASE,
        ),
        (
            "Forms: programmatically associated labels, errors identified in text and "
            "linked to the field that caused them."
        ),
    ),
)
_A11Y_BASELINE = (
    "WCAG 2.2 AA baseline: operable by keyboard, visible focus, 4.5:1 text "
    "contrast, and a page title/heading structure that describes the task."
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _feature_title(spec_text: str) -> str:
    for line in (spec_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = re.sub(
                r"^(feature\s+specification|specification|spec|feature)\s*[:\-]\s*",
                "",
                stripped[2:].strip(),
                flags=re.IGNORECASE,
            )
            return title.strip() or "Proposed change"
    return "Proposed change"


def _blocks(spec_text: str) -> list[tuple[str, str]]:
    """Split the spec into ``(heading, body)`` blocks at any heading level."""
    out: list[tuple[str, str]] = []
    heading = "(preamble)"
    buf: list[str] = []
    for line in (spec_text or "").splitlines():
        match = _HEADING.match(line)
        if match:
            out.append((heading, "\n".join(buf)))
            heading = match.group(1).strip()
            buf = []
        else:
            buf.append(line)
    out.append((heading, "\n".join(buf)))
    return [(h, b) for h, b in out if b.strip() or h != "(preamble)"]


def _clean_clause(text: str) -> str:
    clause = re.sub(r"\s+", " ", (text or "")).strip()
    clause = clause.strip("*_`").strip()
    clause = re.sub(r"[.,;:]+$", "", clause)
    return clause


def _role_key(role: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", role.lower()).strip()


def _story_chunks(block: str) -> list[str]:
    """One collapsed chunk per ``As a …`` story.

    Specs hard-wrap, so a story routinely spans two or three source lines. Match
    on paragraphs with whitespace collapsed, then split at each ``As a`` so a
    paragraph holding several stories still yields one chunk each.
    """
    chunks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", block or ""):
        collapsed = re.sub(r"\s+", " ", paragraph).strip()
        if not collapsed:
            continue
        chunks.extend(
            part.strip()
            for part in re.split(r"(?=\bAs an?\s)", collapsed)
            if part.strip()
        )
    return chunks


def _persona_name(role_key: str, taken: set[str]) -> str:
    digest = hashlib.sha256(role_key.encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % len(_NAMES)
    for offset in range(len(_NAMES)):
        name = _NAMES[(start + offset) % len(_NAMES)]
        if name not in taken:
            taken.add(name)
            return name
    return f"Persona {len(taken) + 1}"


def _proficiency(role: str) -> str:
    for pattern, verdict in _PROFICIENCY:
        if pattern.search(role):
            return verdict
    return _PROFICIENCY_DEFAULT


def _accessibility(role: str, story_text: str, spec_text: str) -> list[str]:
    needs: list[str] = []
    haystack = f"{role}\n{story_text}"
    for pattern, need in _A11Y_RULES:
        if pattern.search(haystack) and need not in needs:
            needs.append(need)
    if len(needs) < 3:
        for pattern, need in _A11Y_RULES:
            if len(needs) >= 3:
                break
            if pattern.search(spec_text or "") and need not in needs:
                needs.append(need)
    needs.append(_A11Y_BASELINE)
    return needs


def _pains(story_text: str, problem_text: str, goals: list[str], source: str) -> list[str]:
    found: list[str] = []
    for blob in (story_text, problem_text):
        for sentence in _SENTENCE.split(re.sub(r"\s+", " ", blob or "")):
            candidate = _clean_clause(re.sub(r"^[-*>\s]+", "", sentence))
            # A trailing "(US2-AC1)" on the previous sentence lands at the front
            # of this one when the naive sentence split runs.
            candidate = re.sub(r"^\(\s*[\w-]+\s*\)\s*", "", candidate)
            if not candidate or len(candidate) < 20 or len(candidate) > 220:
                continue
            if candidate.lower().startswith(("given ", "when ", "then ", "as a ", "as an ")):
                continue
            if _PAIN.search(candidate) and candidate not in found:
                found.append(candidate)
            if len(found) >= 3:
                return found
    if not found and goals:
        found.append(
            f"No supported path to “{goals[0]}” exists in the product today "
            f"({source}); the outcome is reached by hand or not at all."
        )
    return found or [
        (
            f"The spec records no explicit friction for this actor ({source}); "
            "treat this as a gap to close before build."
        )
    ]


def extract_personas(spec_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(personas, unclaimed_criteria_ids)`` mined from ``spec_text``."""
    text = spec_text or ""
    all_ids: list[str] = []
    for match in _AC_ID.finditer(text):
        if match.group(1) not in all_ids:
            all_ids.append(match.group(1))

    problem_text = "\n".join(
        body for heading, body in _blocks(text) if _PROBLEM_HEADING.search(heading)
    )

    grouped: dict[str, dict[str, Any]] = {}
    for heading, body in _blocks(text):
        block = f"{heading}\n{body}"
        is_story_block = bool(_STORY_HEADING.match(f"## {heading}")) or bool(_AS_A.search(block))
        if not is_story_block:
            continue
        for chunk in _story_chunks(block):
            actor = _AS_A.search(chunk)
            if not actor:
                continue
            role_raw = _clean_clause(actor.group("role"))
            role_raw = re.sub(r"\b(who|that|which)\b.*$", "", role_raw, flags=re.IGNORECASE).strip()
            if not role_raw:
                continue
            key = _role_key(role_raw)
            if not key:
                continue
            entry = grouped.setdefault(
                key,
                {"role": role_raw.title(), "goals": [], "sources": [], "criteria": [],
                 "quotes": [], "story_text": ""},
            )
            if heading not in entry["sources"]:
                entry["sources"].append(heading)
            entry["story_text"] += "\n" + block

            want = _I_WANT.search(chunk[actor.end():])
            if want:
                # The trailing "(US2-AC1)" is already captured as an owned
                # criterion; repeating it inside the goal text is just noise.
                goal = _clean_clause(_TRAILING_AC_REF.sub("", want.group("want")))
                outcome = _clean_clause(_TRAILING_AC_REF.sub("", want.group("so") or ""))
                phrase = f"{goal} — so that {outcome}" if outcome else goal
                if phrase and phrase not in entry["goals"]:
                    entry["goals"].append(phrase)
            quote = _clean_clause(re.sub(r"^[-*>\s\d.]+", "", chunk))[:220]
            if quote and quote not in entry["quotes"] and len(entry["quotes"]) < 3:
                entry["quotes"].append(quote)

            for match in _AC_ID.finditer(block):
                if match.group(1) not in entry["criteria"]:
                    entry["criteria"].append(match.group(1))

    personas: list[dict[str, Any]] = []
    taken: set[str] = set()
    for key, entry in grouped.items():
        source = ", ".join(entry["sources"]) or "spec.md"
        goals = entry["goals"] or [
            f"Complete the work described in {source} without leaving the product."
        ]
        personas.append(
            {
                "name": _persona_name(key, taken),
                "role": entry["role"],
                "proficiency": _proficiency(entry["role"]),
                "sources": entry["sources"] or ["spec.md"],
                "goals": goals,
                "pains": _pains(entry["story_text"], problem_text, goals, source),
                "accessibility": _accessibility(entry["role"], entry["story_text"], text),
                "criteria": entry["criteria"],
                "quotes": entry["quotes"],
            }
        )

    claimed = {cid for p in personas for cid in p["criteria"]}
    unclaimed = [cid for cid in all_ids if cid not in claimed]
    return personas, unclaimed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render(personas: list[dict[str, Any]], feature: str, unmapped: list[str]) -> str:
    """Render the template. Raises on a template error — callers must catch."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    return env.get_template(TEMPLATE_NAME).render(
        feature=feature,
        generated_by=GENERATED_BY,
        personas=personas,
        unmapped_criteria=unmapped,
    )


def _skipped(cfg: Config | None) -> bool:
    try:
        skip = ((getattr(cfg, "raw", None) or {}).get("enrich") or {}).get("skip") or []
        return FACET in skip
    except Exception:  # noqa: BLE001 - config shape is not ours to trust
        return False


def generate(run_dir: Path, spec_text: str, cfg: Config) -> list[Path]:
    """Write ``run_dir/enrichment/personas.md`` and return its path.

    Never raises. If the spec names no actor there is nothing to ground a
    persona in, so ``[]`` is returned rather than a fabricated document.
    """
    try:
        if _skipped(cfg):
            log.info("enrich_personas: skipped via config (enrich.skip contains %r)", FACET)
            return []
        run_dir = Path(run_dir)
        text = spec_text or ""
        if not text.strip():
            fallback = run_dir / "spec" / "spec.md"
            try:
                text = fallback.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        if not text.strip():
            log.warning("enrich_personas: no spec text available")
            return []

        personas, unmapped = extract_personas(text)
        if not personas:
            log.warning(
                "enrich_personas: spec contains no 'As a <role>' clause, "
                "refusing to invent personas"
            )
            return []

        rendered = render(personas, _feature_title(text), unmapped)
        out_dir = run_dir / "enrichment"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "personas.md"
        path.write_text(rendered, encoding="utf-8")
        return [path]
    except Exception:
        log.exception("enrich_personas: generation failed")
        return []
