"""Reasoning critique data and UI hooks in the PWA overlay."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path

import jsonschema

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.report.overlay import asset_source
from adlc.runs import RunDir
from adlc.stages.adr import create_adr
from adlc.stages.report import run_report

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "human-feedback-pack.schema.json"
STANCE_ENUM = ("agree", "disagree", "needs_evidence", "out_of_scope")
TARGET_KINDS = ("squad_finding", "persona", "rubric_criterion", "adr")


def read_asset(name: str) -> str:
    return (files("adlc") / "assets" / "feedback-overlay" / name).read_text(encoding="utf-8")

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


def _created_run(cfg: Config, run_id: str = "2026-08-20-rea1") -> RunDir:
    rd = RunDir(cfg, run_id)
    rd.create(profile=cfg.profile, brief_text="# Brief\n\nA change.\n")
    return rd


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
    (rd.reviews_dir / f"adversarial_review.{member}.md").write_text(
        "---\n"
        "squad: adversarial_review\n"
        f"member: {member}\n"
        f"verdict: {verdict}\n"
        f"runId: {rd.run_id}\n"
        f"reviewedSha: {'a' * 40}\n"
        "---\n\n"
        f"## [{severity}] {title}\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _write_personas(rd: RunDir, text: str) -> None:
    rd.enrichment_dir.mkdir(parents=True, exist_ok=True)
    (rd.enrichment_dir / "personas.md").write_text(text, encoding="utf-8")


def _render(cfg: Config, rd: RunDir, *, score: dict | None = None) -> str:
    if score:
        rd.evals_dir.mkdir(parents=True, exist_ok=True)
        (rd.evals_dir / "rubric-score.json").write_text(json.dumps(score), encoding="utf-8")
    reduce_run(cfg, rd)
    run_report(cfg, rd)
    return rd.report.read_text(encoding="utf-8")


def _payload(rendered: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="adlc-critique-data">(.*?)</script>',
        rendered,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _critique_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    subschema = dict(schema["$defs"]["critique"])
    subschema["$defs"] = schema["$defs"]
    return jsonschema.Draft202012Validator(subschema)


def _critique_from_target(target: dict, *, stance: str = "disagree") -> dict:
    obj = {
        "id": target["id"],
        "targetKind": target["targetKind"],
        "targetRef": target["targetRef"],
        "stance": stance,
        "comment": "Pushing back on this.",
    }
    if target.get("targetTitle"):
        obj["targetTitle"] = target["targetTitle"]
    if target.get("sourceDigest"):
        obj["sourceDigest"] = target["sourceDigest"]
    return obj


def test_all_four_sources_render_and_validate(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    _write_personas(rd, PERSONAS_DOC)
    create_adr(cfg, "Adopt dark mode", chosen="CSS custom properties", justification="it matches.", status="accepted")
    out = _render(cfg, rd, score=RUBRIC_SCORE)

    assert "Reasoning critique" in out
    targets = _payload(out)["targets"]
    kinds = [t["targetKind"] for t in targets]
    assert kinds == ["squad_finding", "persona", "persona", "rubric_criterion", "adr"]
    validator = _critique_validator()
    for target in targets:
        validator.validate(_critique_from_target(target))
        assert target["id"] and re.fullmatch(r"[A-Za-z0-9._-]+", target["id"])
        assert target["targetKind"] in TARGET_KINDS
        assert len(target["targetRef"]) <= 512


def test_squad_finding_card_has_expected_reference(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    out = _render(cfg, rd)
    target = _payload(out)["targets"][0]
    assert target["targetKind"] == "squad_finding"
    assert target["targetRef"].endswith("#finding-1")
    assert "adversarial_review.security-adversary.md" in target["targetRef"]
    assert "security-adversary" in out


def test_rubric_and_adr_refs_are_stable_locators(cfg: Config) -> None:
    rd = _created_run(cfg)
    create_adr(cfg, "Adopt dark mode", justification="it fits.", status="accepted")
    out = _render(cfg, rd, score=RUBRIC_SCORE)
    refs = {t["targetKind"]: t["targetRef"] for t in _payload(out)["targets"]}
    assert refs["rubric_criterion"] == "evals/rubric-score.json#US1-AC1"
    assert refs["adr"].startswith("docs/decisions/") and refs["adr"].endswith("#decision-outcome")


def test_bare_run_has_empty_critique_payload(cfg: Config) -> None:
    rd = _created_run(cfg)
    out = _render(cfg, rd)
    assert _payload(out)["targets"] == []
    assert "No critique-able reasoning was recorded." in out


def test_source_digest_pins_exact_text_and_detects_drift(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd, body="`src/auth.py:L42` Original: unsanitised input reaches the query.")
    targets1 = _payload(_render(cfg, rd))["targets"]
    assert targets1[0]["sourceDigest"] == _digest(targets1[0]["text"])
    _write_review(rd, body="`src/auth.py:L42` Rewritten: the input is now fully validated.")
    targets2 = _payload(_render(cfg, rd))["targets"]
    assert targets2[0]["sourceDigest"] == _digest(targets2[0]["text"])
    assert targets2[0]["sourceDigest"] != targets1[0]["sourceDigest"]
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", targets2[0]["sourceDigest"])


def test_hostile_agent_prose_is_escaped_and_json_guarded(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(
        rd,
        title='x"><img src=x onerror=alert(1)>',
        body='`src/a.py:L1` <script>alert(1)</script> and a stray " quote.',
    )
    out = _render(cfg, rd)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert '"><img src=x onerror=alert(1)>' not in out
    block = re.search(
        r'<script type="application/json" id="adlc-critique-data">(.*?)</script>',
        out,
        re.DOTALL,
    ).group(1)  # type: ignore[union-attr]
    assert "<" not in block
    assert "\\u003c" in block
    assert _payload(out)["targets"][0]["targetTitle"].endswith(">")


def test_cards_are_keyboard_and_screen_reader_ready(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    out = _render(cfg, rd)
    assert '<article class="card rcard" data-critique-id="rsn-1"' in out
    assert 'class="reasoning-src" tabindex="0" role="group"' in out
    assert '<fieldset class="stance">' in out
    assert "<legend>Stance" in out
    for value in STANCE_ENUM:
        assert f'value="{value}"' in out
    assert 'role="status"' in out
    assert 'aria-live="polite"' in out


def test_full_report_is_self_contained_with_reasoning(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    _write_personas(rd, PERSONAS_DOC)
    create_adr(cfg, "Adopt dark mode", justification="it fits.", status="accepted")
    html_out = _render(cfg, rd, score=RUBRIC_SCORE)
    assert "Reasoning critique" in html_out
    assert 'src="./' not in html_out
    assert 'href="./' not in html_out
    assert asset_source("critique.js") in html_out


def test_critique_js_structure_is_preserved() -> None:
    js = asset_source("critique.js")
    assert "const store = (window.adlcFeedback = window.adlcFeedback || {" in js
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []," in js
    assert "getElementById('adlc-critique-data')" in js
    assert "JSON.parse(dataEl.textContent)" in js
    assert "'adlc.critiques.' + runId" in js
    assert "id: id," in js
    assert "targetKind: desc.targetKind," in js
    assert "critique.sourceDigest = desc.sourceDigest;" in js


def test_critique_js_implements_shared_store_contract() -> None:
    js = read_asset("critique.js")
    assert "const store = (window.adlcFeedback = window.adlcFeedback || {" in js
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []," in js
    assert "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); }," in js
    assert "subscribe(fn) { this.listeners.push(fn); }," in js


def test_critique_js_persists_and_reads_from_json() -> None:
    js = read_asset("critique.js")
    assert "getElementById('adlc-critique-data')" in js
    assert "JSON.parse(dataEl.textContent)" in js
    assert "'adlc.critiques.' + runId" in js
    assert "window.localStorage.getItem(KEY)" in js
    assert "window.localStorage.setItem(KEY, JSON.stringify(store.critiques))" in js
    assert "store.notify()" in js
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
    assert "severity" not in js
    literal = re.search(r"var critique = \{(.*?)\};", js, re.DOTALL)
    assert literal is not None
    keys = set(re.findall(r"(\w+):", literal.group(1)))
    assert keys == {"id", "targetKind", "targetRef", "stance", "comment"}
    assert set(re.findall(r"critique\.(\w+) =", js)) == {"targetTitle", "sourceDigest"}


def test_bare_run_renders_nothing(cfg: Config) -> None:
    rd = _created_run(cfg)
    out = _render(cfg, rd)
    assert _payload(out)["targets"] == []


def test_uncreated_context_renders_nothing(cfg: Config) -> None:
    rd = RunDir(cfg, "2026-08-20-none")
    rd.create(profile=cfg.profile, brief_text="# Brief\n")
    out = _render(cfg, rd)
    assert _payload(out)["targets"] == []


def test_partial_sources_state_reasons_for_absent(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_personas(rd, PERSONAS_DOC)
    out = _render(cfg, rd)
    assert "Alice the Auditor" in out
    assert "No adversarial squad reviews were recorded" in out
    assert "No rubric score was recorded" in out
    assert "No architecture decision records exist" in out


def test_every_card_digest_matches_its_rendered_source(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd)
    _write_personas(rd, PERSONAS_DOC)
    create_adr(cfg, "Adopt dark mode", justification="it fits.", status="accepted")
    out = _render(cfg, rd, score=RUBRIC_SCORE)
    for target in _payload(out)["targets"]:
        assert target["sourceDigest"] == _digest(target["text"])


def test_hostile_agent_prose_is_escaped(cfg: Config) -> None:
    test_hostile_agent_prose_is_escaped_and_json_guarded(cfg)


def test_embedded_json_neutralises_script_close(cfg: Config) -> None:
    rd = _created_run(cfg)
    _write_review(rd, title="</script><script>alert(1)</script>", body="`src/a.py:L1` benign body.")
    out = _render(cfg, rd)
    marker = '<script type="application/json" id="adlc-critique-data">'
    start = out.index(marker) + len(marker)
    end = out.index("</script>", start)
    block = out[start:end]
    assert "<" not in block
    assert "\\u003c" in block
    data = json.loads(block)
    assert data["targets"][0]["targetTitle"].endswith("</script>")


def test_rubric_badge_spells_out_pass_fail(cfg: Config) -> None:
    rd = _created_run(cfg)
    out = _render(cfg, rd, score=RUBRIC_SCORE)
    assert "Pass" in out


def test_full_report_still_self_contained_with_section_present(cfg: Config) -> None:
    test_full_report_is_self_contained_with_reasoning(cfg)
