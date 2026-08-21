"""Layer 4 -- the evidence section as an annotatable surface.

These tests pin Layer 4's guarantees end to end, all offline with no image
library and no JS runtime:

* image artifacts inline as ``data:`` URIs whose base64 round-trips *exactly*
  back to the real PNG bytes the conftest writer produced;
* the per-image and whole-report byte budgets are enforced, and an over-budget
  image is never silently dropped -- the row and a degraded figure survive with
  the hash, the size and a plain-language reason;
* SVG is refused with a stated reason (it is a script-execution vector);
* every untrusted artifact path is HTML-escaped in markup and ``\\u003c``-escaped
  inside the embedded JSON, so a ``</script>`` in a path cannot break out;
* the rendered report stays self-contained (no ``src="./"`` / ``href="./"``);
* the annotation objects the JS emits validate against ``$defs.annotation`` --
  including negative controls proving the schema enforces the [0,1] geometry
  normalisation the JS depends on;
* the JS/CSS assets carry the keyboard-first form, the aria-live announcements,
  the normalisation helpers and the verbatim ``window.adlcFeedback`` contract the
  sibling feedback layers rely on.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Any

import pytest

from adlc.runs import RunDir, sha256_bytes
from adlc.schemas import is_valid
from adlc.stages.report.context import ReportContext
from adlc.stages.report.sections import evidence
from adlc.stages.report.shell import read_asset, render_shell
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run, png_bytes

DATA_URI_RE = re.compile(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"')
JSON_BLOCK_RE = re.compile(
    r'<script type="application/json" id="adlc-evidence-data">(.*?)</script>', re.DOTALL
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(cfg: Any, rd: RunDir | None = None, **overrides: Any) -> ReportContext:
    return ReportContext(cfg=cfg, rd=rd or RunDir(cfg, "2026-08-20-ctx0"), **overrides)


def _img_artifact(rel: str, data: bytes, *, kind: str = "screenshot", bytes_: int | None = None,
                  sha: str | None = None) -> dict[str, Any]:
    return {
        "path": rel,
        "kind": kind,
        "bytes": len(data) if bytes_ is None else bytes_,
        "sha256": sha256_bytes(data) if sha is None else sha,
    }


def _annotation(**over: Any) -> dict[str, Any]:
    """One annotation in the exact shape ``annotate.js`` ``sanitize()`` emits."""
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


# ---------------------------------------------------------------------------
# Omission path
# ---------------------------------------------------------------------------


def test_no_artifacts_renders_heading_and_stated_omission(cfg: Any) -> None:
    out = evidence.render(_ctx(cfg))
    assert "<h2>Evidence</h2>" in out
    assert "No artifacts were captured" in out
    # No annotatable surface, no data island, no assets when there is nothing to show.
    assert "annot-root" not in out
    assert "adlc-evidence-data" not in out
    assert read_asset("annotate.js") not in out


# ---------------------------------------------------------------------------
# Inlining + data-URI correctness
# ---------------------------------------------------------------------------


def test_png_inlines_as_data_uri_that_roundtrips(cfg: Any) -> None:
    rgb = (10, 20, 30)
    data = png_bytes(rgb=rgb)
    rd = make_run(cfg, "2026-08-20-aa01", head_sha=CANDIDATE_SHA, screenshots={"home.png": rgb})
    art = _img_artifact("evidence/candidate-a/home.png", data)
    out = evidence.render(_ctx(cfg, rd=rd, artifacts=[art]))

    m = DATA_URI_RE.search(out)
    assert m, "expected an inlined data:image/png URI"
    assert base64.b64decode(m.group(1)) == data  # exact bytes, not a re-encode
    assert art["sha256"][:16] in out  # hash shown
    assert 'class="annot-flag ok">inlined' in out


def test_render_injects_both_assets_and_requirements(cfg: Any) -> None:
    rgb = (7, 8, 9)
    data = png_bytes(rgb=rgb)
    rd = make_run(cfg, "2026-08-20-aa02", head_sha=CANDIDATE_SHA, screenshots={"home.png": rgb})
    out = evidence.render(_ctx(cfg, rd=rd, artifacts=[_img_artifact("evidence/candidate-a/home.png", data)]))
    assert read_asset("annotate.css") in out
    assert read_asset("annotate.js") in out
    # requirement ids from the review pack reach the JS data island for the form.
    block = JSON_BLOCK_RE.search(out)
    assert block and "US1-AC1" in block.group(1)


# ---------------------------------------------------------------------------
# Budget enforcement -- never a silent drop
# ---------------------------------------------------------------------------


def test_per_image_over_declared_budget_is_kept_and_degraded(cfg: Any) -> None:
    huge = evidence.MAX_INLINE_BYTES_PER_ARTIFACT + 1
    art = _img_artifact("evidence/candidate-a/big.png", b"x", bytes_=huge, sha="a" * 64)
    out = evidence.render(_ctx(cfg, artifacts=[art]))
    assert "per-image budget" in out
    assert 'data-degraded="1"' in out
    assert "data:image/png;base64," not in out  # the only image was skipped
    assert "a" * 16 in out  # hash still shown
    assert 'class="annot-flag bad"' in out  # row records "not inlined"


def test_read_path_over_budget_is_degraded(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # File is larger than the (patched) cap, but the *declared* size lies small so the
    # early size check passes and the bounded read is what rejects it.
    rgb = (1, 2, 3)
    data = png_bytes(rgb=rgb)
    monkeypatch.setattr(evidence, "MAX_INLINE_BYTES_PER_ARTIFACT", 8)
    rd = make_run(cfg, "2026-08-20-aa03", head_sha=CANDIDATE_SHA, screenshots={"home.png": rgb})
    art = _img_artifact("evidence/candidate-a/home.png", data, bytes_=4)
    out = evidence.render(_ctx(cfg, rd=rd, artifacts=[art]))
    assert "exceeds" in out and "per-image inline budget" in out
    assert "data:image/png;base64," not in out


def test_total_budget_stops_second_inline(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    a_rgb, b_rgb = (1, 2, 3), (200, 100, 50)
    a_data, b_data = png_bytes(rgb=a_rgb), png_bytes(rgb=b_rgb)
    monkeypatch.setattr(evidence, "MAX_INLINE_BYTES_TOTAL", len(a_data) + 1)
    rd = make_run(
        cfg, "2026-08-20-aa04", head_sha=CANDIDATE_SHA,
        screenshots={"a.png": a_rgb, "b.png": b_rgb},
    )
    arts = [
        _img_artifact("evidence/candidate-a/a.png", a_data),
        _img_artifact("evidence/candidate-a/b.png", b_data),
    ]
    out = evidence.render(_ctx(cfg, rd=rd, artifacts=arts))
    assert len(DATA_URI_RE.findall(out)) == 1  # only the first fits
    assert "total inline budget" in out
    assert "Inlined 1 image(s)" in out
    assert "1 image(s) not inlined" in out


def test_totals_line_counts_inlined_and_skipped(cfg: Any) -> None:
    rgb = (4, 5, 6)
    data = png_bytes(rgb=rgb)
    rd = make_run(cfg, "2026-08-20-aa05", head_sha=CANDIDATE_SHA, screenshots={"home.png": rgb})
    over = _img_artifact("evidence/candidate-a/big.png", b"x",
                         bytes_=evidence.MAX_INLINE_BYTES_PER_ARTIFACT + 1, sha="b" * 64)
    arts = [_img_artifact("evidence/candidate-a/home.png", data), over]
    out = evidence.render(_ctx(cfg, rd=rd, artifacts=arts))
    assert "Inlined 1 image(s) totalling" in out
    assert "1 image(s) not inlined" in out


# ---------------------------------------------------------------------------
# SVG refusal
# ---------------------------------------------------------------------------


def test_svg_is_refused_with_stated_reason(cfg: Any) -> None:
    art = {"path": "evidence/candidate-a/diagram.svg", "kind": "image",
           "bytes": 200, "sha256": "c" * 64}
    out = evidence.render(_ctx(cfg, artifacts=[art]))
    assert "SVG" in out and "script" in out
    assert "data:image/svg" not in out
    assert 'data-degraded="1"' in out


# ---------------------------------------------------------------------------
# Escaping + the embedded-JSON </script> guard
# ---------------------------------------------------------------------------


def test_hostile_artifact_path_is_escaped_in_markup(cfg: Any) -> None:
    art = {"path": "evidence/<script>alert(1)</script>.png", "kind": "screenshot",
           "bytes": 2048, "sha256": "ab" * 32}
    out = evidence.render(_ctx(cfg, artifacts=[art]))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_embedded_json_cannot_terminate_the_script_block(cfg: Any) -> None:
    hostile = "evidence/</script><script>alert(1)</script>.png"
    art = {"path": hostile, "kind": "screenshot", "bytes": 64, "sha256": "d" * 64}
    out = evidence.render(_ctx(cfg, artifacts=[art]))

    block = JSON_BLOCK_RE.search(out)
    assert block, "expected the application/json data island"
    body = block.group(1)
    # A literal </script> inside the island would let the path close the block early.
    assert "</script>" not in body
    assert "\\u003c" in body
    # ...but it still parses, and the hostile path round-trips intact.
    parsed = json.loads(body)
    assert parsed["artifacts"][0]["path"] == hostile
    assert parsed["runId"] == "2026-08-20-ctx0"


# ---------------------------------------------------------------------------
# Non-image artifacts + self-containment
# ---------------------------------------------------------------------------


def test_non_image_artifact_is_table_row_only(cfg: Any) -> None:
    art = {"path": "evidence/candidate-a/net.har", "kind": "har",
           "bytes": 4096, "sha256": "e" * 64}
    out = evidence.render(_ctx(cfg, artifacts=[art]))
    assert "net.har" in out  # row present
    assert '<figure class="annot-artifact"' not in out  # no figure
    block = JSON_BLOCK_RE.search(out)
    assert block and json.loads(block.group(1))["artifacts"] == []


def test_rendered_report_is_self_contained(cfg: Any) -> None:
    rgb = (9, 9, 9)
    data = png_bytes(rgb=rgb)
    rd = make_run(cfg, "2026-08-20-aa06", head_sha=CANDIDATE_SHA, screenshots={"home.png": rgb})
    ctx = _ctx(cfg, rd=rd, artifacts=[_img_artifact("evidence/candidate-a/home.png", data)])
    html = render_shell(ctx, evidence.render(ctx))
    assert 'src="./' not in html
    assert 'href="./' not in html
    assert 'src="data:image/png;base64,' in html  # the inline path was exercised


# ---------------------------------------------------------------------------
# Emitted annotation objects validate against $defs.annotation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {},  # rect, the default
        {"shape": "point", "geometry": {"points": [[0.5, 0.5]]}},
        {"shape": "whole"},  # geometry omitted
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
        {"geometry": {"points": [[1.5, 0.1], [0.2, 0.2]]}},  # out of [0,1] -- normalisation matters
        {"comment": ""},  # empty comment
        {"artifactSha256": "not-a-sha"},  # bad hash
        {"shape": "circle"},  # not in the enum
    ],
)
def test_malformed_annotation_is_rejected(valid_pack: dict[str, Any], over: dict[str, Any]) -> None:
    ok, _ = is_valid("human-feedback-pack", _pack_with(valid_pack, _annotation(**over)))
    assert not ok


def test_extra_annotation_property_is_rejected(valid_pack: dict[str, Any]) -> None:
    ann = _annotation()
    ann["unexpected"] = "x"  # additionalProperties: false
    ok, _ = is_valid("human-feedback-pack", _pack_with(valid_pack, ann))
    assert not ok


# ---------------------------------------------------------------------------
# The JS asset -- structural assertions (no JS runtime here)
# ---------------------------------------------------------------------------

CONTRACT_LINES = (
    "window.adlcFeedback = window.adlcFeedback || {",
    "annotations: [], critiques: [], diffDecisions: [], listeners: [],",
    "notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },",
    "subscribe(fn) { this.listeners.push(fn); },",
)


def test_js_carries_verbatim_cross_layer_contract() -> None:
    js = read_asset("annotate.js")
    for line in CONTRACT_LINES:
        assert line in js, f"missing shared-registry contract line: {line!r}"


def test_js_has_geometry_normalisation_helpers() -> None:
    js = read_asset("annotate.js")
    assert "function clamp01" in js
    assert "function normalizePoint" in js
    assert "function buildGeometry" in js
    # normalisation is a fraction of the rendered box == fraction of natural size.
    assert "(clientX - rect.left)" in js and "rect.width" in js
    assert "n > 1 ? 1" in js  # clamp upper bound


def test_js_has_keyboard_and_announcement_paths() -> None:
    js = read_asset("annotate.js")
    assert "addEventListener('submit'" in js  # create/edit without a pointer
    assert "ev.key === 'Delete'" in js and "ev.key === 'Enter'" in js  # list keyboard ops
    assert "'aria-live': 'polite'" in js  # state changes announced
    assert "role: 'status'" in js
    assert "'Editing '" in js  # entering edit mode is announced, not silent


def test_js_sanitize_hardens_the_id_field() -> None:
    # A corrupt/hostile localStorage id (non-string or > 64 chars) must not be
    # emitted verbatim -- it would violate $defs.annotation's id constraints.
    js = read_asset("annotate.js")
    assert "typeof a.id === 'string'" in js
    assert "a.id.length <= 64" in js


def test_js_persists_per_run_and_guards_storage() -> None:
    js = read_asset("annotate.js")
    assert "adlc.annotations." in js
    assert "localStorage" in js
    assert "store.notify()" in js
    assert "store.subscribe(" in js
    # persistence is wrapped so a disabled/full localStorage never throws.
    assert "catch (e)" in js


# ---------------------------------------------------------------------------
# The CSS asset -- structural assertions
# ---------------------------------------------------------------------------


def test_css_has_focus_indicators_and_non_colour_severity() -> None:
    css = read_asset("annotate.css")
    assert "{" in css and "}" in css
    assert ":focus" in css and ":focus-visible" in css  # visible keyboard focus
    assert ".annot-badge.sev-blocker" in css and ".annot-badge.sev-info" in css
    # severity is not conveyed by hue alone: the badges differ by border style/width.
    assert "border-style: dashed" in css or "border-style: double" in css
