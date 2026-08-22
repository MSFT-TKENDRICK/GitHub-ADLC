"""Annotation overlay attached to the redesigned PWA report."""

from __future__ import annotations

import base64
import copy
import json
import re
from importlib.resources import files
from typing import Any

import pytest

from adlc.reduce import reduce_run
from adlc.report.overlay import asset_source
from adlc.runs import read_json, sha256_bytes, write_json
from adlc.schemas import is_valid
from adlc.stages.report import run_report
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run, png_bytes

DATA_URI_RE = re.compile(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"')
JSON_BLOCK_RE = re.compile(
    r'<script type="application/json" id="adlc-evidence-data">(.*?)</script>', re.DOTALL
)


def read_asset(name: str) -> str:
    return (files("adlc") / "assets" / "feedback-overlay" / name).read_text(encoding="utf-8")


CONTRACT_LINES = (
    "window.adlcFeedback = window.adlcFeedback || {",
    "annotations: [], critiques: [], diffDecisions: [], listeners: [],",
    "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },",
    "subscribe(fn) { this.listeners.push(fn); },",
)


def _render(cfg: Any, rd: Any) -> str:
    reduce_run(cfg, rd)
    run_report(cfg, rd)
    return rd.report.read_text(encoding="utf-8")


def _payload(html: str) -> dict[str, Any]:
    match = JSON_BLOCK_RE.search(html)
    assert match, "expected the evidence data island"
    return json.loads(match.group(1))


def _annotation(**over: Any) -> dict[str, Any]:
    ann: dict[str, Any] = {
        "id": "an-abc123",
        "artifactSha256": "c" * 64,
        "artifactPath": "evidence/candidate-a/home.png",
        "artifactKind": "screenshot",
        "shape": "rect",
        "severity": "info",
        "comment": "Looks off.",
        "requirementIds": ["US1-AC1"],
        "geometry": {"points": [[0.1, 0.1], [0.4, 0.35]]},
    }
    ann.update(over)
    if ann.get("shape") == "whole":
        ann.pop("geometry", None)
    return ann


def _pack_with(valid_pack: dict[str, Any], ann: dict[str, Any]) -> dict[str, Any]:
    pack = copy.deepcopy(valid_pack)
    pack["annotations"] = [ann]
    return pack


def _data_uri_len(raw: bytes, mime: str = "image/png") -> int:
    return len(f"data:{mime};base64,") + 4 * ((len(raw) + 2) // 3)


def test_png_inlines_as_data_uri_that_roundtrips(cfg: Any) -> None:
    rgb = (10, 20, 30)
    data = png_bytes(rgb=rgb)
    rd = make_run(cfg, "2026-08-20-aa01", head_sha=CANDIDATE_SHA, screenshots={"home.png": rgb})
    out = _render(cfg, rd)

    match = DATA_URI_RE.search(out)
    assert match, "expected an inlined data:image/png URI"
    assert base64.b64decode(match.group(1)) == data
    assert sha256_bytes(data) in out
    assert '<figure class="annot-artifact"' in out
    assert '<div class="annot-mount"></div>' in out


def test_overlay_injects_assets_and_requirements(cfg: Any) -> None:
    rd = make_run(cfg, "2026-08-20-aa02", head_sha=CANDIDATE_SHA, screenshots={"home.png": (7, 8, 9)})
    out = _render(cfg, rd)
    assert asset_source("annotate.css") in out
    assert asset_source("annotate.js") in out
    assert "US1-AC1" in JSON_BLOCK_RE.search(out).group(1)  # type: ignore[union-attr]


def test_per_image_over_budget_is_kept_and_degraded(cfg: Any) -> None:
    cfg.raw["feedback"] = {"perArtifactBytes": 8}
    rd = make_run(cfg, "2026-08-20-aa03", head_sha=CANDIDATE_SHA, screenshots={"home.png": (1, 2, 3)})
    out = _render(cfg, rd)
    assert "per-artifact budget" in out
    assert 'data-degraded="1"' in out
    overlay = out[out.index("<!-- ADLC human-feedback overlay -->") :]
    assert "data:image/png;base64," not in overlay
    assert "not inlined" in out


def test_total_budget_stops_second_inline(cfg: Any) -> None:
    cfg.raw["feedback"] = {"totalBytes": _data_uri_len(png_bytes(rgb=(1, 2, 3)))}
    rd = make_run(
        cfg,
        "2026-08-20-aa04",
        head_sha=CANDIDATE_SHA,
        screenshots={"a.png": (1, 2, 3), "b.png": (200, 100, 50)},
    )
    out = _render(cfg, rd)
    assert len(DATA_URI_RE.findall(out)) == 1
    assert "document budget is exhausted" in out
    assert _payload(out)["artifacts"][1]["reason"]


def test_hostile_artifact_path_is_escaped_in_markup_and_json_guarded(cfg: Any) -> None:
    rd = make_run(cfg, "2026-08-20-aa05", head_sha=CANDIDATE_SHA, screenshots={})
    hostile = "evidence/</script><script>alert(1)</script>.png"
    seed = read_json(rd.path / "seed.json")
    seed["artifacts"] = [
        {
            "path": hostile,
            "kind": "screenshot",
            "mimeType": "image/png",
            "bytes": 64,
            "sha256": "d" * 64,
        }
    ]
    write_json(rd.path / "seed.json", seed)
    run_report(cfg, rd)
    out = rd.report.read_text(encoding="utf-8")

    assert "<script>alert(1)</script>" not in out
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in out
    block = JSON_BLOCK_RE.search(out).group(1)  # type: ignore[union-attr]
    assert "</script>" not in block
    assert "\\u003c/script>" in block
    assert json.loads(block)["artifacts"][0]["path"] == hostile


def test_non_image_artifact_is_not_annotatable(cfg: Any) -> None:
    rd = make_run(cfg, "2026-08-20-aa06", head_sha=CANDIDATE_SHA)
    har = rd.evidence_dir / "candidate-a" / "net.har"
    har.parent.mkdir(parents=True, exist_ok=True)
    har.write_text("{}", encoding="utf-8")
    out = _render(cfg, rd)
    assert "net.har" in out
    assert _payload(out)["artifacts"] == []


def test_no_artifacts_renders_heading_and_stated_omission(cfg: Any) -> None:
    rd = make_run(cfg, "2026-08-20-aa00", head_sha=CANDIDATE_SHA)
    out = _render(cfg, rd)
    assert "Annotate evidence" in out
    assert "No annotatable artifacts were captured" in out
    assert '<script type="application/json" id="adlc-evidence-data">' in out


def test_render_injects_both_assets_and_requirements(cfg: Any) -> None:
    test_overlay_injects_assets_and_requirements(cfg)


def test_per_image_over_declared_budget_is_kept_and_degraded(cfg: Any) -> None:
    test_per_image_over_budget_is_kept_and_degraded(cfg)


def test_read_path_over_budget_is_degraded(cfg: Any) -> None:
    cfg.raw["feedback"] = {"perArtifactBytes": 8}
    rd = make_run(cfg, "2026-08-20-aa08", head_sha=CANDIDATE_SHA, screenshots={"home.png": (1, 2, 3)})
    seed = read_json(rd.path / "seed.json")
    seed["artifacts"] = [
        {
            "path": "evidence/candidate-a/home.png",
            "kind": "screenshot",
            "mimeType": "image/png",
            "bytes": 4,
            "sha256": sha256_bytes(png_bytes(rgb=(1, 2, 3))),
        }
    ]
    write_json(rd.path / "seed.json", seed)
    run_report(cfg, rd)
    out = rd.report.read_text(encoding="utf-8")
    assert "exceeds" in out and "per-artifact budget" in out
    overlay = out[out.index("<!-- ADLC human-feedback overlay -->") :]
    assert "data:image/png;base64," not in overlay


def test_totals_line_counts_inlined_and_skipped(cfg: Any) -> None:
    cfg.raw["feedback"] = {"perArtifactBytes": 8}
    rd = make_run(
        cfg,
        "2026-08-20-aa09",
        head_sha=CANDIDATE_SHA,
        screenshots={"a.png": (1, 2, 3), "b.png": (4, 5, 6)},
    )
    out = _render(cfg, rd)
    assert "Inlined 0 image(s)" in out
    assert "2 image(s) not inlined" in out


def test_one_document_budget_is_shared_by_every_section(cfg: Any) -> None:
    cfg.raw["feedback"] = {"totalBytes": _data_uri_len(png_bytes())}
    rd = make_run(
        cfg,
        "2026-08-20-aa10",
        head_sha=CANDIDATE_SHA,
        screenshots={"a.png": (1, 2, 3), "b.png": (4, 5, 6)},
    )
    out = _render(cfg, rd)
    payload = _payload(out)
    assert sum(1 for a in payload["artifacts"] if a["inlined"]) == 1
    assert "document budget is exhausted" in out


def test_budget_is_charged_in_encoded_not_raw_bytes(cfg: Any) -> None:
    raw_len = len(png_bytes())
    cfg.raw["feedback"] = {"totalBytes": raw_len + 1}
    rd = make_run(cfg, "2026-08-20-aa11", head_sha=CANDIDATE_SHA, screenshots={"a.png": (1, 2, 3)})
    out = _render(cfg, rd)
    assert _payload(out)["artifacts"][0]["inlined"] is False
    assert "document budget is exhausted" in out


def test_svg_is_refused_with_stated_reason(cfg: Any) -> None:
    rd = make_run(cfg, "2026-08-20-aa12", head_sha=CANDIDATE_SHA)
    seed = read_json(rd.path / "seed.json")
    seed["artifacts"] = [
        {
            "path": "evidence/candidate-a/diagram.svg",
            "kind": "image",
            "mimeType": "image/svg+xml",
            "bytes": 200,
            "sha256": "c" * 64,
        }
    ]
    write_json(rd.path / "seed.json", seed)
    run_report(cfg, rd)
    out = rd.report.read_text(encoding="utf-8")
    assert "SVG" in out and "script" in out
    assert "data:image/svg" not in out
    assert 'data-degraded="1"' in out


def test_hostile_artifact_path_is_escaped_in_markup(cfg: Any) -> None:
    test_hostile_artifact_path_is_escaped_in_markup_and_json_guarded(cfg)


def test_embedded_json_cannot_terminate_the_script_block(cfg: Any) -> None:
    test_hostile_artifact_path_is_escaped_in_markup_and_json_guarded(cfg)


def test_non_image_artifact_is_table_row_only(cfg: Any) -> None:
    test_non_image_artifact_is_not_annotatable(cfg)


def test_rendered_report_is_self_contained(cfg: Any) -> None:
    rd = make_run(cfg, "2026-08-20-aa07", head_sha=CANDIDATE_SHA, screenshots={"home.png": (9, 9, 9)})
    html = _render(cfg, rd)
    assert 'src="./' not in html
    assert 'href="./' not in html
    assert 'src="data:image/png;base64,' in html


@pytest.mark.parametrize(
    "over",
    [
        {},
        {"shape": "point", "geometry": {"points": [[0.5, 0.5]]}},
        {"shape": "whole"},
        {"shape": "freehand", "geometry": {"points": [[0.1, 0.1], [0.2, 0.2], [0.3, 0.25]]}},
        {"shape": "arrow", "geometry": {"points": [[0.0, 0.0], [1.0, 1.0]]}},
        {"shape": "highlight", "geometry": {"points": [[0.2, 0.2], [0.6, 0.7]]}},
        {"severity": "blocker"},
        {"requirementIds": []},
        {"artifactPath": "", "artifactKind": ""},
    ],
)
def test_emitted_annotation_variants_validate(valid_pack: dict[str, Any], over: dict[str, Any]) -> None:
    ok, errors = is_valid("human-feedback-pack", _pack_with(valid_pack, _annotation(**over)))
    assert ok, errors


@pytest.mark.parametrize(
    "over",
    [
        {"geometry": {"points": [[1.5, 0.1], [0.2, 0.2]]}},
        {"comment": ""},
        {"artifactSha256": "not-a-sha"},
        {"shape": "circle"},
    ],
)
def test_malformed_annotation_is_rejected(valid_pack: dict[str, Any], over: dict[str, Any]) -> None:
    ok, _ = is_valid("human-feedback-pack", _pack_with(valid_pack, _annotation(**over)))
    assert not ok


def test_extra_annotation_property_is_rejected(valid_pack: dict[str, Any]) -> None:
    ann = _annotation()
    ann["unexpected"] = "x"
    ok, _ = is_valid("human-feedback-pack", _pack_with(valid_pack, ann))
    assert not ok


def test_annotation_asset_keeps_keyboard_a11y_and_registry_contract() -> None:
    js = asset_source("annotate.js")
    for line in (
        "window.adlcFeedback = window.adlcFeedback || {",
        "annotations: [], critiques: [], diffDecisions: [], listeners: [],",
        "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },",
        "subscribe(fn) { this.listeners.push(fn); },",
    ):
        assert line in js
    assert "function clamp01" in js
    assert "function normalizePoint" in js
    assert "comment.setAttribute('aria-invalid', 'true')" in js
    assert "Press Delete again to delete annotation" in js


def test_css_has_focus_indicators_and_non_colour_severity() -> None:
    css = asset_source("annotate.css")
    assert ":focus" in css and ":focus-visible" in css
    assert ".annot-badge.sev-blocker" in css and ".annot-badge.sev-info" in css
    assert "border-style: dashed" in css or "border-style: double" in css


def test_js_carries_verbatim_cross_layer_contract() -> None:
    js = read_asset("annotate.js")
    for line in CONTRACT_LINES:
        assert line in js, f"missing shared-registry contract line: {line!r}"


def test_js_has_geometry_normalisation_helpers() -> None:
    js = read_asset("annotate.js")
    assert "function clamp01" in js
    assert "function normalizePoint" in js
    assert "function buildGeometry" in js
    assert "(clientX - rect.left)" in js and "rect.width" in js
    assert "n > 1 ? 1" in js


def test_js_has_keyboard_and_announcement_paths() -> None:
    js = read_asset("annotate.js")
    assert "addEventListener('submit'" in js
    assert "ev.key === 'Delete'" in js and "ev.key === 'Enter'" in js
    assert "Press Delete again to delete annotation" in js
    assert "'aria-keyshortcuts': 'Enter Delete'" in js
    assert "'aria-live': 'polite'" in js
    assert "role: 'status'" in js
    assert "'Editing '" in js


def test_js_links_annotation_validation_to_comment_field() -> None:
    js = read_asset("annotate.js")
    assert "'aria-describedby': status.getAttribute('id')" in js
    assert "comment.setAttribute('aria-invalid', 'true')" in js
    assert "comment.focus()" in js
    assert "comment.removeAttribute('aria-invalid')" in js


def test_js_summarises_freehand_geometry_and_disables_unused_region_fields() -> None:
    js = read_asset("annotate.js")
    assert "points.length + ' points spanning '" in js
    assert "ann.shape + ', ' + fmtPts" in js
    assert "inp.setAttribute('disabled', 'disabled')" in js
    assert "shapeSel.addEventListener('change'" in js


def test_js_sanitize_hardens_the_id_field() -> None:
    js = read_asset("annotate.js")
    assert "typeof a.id === 'string'" in js
    assert "a.id.length <= 64" in js


def test_js_persists_per_run_and_guards_storage() -> None:
    js = read_asset("annotate.js")
    assert "adlc.annotations." in js
    assert "localStorage" in js
    assert "store.notify()" in js
    assert "store.subscribe(" in js
    assert "catch (e)" in js
