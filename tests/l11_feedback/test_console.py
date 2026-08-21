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
from adlc.stages.evidence_diff import run_evidence_diff
from adlc.stages.feedback_console import build_console, console_asset, write_console
from adlc.stages.feedback_sdk import sdk_source
from adlc.stages.feedback_targets import compute_targets

from .conftest import BASELINE_SHA, CANDIDATE_SHA, make_run

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


# ---------------------------------------------------------------------------
# Annotations must be reviewable, not merely drawable
# ---------------------------------------------------------------------------
#
# An `accessibility-adversary` pass found this console could only CREATE
# annotations. The marks existed solely as SVG inside a `role="presentation"`
# overlay, so a reviewer not looking at the picture could not list what they had
# annotated, read back where a mark landed, correct it, or delete it -- a
# mis-placed mark was permanent in the pack, with "reload and lose everything"
# as the only escape. That made the second reference GUI, whose entire job is to
# prove the contract is GUI-agnostic, evidence only that the contract is
# pointer-and-sight-agnostic up to the moment of creation.


def _extract_function(js: str, name: str) -> str:
    """Pull one top-level function out of the console IIFE by brace matching.

    The console has no module boundary by design -- it is one file that runs in a
    browser -- so a behavioural test has to lift the function out to call it.
    Brace matching rather than a regex, because the body contains braces.
    """
    start = js.index(f"function {name}(")
    depth = 0
    for i in range(js.index("{", start), len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def test_annotations_are_listed_and_not_only_drawn() -> None:
    js = console_asset("console.js")
    assert 'el("ul", { class: "annotations"' in js, "no annotation list is built"
    assert "a.artifactSha256 === artifact.sha256" in js, (
        "the list must be scoped to the artifact whose card it sits in"
    )


def test_every_listed_annotation_can_be_edited_and_deleted() -> None:
    js = console_asset("console.js")
    assert 'text: "Edit"' in js and 'text: "Delete"' in js
    assert "session.removeAnnotation(annotation.id)" in js
    # A bare "Delete" in a list of forty reads as forty identical buttons.
    assert '"aria-label": "Delete " + label' in js
    assert '"aria-label": "Edit " + label' in js


def test_deleting_an_annotation_does_not_strand_focus() -> None:
    """Removing the focused button sends focus to <body> unless told otherwise."""
    js = console_asset("console.js")
    assert '(list.querySelector("button") || shape).focus()' in js


def test_the_annotation_label_carries_severity_and_position() -> None:
    js = console_asset("console.js")
    assert "describeGeometry(annotation)" in js
    for part in ('"Annotation "', '" of "', "annotation.severity"):
        assert part in js, f"the accessible label omits {part}"


@needs_node
def test_geometry_is_described_in_words(tmp_path: Path) -> None:
    """Position is data. A reviewer who cannot see the overlay still needs it."""
    src = _extract_function(console_asset("console.js"), "describeGeometry")
    runner = tmp_path / "geom.js"
    runner.write_text(
        src + "\n"
        "const out = [\n"
        "  describeGeometry({}),\n"
        "  describeGeometry({geometry: {shape: 'point', points: [[0.5, 0.25]]}}),\n"
        "  describeGeometry({geometry: {shape: 'rect',"
        " points: [[0.1, 0.2], [0.35, 0.45]]}}),\n"
        "];\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE or "node", str(runner)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    whole, point, rect = json.loads(proc.stdout)
    assert whole == "whole artifact"
    assert point == "point at 50%, 25%"
    assert rect == "region from 10%, 20% to 35%, 45%"


@needs_node
def test_a_freehand_annotation_is_summarised_not_enumerated(tmp_path: Path) -> None:
    """400 coordinate pairs read aloud is a denial of service, not a description."""
    src = _extract_function(console_asset("console.js"), "describeGeometry")
    runner = tmp_path / "geom.js"
    runner.write_text(
        src + "\n"
        "const points = [];\n"
        "for (let i = 0; i < 400; i++) points.push([0.12 + i / 4000, 0.64 + i / 8000]);\n"
        "console.log(describeGeometry({geometry: {shape: 'freehand', points}}));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE or "node", str(runner)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    described = proc.stdout.strip()
    assert "400 points" in described
    assert "spanning" in described
    # The extent, not the path: a bounded sentence rather than 400 pairs.
    assert len(described) < 120, described


@needs_node
def test_the_sdk_really_removes_what_the_console_asks_it_to(
    targets: dict, tmp_path: Path
) -> None:
    """The delete button is only real if the call behind it is."""
    (tmp_path / "sdk.js").write_text(sdk_source(), encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(
        "const SDK = require('./sdk.js');\n"
        "const targets = require('./targets.json');\n"
        "const s = SDK.createSession(targets);\n"
        "const sha = targets.artifacts[0].sha256;\n"
        "const a = s.addAnnotation({artifactSha256: sha, shape: 'whole',"
        " severity: 'minor', comment: 'first'});\n"
        "s.addAnnotation({artifactSha256: sha, shape: 'whole',"
        " severity: 'minor', comment: 'second'});\n"
        "const before = s.state().annotations.length;\n"
        "s.removeAnnotation(a.id);\n"
        "const after = s.state().annotations;\n"
        "console.log(JSON.stringify({before, after: after.length,"
        " left: after.map(x => x.comment)}));\n",
        encoding="utf-8",
    )
    (tmp_path / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
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
    assert out["before"] == 2
    assert out["after"] == 1
    assert out["left"] == ["second"]


# ---------------------------------------------------------------------------
# Focus, state and status must survive the moment the task completes
# ---------------------------------------------------------------------------


def test_submitting_never_disables_the_focused_button() -> None:
    """Disabling the focused element blurs it to <body>, at the worst moment."""
    js = console_asset("console.js")
    assert "submitButton.disabled" not in js, (
        "disabling the button the user just activated throws focus to the top of "
        "a long document with no way back but Tab"
    )
    assert 'submitButton.setAttribute("aria-busy", "true")' in js
    assert 'submitButton.removeAttribute("aria-busy")' in js


def test_an_unavailable_submit_still_explains_itself() -> None:
    """A `disabled` button is unfocusable, so its reason can never be reached."""
    js = console_asset("console.js")
    assert 'submitButton.setAttribute("aria-disabled", "true")' in js
    assert 'announce($("#submit-note").textContent)' in js


def test_diff_decision_buttons_carry_state_and_row_identity() -> None:
    js = console_asset("console.js")
    assert '"aria-pressed": "false"' in js
    assert 'decision + " change to " + row.targetKind + " " + row.targetId' in js
    assert 'other.setAttribute("aria-pressed", other === button ? "true" : "false")' in js


def test_the_conflict_warning_is_announced(targets: dict) -> None:
    html = build_console(targets)
    assert '<p id="conflicts" role="alert" hidden>' in html, (
        "a silent contradiction is discovered only when submission fails"
    )


def test_the_conflict_alert_is_not_rewritten_on_every_keystroke() -> None:
    """`refreshCounts` runs on every session change, and setSummary notifies.

    Reassigning an assertive live region per character interrupts the user's own
    typing echo, making the summary impossible to compose.
    """
    js = console_asset("console.js")
    assert "lastConflictText" in js
    assert "if (text !== lastConflictText)" in js


def test_focus_is_not_parked_under_the_sticky_header() -> None:
    css = console_asset("console.css")
    assert "scroll-margin-top" in css, (
        "the sticky header paints over whatever the browser scrolls into focus"
    )


def test_severity_is_never_carried_by_colour_alone() -> None:
    """The list item states its severity in words as well as in a border."""
    js = console_asset("console.js")
    assert "annotation.severity" in js
    css = console_asset("console.css")
    assert "li.annotation.sev-blocker" in css


# ---------------------------------------------------------------------------
# The console must read the manifest the way the manifest is actually written
#
# A code reviewer and an architecture reviewer independently found the same
# drift: the console read three keys the manifest schema forbids
# (`additionalProperties: false`) and therefore never emits -- `run.referencesRun`
# (the manifest emits `run.baselineRunId`), `row.candidateValue` (the manifest
# emits `row.value`), and `row.candidateInline` (the candidate image is inlined
# once under `artifacts`, keyed by sha256, and `row.inline` is null by design).
#
# The tests below assert on the DOM the *real* console builds from a *real*
# manifest, because a DOM-level assertion is the one thing that would have caught
# this. The manifests come from `compute_targets` over real runs -- never a
# hand-written fixture, which is exactly how the drift survived: a hand-built
# copy in test_sdk_parity.py once masked the same class of bug.
# ---------------------------------------------------------------------------


@pytest.fixture
def diffed_targets(cfg: Config) -> dict:
    """A real manifest with a baseline, a regressed measurement and a changed
    screenshot whose candidate image is inlined once under ``artifacts``."""
    baseline = make_run(
        cfg,
        "2026-08-19-a1b2",
        head_sha=BASELINE_SHA,
        screenshots={"home.png": (10, 20, 30)},
        measurements=[
            {"metricId": "lcp_ms", "value": 1800.0, "budget": 2500.0, "passed": True}
        ],
        coverage=[
            {"requirementId": "US1-AC1", "present": True, "evidenceKinds": ["screenshot"]}
        ],
    )
    reduce_run(cfg, baseline)
    candidate = make_run(
        cfg,
        "2026-08-20-c0de",
        head_sha=CANDIDATE_SHA,
        references_run=baseline.run_id,
        screenshots={"home.png": (99, 99, 99)},
        measurements=[
            {"metricId": "lcp_ms", "value": 2600.0, "budget": 2500.0, "passed": False}
        ],
        coverage=[{"requirementId": "US1-AC1", "present": False, "evidenceKinds": []}],
    )
    reduce_run(cfg, candidate)
    run_evidence_diff(cfg, candidate)
    return compute_targets(cfg, candidate)


@pytest.fixture
def baseline_but_no_diff_targets(cfg: Config) -> dict:
    """A run that references a baseline but has no evidence diff computed: the
    manifest carries ``run.baselineRunId`` yet ``diff`` is null, so the console
    reaches the empty-diff notice while a baseline genuinely exists."""
    baseline = make_run(
        cfg, "2026-08-19-a1b2", head_sha=BASELINE_SHA, screenshots={"home.png": (10, 20, 30)}
    )
    reduce_run(cfg, baseline)
    candidate = make_run(
        cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA, references_run=baseline.run_id
    )
    reduce_run(cfg, candidate)
    return compute_targets(cfg, candidate)


@pytest.fixture
def reasoning_targets(cfg: Config) -> dict:
    """A real manifest carrying one squad finding (so a critique form renders)
    and one inline artifact (so an annotation form renders alongside it)."""
    run = make_run(
        cfg, "2026-08-20-rzn", head_sha=CANDIDATE_SHA, screenshots={"home.png": (10, 20, 30)}
    )
    run.reviews_dir.mkdir(parents=True, exist_ok=True)
    (run.reviews_dir / "adversarial_review.security-adversary.md").write_text(
        "---\nsquad: adversarial_review\nmember: security-adversary\nverdict: block\n---\n\n"
        "## [high] Unescaped slug\n\nThe repo slug is interpolated raw into an href.\n",
        encoding="utf-8",
    )
    reduce_run(cfg, run)
    return compute_targets(cfg, run)


# A DOM just real enough to let the console's own mount() run to completion in
# node, so a test can read back what the console actually put on the page. There
# is no jsdom or linkedom available here, so this is the closest a test can get
# to a DOM-level assertion -- and a DOM-level assertion is what the manifest-key
# drift needed. The console never touches interaction-only APIs at mount
# (`form.elements`, geometry, pointer capture), so those are omitted.
_MOUNT_HARNESS = r"""
'use strict';
var fs = require('fs');

function FakeNode(tag, ns) {
  this.tag = String(tag).toLowerCase();
  this.ns = ns || null;
  this.attrs = {};
  this.children = [];
  this._text = null;
  this._handlers = {};
  this.parent = null;
  this.value = undefined;
  this.checked = false;
  this.hidden = false;
  this.disabled = false;
}
Object.defineProperty(FakeNode.prototype, 'textContent', {
  get: function () {
    if (this._text !== null) return this._text;
    return this.children.map(function (c) { return c.textContent; }).join('');
  },
  set: function (v) {
    this._text = (v === null || v === undefined) ? '' : String(v);
    this.children = [];
  }
});
Object.defineProperty(FakeNode.prototype, 'firstChild', {
  get: function () { return this.children[0] || null; }
});
FakeNode.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
FakeNode.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
};
FakeNode.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
FakeNode.prototype.appendChild = function (kid) { kid.parent = this; this.children.push(kid); return kid; };
FakeNode.prototype.removeChild = function (kid) {
  this.children = this.children.filter(function (c) { return c !== kid; });
  return kid;
};
FakeNode.prototype.addEventListener = function (type, fn) {
  (this._handlers[type] = this._handlers[type] || []).push(fn);
};
FakeNode.prototype.dispatchEvent = function () { return true; };
FakeNode.prototype.setPointerCapture = function () {};
FakeNode.prototype.releasePointerCapture = function () {};
FakeNode.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 100, height: 100 }; };
FakeNode.prototype.focus = function () {};
FakeNode.prototype.select = function () {};
FakeNode.prototype.click = function () {};

function classListOf(node) {
  return (node.attrs['class'] || '').split(/\s+/).filter(Boolean);
}
function hasClass(node, cls) { return classListOf(node).indexOf(cls) >= 0; }
function lastToken(sel) {
  return sel.trim().split(/\s+/).pop().replace(':checked', '').replace(':scope', '');
}
function matchesSimple(node, token) {
  if (!token) return false;
  if (token[0] === '#') return node.attrs.id === token.slice(1);
  var parts = token.split('.');
  var tag = parts.shift();
  if (tag && node.tag !== tag.toLowerCase()) return false;
  var cls = classListOf(node);
  for (var i = 0; i < parts.length; i++) {
    if (cls.indexOf(parts[i]) < 0) return false;
  }
  return true;
}
function walkDesc(node, visit) {
  for (var i = 0; i < node.children.length; i++) {
    visit(node.children[i]);
    walkDesc(node.children[i], visit);
  }
}
FakeNode.prototype.querySelector = function (sel) {
  var token = lastToken(sel);
  var found = null;
  walkDesc(this, function (n) { if (!found && matchesSimple(n, token)) found = n; });
  return found;
};
FakeNode.prototype.querySelectorAll = function (sel) {
  var token = lastToken(sel);
  var out = [];
  walkDesc(this, function (n) { if (matchesSimple(n, token)) out.push(n); });
  return out;
};

var byId = {};
var document = {
  readyState: 'complete',
  body: new FakeNode('body'),
  createElement: function (t) { return new FakeNode(t); },
  createElementNS: function (ns, t) { return new FakeNode(t, ns); },
  createTextNode: function (s) {
    var n = new FakeNode('#text');
    n._text = (s === null || s === undefined) ? '' : String(s);
    return n;
  },
  getElementById: function (id) { return byId[id] || null; },
  querySelector: function (sel) {
    if (sel[0] === '#') {
      var id = sel.slice(1);
      if (!byId[id]) { var n = new FakeNode('div'); n.attrs.id = id; byId[id] = n; }
      return byId[id];
    }
    return document.body.querySelector(sel);
  },
  querySelectorAll: function (sel) { return document.body.querySelectorAll(sel); },
  addEventListener: function () {}
};

global.document = document;
/* node >= 25 exposes `navigator`, `URL` and `Event` as read-only globals, and
 * the console only touches those inside click handlers this harness never fires,
 * so they are left as node's own. `localStorage` is the one the console reaches
 * at restore time, so force ours in regardless of node's webstorage state. */
Object.defineProperty(global, 'localStorage', {
  configurable: true,
  writable: true,
  value: (function () {
    var m = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null; },
      setItem: function (k, v) { m[k] = String(v); },
      removeItem: function (k) { delete m[k]; }
    };
  })()
});

function queryAll(root, pred) {
  var out = [];
  (function rec(n) {
    if (pred(n)) out.push(n);
    (n.children || []).forEach(rec);
  })(root);
  return out;
}

var targetsJson = fs.readFileSync('targets.json', 'utf8');
byId['adlc-targets'] = new FakeNode('script');
byId['adlc-targets']._text = targetsJson;

var realSDK = require('./sdk.js');
var _origCreate = realSDK.createSession;
realSDK.createSession = function (t) { var s = _origCreate(t); global.__session = s; return s; };

require('./console.js');
"""


def _mount(targets: dict, tmp_path: Path, probe: str) -> dict:
    """Mount the real console against a real manifest in the fake DOM, then run
    ``probe`` (which must ``console.log(JSON.stringify(...))`` exactly once)."""
    (tmp_path / "sdk.js").write_text(sdk_source(), encoding="utf-8")
    (tmp_path / "console.js").write_text(console_asset("console.js"), encoding="utf-8")
    (tmp_path / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
    (tmp_path / "runner.js").write_text(_MOUNT_HARNESS + "\n" + probe + "\n", encoding="utf-8")
    proc = subprocess.run(
        [NODE or "node", str(tmp_path / "runner.js")],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_the_header_names_the_baseline_when_one_exists(
    diffed_targets: dict, tmp_path: Path
) -> None:
    """Defect 1a: the header read `run.referencesRun`, which the schema forbids,
    so it said "no baseline" for every run -- including runs that have one."""
    out = _mount(
        diffed_targets,
        tmp_path,
        "console.log(JSON.stringify({ runLine: byId['run-line'].textContent }));",
    )
    assert "vs 2026-08-19-a1b2" in out["runLine"], out["runLine"]
    assert "no baseline" not in out["runLine"], out["runLine"]


@needs_node
def test_the_empty_diff_notice_tells_the_truth_about_the_baseline(
    baseline_but_no_diff_targets: dict, tmp_path: Path
) -> None:
    """Defect 1a: with a baseline present but no diff rows, the notice read
    `run.referencesRun` and printed the false "this run has no baseline"."""
    out = _mount(
        baseline_but_no_diff_targets,
        tmp_path,
        "var notices = queryAll(byId['diff'], function (n) {"
        " return n.tag === 'p' && hasClass(n, 'muted'); })"
        ".map(function (p) { return p.textContent; });"
        "console.log(JSON.stringify({ notices: notices }));",
    )
    assert "Nothing changed against the baseline run." in out["notices"], out["notices"]
    assert "This run has no baseline, so there is nothing to diff." not in out["notices"], (
        "the console printed a false statement about a run that has a baseline"
    )


@needs_node
def test_a_measurement_row_shows_the_regressed_candidate_value(
    diffed_targets: dict, tmp_path: Path
) -> None:
    """Defect 1b: the row read `row.candidateValue` (forbidden), so a reviewer
    saw "was 1800" and never the regressed "now 2600" they must judge."""
    out = _mount(
        diffed_targets,
        tmp_path,
        "var rows = queryAll(byId['diff'], function (n) {"
        " return n.tag === 'section' && hasClass(n, 'diff-row'); })"
        ".map(function (c) {"
        "  var h = queryAll(c, function (n) { return n.tag === 'h3'; })[0];"
        "  var p = queryAll(c, function (n) { return hasClass(n, 'muted'); })[0];"
        "  return { id: h ? h.textContent : null, facts: p ? p.textContent : '' };"
        "});"
        "console.log(JSON.stringify({ rows: rows }));",
    )
    row = next(r for r in out["rows"] if r["id"] == "lcp_ms")
    assert "now 2600" in row["facts"], row["facts"]
    assert "was 1800" in row["facts"], row["facts"]


@needs_node
def test_a_changed_screenshot_shows_the_candidate_image(
    diffed_targets: dict, tmp_path: Path
) -> None:
    """Defect 1c: the row read `row.candidateInline` (always absent), so only the
    baseline "before" image rendered. The candidate is inlined under `artifacts`
    and must be recovered by sha256 -- `row.inline` is null by design."""
    out = _mount(
        diffed_targets,
        tmp_path,
        "var imgs = queryAll(byId['diff'], function (n) { return n.tag === 'img'; });"
        "var alts = imgs.map(function (n) { return n.attrs.alt; });"
        "var srcs = imgs.map(function (n) { return n.attrs.src; });"
        "var s = global.__session;"
        "var row = (s.diffRows() || []).filter(function (r) {"
        " return r.targetKind === 'screenshot' && r.targetId === 'home.png'; })[0];"
        "var art = row && row.sha256 ? s.artifactBySha(row.sha256) : null;"
        "var expected = art ? art.inline : null;"
        "console.log(JSON.stringify({"
        " alts: alts,"
        " candidateSrcMatchesArtifact: expected !== null && srcs.indexOf(expected) >= 0,"
        " rowInline: row ? row.inline : 'NO_ROW'"
        "}));",
    )
    assert "Candidate rendering of home.png" in out["alts"], out["alts"]
    assert "Baseline rendering of home.png" in out["alts"], out["alts"]
    assert out["candidateSrcMatchesArtifact"], (
        "the candidate <img> src must be the artifact's inlined image, found by sha256"
    )
    assert out["rowInline"] is None, (
        "row.inline is null by design; the candidate must come from artifacts, not the row"
    )


@needs_node
def test_restoring_a_draft_updates_the_form_dom_not_only_the_session(
    diffed_targets: dict, tmp_path: Path
) -> None:
    """Defect 2: restore() rewrote the session but not the controls seeded once at
    mount, so the verdict select and summary box kept showing stale values while
    the pack carried the restored ones -- the reviewer submits what they cannot
    see, and the next keystroke overwrites the restored prose."""
    probe = (
        "var s = global.__session;"
        "var st = s.state();"
        "var draftVerdict = (s.enums.verdict || []).filter(function (v) { return v !== st.verdict; })[0];"
        "var draftRoute = (s.enums.route || []).filter(function (v) { return v !== st.route; })[0];"
        "var draftDecision = (s.enums.diffDecision || []).filter(function (d) { return d !== 'reject'; })[0];"
        "var mrow = (s.diffRows() || []).filter(function (r) { return r.targetKind === 'measurement'; })[0];"
        # Author a draft in the session and persist it.
        "s.setVerdict(draftVerdict);"
        "s.setRoute(draftRoute);"
        "s.setSummary('restored prose');"
        "s.setSubmittedBy('reviewer');"
        "s.decide({ targetKind: mrow.targetKind, targetId: mrow.targetId, decision: draftDecision, comment: '' });"
        "s.save();"
        # The controls seeded once at mount now hold stale/empty values, exactly
        # as they would on a fresh page load before the reviewer clicks Restore.
        "byId['verdict'].value = st.verdict;"
        "byId['route'].value = st.route;"
        "byId['summary'].value = '';"
        "byId['submitted-by'].value = '';"
        # Click Restore.
        "byId['restore']._handlers.click[0]();"
        "var pressed = queryAll(byId['diff'], function (n) {"
        " return n.tag === 'button' && n.attrs['aria-pressed'] === 'true'; })"
        ".map(function (b) { return b.attrs['aria-label']; });"
        "var decisions = queryAll(byId['diff'], function (n) { return hasClass(n, 'decision'); })"
        ".map(function (sp) { return sp.textContent; });"
        "console.log(JSON.stringify({"
        " draftVerdict: draftVerdict, draftRoute: draftRoute, draftDecision: draftDecision,"
        " verdictValue: byId['verdict'].value, routeValue: byId['route'].value,"
        " summaryValue: byId['summary'].value, submittedByValue: byId['submitted-by'].value,"
        " announcement: byId['status'].textContent,"
        " pressedLabels: pressed, decisionSpans: decisions"
        "}));"
    )
    out = _mount(diffed_targets, tmp_path, probe)
    assert out["verdictValue"] == out["draftVerdict"], "verdict select not updated on restore"
    assert out["routeValue"] == out["draftRoute"], "route select not updated on restore"
    assert out["summaryValue"] == "restored prose", "summary box not updated on restore"
    assert out["submittedByValue"] == "reviewer", "submitted-by field not updated on restore"
    # The diff row's pressed state and status text must match the restored decision.
    assert any(
        out["draftDecision"] in lbl and "lcp_ms" in lbl for lbl in out["pressedLabels"]
    ), out["pressedLabels"]
    assert out["draftDecision"] in out["decisionSpans"], out["decisionSpans"]
    # The announcement must be true: reloading would lose the restored session.
    assert out["draftVerdict"] in out["announcement"], out["announcement"]
    assert "Reload" not in out["announcement"], (
        "the old announcement told the reviewer to reload, which discards the restore"
    )


@needs_node
def test_the_critique_form_offers_no_requirement_picker(
    reasoning_targets: dict, tmp_path: Path
) -> None:
    """Defect 3: the critique form rendered a requirement picker whose selections
    the pack schema (`additionalProperties: false`) and the SDK's fixed allowlist
    both drop in silence. It must be gone -- while the annotation form, which
    legitimately carries requirementIds, keeps its own picker."""
    out = _mount(
        reasoning_targets,
        tmp_path,
        "function reqCount(form) {"
        " return queryAll(form, function (n) { return hasClass(n, 'reqs'); }).length; }"
        "var annotate = queryAll(byId['artifacts'], function (n) {"
        " return n.tag === 'form' && hasClass(n, 'annotate'); });"
        "var critique = queryAll(byId['reasoning'], function (n) {"
        " return n.tag === 'form' && hasClass(n, 'critique'); });"
        "console.log(JSON.stringify({"
        " annotateForms: annotate.length,"
        " critiqueForms: critique.length,"
        " annotateReqs: annotate.map(reqCount),"
        " critiqueReqs: critique.map(reqCount)"
        "}));",
    )
    assert out["annotateForms"] >= 1, "no annotation form rendered to compare against"
    assert out["critiqueForms"] >= 1, "no critique form rendered"
    assert all(n >= 1 for n in out["annotateReqs"]), (
        "the annotation form legitimately carries requirementIds and must keep its picker"
    )
    assert all(n == 0 for n in out["critiqueReqs"]), (
        "the critique form must not offer a requirement picker: the selections are "
        "silently dropped by the pack schema and the SDK allowlist"
    )


@needs_node
def test_requirement_chips_show_the_manifest_text_not_only_the_id(
    reasoning_targets: dict, tmp_path: Path
) -> None:
    """Same drift class as Defect 1: the requirement picker read `req.title`,
    which the schema forbids (`additionalProperties: false`, fields are
    `id`/`text`/`source`), so every chip showed a bare id and dropped the prose
    the manifest carries under `req.text`. Found by the property-access audit."""
    out = _mount(
        reasoning_targets,
        tmp_path,
        "var chips = queryAll(byId['artifacts'], function (n) {"
        " return n.tag === 'label' && hasClass(n, 'chip'); })"
        ".map(function (n) { return n.textContent; });"
        "console.log(JSON.stringify({"
        " chips: chips,"
        " requirements: require('./targets.json').requirements"
        "}));",
    )
    texts = [r["text"] for r in out["requirements"] if r.get("text")]
    assert texts, "the fixture manifest must carry requirement text to prove the point"
    joined = " || ".join(out["chips"])
    for text in texts:
        assert text in joined, (
            f"requirement text {text!r} never appears in any picker chip: {out['chips']}"
        )

