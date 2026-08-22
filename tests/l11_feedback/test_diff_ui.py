"""Evidence-diff decisions in the PWA feedback overlay."""

from __future__ import annotations

import base64
import json
import re
from importlib.resources import files
from typing import Any

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.report.overlay import asset_source
from adlc.runs import RunDir, write_json
from adlc.schemas import is_valid
from adlc.stages.evidence_diff import diff_path
from adlc.stages.report import run_report
from tests.l11_feedback.conftest import CANDIDATE_SHA, write_png

BASE_ID = "2026-08-19-a1b2"
CAND_ID = "2026-08-20-c0de"
SHA_A = "a" * 64


def read_asset(name: str) -> str:
    return (files("adlc") / "assets" / "feedback-overlay" / name).read_text(encoding="utf-8")


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


def _render(cfg: Config, cand: RunDir, doc: dict[str, Any]) -> str:
    write_json(diff_path(cand), doc)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    return cand.report.read_text(encoding="utf-8")


def _island(html: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/json" id="adlc-diff-model">(.*?)</script>', html)
    assert match
    return json.loads(match.group(1))


def test_no_diff_file_still_has_empty_decision_model(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    model = _island(cand.report.read_text(encoding="utf-8"))
    assert model == {"runId": CAND_ID, "rows": {}}


def test_measurement_and_coverage_rows_carry_decision_targets(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(
        cfg,
        cand,
        _diff_doc(
            measurements=[
                _m(
                    "lcp_ms",
                    value=2600.0,
                    baselineValue=1800.0,
                    delta=800.0,
                    budget=2500.0,
                    passed=False,
                    baselinePassed=True,
                    budgetCrossed="entered_breach",
                )
            ],
            coverage=[_c("US1-AC1", "lost", present=False, baselinePresent=True)],
        ),
    )
    assert "Evidence changes since baseline" in out
    assert 'aria-label="Accept change to measurement lcp_ms"' in out
    assert 'aria-label="Reject change to coverage US1-AC1"' in out
    assert 'scope="col"' in out and '<th scope="row">' in out
    model = _island(out)
    assert model["rows"]["dd-m-0"]["targetKind"] == "measurement"
    assert model["rows"]["dd-m-0"]["targetId"] == "lcp_ms"
    assert model["rows"]["dd-c-0"]["targetKind"] == "coverage"
    assert model["rows"]["dd-c-0"]["targetId"] == "US1-AC1"


def test_screenshot_decision_row_and_baseline_inline_are_modelled(cfg: Config) -> None:
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    csha = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=(3, 3, 3))
    bsha = write_png(base.evidence_dir / "candidate-a" / "home.png", rgb=(9, 1, 1))
    out = _render(
        cfg,
        cand,
        _diff_doc(
            screenshots=[_s("home.png", "changed", sha256=csha, baselineSha256=bsha, bytes=90, baselineBytes=90)]
        ),
    )
    assert 'aria-label="Accept change to screenshot home.png"' in out
    assert "data:image/png;base64," in out  # candidate artifact and/or baseline is embedded
    assert _island(out)["rows"]["dd-s-0"] == {
        "targetKind": "screenshot",
        "targetId": "home.png",
        "sha256": csha,
        "artifactSha256": csha,
    }


def test_json_island_neutralises_script_close(cfg: Config) -> None:
    hostile_path = "a</script><img src=x onerror=alert(1)>.png"
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(screenshots=[_s(hostile_path, "unchanged", sha256=SHA_A, baselineSha256=SHA_A)]))
    start = out.index('id="adlc-diff-model">') + len('id="adlc-diff-model">')
    block = out[start : out.index("</script>", start)]
    assert "</script>" not in block
    assert "\\u003c/script>" in block
    assert any(row["targetId"] == hostile_path for row in json.loads(block)["rows"].values())


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
    decisions = [
        {"id": "dd-m-0", "targetKind": "measurement", "targetId": "lcp_ms", "decision": "reject", "comment": "A 400ms regression is not acceptable.", "annotationIds": ["an-1"]},
        {"id": "dd-c-0", "targetKind": "coverage", "targetId": "US1-AC1", "decision": "accept", "comment": "", "annotationIds": []},
        {"id": "dd-s-0", "targetKind": "screenshot", "targetId": "nested/dir/home.png", "decision": "reject", "comment": "", "annotationIds": []},
    ]
    valid, errors = is_valid("human-feedback-pack", _pack(decisions))
    assert valid, errors


def test_schema_rejects_a_malformed_decision() -> None:
    valid, _errors = is_valid(
        "human-feedback-pack",
        _pack([{"id": "dd-x", "targetKind": "metric", "targetId": "lcp", "decision": "accept"}]),
    )
    assert not valid


def test_model_ids_match_the_id_pattern(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(
        cfg,
        cand,
        _diff_doc(
            measurements=[_m("lcp_ms", value=1.0, baselineValue=0.0, delta=1.0)],
            coverage=[_c("US1-AC1", "lost", present=False, baselinePresent=True)],
            screenshots=[_s("home.png", "unchanged", sha256=SHA_A, baselineSha256=SHA_A)],
        ),
    )
    id_re = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
    assert all(id_re.match(decision_id) for decision_id in _island(out)["rows"])


def test_diff_js_structure_is_preserved() -> None:
    js = asset_source("diff.js")
    assert "window.adlcFeedback = window.adlcFeedback || {" in js
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []," in js
    assert "adlc.diffDecisions." in js
    assert "store.diffDecisions.length = 0" in js
    assert "function sanitize(d)" in js
    assert "targetKind: meta.targetKind" in js
    assert "d.decision !== 'accept' && d.decision !== 'reject'" in js


def test_diff_js_defines_the_shared_store_contract_verbatim() -> None:
    js = read_asset("diff.js")
    assert "window.adlcFeedback = window.adlcFeedback || {" in js
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []," in js
    assert "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); }," in js
    assert "subscribe(fn) { this.listeners.push(fn); }," in js


def test_diff_js_wires_persistence_and_liveness() -> None:
    js = read_asset("diff.js")
    assert "JSON.parse(" in js
    assert "adlc.diffDecisions." in js
    assert "localStorage" in js
    assert "store.notify()" in js
    assert "aria-pressed" in js
    assert "data-blend-toggle" in js
    assert "store.diffDecisions.length = 0" in js


def test_diff_js_sanitises_untrusted_localstorage() -> None:
    js = read_asset("diff.js")
    assert "function sanitize(d)" in js
    assert "targetKind: meta.targetKind" in js
    assert "targetId: meta.targetId" in js
    assert "d.decision !== 'accept' && d.decision !== 'reject'" in js
    assert "function sanitizeAnnotationIds(v)" in js
    assert "ID_RE.test(x)" in js


def _changed_pair(cfg: Config) -> tuple[RunDir, RunDir, str, str]:
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    csha = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=(200, 10, 10))
    bsha = write_png(base.evidence_dir / "candidate-a" / "home.png", rgb=(10, 10, 200))
    write_json(
        diff_path(cand),
        _diff_doc(screenshots=[_s("home.png", "changed", sha256=csha, baselineSha256=bsha, bytes=90, baselineBytes=90)]),
    )
    return cand, base, csha, bsha


def test_no_diff_file_renders_empty(cfg: Config) -> None:
    test_no_diff_file_still_has_empty_decision_model(cfg)


def test_stated_absence_renders_empty(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(baselineRunId=None, reason="run has no referencesRun"))
    assert _island(out)["rows"] == {}
    assert "No evidence diff decisions are available" in out


def test_unreadable_diff_file_renders_empty(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    diff_path(cand).write_text("{ this is not json", encoding="utf-8")
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    out = cand.report.read_text(encoding="utf-8")
    assert _island(out)["rows"] == {}
    assert "No evidence diff decisions are available" in out


def test_measurement_row_shows_values_and_delta(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(
        cfg,
        cand,
        _diff_doc(measurements=[_m("lcp_ms", value=2200.0, baselineValue=1800.0, delta=400.0, budget=2500.0, passed=True, baselinePassed=True, collector="lighthouse")]),
    )
    assert "Evidence changes since baseline" in out
    assert "lcp_ms" in out
    assert "1800" in out and "2200" in out
    assert "+400" in out
    assert 'scope="row"' in out and 'scope="col"' in out


def test_boolean_measurement_values_include_words(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m("flag", value=True, baselineValue=False, delta=None)]))
    assert "&#10003; yes" in out
    assert "&#10007; no" in out


def test_budget_crossing_is_surfaced_without_colour(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m("lcp_ms", value=2600.0, baselineValue=1800.0, delta=800.0, budget=2500.0, passed=False, baselinePassed=True, budgetCrossed="entered_breach")]))
    assert "Entered breach" in out
    assert "now failing" in out
    assert "budget crossing" in out.lower()


def test_left_breach_is_labelled(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m("lcp_ms", value=2100.0, baselineValue=2600.0, delta=-500.0, budget=2500.0, passed=True, baselinePassed=False, budgetCrossed="left_breach")]))
    assert "Left breach" in out
    assert "-500" in out


def test_lost_coverage_is_surfaced_as_regression(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(coverage=[_c("US1-AC1", "lost", present=False, baselinePresent=True, evidenceKinds=[], baselineEvidenceKinds=["screenshot"])]))
    assert "Evidence lost" in out
    assert "regression" in out.lower()
    assert "lost evidence" in out.lower()


def test_gained_coverage_is_labelled(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(coverage=[_c("US1-AC2", "gained", present=True, baselinePresent=False, evidenceKinds=["screenshot"], baselineEvidenceKinds=[])]))
    assert "Evidence gained" in out
    assert "US1-AC2" in out


def test_changed_screenshot_inlines_pair_and_blend(cfg: Config) -> None:
    cand, _base, _csha, _bsha = _changed_pair(cfg)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    overlay = cand.report.read_text(encoding="utf-8").split("<!-- ADLC human-feedback overlay -->", 1)[1]
    assert "Overlay difference blend" in overlay
    assert overlay.count("data:image/png;base64,") >= 2
    assert "SHA-256 changed:" in overlay


def test_base64_decodes_to_the_captured_png(cfg: Config) -> None:
    cand, _base, _csha, _bsha = _changed_pair(cfg)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    overlay = cand.report.read_text(encoding="utf-8").split("<!-- ADLC human-feedback overlay -->", 1)[1]
    blobs = {base64.b64decode(m) for m in re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", overlay)}
    assert blobs
    assert all(b.startswith(b"\x89PNG\r\n\x1a\n") for b in blobs)


def test_added_removed_unchanged_are_treated(cfg: Config) -> None:
    base = _run(cfg, BASE_ID)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    add_sha = write_png(cand.evidence_dir / "candidate-a" / "added.png", rgb=(1, 2, 3))
    rem_sha = write_png(base.evidence_dir / "candidate-a" / "removed.png", rgb=(4, 5, 6))
    unc_sha = write_png(cand.evidence_dir / "candidate-a" / "same.png", rgb=(7, 8, 9))
    write_png(base.evidence_dir / "candidate-a" / "same.png", rgb=(7, 8, 9))
    out = _render(
        cfg,
        cand,
        _diff_doc(screenshots=[_s("added.png", "added", sha256=add_sha, bytes=90), _s("removed.png", "removed", baselineSha256=rem_sha, baselineBytes=90), _s("same.png", "unchanged", sha256=unc_sha, baselineSha256=unc_sha, bytes=90, baselineBytes=90)]),
    )
    assert "Added" in out and "Removed" in out
    assert "unchanged &mdash; identical hash" in out
    assert unc_sha in out


def test_over_per_image_budget_degrades_with_reason(cfg: Config) -> None:
    cfg.raw["feedback"] = {"perArtifactBytes": 8}
    cand, _base, _csha, _bsha = _changed_pair(cfg)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    overlay = cand.report.read_text(encoding="utf-8").split("<!-- ADLC human-feedback overlay -->", 1)[1]
    assert "per-artifact budget" in overlay
    assert "exceeds" in overlay
    assert "Difference blend unavailable" in overlay


def test_section_budget_exhaustion_degrades_with_reason(cfg: Config) -> None:
    cfg.raw["feedback"] = {"totalBytes": 100}
    cand, _base, _csha, _bsha = _changed_pair(cfg)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    overlay = cand.report.read_text(encoding="utf-8").split("<!-- ADLC human-feedback overlay -->", 1)[1]
    assert "document budget is exhausted" in overlay


def test_unreadable_baseline_image_degrades_not_crashes(cfg: Config) -> None:
    base = _run(cfg, BASE_ID)
    (base.evidence_dir / "candidate-a").mkdir(parents=True, exist_ok=True)
    cand = _run(cfg, CAND_ID, references=BASE_ID)
    csha = write_png(cand.evidence_dir / "candidate-a" / "home.png", rgb=(9, 9, 9))
    out = _render(cfg, cand, _diff_doc(screenshots=[_s("home.png", "changed", sha256=csha, baselineSha256="b" * 64, bytes=90, baselineBytes=90)]))
    assert "image not found in the run directory" in out
    assert "b" * 64 in out
    assert "Difference blend unavailable" in out


def test_hostile_metric_id_is_escaped_everywhere(cfg: Config) -> None:
    hostile = '<script>alert(1)</script>" src="./x'
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m(hostile, value=1.0, baselineValue=0.0, delta=1.0)]))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert 'src="./' not in out
    assert 'href="./' not in out


def test_embedded_json_neutralises_script_close(cfg: Config) -> None:
    test_json_island_neutralises_script_close(cfg)


def test_controls_are_labelled_per_row(cfg: Config) -> None:
    test_measurement_and_coverage_rows_carry_decision_targets(cfg)


def test_uses_real_table_semantics(cfg: Config) -> None:
    cand = _run(cfg, CAND_ID)
    out = _render(cfg, cand, _diff_doc(measurements=[_m("lcp_ms", value=1.0, baselineValue=0.0, delta=1.0)]))
    assert 'scope="col"' in out
    assert '<th scope="row"' in out


def test_full_report_is_self_contained_with_diff(cfg: Config) -> None:
    cand, _base, _csha, _bsha = _changed_pair(cfg)
    reduce_run(cfg, cand)
    run_report(cfg, cand)
    html = cand.report.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "Evidence changes since baseline" in html
    assert 'src="./' not in html
    assert 'href="./' not in html
