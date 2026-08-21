"""L11 -- the evidence-diff review UI (report section ``diff``).

The section turns ``evidence-diff.json`` into a page a human decides on. These
tests pin the properties that make that safe and usable and would otherwise
regress silently: an existing (baseline-less) run still renders nothing; a budget
crossing and a lost requirement are surfaced by *words*, not colour; screenshots
are inlined as real base64 that decodes back to the captured PNG; the byte budgets
degrade with a stated reason instead of dropping or crashing; hostile ids cannot
break out of markup or the JSON island; and every emitted decision validates
against ``$defs/diffDecision``.

There is no JS runtime here, so the client behaviour is asserted structurally on
the asset text and the decision shape is validated in Python against the schema.
Everything runs offline with no credentials, reusing the shared ``cfg`` fixture
and the zlib PNG writer from ``conftest``.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest

from adlc.config import Config
from adlc.runs import RunDir, write_json
from adlc.schemas import is_valid
from adlc.stages.evidence_diff import diff_path
from adlc.stages.report import render as render_report
from adlc.stages.report.context import (
    InlineBudget,
    ReportContext,
    encoded_data_uri_len,
)
from adlc.stages.report.sections import diff as diff_section
from adlc.stages.report.shell import read_asset
from tests.l11_feedback.conftest import CANDIDATE_SHA, png_bytes, write_png

BASE_ID = "2026-08-19-a1b2"
CAND_ID = "2026-08-20-c0de"
SHA_A = "a" * 64
SHA_B = "b" * 64


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _run(cfg: Config, run_id: str, *, references: str | None = None) -> RunDir:
    rd = RunDir(cfg, run_id)
    rd.create(profile=cfg.profile, brief_text="# Brief\n\nA change.\n", references_run=references)
    return rd


def _diff_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schemaVersion": "adlc-evidence-diff/v1",
        "runId": CAND_ID,
        "baselineRunId": BASE_ID,
        "measurements": [],
        "coverage": [],
        "screenshots": [],
        "summary": {},
    }
    doc.update(overrides)
    return doc


def _render(
    cfg: Config, cand: RunDir, doc: dict[str, Any], budget: InlineBudget | None = None
) -> str:
    write_json(diff_path(cand), doc)
    extra = {"inline_budget": budget} if budget is not None else {}
    return diff_section.render(ReportContext(cfg=cfg, rd=cand, **extra))


def _m(metric_id: str, **kw: Any) -> dict[str, Any]:
    entry = {"metricId": metric_id, "change": "changed", "budgetCrossed": "none"}
    entry.update(kw)
    return entry


def _c(req_id: str, change: str, **kw: Any) -> dict[str, Any]:
    entry = {"requirementId": req_id, "change": change}
    entry.update(kw)
    return entry


def _s(path: str, change: str, **kw: Any) -> dict[str, Any]:
    entry = {"path": path, "change": change}
    entry.update(kw)
    return entry


# ---------------------------------------------------------------------------
# Backwards compatibility: a baseline-less run renders nothing
# ---------------------------------------------------------------------------


def test_no_diff_file_renders_empty(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    assert diff_section.render(ReportContext(cfg=cfg, rd=cand)) == ""


def test_stated_absence_renders_empty(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(baselineRunId=None, reason="run has no referencesRun")
    assert _render(cfg, cand, doc) == ""


def test_unreadable_diff_file_renders_empty(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    diff_path(cand).write_text("{ this is not json", encoding="utf-8")
    assert diff_section.render(ReportContext(cfg=cfg, rd=cand)) == ""


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def test_measurement_row_shows_values_and_delta(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        measurements=[
            _m("lcp_ms", value=2200.0, baselineValue=1800.0, delta=400.0, budget=2500.0,
               passed=True, baselinePassed=True, collector="lighthouse"),
        ]
    )
    out = _render(cfg, cand, doc)
    assert "Evidence changes since baseline" in out
    assert "lcp_ms" in out
    assert "1800" in out and "2200" in out
    assert "+400" in out  # signed delta
    assert 'scope="row"' in out and 'scope="col"' in out
    assert "lighthouse" in out


def test_boolean_measurement_values_include_words(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(
        cfg,
        cand,
        _diff_doc(measurements=[_m("flag", value=True, baselineValue=False, delta=None)]),
    )
    assert "&#10003; yes" in out
    assert "&#10007; no" in out


def test_budget_crossing_is_surfaced_without_colour(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        measurements=[
            _m("lcp_ms", value=2600.0, baselineValue=1800.0, delta=800.0, budget=2500.0,
               passed=False, baselinePassed=True, budgetCrossed="entered_breach"),
        ],
        summary={"budgetsEntered": 1},
    )
    out = _render(cfg, cand, doc)
    # Carried by words, not colour: the phrase itself must be present, in the row
    # and in a prominent banner, and the failing verdict spelled out.
    assert "Entered breach" in out
    assert "now failing" in out
    assert "budget crossing" in out.lower()
    assert "Fail" in out  # pass/fail spelled out, not just a red cell
    assert 'class="banner bad"' in out


def test_left_breach_is_labelled(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        measurements=[
            _m("lcp_ms", value=2100.0, baselineValue=2600.0, delta=-500.0, budget=2500.0,
               passed=True, baselinePassed=False, budgetCrossed="left_breach"),
        ]
    )
    out = _render(cfg, cand, doc)
    assert "Left breach" in out
    assert "-500" in out


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_lost_coverage_is_surfaced_as_regression(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        coverage=[
            _c("US1-AC1", "lost", present=False, baselinePresent=True,
               evidenceKinds=[], baselineEvidenceKinds=["screenshot"]),
        ],
        summary={"coverageLost": 1},
    )
    out = _render(cfg, cand, doc)
    assert "Evidence lost" in out
    assert "regression" in out.lower()
    assert "lost evidence" in out.lower()  # the banner
    assert 'class="banner bad"' in out


def test_gained_coverage_is_labelled(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        coverage=[
            _c("US1-AC2", "gained", present=True, baselinePresent=False,
               evidenceKinds=["screenshot"], baselineEvidenceKinds=[]),
        ]
    )
    out = _render(cfg, cand, doc)
    assert "Evidence gained" in out
    assert "US1-AC2" in out


# ---------------------------------------------------------------------------
# Screenshots: inlining, blend, base64 fidelity
# ---------------------------------------------------------------------------


def _changed_pair(cfg: Config) -> tuple[RunDir, RunDir, str, tuple[int, int, int], tuple[int, int, int]]:
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    cand_rgb, base_rgb = (200, 10, 10), (10, 10, 200)
    csha = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=cand_rgb)
    bsha = write_png(base.evidence_dir / "candidate-a" / "home.png", rgb=base_rgb)
    doc = _diff_doc(
        screenshots=[_s("home.png", "changed", sha256=csha, baselineSha256=bsha, bytes=128, baselineBytes=120)]
    )
    write_json(diff_path(cand), doc)
    return cand, base, csha, cand_rgb, base_rgb


def test_changed_screenshot_inlines_pair_and_blend(cfg: Config) -> None:
    cand, _base, _csha, _c_rgb, _b_rgb = _changed_pair(cfg)
    out = diff_section.render(ReportContext(cfg=cfg, rd=cand))
    assert "mix-blend-mode" in out
    assert "Overlay difference blend" in out
    assert 'data-blend-toggle="ss-diff-0"' in out
    # The toggle names its own row for a screen reader, not a generic "blend".
    assert 'aria-label="Overlay difference blend for home.png"' in out
    # Dedup: the blend reuses the two images already shown side by side, so each is
    # inlined exactly once -- two data URIs, not four.
    assert out.count("data:image/png;base64,") == 2
    assert "ss-cell-base" in out and "ss-cell-cand" in out
    # The non-visual facts appear before the images, not only in a visual blend.
    assert "Non-visual change facts" in out
    assert "SHA-256 changed:" in out
    assert "120 B &rarr; 128 B (+8 B)" in out
    assert "Full SHA-256" in out


def test_base64_decodes_to_the_captured_png(cfg: Config) -> None:
    cand, _base, _csha, cand_rgb, base_rgb = _changed_pair(cfg)
    out = diff_section.render(ReportContext(cfg=cfg, rd=cand))
    blobs = {base64.b64decode(m) for m in re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", out)}
    assert png_bytes(rgb=cand_rgb) in blobs
    assert png_bytes(rgb=base_rgb) in blobs
    # And every inlined blob is a real PNG.
    assert all(b.startswith(b"\x89PNG\r\n\x1a\n") for b in blobs)


def test_added_removed_unchanged_are_treated(cfg: Config) -> None:
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    add_sha = write_png(cand.evidence_dir / "candidate-a" / "added.png", rgb=(1, 2, 3))
    rem_sha = write_png(base.evidence_dir / "candidate-a" / "removed.png", rgb=(4, 5, 6))
    unc_sha = write_png(cand.evidence_dir / "candidate-a" / "same.png", rgb=(7, 8, 9))
    write_png(base.evidence_dir / "candidate-a" / "same.png", rgb=(7, 8, 9))
    doc = _diff_doc(
        screenshots=[
            _s("added.png", "added", sha256=add_sha, bytes=90),
            _s("removed.png", "removed", baselineSha256=rem_sha, baselineBytes=90),
            _s("same.png", "unchanged", sha256=unc_sha, baselineSha256=unc_sha, bytes=90, baselineBytes=90),
        ]
    )
    out = _render(cfg, cand, doc)
    assert "Added" in out and "Removed" in out
    # Unchanged is collapsed to hash + size, never inlined (budget goes to changes).
    assert "unchanged &mdash; identical hash" in out
    assert unc_sha[:16] in out
    # added + removed inline exactly one image each; unchanged inlines none.
    assert out.count("data:image/png;base64,") == 2


# ---------------------------------------------------------------------------
# Byte budgets: degrade, never drop or crash
# ---------------------------------------------------------------------------


def test_over_per_image_budget_degrades_with_reason(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diff_section, "_MAX_IMAGE_BYTES", 10)
    cand, _base, _csha, _c_rgb, _b_rgb = _changed_pair(cfg)
    out = diff_section.render(ReportContext(cfg=cfg, rd=cand))
    assert "per-image budget" in out
    assert "exceeds" in out
    assert out.count("data:image/png;base64,") == 0  # neither side inlined
    # Blend needs both inlined; it must state why it is unavailable, not crash.
    assert "Difference blend unavailable" in out


def test_section_budget_exhaustion_degrades_with_reason(cfg: Config) -> None:
    # Identical bytes for every image so the running total is deterministic and
    # does not depend on how a given colour happens to compress: four images of
    # size `unit`, a section budget of `3 * unit`, so the fourth must degrade.
    rgb = (1, 2, 3)
    unit = len(png_bytes(rgb=rgb))
    budget = InlineBudget(total=3 * encoded_data_uri_len(unit, "image/png"))
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    shots = []
    for i in range(2):
        cs = write_png(cand.evidence_dir / "candidate-a" / f"s{i}.png", rgb=rgb)
        bs = write_png(base.evidence_dir / "candidate-a" / f"s{i}.png", rgb=rgb)
        shots.append(_s(f"s{i}.png", "changed", sha256=cs, baselineSha256=bs, bytes=unit, baselineBytes=unit))
    out = _render(cfg, cand, _diff_doc(screenshots=shots), budget)
    assert "document inline budget is exhausted" in out
    # Degradation, not a drop. Each image is inlined at most once (the blend reuses
    # them), so the first pair spends 2 units, the second pair's candidate spends
    # the third, and its baseline degrades to a hash. Three inlined images total,
    # and one stated exhaustion.
    assert out.count("data:image/png;base64,") == 3


def test_unreadable_baseline_image_degrades_not_crashes(cfg: Config) -> None:
    base = _run(cfg, BASE_ID)
    # The baseline run exists but the referenced image is gone from its tree --
    # the realistic "evidence deleted" regression, not a whole-run absence.
    (base.evidence_dir / "candidate-a").mkdir(parents=True, exist_ok=True)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    csha = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=(9, 9, 9))
    doc = _diff_doc(
        screenshots=[_s("home.png", "changed", sha256=csha, baselineSha256=SHA_B, bytes=90, baselineBytes=90)]
    )
    out = _render(cfg, cand, doc)
    assert "image not found in the run directory" in out
    assert SHA_B[:16] in out  # degraded rendering still cites the hash
    assert "data:image/png;base64," in out  # candidate side still inlined
    assert "Difference blend unavailable" in out


# ---------------------------------------------------------------------------
# Escaping and the JSON-island script guard (XSS)
# ---------------------------------------------------------------------------


def test_hostile_metric_id_is_escaped_everywhere(cfg: Config) -> None:
    hostile = '<script>alert(1)</script>" src="./x'
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m(hostile, value=1.0, baselineValue=0.0, delta=1.0)]))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    # The self-containment guard: a crafted id must not synthesise a local ref.
    assert 'src="./' not in out
    assert 'href="./' not in out


def test_json_island_neutralises_script_close(cfg: Config) -> None:
    hostile_path = "a</script><img src=x onerror=alert(1)>.png"
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(screenshots=[_s(hostile_path, "unchanged", sha256=SHA_A, baselineSha256=SHA_A)]))
    island = _island_text(out)
    # The raw closing tag must not appear inside the data island; it is escaped.
    assert "</script>" not in island
    assert "\\u003c/script>" in island
    # And the island is still valid JSON that round-trips the exact target id.
    model = json.loads(island)
    assert any(row["targetId"] == hostile_path for row in model["rows"].values())


def _island_text(out: str) -> str:
    start = out.index('id="adlc-diff-model">') + len('id="adlc-diff-model">')
    return out[start : out.index("</script>", start)]


# ---------------------------------------------------------------------------
# Decision objects: exact schema shape
# ---------------------------------------------------------------------------


def _pack(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": CAND_ID,
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00Z",
        "verdict": "revise",
        "route": "inner",
        "diffDecisions": decisions,
    }


def test_emitted_decisions_validate_against_schema() -> None:
    # The exact object shape the client emits, one per targetKind.
    decisions = [
        {"id": "dd-m-0", "targetKind": "measurement", "targetId": "lcp_ms",
         "decision": "reject", "comment": "A 400ms regression is not acceptable.",
         "annotationIds": ["an-1"]},
        {"id": "dd-c-0", "targetKind": "coverage", "targetId": "US1-AC1",
         "decision": "accept", "comment": "", "annotationIds": []},
        {"id": "dd-s-0", "targetKind": "screenshot", "targetId": "nested/dir/home.png",
         "decision": "reject", "comment": "", "annotationIds": []},
    ]
    valid, errors = is_valid("human-feedback-pack", _pack(decisions))
    assert valid, errors


def test_schema_rejects_a_malformed_decision() -> None:
    bad = [{"id": "dd-x", "targetKind": "metric", "targetId": "lcp", "decision": "accept"}]
    valid, _errors = is_valid("human-feedback-pack", _pack(bad))
    assert not valid  # proves the schema actually constrains targetKind


def test_model_ids_match_the_id_pattern(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        measurements=[_m("lcp_ms", value=1.0, baselineValue=0.0, delta=1.0)],
        coverage=[_c("US1-AC1", "lost", present=False, baselinePresent=True)],
        screenshots=[_s("home.png", "unchanged", sha256=SHA_A, baselineSha256=SHA_A)],
    )
    out = _render(cfg, cand, doc)
    model = json.loads(_island_text(out))
    id_re = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
    for decision_id in model["rows"]:
        assert id_re.match(decision_id), decision_id


# ---------------------------------------------------------------------------
# Accessibility structure
# ---------------------------------------------------------------------------


def test_controls_are_labelled_per_row(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    doc = _diff_doc(
        measurements=[_m("lcp_ms", value=1.0, baselineValue=0.0, delta=1.0)],
        coverage=[_c("US1-AC1", "lost", present=False, baselinePresent=True)],
    )
    out = _render(cfg, cand, doc)
    # Each control names its row -- not forty identical "Accept" buttons.
    assert 'aria-label="Accept change to measurement lcp_ms"' in out
    assert 'aria-label="Reject change to measurement lcp_ms"' in out
    assert 'aria-label="Comment on measurement lcp_ms"' in out
    assert 'aria-label="Accept change to coverage US1-AC1"' in out
    # Grouped and announced.
    assert "<fieldset" in out and "<legend" in out
    assert 'aria-live="polite"' in out
    assert 'role="status"' in out


def test_uses_real_table_semantics(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m("lcp_ms", value=1.0, baselineValue=0.0, delta=1.0)]))
    assert 'scope="col"' in out
    assert '<th scope="row"' in out


# ---------------------------------------------------------------------------
# Self-containment of the whole report with a diff present
# ---------------------------------------------------------------------------


def test_full_report_is_self_contained_with_diff(cfg: Config) -> None:
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    csha = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=(3, 3, 3))
    bsha = write_png(base.evidence_dir / "candidate-a" / "home.png", rgb=(9, 1, 1))
    doc = _diff_doc(
        measurements=[_m("lcp_ms", value=2600.0, baselineValue=1800.0, delta=800.0, budget=2500.0,
                         passed=False, baselinePassed=True, budgetCrossed="entered_breach")],
        screenshots=[_s("home.png", "changed", sha256=csha, baselineSha256=bsha, bytes=90, baselineBytes=90)],
    )
    write_json(diff_path(cand), doc)
    html = render_report(cfg, cand)
    assert html.startswith("<!doctype html>")
    assert "Evidence changes since baseline" in html
    assert 'src="./' not in html
    assert 'href="./' not in html


# ---------------------------------------------------------------------------
# The client asset (structural -- no JS runtime here)
# ---------------------------------------------------------------------------


def test_diff_js_defines_the_shared_store_contract_verbatim() -> None:
    js = read_asset("diff.js")
    assert "window.adlcFeedback = window.adlcFeedback || {" in js
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []," in js
    assert "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); }," in js
    assert "subscribe(fn) { this.listeners.push(fn); }," in js


def test_diff_js_wires_persistence_and_liveness() -> None:
    js = read_asset("diff.js")
    assert "JSON.parse(" in js
    assert "adlc.diffDecisions." in js  # localStorage key prefix, keyed by run id
    assert "localStorage" in js
    assert "store.notify()" in js
    assert "aria-pressed" in js
    assert "data-blend-toggle" in js  # the blend overlay is wired here, in JS
    assert "store.diffDecisions.length = 0" in js  # mutates the live array in place


def test_diff_js_sanitises_untrusted_localstorage() -> None:
    # A saved decision is rebuilt from the trusted model, never trusted verbatim,
    # so a stale/hand-edited entry cannot smuggle an out-of-schema object into the
    # exported pack. Assert the reconstruction and enum guards are present.
    js = read_asset("diff.js")
    assert "function sanitize(d)" in js
    assert "targetKind: meta.targetKind" in js  # authoritative, from the model
    assert "targetId: meta.targetId" in js  # authoritative, from the model
    assert "d.decision !== 'accept' && d.decision !== 'reject'" in js  # enum guard
    assert "function sanitizeAnnotationIds(v)" in js
    assert "ID_RE.test(x)" in js  # annotation ids filtered to the id pattern


def test_diff_js_is_loaded_into_the_section(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m("lcp_ms", value=1.0, baselineValue=0.0, delta=1.0)]))
    assert read_asset("diff.js") in out
    assert '<script type="application/json" id="adlc-diff-model">' in out


def test_one_document_budget_is_shared_by_every_section(cfg: Config) -> None:
    """The inline allowance belongs to the document, not to a section.

    Regression test for a bug that no section-local test could see: the evidence
    section and the screenshot-diff section each held a private 12 MiB budget,
    charged in raw bytes. Each section's own accounting was internally correct,
    so both looked right; the emitted file was 2x the intended inline payload,
    and 2.67x once base64 expansion is counted. Inlining exists *only* to keep
    report.html mailable, so that silently defeated the feature's whole premise.

    Rendering both sections against one context is the only place the invariant
    is observable, which is exactly why it went unnoticed.
    """
    from adlc.stages.report.sections import evidence as evidence_section

    rgb = (7, 8, 9)
    data = png_bytes(rgb=rgb)
    one_image = encoded_data_uri_len(len(data), "image/png")

    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    cs = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=rgb)
    bs = write_png(base.evidence_dir / "candidate-a" / "home.png", rgb=(1, 1, 1))
    write_json(
        diff_path(cand),
        _diff_doc(screenshots=[
            _s("home.png", "changed", sha256=cs, baselineSha256=bs,
               bytes=len(data), baselineBytes=len(data)),
        ]),
    )

    # Room for exactly one image in the whole document.
    budget = InlineBudget(total=one_image)
    ctx = ReportContext(
        cfg=cfg,
        rd=cand,
        artifacts=[{
            "path": "evidence/candidate-a/home.png",
            "kind": "screenshot",
            "bytes": len(data),
            "sha256": cs,
        }],
        inline_budget=budget,
    )

    ev_html = evidence_section.render(ctx)
    diff_html = diff_section.render(ctx)

    # The evidence section spends the allowance; the diff section must then find
    # it gone. Under the old code both inlined, because neither could see the
    # other's spend.
    assert ev_html.count("data:image/png;base64,") == 1
    assert diff_html.count("data:image/png;base64,") == 0
    assert "document inline budget is exhausted" in diff_html

    # The invariant that actually matters: the document never carries more
    # inlined bytes than the budget allows.
    inlined = re.findall(r'data:image/[a-z]+;base64,[A-Za-z0-9+/=]+', ev_html + diff_html)
    assert sum(len(uri) for uri in inlined) <= budget.total
    assert budget.spent <= budget.total


def test_budget_is_charged_in_encoded_not_raw_bytes(cfg: Config) -> None:
    """Charging raw bytes under-counts the document by a third.

    base64 is 4/3, so a budget spent in raw bytes lets ~1.33x the intended
    payload into the file. The check is arithmetic, not a magic constant.
    """
    rgb = (3, 1, 4)
    data = png_bytes(rgb=rgb)
    raw = len(data)
    encoded = encoded_data_uri_len(raw, "image/png")
    assert encoded > raw * 4 // 3  # the prefix plus base64 padding

    budget = InlineBudget(total=encoded)
    assert budget.charge(encoded) is True
    assert budget.remaining == 0
    # A rejected charge must cost nothing, so one oversized image cannot strand
    # the budget for the images after it.
    assert budget.charge(1) is False
    assert budget.spent == encoded

    raw_sized = InlineBudget(total=raw)
    assert raw_sized.charge(encoded) is False, "a raw-sized budget must not admit an encoded image"
