"""The console is the proof that the feedback contract is GUI-agnostic.

A contract with one consumer is not a contract; it is an internal function call
with extra ceremony. These tests exist to keep the second consumer honest:

* it must not import the report package, even transitively;
* it must be buildable from a manifest alone, with no ``RunDir`` in sight;
* it must survive being emailed as one file, like the report it does not share
  code with;
* a hostile string anywhere in the manifest must not become markup.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.stages.feedback_console import build_console, console_asset, write_console
from adlc.stages.feedback_sdk import sdk_source
from adlc.stages.feedback_targets import compute_targets

from .conftest import CANDIDATE_SHA, make_run

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture
def targets(cfg: Config) -> dict:
    run = make_run(
        cfg,
        "2026-08-20-c0de",
        head_sha=CANDIDATE_SHA,
        screenshots={"home.png": (10, 20, 30)},
        measurements=[
            {
                "metricId": "lcp_ms",
                "value": 2200.0,
                "budget": 2500.0,
                "passed": True,
                "collector": "lighthouse",
            }
        ],
    )
    reduce_run(cfg, run)
    return compute_targets(cfg, run)


# ---------------------------------------------------------------------------
# Independence from the GUI it is meant to outlive
# ---------------------------------------------------------------------------


def test_the_console_does_not_import_the_report_package() -> None:
    """The whole point. If this fails, the seam is decorative.

    Checked in a fresh interpreter rather than against the current
    ``sys.modules``, because this test suite imports the report package for
    other reasons and would otherwise pass for the wrong reason.
    """
    code = (
        "import sys, json;"
        "import adlc.stages.feedback_console;"
        "print(json.dumps([m for m in sys.modules if m.startswith('adlc.stages.report')]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": SRC, "PATH": "", "SYSTEMROOT": "C:\\Windows"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == []


def test_the_console_source_never_names_the_report_package() -> None:
    """A lazy import inside a function would slip past the module-level check."""
    source = Path(__file__).resolve().parents[2] / "src" / "adlc" / "stages" / "feedback_console.py"
    text = source.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("#", "*", '"'))
    )
    assert "stages.report" not in code
    assert "stages import report" not in code


def test_the_console_client_reads_only_the_manifest_and_the_sdk() -> None:
    """No reaching into report.html's globals, ids or storage keys."""
    js = console_asset("console.js")
    for forbidden in ("window.adlcFeedback", "adlc-annotations", "adlc.critiques", "reportDigest\""):
        assert forbidden not in js, f"console.js reaches into report internals: {forbidden}"
    assert "AdlcFeedbackSDK" in js
    assert "#adlc-targets" in js


def test_building_the_console_needs_no_run_directory(targets: dict, tmp_path: Path) -> None:
    """The manifest is the whole input. A GUI author gets a JSON file, nothing else."""
    doc = json.loads(json.dumps(targets))  # prove it survives a round trip through a file
    html = build_console(doc)
    assert "<!DOCTYPE html>" in html
    assert doc["run"]["runId"] in html
    written = write_console(doc, tmp_path / "nested" / "console.html")
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == html


def test_a_foreign_document_is_refused_rather_than_half_rendered(targets: dict) -> None:
    bad = dict(targets)
    bad["schemaVersion"] = "adlc-feedback-targets/v2"
    with pytest.raises(ValueError, match="adlc-feedback-targets/v1"):
        build_console(bad)


# ---------------------------------------------------------------------------
# Self-containment -- the same guarantee report.html carries
# ---------------------------------------------------------------------------


def test_the_console_is_a_single_file(targets: dict) -> None:
    html = build_console(targets)
    assert "<link" not in html
    assert 'src="http' not in html
    assert "<script src=" not in html
    # data: URIs are inlined bytes, not references, so they are allowed.
    for match in re.finditer(r'src="([^"]*)"', html):
        assert match.group(1).startswith("data:"), match.group(1)


def test_the_sdk_is_inlined_verbatim(targets: dict) -> None:
    """One source of truth for the digest. A drifting copy is a silent forgery."""
    html = build_console(targets)
    assert sdk_source() in html


def test_every_sentinel_is_consumed(targets: dict) -> None:
    html = build_console(targets)
    for sentinel in ("/*ADLC:CSS*/", "/*ADLC:TARGETS*/", "/*ADLC:SDK*/", "/*ADLC:CONSOLE*/"):
        assert sentinel not in html, f"{sentinel} survived assembly"


def test_a_missing_sentinel_fails_loudly(monkeypatch, targets: dict) -> None:
    """A silently unsubstituted template renders a blank page with no clue why."""
    import adlc.stages.feedback_console as mod

    monkeypatch.setattr(mod, "console_asset", lambda name: "<html></html>")
    with pytest.raises(RuntimeError, match="exactly once"):
        mod.build_console(targets)


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def test_a_hostile_path_cannot_terminate_the_json_island(targets: dict) -> None:
    """A `</script>` in an artifact path would drop the rest of the page into the DOM."""
    doc = json.loads(json.dumps(targets))
    hostile = "evidence/</script><img src=x onerror=alert(1)>.png"
    doc["artifacts"] = list(doc["artifacts"])
    if doc["artifacts"]:
        doc["artifacts"][0] = dict(doc["artifacts"][0], path=hostile)
    else:
        pytest.skip("fixture produced no artifacts to poison")

    html = build_console(doc)
    assert "</script><img" not in html

    island = re.search(
        r'<script type="application/json" id="adlc-targets">(.*?)</script>', html, re.DOTALL
    )
    assert island is not None
    # The bytes survive intact after JSON parsing -- escaped, not censored.
    assert json.loads(island.group(1))["artifacts"][0]["path"] == hostile


def test_line_separators_cannot_break_the_island(targets: dict) -> None:
    """U+2028/U+2029 are newlines to a JS parser but not to json.dumps."""
    doc = json.loads(json.dumps(targets))
    doc["run"] = dict(doc["run"], summary="a\u2028b\u2029c")
    html = build_console(doc)
    assert "\u2028" not in html
    assert "\u2029" not in html


# ---------------------------------------------------------------------------
# The client actually parses
# ---------------------------------------------------------------------------


@needs_node
def test_console_js_is_syntactically_valid(tmp_path: Path) -> None:
    script = tmp_path / "console.js"
    script.write_text(console_asset("console.js"), encoding="utf-8")
    proc = subprocess.run(
        [NODE or "node", "--check", str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


@needs_node
def test_the_console_html_carries_a_parseable_manifest(targets: dict, tmp_path: Path) -> None:
    """Round-trip the island through a real JS engine, not a Python regex."""
    html = build_console(targets)
    (tmp_path / "console.html").write_text(html, encoding="utf-8")
    runner = tmp_path / "runner.js"
    runner.write_text(
        "const fs = require('fs');\n"
        "const html = fs.readFileSync('console.html', 'utf8');\n"
        "const m = html.match(/id=\"adlc-targets\">([\\s\\S]*?)<\\/script>/);\n"
        "const doc = JSON.parse(m[1]);\n"
        "console.log(JSON.stringify({v: doc.schemaVersion, run: doc.run.runId}));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE or "node", str(runner)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["v"] == "adlc-feedback-targets/v1"
    assert out["run"] == targets["run"]["runId"]


# ---------------------------------------------------------------------------
# Accessibility floor
# ---------------------------------------------------------------------------


def test_the_shell_carries_the_accessibility_scaffolding(targets: dict) -> None:
    html = build_console(targets)
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert 'class="skip"' in html
    assert "<html lang=" in html
    for control in ("verdict", "route", "summary", "submitted-by"):
        assert f'for="{control}"' in html, f"{control} has no label"


def test_no_control_is_pointer_only() -> None:
    """Every action must be a real button or a real form submit."""
    js = console_asset("console.js")
    handlers = re.findall(r'addEventListener\("([a-z]+)"', js)
    pointer_only = {"mousedown", "mouseup", "mousemove", "dblclick", "drag"}
    assert not pointer_only.intersection(handlers), sorted(set(handlers))
    # Pointer events are allowed, but only as a shortcut that fills the form.
    assert "form.elements.comment.focus()" in js
