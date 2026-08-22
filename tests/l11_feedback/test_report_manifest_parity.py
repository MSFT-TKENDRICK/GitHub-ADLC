"""The report and the GUI-agnostic manifest must describe the same feedback targets.

``report.html`` and ``feedback-targets.json`` are produced by two entirely separate
Python code paths. The report is the GUI that exists today; the manifest is the
contract every *future* GUI reads. The whole premise of the manifest -- that a
different front end can replace the report without a reviewer losing anything -- is
false the moment those two disagree, and nothing in the suite compared them.

They had already drifted in four places, all of which this module now pins:

* **requirements** -- the report read the reduced ``review-pack.json``; the manifest
  re-derived them from ``spec.md`` with a regex. They agreed only because
  ``stages.evidence`` happened to build the pack from that same function.
* **squad findings** -- the report digested ``title + body``, the manifest digested
  the body alone, so the *same* ``targetRef`` carried two different
  ``sourceDigest`` values.
* **personas** -- the report emitted one target per persona; the manifest emitted a
  single target spanning the whole file, because its splitter chose the shallowest
  heading level and ``personas.md`` opens with an ``# Personas`` title.
* **ADRs** -- the report referenced ``docs/decisions/0001-x.md#decision-outcome``
  and digested the prose; the manifest referenced the bare number ``0001`` and
  digested the YAML frontmatter along with it.

Each of those silently destroys human feedback rather than failing loudly. A
critique carries ``targetKind`` + ``targetRef`` to say *what* it argues with and
``sourceDigest`` to prove that reasoning has not changed underneath it. Mismatched
refs make a critique match nothing; mismatched digests make ingestion discard a
critique as stale when nothing is stale. In both cases a human's review is thrown
away without a message.

Parity is asserted on **identity and drift-detection only** -- ``targetKind``,
``targetRef``, ``sourceDigest``, requirement ids, artifact hashes. Presentation is
deliberately left free: a GUI may title, order, group or style targets however it
likes. That is the point of having a manifest. What it may not do is disagree about
which reasoning a critique is attached to.

Every assertion here is guarded by a non-vacuity check, because "both sides are
empty" is precisely how this drift survived twenty layers of tests.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.runs import RunDir, write_json
from adlc.stages.feedback_targets import compute_targets
from adlc.stages.report import run_report

from .conftest import CANDIDATE_SHA, make_run

PERSONAS_DOC = (
    "# Personas\n\n"
    "## 1. Alice the Auditor - Compliance lead\n\n"
    "Alice needs a defensible audit trail for every decision.\n\n"
    "### Goals\n\n* Traceability\n\n"
    "---\n\n"
    "## 2. Bob - Developer\n\n"
    "Bob wants fast, specific feedback on his change.\n"
)

RUBRIC_SCORE = {
    "overall": 1.0,
    "threshold": 0.7,
    "passed": True,
    "criteria": [
        {
            "id": "US1-AC1",
            "score": 1.0,
            "weight": 1.0,
            "passed": True,
            "rationale": "The toggle is present, labelled and keyboard reachable.",
            "evidence": [],
        },
        {
            "id": "US1-AC2",
            "score": 0.5,
            "weight": 1.0,
            "passed": False,
            "rationale": "The theme is applied on reload rather than immediately.",
            "evidence": [],
        },
    ],
}

ADR_DOC = (
    "---\n"
    "status: accepted\n"
    "date: 2026-08-20\n"
    "---\n\n"
    "# Inline evidence as data URIs\n\n"
    "## Context and Problem Statement\n\n"
    "The report must survive being emailed as a single file.\n\n"
    "## Decision Outcome\n\n"
    "Chosen option: inline under a byte budget, because a relative `src` would\n"
    "break the moment the file moved.\n\n"
    "## Consequences\n\n"
    "The document grows with the evidence.\n"
)


# ---------------------------------------------------------------------------
# Fixture -- deliberately rich, because parity over an empty run proves nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def rich_run(cfg: Config) -> RunDir:
    """A run carrying every reasoning source and two annotatable artifacts."""
    rd = make_run(
        cfg,
        "2026-08-20-c0de",
        head_sha=CANDIDATE_SHA,
        screenshots={"home.png": (10, 20, 30), "about.png": (99, 5, 5)},
        measurements=[
            {
                "metricId": "lcp_ms",
                "value": 2200.0,
                "budget": 2500.0,
                "passed": True,
                "collector": "lighthouse",
            }
        ],
        coverage=[
            {
                "requirementId": "US1-AC1",
                "present": True,
                "evidenceKinds": ["screenshot"],
                "artifactSha256": ["c" * 64],
            }
        ],
    )

    rd.reviews_dir.mkdir(parents=True, exist_ok=True)
    (rd.reviews_dir / "adversarial_review.security-adversary.md").write_text(
        "---\n"
        "squad: adversarial_review\n"
        "member: security-adversary\n"
        "verdict: block\n"
        f"runId: {rd.run_id}\n"
        f"reviewedSha: {'a' * 40}\n"
        "---\n\n"
        "## [high] SQL injection in login\n\n"
        "`src/auth.py:L42` Unsanitised input reaches the query.\n\n"
        "## [medium] Missing rate limit\n\n"
        "`src/api.py:L18` The endpoint accepts unbounded requests.\n",
        encoding="utf-8",
    )

    rd.enrichment_dir.mkdir(parents=True, exist_ok=True)
    (rd.enrichment_dir / "personas.md").write_text(PERSONAS_DOC, encoding="utf-8")

    rd.evals_dir.mkdir(parents=True, exist_ok=True)
    write_json(rd.evals_dir / "rubric-score.json", RUBRIC_SCORE)

    cfg.decisions_dir.mkdir(parents=True, exist_ok=True)
    (cfg.decisions_dir / "0001-inline-evidence.md").write_text(ADR_DOC, encoding="utf-8")

    reduce_run(cfg, rd)
    return rd


# ---------------------------------------------------------------------------
# Helpers -- read the *artifacts*, not the functions that build them
# ---------------------------------------------------------------------------


def _embedded(html: str, element_id: str) -> dict[str, Any] | None:
    """Parse a JSON island out of the rendered report.

    Deliberately goes through the emitted HTML rather than calling the section
    functions. A GUI author has the file, not the call graph, so the file is what
    has to be correct.
    """
    match = re.search(
        r'<script type="application/json" id="' + re.escape(element_id) + r'">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        return None
    return json.loads(match.group(1).replace("\\u003c", "<"))


def _rendered(cfg: Config, rd: RunDir) -> str:
    run_report(cfg, rd)
    return (rd.path / "report.html").read_text(encoding="utf-8")


def _identity(rows: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (str(r.get("targetKind", "")), str(r.get("targetRef", "")), str(r.get("sourceDigest", "")))
        for r in rows
    }


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


def test_the_same_requirements_are_offered_for_linkage(cfg: Config, rich_run: RunDir) -> None:
    """An annotation carries ``requirementIds``; both GUIs must offer the same ids.

    The report reads the reduced pack. If the manifest re-derives the list from
    source instead, a requirement present in one GUI's picker is absent from the
    other's, and the same annotation is linkable in one and not the other.
    """
    html = _rendered(cfg, rich_run)
    report = _embedded(html, "adlc-evidence-data") or {}
    manifest = compute_targets(cfg, rich_run)

    from_report = {str(r["id"]) for r in report.get("requirements", [])}
    from_manifest = {str(r["id"]) for r in manifest["requirements"]}

    assert from_report, "fixture regression: the report offered no requirements to link"
    assert from_report == from_manifest


def test_the_same_artifacts_are_annotatable(cfg: Config, rich_run: RunDir) -> None:
    """Annotations are anchored by ``artifactSha256``, so the hash sets must agree."""
    html = _rendered(cfg, rich_run)
    report = _embedded(html, "adlc-evidence-data") or {}
    manifest = compute_targets(cfg, rich_run)

    from_report = {a["sha256"] for a in report.get("artifacts", []) if a.get("annotatable")}
    from_manifest = {a["sha256"] for a in manifest["artifacts"] if a.get("annotatable")}

    assert len(from_report) >= 2, "fixture regression: expected two annotatable screenshots"
    assert from_report == from_manifest


def test_every_reasoning_target_has_the_same_identity_and_digest(
    cfg: Config, rich_run: RunDir
) -> None:
    """The load-bearing assertion: ``(targetKind, targetRef, sourceDigest)`` must match.

    A mismatched ref makes a critique match nothing. A mismatched digest makes
    ingestion discard it as stale when nothing changed. Both throw away a human's
    review in silence, which is worse than refusing it out loud.
    """
    html = _rendered(cfg, rich_run)
    report = _embedded(html, "adlc-critique-data") or {}
    manifest = compute_targets(cfg, rich_run)

    from_report = _identity(report.get("targets", []))
    from_manifest = _identity(manifest["reasoning"])

    assert from_report, "fixture regression: the report rendered no critique targets"
    assert from_report == from_manifest, (
        "report-only: "
        + repr(sorted(from_report - from_manifest))
        + " manifest-only: "
        + repr(sorted(from_manifest - from_report))
    )


def test_all_four_reasoning_kinds_are_covered_on_both_sides(
    cfg: Config, rich_run: RunDir
) -> None:
    """Guards the parity assertion above from passing because a source is missing.

    Parity over three kinds is not parity if the fourth silently produced nothing
    on both sides. Naming the kinds means a future source that one path forgets to
    emit fails here rather than quietly narrowing what a human can argue with.
    """
    html = _rendered(cfg, rich_run)
    report = _embedded(html, "adlc-critique-data") or {}
    manifest = compute_targets(cfg, rich_run)

    expected = {"squad_finding", "persona", "rubric_criterion", "adr"}
    assert {str(t["targetKind"]) for t in report.get("targets", [])} == expected
    assert {str(t["targetKind"]) for t in manifest["reasoning"]} == expected


def test_each_persona_is_critiqued_separately(cfg: Config, rich_run: RunDir) -> None:
    """One target per persona, not one target for the file.

    ``personas.md`` opens with an ``# Personas`` title, so a splitter that picks the
    shallowest heading level collapses every persona into a single span. A reviewer
    who disagrees with Alice would be forced to attach that disagreement to Bob as
    well, which is not a critique of anything.
    """
    manifest = compute_targets(cfg, rich_run)
    refs = sorted(t["targetRef"] for t in manifest["reasoning"] if t["targetKind"] == "persona")

    assert refs == [
        "enrichment/personas.md#persona-1",
        "enrichment/personas.md#persona-2",
    ]
    texts = [t["text"] for t in manifest["reasoning"] if t["targetKind"] == "persona"]
    assert "Alice" in texts[0] and "Bob" not in texts[0]
    assert "Bob" in texts[1] and "Alice" not in texts[1]


def test_adr_targets_name_a_file_and_carry_no_frontmatter(
    cfg: Config, rich_run: RunDir
) -> None:
    """An ADR ref must locate the document, and its digest must cover only reasoning.

    Referencing a bare number cannot be resolved to a file by a GUI, and digesting
    the YAML frontmatter makes an unrelated ``status:`` transition look like the
    decision itself was rewritten -- discarding critiques that were still valid.
    """
    manifest = compute_targets(cfg, rich_run)
    adrs = [t for t in manifest["reasoning"] if t["targetKind"] == "adr"]

    assert len(adrs) == 1
    assert adrs[0]["targetRef"] == "docs/decisions/0001-inline-evidence.md#decision-outcome"
    assert "status: accepted" not in adrs[0]["text"]
    assert "Chosen option" in adrs[0]["text"]


def test_unnumbered_persona_headings_stay_in_parity(cfg: Config, rich_run: RunDir) -> None:
    """A hand-written ``personas.md`` must not diverge, or yield nothing.

    ``enrich_personas`` numbers its headings, but nothing stops an agent or a human
    writing plain ``## <name>`` sections. Recognising only the numbered form makes
    both GUIs render zero personas -- silently dropping the feedback this layer
    exists to collect -- and recognising it in only *one* of them is worse, because
    then a critique exists in one GUI that the other cannot resolve.
    """
    (rich_run.enrichment_dir / "personas.md").write_text(
        "## Keyboard-only operator\n\nCannot use a mouse; relies on focus order.\n\n"
        "## Screen-reader user\n\nNeeds every state change announced.\n",
        encoding="utf-8",
    )
    reduce_run(cfg, rich_run)

    html = _rendered(cfg, rich_run)
    report = _embedded(html, "adlc-critique-data") or {}
    manifest = compute_targets(cfg, rich_run)

    from_report = {t for t in _identity(report.get("targets", [])) if t[0] == "persona"}
    from_manifest = {t for t in _identity(manifest["reasoning"]) if t[0] == "persona"}

    assert len(from_manifest) == 2, "an unnumbered personas.md must still be critique-able"
    assert from_report == from_manifest


def test_persona_subheadings_are_not_mistaken_for_personas(
    cfg: Config, rich_run: RunDir
) -> None:
    """``### Goals`` is structure inside a persona, not another persona.

    Relaxing the heading pattern to accept unnumbered ``##`` must not relax it into
    accepting ``###``, or every persona fragments into its own sub-sections and a
    reviewer critiques a bullet list instead of a person.
    """
    manifest = compute_targets(cfg, rich_run)
    personas = [t for t in manifest["reasoning"] if t["targetKind"] == "persona"]

    assert len(personas) == 2
    assert "### Goals" in personas[0]["text"], "the subheading belongs to the persona body"


def test_a_retitled_finding_changes_its_digest(cfg: Config, rich_run: RunDir) -> None:
    """``sourceDigest`` must cover the title, or a retitle is invisible drift.

    A finding's title is the claim; the body is the support. Digesting only the
    body lets the claim be rewritten while every critique attached to it still
    reports as current.
    """
    before = {
        t["targetRef"]: t["sourceDigest"]
        for t in compute_targets(cfg, rich_run)["reasoning"]
        if t["targetKind"] == "squad_finding"
    }
    assert before, "fixture regression: no squad findings"

    path = rich_run.reviews_dir / "adversarial_review.security-adversary.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "SQL injection in login", "SQL injection in the login form"
        ),
        encoding="utf-8",
    )

    after = {
        t["targetRef"]: t["sourceDigest"]
        for t in compute_targets(cfg, rich_run)["reasoning"]
        if t["targetKind"] == "squad_finding"
    }
    assert after["reviews/adversarial_review.security-adversary.md#finding-1"] != (
        before["reviews/adversarial_review.security-adversary.md#finding-1"]
    )
    # The untouched finding must not move, or every digest changes on every edit
    # and the drift signal degenerates into noise.
    assert after["reviews/adversarial_review.security-adversary.md#finding-2"] == (
        before["reviews/adversarial_review.security-adversary.md#finding-2"]
    )
