"""Reasoning section: critique cards for agent-authored reasoning (layer 5).

The report's purpose at this layer is to make agent persona "thoughts"
*critique-able*: every piece of agent-authored reasoning is rendered as a card a
human can push back on, and each recorded critique is captured in the exact
``$defs.critique`` shape of ``schemas/human-feedback-pack.schema.json`` so the
successor run can read it back.

Four sources feed the cards, all read here rather than from the shared context:

* **Squad findings** -- ``reviews/adversarial_review.<member>.md`` parsed with
  :func:`adlc.adapters.gate.adversarial_review.iter_reviews`. Per finding we rely
  on ``Finding.severity``, ``Finding.title`` and ``Finding.body`` and on the
  parent ``Review.member`` / ``Review.verdict`` / ``Review.path``.
* **Personas** -- ``enrichment/personas.md``; each ``## <n>. <name> -- <role>``
  section is one card.
* **Rubric criterion rationales** -- ``ctx.score["criteria"][*]`` fields ``id``,
  ``score``, ``passed`` and ``rationale``.
* **ADR justifications** -- the "Decision Outcome" of each record from
  :func:`adlc.stages.adr.list_adrs` (``Adr.number`` / ``Adr.title`` /
  ``Adr.status`` / ``Adr.path``).

DEVIATION FROM THE PURE-SECTION CONTRACT
----------------------------------------
Sections are documented as pure functions over :class:`ReportContext`, but
``build_context`` (a shared file this layer must not edit) does not read
``reviews/*.md``, ``enrichment/personas.md`` or the ADR corpus. This section
therefore reads them itself from ``ctx.rd`` / ``ctx.cfg``, with every read
wrapped in ``try/except (OSError, ValueError)``. A missing or malformed source is
a stated omission, never a crash -- this section renders inside the
module-scoped conformance pipeline, where a single raised exception would fail
the whole suite. If every source is absent the section renders ``""`` and leaves
no trace; if some are present it renders those and states the reason each absent
one is missing.

TRUST AND DRIFT
---------------
Agent-authored prose is untrusted model output: it may contain ``<script>`` or a
quote that breaks an attribute, so every value is routed through
:func:`escape`. Data handed to the browser is emitted as JSON inside a
``<script type="application/json">`` block with ``<`` rewritten to ``\\u003c`` so
an embedded ``</script>`` cannot close the block early; the JS reads it with
``JSON.parse`` rather than from JS source.

``sourceDigest`` is ``sha256:`` over the *exact* reasoning text shown in the
card's ``reasoning-src`` block -- the same bytes the human reads. It is computed
in Python (never in JS, which would hash the escaped DOM form), so that if an
agent later rewrites that reasoning the recorded critique no longer matches its
target and the drift is detectable.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from adlc.adapters.gate.adversarial_review import iter_reviews
from adlc.stages.adr import list_adrs
from adlc.stages.report.context import ReportContext, escape, omission
from adlc.stages.report.shell import read_asset

SQUAD_ID = "adversarial_review"

#: Stance vocabulary, verbatim from ``$defs.critique.properties.stance.enum``.
_STANCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("agree", "Agree"),
    ("disagree", "Disagree"),
    ("needs_evidence", "Needs evidence"),
    ("out_of_scope", "Out of scope"),
)

_SEVERITY_CLASS = {
    "critical": "pill bad",
    "high": "pill bad",
    "medium": "pill warn",
    "low": "tag",
    "info": "tag",
}
_STATUS_CLASS = {"accepted": "pill ok", "rejected": "pill bad", "proposed": "pill warn"}

_PERSONA_HEADING = re.compile(r"^##\s+\d+\.\s+(?P<title>.+?)\s*$", re.MULTILINE)
_ADR_OUTCOME = re.compile(
    r"^##\s+Decision Outcome\s*\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_ADR_FRONTMATTER = re.compile(r"\A\ufeff?---.*?\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class _Card:
    """One critique-able unit: metadata plus the exact text being critiqued."""

    cid: str
    kind: str
    ref: str
    title: str
    source_text: str
    badges: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        """``sha256:`` over the exact reasoning text this card displays."""
        return "sha256:" + hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _make_card(
    ids: Iterator[str],
    *,
    kind: str,
    ref: str,
    title: str,
    source_text: str,
    badges: tuple[tuple[str, str], ...],
) -> _Card:
    return _Card(
        cid=next(ids),
        kind=kind,
        ref=_clip(ref, 512),
        title=_clip(title, 512),
        source_text=source_text,
        badges=badges,
    )


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _finding_cards(ctx: ReportContext, ids: Iterator[str]) -> tuple[list[_Card], str | None]:
    try:
        reviews = iter_reviews(ctx.rd.reviews_dir, SQUAD_ID, citation="file-line")
    except (OSError, ValueError):
        reviews = []
    if not reviews:
        return [], "No adversarial squad reviews were recorded for this run."

    cards: list[_Card] = []
    for review in reviews:
        member = review.member or review.path_obj.stem
        try:
            rel = ctx.rd.rel(review.path_obj)
        except (OSError, ValueError):
            rel = review.path_obj.name
        for index, finding in enumerate(review.findings, start=1):
            source_text = f"{finding.title}\n\n{finding.body}".strip()
            if not source_text:
                continue
            cards.append(
                _make_card(
                    ids,
                    kind="squad_finding",
                    ref=f"{rel}#finding-{index}",
                    title=f"{member}: {finding.title}",
                    source_text=source_text,
                    badges=(
                        (_SEVERITY_CLASS.get(finding.severity, "tag"),
                         finding.severity or "unrated"),
                        ("tag", f"verdict {review.verdict}"),
                        ("tag", member),
                    ),
                )
            )
    if not cards:
        return [], "The adversarial squad filed verdicts but no findings to critique."
    return cards, None


def _persona_cards(ctx: ReportContext, ids: Iterator[str]) -> tuple[list[_Card], str | None]:
    path = ctx.rd.enrichment_dir / "personas.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return [], "No personas were enriched for this run (enrichment/personas.md absent)."
    try:
        rel = ctx.rd.rel(path)
    except (OSError, ValueError):
        rel = "enrichment/personas.md"

    matches = list(_PERSONA_HEADING.finditer(text))
    if not matches:
        return [], "enrichment/personas.md has no persona sections to critique."

    cards: list[_Card] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = re.sub(r"\s*-{3,}\s*\Z", "", text[start:end]).strip()
        if not body:
            continue
        cards.append(
            _make_card(
                ids,
                kind="persona",
                ref=f"{rel}#persona-{i + 1}",
                title=match.group("title").strip(),
                source_text=body,
                badges=(("tag", "persona"),),
            )
        )
    if not cards:
        return [], "The personas document has headings but no descriptive text to critique."
    return cards, None


def _score_text(crit: dict[str, Any]) -> str:
    try:
        return f"score {float(crit.get('score')):.2f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "score n/a"


def _rubric_cards(ctx: ReportContext, ids: Iterator[str]) -> tuple[list[_Card], str | None]:
    score = ctx.score
    if not isinstance(score, dict):
        return [], "No rubric score was recorded for this run (evals/rubric-score.json absent)."
    criteria = score.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return [], "The rubric score records no criteria to critique."

    cards: list[_Card] = []
    for crit in criteria:
        if not isinstance(crit, dict):
            continue
        rationale = str(crit.get("rationale") or "").strip()
        if not rationale:
            continue
        cid = str(crit.get("id") or "").strip()
        cards.append(
            _make_card(
                ids,
                kind="rubric_criterion",
                ref=f"evals/rubric-score.json#{cid}" if cid else "evals/rubric-score.json",
                title=cid or "criterion",
                source_text=rationale,
                badges=(
                    ("pill ok" if crit.get("passed") else "pill bad", _score_text(crit)),
                    ("tag", "rubric criterion"),
                ),
            )
        )
    if not cards:
        return [], "The rubric criteria carry no rationale text to critique."
    return cards, None


def _adr_reasoning(text: str) -> tuple[str, str]:
    match = _ADR_OUTCOME.search(text)
    if match:
        body = match.group("body").strip()
        if body:
            return body, "decision-outcome"
    return _ADR_FRONTMATTER.sub("", text, count=1).strip(), "body"


def _adr_cards(ctx: ReportContext, ids: Iterator[str]) -> tuple[list[_Card], str | None]:
    try:
        adrs = list_adrs(ctx.cfg)
    except (OSError, ValueError):
        adrs = []
    if not adrs:
        return [], "No architecture decision records exist to critique."

    cards: list[_Card] = []
    for adr in adrs:
        try:
            text = adr.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        source_text, anchor = _adr_reasoning(text)
        if not source_text:
            continue
        try:
            ref_path = adr.path.relative_to(ctx.cfg.root).as_posix()
        except (ValueError, OSError):
            ref_path = f"docs/decisions/{adr.path.name}"
        cards.append(
            _make_card(
                ids,
                kind="adr",
                ref=f"{ref_path}#{anchor}",
                title=f"{adr.number} - {adr.title}",
                source_text=source_text,
                badges=((_STATUS_CLASS.get(adr.status, "tag"), adr.status), ("tag", "ADR")),
            )
        )
    if not cards:
        return [], "The ADR corpus carries no decision rationale to critique."
    return cards, None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _descriptor(card: _Card) -> dict[str, str]:
    return {
        "id": card.cid,
        "targetKind": card.kind,
        "targetRef": card.ref,
        "targetTitle": card.title,
        "sourceDigest": card.digest,
    }


def _card_html(card: _Card) -> str:
    cid = card.cid
    title = escape(card.title)
    badges = " ".join(
        f'<span class="{cls}">{escape(text)}</span>' for cls, text in card.badges
    )
    stance = "\n".join(
        f'          <label><input type="radio" name="{cid}-stance" value="{value}">'
        f" {escape(label)}</label>"
        for value, label in _STANCE_CHOICES
    )
    return "\n".join(
        [
            f'    <article class="card rcard" data-critique-id="{cid}">',
            (
                f'      <header class="rcard-head">'
                f'<strong class="rcard-title">{title}</strong> {badges}</header>'
            ),
            f'      <div class="reasoning-src">{escape(card.source_text)}</div>',
            (
                f'      <p class="mono muted rcard-ref">{escape(card.ref)} &middot; '
                f'<span title="{escape(card.digest)}">'
                f"{escape(card.digest[:17])}&hellip;</span></p>"
            ),
            '      <div class="critique-form">',
            f'        <fieldset class="stance"><legend>Stance &mdash; {title}</legend>',
            stance,
            "        </fieldset>",
            f'        <label for="{cid}-comment">Comment &mdash; {title}</label>',
            (
                f'        <textarea id="{cid}-comment" maxlength="4000"'
                f' aria-describedby="{cid}-status"'
                ' placeholder="Why do you agree or push back?"></textarea>'
            ),
            '        <div class="actions">',
            (
                f'          <button type="button" class="btn ok" data-action="record"'
                f' aria-label="Record critique for {title}">Record critique</button>'
            ),
            (
                f'          <button type="button" class="btn" data-action="clear"'
                f' aria-label="Clear critique for {title}">Clear critique</button>'
            ),
            "        </div>",
            (
                f'        <span id="{cid}-status" class="critique-status" role="status"'
                ' aria-live="polite"></span>'
            ),
            "      </div>",
            "    </article>",
        ]
    )


_STYLE = """  <style>
  .rcards { display:grid; gap:12px; margin:8px 0 6px }
  .rcard .rcard-head { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:6px }
  .rcard .rcard-title { font-size:14px }
  .rcard .reasoning-src { white-space:pre-wrap; word-break:break-word; background:var(--bg);
    border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:4px 0;
    font-size:13px; max-height:280px; overflow:auto }
  .rcard .rcard-ref { margin:4px 0 0; word-break:break-all }
  .rcard .critique-form { margin-top:10px }
  .rcard fieldset.stance { border:1px solid var(--line); border-radius:8px; padding:8px 12px;
    margin:0 0 8px }
  .rcard fieldset.stance legend { font-size:12px; color:var(--muted); padding:0 6px }
  .rcard .stance label { display:inline-flex; align-items:center; gap:6px; margin-right:14px;
    font-size:13px }
  .rcard .critique-form > label { display:block; font-size:12px; color:var(--muted);
    margin-bottom:4px }
  .rcard textarea { width:100%; min-height:64px; font:inherit; color:var(--fg);
    background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px 10px;
    resize:vertical }
  .rcard .critique-status { display:inline-block; min-height:1.2em; margin-top:8px;
    font-size:12px; color:var(--muted) }
  .rcard .critique-status.recorded { color:var(--ok); font-weight:600 }
  .rcard.has-critique { border-left:3px solid var(--accent) }
  .rcard :focus-visible { outline:2px solid var(--accent); outline-offset:2px }
  </style>"""


def render(ctx: ReportContext) -> str:
    ids = (f"cr-{n}" for n in itertools.count())
    groups: tuple[tuple[str, tuple[list[_Card], str | None]], ...] = (
        ("Squad findings", _finding_cards(ctx, ids)),
        ("Personas", _persona_cards(ctx, ids)),
        ("Rubric criteria", _rubric_cards(ctx, ids)),
        ("Decision records", _adr_cards(ctx, ids)),
    )
    all_cards = [card for _, (cards, _) in groups for card in cards]
    if not all_cards:
        return ""

    payload = {"runId": ctx.rd.run_id, "targets": [_descriptor(c) for c in all_cards]}
    # Escape ``<`` so an untrusted ``</script>`` cannot terminate the JSON block
    # early; JSON.parse restores it. Only string values can contain ``<``.
    data_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )

    lines: list[str] = [
        "  <h2>Reasoning</h2>",
        (
            '  <p class="note">Each card is agent-authored reasoning you can push back on. '
            "Choose a stance, add a comment, and record it; the critique is stored in this "
            "browser and travels with the exported feedback pack. Its digest pins the exact "
            "text you judged, so later edits to that reasoning are detectable.</p>"
        ),
    ]
    for label, (cards, reason) in groups:
        lines.append(f"  <h3>{escape(label)}</h3>")
        if cards:
            lines.append('  <div class="rcards">')
            lines.extend(_card_html(card) for card in cards)
            lines.append("  </div>")
        else:
            lines.append(f"  {omission(reason or 'Not available.')}")

    lines.append(_STYLE)
    lines.append(f'  <script type="application/json" id="adlc-critique-data">{data_json}</script>')
    # The behaviour asset ships with the package; guard the read anyway so a
    # missing or empty asset degrades to static (still readable) cards rather
    # than raising and failing the whole report pipeline.
    try:
        behaviour = read_asset("critique.js")
    except (OSError, RuntimeError):
        behaviour = ""
    if behaviour:
        lines.append(f"  <script>\n{behaviour}\n  </script>")
    return "\n".join(lines)
