"""Submit-feedback overlay for the redesigned PWA report."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.report.overlay import asset_source
from adlc.schemas import is_valid
from adlc.stages import feedback as fb
from adlc.stages.report import run_report
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run

ASSET = Path(__file__).resolve().parents[2] / "src" / "adlc" / "assets" / "feedback-overlay" / "feedback.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def read_asset(name: str) -> str:
    return (files("adlc") / "assets" / "feedback-overlay" / name).read_text(encoding="utf-8")

_NODE_DRIVER = r"""
const crypto = require("crypto");
const mod = require(process.argv[1]);
let data = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", function (d) { data += d; });
process.stdin.on("end", function () {
  const value = JSON.parse(data);
  const canon = mod.canonicalize(value);
  const wire = JSON.stringify(value);
  const canonHex = Buffer.from(canon, "utf8").toString("hex");
  const wireHex = Buffer.from(wire, "utf8").toString("hex");
  const digest = "sha256:" + crypto.createHash("sha256").update(Buffer.from(canon, "utf8")).digest("hex");
  process.stdout.write(canonHex + "\n" + wireHex + "\n" + digest + "\n");
});
"""


def _node_canon(value: Any) -> tuple[str, str, str]:
    proc = subprocess.run(
        [NODE, "-e", _NODE_DRIVER, str(ASSET)],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    lines = proc.stdout.strip().split("\n")
    assert len(lines) == 3
    return lines[0], lines[1], lines[2]


def _render(cfg: Config, run_id: str = "2026-08-20-c0de", *, head_sha: str = CANDIDATE_SHA) -> str:
    rd = make_run(cfg, run_id, head_sha=head_sha)
    reduce_run(cfg, rd)
    run_report(cfg, rd)
    return rd.report.read_text(encoding="utf-8")


def _config_json(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="adlc-feedback-config">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "config script block not found"
    return json.loads(match.group(1))


def _representative_pack(run_id: str = "2026-08-20-c0de") -> dict[str, Any]:
    return {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": run_id,
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00.123Z",
        "verdict": "revise",
        "route": "outer",
        "submittedBy": "Reviewer",
        "summary": "caf\u00e9 \u2264 blocks </script> \U0001f600",
        "annotations": [
            {
                "id": "an-1",
                "artifactSha256": "c" * 64,
                "shape": "rect",
                "geometry": {"points": [[0.1, 0.35], [0.4, 0.6666666666666666], [0.0, 1.0]]},
                "timestampMs": 1234,
                "severity": "major",
                "comment": "No visible focus ring.",
                "requirementIds": ["US1-AC1"],
            }
        ],
        "critiques": [
            {
                "id": "cr-1",
                "targetKind": "adr",
                "targetRef": "reviews/x.md#f1",
                "stance": "agree",
                "comment": "Correct.",
            }
        ],
        "diffDecisions": [
            {"id": "dd-1", "targetKind": "measurement", "targetId": "lcp", "decision": "accept"}
        ],
    }


def test_real_run_renders_submit_ui_inside_pwa(cfg: Config) -> None:
    html = _render(cfg)
    assert "Submit feedback" in html
    assert '<script type="application/json" id="adlc-feedback-config">' in html
    assert asset_source("feedback.js") in html
    assert 'src="./' not in html
    assert 'href="./' not in html


def test_real_run_renders_the_submit_ui(cfg: Config) -> None:
    html = _render(cfg)
    assert "Submit feedback" in html
    assert '<script type="application/json" id="adlc-feedback-config">' in html
    assert "<script>" in html and "</script>" in html


def test_render_is_self_contained(cfg: Config) -> None:
    html = _render(cfg)
    assert 'src="./' not in html
    assert 'href="./' not in html


def test_full_report_still_self_contained_with_section_present(cfg: Config) -> None:
    html = _render(cfg)
    assert "Submit feedback" in html
    assert 'src="./' not in html
    assert 'href="./' not in html


def test_embedded_identity_is_correct(cfg: Config) -> None:
    cfgjson = _config_json(_render(cfg, "2026-08-20-abcd", head_sha=CANDIDATE_SHA))
    assert cfgjson["schemaVersion"] == "adlc-human-feedback/v1"
    assert cfgjson["runId"] == "2026-08-20-abcd"
    assert cfgjson["candidateSha"] == CANDIDATE_SHA
    assert cfgjson["submitPath"] == "/feedback"
    assert cfgjson["nonceHeader"] == "X-ADLC-Nonce"
    assert cfgjson["maxBodyBytes"] == 4 * 1024 * 1024


def test_config_script_guards_against_early_termination(cfg: Config) -> None:
    html = _render(cfg, head_sha="deadbeef</script><script>alert(1)")
    block = re.search(
        r'<script type="application/json" id="adlc-feedback-config">(.*?)</script>',
        html,
        re.DOTALL,
    ).group(1)  # type: ignore[union-attr]
    assert "</script>" not in block
    assert r"\u003c/script>" in block
    assert _config_json(html)["candidateSha"] == "deadbeef</script><script>alert(1)"


def test_untrusted_head_sha_is_html_escaped(cfg: Config) -> None:
    html = _render(cfg, head_sha="ab<script>cd")
    assert "<script>cd" not in html.split('id="adlc-feedback-config"')[0]
    assert "&lt;script&gt;" in html


@requires_node
def test_shipped_canonicaliser_is_byte_identical_to_python(cfg: Config) -> None:
    pack = _representative_pack()
    canon_hex, wire_hex, digest = _node_canon(pack)
    parsed = json.loads(bytes.fromhex(wire_hex).decode("utf-8"))
    assert fb.canonical_bytes(parsed).hex() == canon_hex
    assert fb.pack_digest(parsed) == digest


@requires_node
def test_numeric_canonicalisation_fuzz(cfg: Config) -> None:
    import math
    import random
    import struct

    sample = [0.0, -0.0, 0.1, 1.0 / 3.0, 1e-5, 1e16, 1e21, 5e-324, 1.7976931348623157e308]
    rng = random.Random(1729)
    while len(sample) < 200:
        d = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
        if math.isfinite(d):
            sample.append(d)
        sample.append(rng.random())
    canon_hex, wire_hex, _ = _node_canon(sample)
    parsed = json.loads(bytes.fromhex(wire_hex).decode("utf-8"))
    assert fb.canonical_bytes(parsed).hex() == canon_hex


@requires_node
def test_string_canonicalisation_fuzz(cfg: Config) -> None:
    strings = ["", "plain", "caf\u00e9", "\u2264\u2265", "\U0001f600", "</script>", "a\"b\\c", "\u2028\u2029"]
    canon_hex, wire_hex, _ = _node_canon(strings)
    parsed = json.loads(bytes.fromhex(wire_hex).decode("utf-8"))
    assert fb.canonical_bytes(parsed).hex() == canon_hex


def test_representative_pack_validates() -> None:
    pack = _representative_pack()
    pack["packDigest"] = fb.pack_digest(pack)
    pack["reportDigest"] = "sha256:" + "a" * 64
    ok, errors = is_valid("human-feedback-pack", pack)
    assert ok, errors


def test_accept_with_blocker_is_a_conflict(cfg: Config) -> None:
    rd = make_run(cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA)
    pack = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "2026-08-20-c0de",
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00.000Z",
        "verdict": "accept",
        "route": "outer",
        "summary": "Looks good.",
        "annotations": [{"id": "an-block", "artifactSha256": "c" * 64, "shape": "rect", "comment": "This blocks shipping.", "severity": "blocker"}],
        "diffDecisions": [{"id": "dd-rej", "targetKind": "measurement", "targetId": "lcp", "decision": "reject"}],
    }
    assert fb.blocking_conflicts(pack) == ["an-block", "dd-rej"]
    assert fb.apply_feedback(cfg, rd, pack)["applied"] is False


def test_accessibility_hooks_are_present(cfg: Config) -> None:
    html = _render(cfg)
    for target in ("adlc-verdict", "adlc-route", "adlc-summary", "adlc-submitted-by"):
        assert f'<label for="{target}">' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert 'role="alert"' in html and 'aria-live="assertive"' in html
    assert 'id="adlc-error"' in html and 'tabindex="-1"' in html
    assert html.count('aria-describedby="adlc-guidance"') == 2
    assert 'aria-describedby="adlc-guidance adlc-submit-note"' in html


def test_conflict_is_not_conveyed_by_colour_alone(cfg: Config) -> None:
    html = _render(cfg)
    assert 'id="adlc-conflict"' in html
    assert "\\u26a0" in read_asset("feedback.js")


def test_feedback_asset_keeps_egress_and_failure_guards() -> None:
    text = asset_source("feedback.js")
    assert "window.adlcFeedback = window.adlcFeedback ||" in text
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []" in text
    assert "btn.setAttribute(\"aria-disabled\"" in text
    assert "resp.ok && result && result.applied === true" in text
    assert "REFUSED" in text
    assert "outcome is unknown" in text
    assert 'location.hostname === "127.0.0.1"' in text


def test_asset_is_ascii_and_has_no_script_terminator() -> None:
    raw = ASSET.read_bytes()
    assert all(b < 128 for b in raw), "asset must be pure ASCII"
    assert "</script>" not in raw.decode("ascii"), "a literal </script> would end the block early"


def test_asset_uses_the_shared_registry_initialiser() -> None:
    text = read_asset("feedback.js")
    assert "window.adlcFeedback = window.adlcFeedback ||" in text
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []" in text
    assert "subscribe(fn)" in text


def test_egress_buttons_use_aria_disabled_and_busy_state() -> None:
    text = read_asset("feedback.js")
    assert "btn.setAttribute(\"aria-disabled\"" in text
    assert "isAriaDisabled(dlBtn)" in text
    assert "isAriaDisabled(copyBtn)" in text
    assert "isAriaDisabled(postBtn)" in text
    assert "postBtn.setAttribute(\"aria-busy\", \"true\")" in text
    assert "postBtn.removeAttribute(\"aria-busy\")" in text
    assert "let submitting = false" in text
    assert "Feedback pack actions are now available." in text


def test_refresh_guards_repeated_live_region_writes() -> None:
    text = read_asset("feedback.js")
    assert "function setRegion" in text
    assert "if (el.textContent !== msg) el.textContent = msg" in text
    assert "if (countsEl.textContent !== counts) countsEl.textContent = counts" in text
    assert "annotation #\" + (n || \"?\")" in text


def test_post_is_a_simple_same_origin_request() -> None:
    text = read_asset("feedback.js")
    assert "cfg.submitPath" in text
    assert "text/plain;charset=UTF-8" in text
    assert "cfg.nonceHeader" in text
    assert "URLSearchParams" in text
    assert 'location.hostname === "127.0.0.1"' in text
    assert 'location.hostname === "localhost"' in text


def test_no_failure_is_dressed_up_as_success() -> None:
    text = read_asset("feedback.js")
    assert "resp.ok && result && result.applied === true" in text
    assert "REFUSED" in text
    assert "outcome is unknown" in text
    assert "resubmitting is safe" in text


def test_egress_has_a_clipboard_fallback_and_size_guard() -> None:
    text = read_asset("feedback.js")
    assert "adlc-copy-fallback" in text
    assert "cfg.maxBodyBytes" in text
    assert "REFUSED" in text


def test_accept_without_blockers_is_applied(cfg: Config) -> None:
    rd = make_run(cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA)
    pack = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "2026-08-20-c0de",
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00.000Z",
        "verdict": "accept",
        "route": "outer",
        "summary": "Ready to ship.",
    }
    if NODE is not None:
        _, _, digest = _node_canon(pack)
        pack["packDigest"] = digest
    else:
        pack["packDigest"] = fb.pack_digest(pack)
    result = fb.apply_feedback(cfg, rd, pack)
    assert result["applied"] is True, result
    assert result["outcome"] == "ship"
    assert result["successorRun"] is None


def test_stale_candidate_sha_is_refused(cfg: Config) -> None:
    rd = make_run(cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA)
    pack = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "2026-08-20-c0de",
        "candidateSha": "b" * 40,
        "submittedAt": "2026-08-20T12:00:00.000Z",
        "verdict": "revise",
        "route": "outer",
        "summary": "x",
    }
    result = fb.apply_feedback(cfg, rd, pack)
    assert result["applied"] is False


@requires_node
def test_owned_field_scrub_keeps_the_pack_applicable(cfg: Config) -> None:
    assert "function scrubText" in read_asset("feedback.js")
    rd = make_run(cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA)
    pack = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "2026-08-20-c0de",
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00.000Z",
        "verdict": "accept",
        "route": "outer",
        "summary": "scrubbed \ufffd replacement is fine",
    }
    _, _, digest = _node_canon(pack)
    pack["packDigest"] = digest
    assert fb.apply_feedback(cfg, rd, pack)["applied"] is True


def test_server_constants_match(cfg: Config) -> None:
    from adlc import serve

    rendered = _config_json(_render(cfg, "2026-08-20-c0de"))
    assert rendered.get("submitPath") == serve.SUBMIT_PATH
    assert rendered.get("nonceHeader") == serve.NONCE_HEADER
    assert rendered.get("maxBodyBytes") == serve.MAX_BODY_BYTES


@requires_node
def test_seam_exports_only_the_pure_functions() -> None:
    proc = subprocess.run(
        [
            NODE,
            "-e",
            "const m=require(process.argv[1]);process.stdout.write(Object.keys(m).sort().join(','));",
            str(ASSET),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "canonicalize,numToken,pyFloatRepr"
