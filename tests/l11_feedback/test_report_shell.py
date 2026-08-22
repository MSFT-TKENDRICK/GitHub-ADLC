"""PWA report shell invariants plus the human-feedback overlay attachment."""

from __future__ import annotations

from adlc.config import Config
from adlc.report.html import fill, render, run_report
from adlc.report.overlay import asset_source, escape, json_script, omission
from adlc.runs import RunDir

CONFORMANCE_FRAGMENTS = ("ADLC run", "Gates", "Evidence", "Decisions", "Task graph", "flowchart")


def _created_run(cfg: Config, run_id: str = "2026-08-20-she1") -> RunDir:
    rd = RunDir(cfg, run_id)
    rd.create(profile=cfg.profile, brief_text="# Brief\n\nA change.\n")
    return rd


def test_public_api_is_importable_and_callable() -> None:
    assert callable(render)
    assert callable(run_report)


def test_render_starts_with_doctype(cfg: Config) -> None:
    assert render(cfg, _created_run(cfg)).startswith("<!doctype html>")


def test_render_contains_all_conformance_fragments(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    for fragment in CONFORMANCE_FRAGMENTS:
        assert fragment in html, f"missing conformance fragment: {fragment!r}"


def test_render_is_self_contained(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    assert 'src="./' not in html
    assert 'href="./' not in html


def test_run_report_writes_pwa_with_overlay(cfg: Config) -> None:
    rd = _created_run(cfg)
    result = run_report(cfg, rd)
    written = rd.report.read_text(encoding="utf-8")
    assert result == {"path": str(rd.report), "bytes": len(written)}
    assert written.startswith("<!doctype html>")
    assert '<script type="application/json" id="adlc-evidence-data">' in written
    assert '<script type="application/json" id="adlc-critique-data">' in written
    assert '<script type="application/json" id="adlc-diff-model">' in written
    assert '<script type="application/json" id="adlc-feedback-config">' in written
    assert written.index("<!-- ADLC human-feedback overlay -->") < written.index("</body>")


def test_run_report_writes_file_and_returns_shape(cfg: Config) -> None:
    rd = _created_run(cfg)
    result = run_report(cfg, rd)
    written = rd.report.read_text(encoding="utf-8")
    assert result == {"path": str(rd.report), "bytes": len(written)}
    assert written.startswith("<!doctype html>")


def test_assets_contain_braces_and_survive_rendering(cfg: Config) -> None:
    html = render(cfg, _created_run(cfg))
    assert "{" in html and "}" in html
    assert ":root {" in html


def test_fill_does_not_rescan_substituted_braces() -> None:
    out = fill("{{BODY}}", {"BODY": "{ not: a, format: field }"})
    assert out == "{ not: a, format: field }"


def test_overlay_asset_loading() -> None:
    assert "window.adlcFeedback" in asset_source("feedback.js")


def test_json_script_guards_against_early_termination() -> None:
    out = json_script("x", {"path": "</script><script>alert(1)</script>"})
    assert out.count("</script>") == 1
    assert "\\u003c/script>" in out


def test_escape_and_omission_helpers() -> None:
    assert escape('<a href="x">&y') == "&lt;a href=&quot;x&quot;&gt;&amp;y"
    assert escape(None) == ""
    assert omission("No data <yet>") == '<p class="muted">No data &lt;yet&gt;</p>'


def test_main_pwa_escapes_artifact_paths(cfg: Config) -> None:
    rd = _created_run(cfg)
    seed = rd.path / "seed.json"
    import json

    data = json.loads(seed.read_text(encoding="utf-8"))
    data["artifacts"] = [
        {
            "path": "evidence/<script>alert(1)</script>.png",
            "kind": "screenshot",
            "bytes": 2048,
            "sha256": "ab" * 32,
        }
    ]
    seed.write_text(json.dumps(data), encoding="utf-8")
    out = render(cfg, rd)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
