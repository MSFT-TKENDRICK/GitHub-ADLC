"""Layer 5 -- the reasoning section renders critique-able cards for agent prose.

These tests pin the layer's guarantees offline, with no JS runtime: each of the
four reasoning sources renders a card; a bare run renders nothing; the emitted
critique objects validate against ``$defs.critique``; ``sourceDigest`` is a
stable sha256 of the *exact* text shown, and changes when that text changes;
hostile agent prose cannot break out of the HTML; the embedded JSON cannot be
closed early by a ``</script>`` in the data; and the full report stays
self-contained. The JS asset is asserted on structurally -- there is no runtime
here to execute it.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

import jsonschema

from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages.adr import create_adr
from adlc.stages.report import render as render_report
from adlc.stages.report.context import ReportContext
from adlc.stages.report.sections import reasoning
from adlc.stages.report.shell import read_asset

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "human-feedback-pack.schema.json"

STANCE_ENUM = ("agree", "disagree", "needs_evidence", "out_of_scope")
TARGET_KINDS = ("squad_finding", "persona", "rubric_criterion", "adr")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _created_run(cfg: Config, run_id: str = "2026-08-20-rea1") -> RunDir:
    rd = RunDir(cfg, run_id)
    rd.create(profile=cfg.profile, brief_text="# Brief\n\nA change.\n")
    return rd


def _ctx(cfg: Config, rd: RunDir, **overrides: object) -> ReportContext:
    return ReportContext(cfg=cfg, rd=rd, **overrides)


def _write_review(
    rd: RunDir,
    *,
    member: str = "security-adversary",
    verdict: str = "block",
    severity: str = "high",
    title: str = "SQL injection in login",
    body: str = "`src/auth.py:L42` Unsanitised input reaches the query.",
) -> None:
    rd.reviews_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        "squad: adversarial_review\n"
        f"member: {member}\n"
        f"verdict: {verdict}\n"
        f"runId: {rd.run_id}\n"
        f"reviewedSha: {'a' * 40}\n"
        "---\n\n"
        f"## [{severity}] {title}\n\n"
        f"{body}\n"
    )
    (rd.reviews_dir / f"adversarial_review.{member}.md").write_text(text, encoding="utf-8")


def _write_personas(rd: RunDir, text: str) -> None:
    rd.enrichment_dir.mkdir(parents=True, exist_ok=True)
    (rd.enrichment_dir / "personas.md").write_text(text, encoding="utf-8")


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
        }
    ],
}


def _srcs(rendered: str) -> list[str]:
    """The exact (un-escaped) reasoning text of each card, in document order."""
    raw = re.findall(r'<div class="reasoning-src">(.*?)</div>', rendered, re.DOTALL)
    return [html.unescape(chunk) for chunk in raw]


def _payload(rendered: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="adlc-critique-data">(.*?)</script>',
        rendered,
        re.DOTALL,
    )
    assert match is not None, "expected an embedded critique-data block"
    return json.loads(match.group(1))


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _critique_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    subschema = dict(schema["$defs"]["critique"])
    subschema["$defs"] = schema["$defs"]
    return jsonschema.Draft202012Validator(subschema)


def _critique_from_target(
    target: dict, *, stance: str = "disagree", comment: str = "Pushing back on this."
) -> dict:
    """Reconstruct the object critique.js records, so we validate what ships."""
    obj = {
        "id": target["id"],
        "targetKind": target["targetKind"],
        "targetRef": target["targetRef"],
        "stance": stance,
        "comment": comment,
    }
    if target.get("targetTitle"):
        obj["targetTitle"] = target["targetTitle"]
    if target.get("sourceDigest"):
        obj["sourceDigest"] = target["sourceDigest"]
    return obj


# ---------------------------------------------------------------------------
# Each of the four sources renders a card
# ---------------------------------------------------------------------------


def test_all_four_sources_render_and_validate(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    _write_personas(rd, PERSONAS_DOC)
    create_adr(
        cfg,
        "Adopt dark mode",
        chosen="CSS custom properties",
        justification="it matches the design system.",
        status="accepted",
    )
    out = reasoning.render(_ctx(cfg, rd, score=RUBRIC_SCORE))

    assert out.startswith("  <h2>Reasoning</h2>")
    for heading in ("Squad findings", "Personas", "Rubric criteria", "Decision records"):
        assert f"<h3>{heading}</h3>" in out

    targets = _payload(out)["targets"]
    kinds = [t["targetKind"] for t in targets]
    # squad finding, two personas, one rubric criterion, one ADR
    assert kinds == ["squad_finding", "persona", "persona", "rubric_criterion", "adr"]

    validator = _critique_validator()
    for target in targets:
        # every kind must yield a schema-valid critique object
        validator.validate(_critique_from_target(target))
        assert target["id"] and re.fullmatch(r"[A-Za-z0-9._-]+", target["id"])
        assert target["targetKind"] in TARGET_KINDS
        assert len(target["targetRef"]) <= 512


def test_squad_finding_card_has_expected_reference(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    out = reasoning.render(_ctx(cfg, rd))
    target = _payload(out)["targets"][0]
    assert target["targetKind"] == "squad_finding"
    assert target["targetRef"].endswith("#finding-1")
    assert "adversarial_review.security-adversary.md" in target["targetRef"]
    assert "security-adversary: SQL injection in login" in out


def test_rubric_and_adr_refs_are_stable_locators(cfg: Config) -> None:
    rd = _created_run(cfg)
    create_adr(cfg, "Adopt dark mode", justification="it fits.", status="accepted")
    out = reasoning.render(_ctx(cfg, rd, score=RUBRIC_SCORE))
    refs = {t["targetKind"]: t["targetRef"] for t in _payload(out)["targets"]}
    assert refs["rubric_criterion"] == "evals/rubric-score.json#US1-AC1"
    assert refs["adr"].startswith("docs/decisions/") and refs["adr"].endswith("#decision-outcome")


# ---------------------------------------------------------------------------
# The empty / omission path
# ---------------------------------------------------------------------------


def test_bare_run_renders_nothing(cfg: Config) -> None:
    rd = _created_run(cfg)
    assert reasoning.render(_ctx(cfg, rd)) == ""


def test_uncreated_context_renders_nothing(cfg: Config) -> None:
    ctx = ReportContext(cfg=cfg, rd=RunDir(cfg, "2026-08-20-none"))
    assert reasoning.render(ctx) == ""


def test_partial_sources_state_reasons_for_absent(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_personas(rd, PERSONAS_DOC)
    out = reasoning.render(_ctx(cfg, rd))
    assert "<h3>Personas</h3>" in out
    assert '<div class="rcards">' in out
    # absent groups state a reason rather than a card
    assert "No adversarial squad reviews were recorded" in out
    assert "No rubric score was recorded" in out
    assert "No architecture decision records exist" in out


# ---------------------------------------------------------------------------
# sourceDigest pins the exact text, and detects drift
# ---------------------------------------------------------------------------


def test_source_digest_pins_exact_text_and_detects_drift(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd, body="`src/auth.py:L42` Original: unsanitised input reaches the query.")
    out1 = reasoning.render(_ctx(cfg, rd))
    srcs1 = _srcs(out1)
    targets1 = _payload(out1)["targets"]
    assert len(srcs1) == 1
    # the digest is exactly sha256 of the text the human reads in the card
    assert targets1[0]["sourceDigest"] == _digest(srcs1[0])

    # rewrite the same finding's reasoning -> the pinned digest must move
    _write_review(rd, body="`src/auth.py:L42` Rewritten: the input is now fully validated.")
    out2 = reasoning.render(_ctx(cfg, rd))
    srcs2 = _srcs(out2)
    targets2 = _payload(out2)["targets"]
    assert targets2[0]["sourceDigest"] == _digest(srcs2[0])
    assert targets2[0]["sourceDigest"] != targets1[0]["sourceDigest"]
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", targets2[0]["sourceDigest"])


def test_every_card_digest_matches_its_rendered_source(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    _write_personas(rd, PERSONAS_DOC)
    create_adr(cfg, "Adopt dark mode", justification="it fits.", status="accepted")
    out = reasoning.render(_ctx(cfg, rd, score=RUBRIC_SCORE))
    srcs = _srcs(out)
    targets = _payload(out)["targets"]
    assert len(srcs) == len(targets)
    for src, target in zip(srcs, targets):
        assert target["sourceDigest"] == _digest(src)


# ---------------------------------------------------------------------------
# Hostile agent prose cannot escape the document
# ---------------------------------------------------------------------------


def test_hostile_agent_prose_is_escaped(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(
        rd,
        title='x"><img src=x onerror=alert(1)>',
        body='`src/a.py:L1` <script>alert(1)</script> and a stray " quote.',
    )
    out = reasoning.render(_ctx(cfg, rd))
    # the script payload never appears unescaped
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    # the title flows into attributes (aria-label, legend); the quote cannot break out
    assert '"><img src=x onerror=alert(1)>' not in out
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in out


def test_embedded_json_neutralises_script_close(cfg: Config) -> None:
    rd = _created_run(cfg)
    # a hostile title flows into the JSON island as targetTitle; a raw </script>
    # there would otherwise close the data block early.
    _write_review(
        rd,
        title="</script><script>alert(1)</script>",
        body="`src/a.py:L1` benign body.",
    )
    out = reasoning.render(_ctx(cfg, rd))

    marker = '<script type="application/json" id="adlc-critique-data">'
    start = out.index(marker) + len(marker)
    end = out.index("</script>", start)
    block = out[start:end]
    # not a single raw '<' survives in the data island, so it cannot self-close
    assert "<" not in block
    assert "\\u003c" in block
    # yet it is still valid JSON that round-trips the title back, '<' intact
    data = json.loads(block)
    assert data["targets"][0]["targetTitle"].endswith("</script>")
    # and the digest still pins the exact rendered source text
    src = _srcs(out)[0]
    assert data["targets"][0]["sourceDigest"] == _digest(src)


# ---------------------------------------------------------------------------
# Accessibility structure
# ---------------------------------------------------------------------------


def test_cards_are_keyboard_and_screen_reader_ready(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    out = reasoning.render(_ctx(cfg, rd))
    # native focusable controls, each labelled for its own card
    assert '<fieldset class="stance">' in out
    assert "<legend>Stance" in out
    assert 'for="cr-0-comment"' in out
    assert 'id="cr-0-comment"' in out
    assert 'name="cr-0-stance"' in out
    for value in STANCE_ENUM:
        assert f'value="{value}"' in out
    # buttons carry a per-card accessible name
    assert 'aria-label="Record critique for' in out
    assert 'aria-label="Clear critique for' in out
    # state changes are announced, and focus is visible
    assert 'role="status"' in out
    assert 'aria-live="polite"' in out
    assert 'id="cr-0-status"' in out
    assert ":focus-visible" in out


# ---------------------------------------------------------------------------
# The full report stays self-contained
# ---------------------------------------------------------------------------


def test_full_report_is_self_contained_with_reasoning(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    _write_personas(rd, PERSONAS_DOC)
    (rd.evals_dir / "rubric-score.json").write_text(json.dumps(RUBRIC_SCORE), encoding="utf-8")
    create_adr(cfg, "Adopt dark mode", justification="it fits.", status="accepted")

    html_out = render_report(cfg, rd)
    assert "Reasoning" in html_out
    assert 'src="./' not in html_out
    assert 'href="./' not in html_out
    # the behaviour asset is inlined, not linked
    assert read_asset("critique.js") in html_out

    # Stronger, section-scoped guarantee: our own fragment references NO external
    # resource at all -- not just no "./" ones -- so it can never reach the network.
    fragment = reasoning.render(_ctx(cfg, rd, score=RUBRIC_SCORE))
    assert 'src="' not in fragment
    assert 'href="' not in fragment


# ---------------------------------------------------------------------------
# The JS asset -- asserted structurally, never executed
# ---------------------------------------------------------------------------


def test_critique_js_implements_shared_store_contract() -> None:
    js = read_asset("critique.js")
    # the cross-layer registry, verbatim
    assert "const store = (window.adlcFeedback = window.adlcFeedback || {" in js
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []," in js
    assert "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); }," in js
    assert "subscribe(fn) { this.listeners.push(fn); }," in js


def test_critique_js_persists_and_reads_from_json() -> None:
    js = read_asset("critique.js")
    # data comes from the JSON island, never interpolated JS
    assert "getElementById('adlc-critique-data')" in js
    assert "JSON.parse(dataEl.textContent)" in js
    # persistence keyed by run id, guarded against unavailable storage
    assert "'adlc.critiques.' + runId" in js
    assert "window.localStorage.getItem(KEY)" in js
    assert "window.localStorage.setItem(KEY, JSON.stringify(store.critiques))" in js
    assert "store.notify()" in js
    # single braces: str.format substitution values are not rescanned
    assert "{{" not in js


def test_critique_js_builds_exact_schema_shape() -> None:
    js = read_asset("critique.js")
    for fragment in (
        "id: id,",
        "targetKind: desc.targetKind,",
        "targetRef: desc.targetRef,",
        "stance: stance,",
        "comment: comment,",
        "critique.targetTitle = desc.targetTitle;",
        "critique.sourceDigest = desc.sourceDigest;",
    ):
        assert fragment in js
    # we never emit the optional severity field, so it must not appear
    assert "severity" not in js
    # the recorded object literal must carry ONLY the schema's required keys...
    literal = re.search(r"var critique = \{(.*?)\};", js, re.DOTALL)
    assert literal is not None
    keys = set(re.findall(r"(\w+):", literal.group(1)))
    assert keys == {"id", "targetKind", "targetRef", "stance", "comment"}
    # ...and the only conditionally-added keys are the two allowed optionals.
    assert set(re.findall(r"critique\.(\w+) =", js)) == {"targetTitle", "sourceDigest"}
