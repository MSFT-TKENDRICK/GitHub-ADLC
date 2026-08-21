"""Layer 7-UI -- the feedback section that turns three annotation surfaces into
one submitted ``adlc-human-feedback/v1`` act.

The hard part of this layer is the ``packDigest``: the page computes it in the
browser and the ingest (:mod:`adlc.stages.feedback`) recomputes it and refuses
on mismatch. So the centre of gravity here is a byte-for-byte equivalence proof
between the *shipped* ``assets/feedback.js`` canonicaliser and
:func:`adlc.stages.feedback.canonical_bytes`. When ``node`` is on PATH we execute
the real asset (via a small CommonJS test seam) and compare bytes; the remaining
tests pin the section's rendering, escaping, self-containment, the embedded
identity, the conflict rule, and that an assembled pack survives ingest.

Offline, no credentials, no editable install (``conftest`` puts ``src`` on
``sys.path``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.runs import RunDir
from adlc.schemas import is_valid
from adlc.stages import feedback as fb
from adlc.stages.report import render as full_render
from adlc.stages.report import shell
from adlc.stages.report.context import ReportContext
from adlc.stages.report.render import build_context
from adlc.stages.report.sections import feedback as feedback_section
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run

ASSET = Path(shell.__file__).resolve().parent / "assets" / "feedback.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

# A CommonJS driver that loads the *shipped* asset through its test seam and
# emits, for whatever JSON arrives on stdin: the canonical bytes (hex), the wire
# bytes JSON.stringify would send (hex), and the sha256 packDigest. Kept as an
# argv -e string so the test creates no files outside its write-set.
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
    """Run the shipped canonicaliser under node. Returns (canonHex, wireHex, digest)."""
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
    assert len(lines) == 3, f"unexpected node output: {proc.stdout!r} / {proc.stderr!r}"
    return lines[0], lines[1], lines[2]


def _asset_text() -> str:
    return ASSET.read_text(encoding="utf-8")


def _render(cfg: Config, run_id: str = "2026-08-20-c0de", *, head_sha: str = CANDIDATE_SHA) -> str:
    rd = make_run(cfg, run_id, head_sha=head_sha)
    return feedback_section.render(build_context(cfg, rd))


def _config_json(html: str) -> dict[str, Any]:
    m = re.search(
        r'<script type="application/json" id="adlc-feedback-config">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m, "config script block not found"
    # json.loads natively decodes the \u003c produced by the < guard.
    return json.loads(m.group(1))


def _representative_pack(run_id: str = "2026-08-20-c0de") -> dict[str, Any]:
    """A pack shaped exactly as ``assemblePack`` in feedback.js builds it."""
    pack: dict[str, Any] = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": run_id,
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00.123Z",
        "verdict": "revise",
        "route": "outer",
        "submittedBy": "Reviewer",
        # Astral char, a </script> sequence, U+2264, and a caf\u00e9 to exercise
        # string canonicalisation. None of this is embedded in HTML; it only ever
        # travels in the pack.
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
    return pack


# ---------------------------------------------------------------------------
# Rendering: omission, the real-run shape, self-containment
# ---------------------------------------------------------------------------


def test_bare_context_renders_nothing(cfg: Config) -> None:
    # No loaded run -> no identity to bind feedback to -> render "" so the
    # pre-split output stays byte-identical and no empty submit UI appears.
    ctx = ReportContext(cfg=cfg, rd=RunDir(cfg, "2026-08-20-ctx0"))
    assert feedback_section.render(ctx) == ""


def test_real_run_renders_the_submit_ui(cfg: Config) -> None:
    html = _render(cfg)
    assert html.startswith("  <h2>Submit feedback</h2>")
    assert not html.endswith("\n")
    assert '<script type="application/json" id="adlc-feedback-config">' in html
    assert "<script>" in html and "</script>" in html


def test_render_is_self_contained(cfg: Config) -> None:
    # The report must survive being emailed as one file:// document.
    html = _render(cfg)
    assert 'src="./' not in html
    assert 'href="./' not in html


def test_full_report_still_self_contained_with_section_present(cfg: Config) -> None:
    rd = make_run(cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA)
    html = full_render(cfg, rd)
    assert "Submit feedback" in html, "the section must render for a real run"
    assert 'src="./' not in html
    assert 'href="./' not in html


# ---------------------------------------------------------------------------
# Embedded identity + the </script> guard + escaping
# ---------------------------------------------------------------------------


def test_embedded_identity_is_correct(cfg: Config) -> None:
    cfgjson = _config_json(_render(cfg, "2026-08-20-abcd", head_sha=CANDIDATE_SHA))
    assert cfgjson["schemaVersion"] == "adlc-human-feedback/v1"
    assert cfgjson["runId"] == "2026-08-20-abcd"
    assert cfgjson["candidateSha"] == CANDIDATE_SHA
    assert cfgjson["submitPath"] == "/feedback"
    assert cfgjson["nonceHeader"] == "X-ADLC-Nonce"
    assert cfgjson["maxBodyBytes"] == 4 * 1024 * 1024


def test_config_script_guards_against_early_termination(cfg: Config) -> None:
    # A candidateSha carrying </script> must not close the JSON block early.
    html = _render(cfg, head_sha="deadbeef</script><script>alert(1)")
    # Exactly two real closers: the config block and the asset block.
    assert html.count("</script>") == 2
    assert "<script>alert(1)" not in html
    # The guard fired: < became the \u003c escape inside the JSON.
    assert r"\u003c/script>" in html
    # And it still parses back to the original bytes.
    assert _config_json(html)["candidateSha"] == "deadbeef</script><script>alert(1)"


def test_untrusted_head_sha_is_html_escaped(cfg: Config) -> None:
    html = _render(cfg, head_sha="ab<script>cd")
    assert "<script>cd" not in html.split('id="adlc-feedback-config"')[0]
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# packDigest -- the byte-for-byte equivalence proof (the trap in this layer)
# ---------------------------------------------------------------------------


@requires_node
def test_shipped_canonicaliser_is_byte_identical_to_python(cfg: Config) -> None:
    pack = _representative_pack()
    canon_hex, wire_hex, digest = _node_canon(pack)

    # The server canonicalises what it PARSES off the wire, so compare against
    # canonical_bytes(json.loads(wire)) -- exactly the ingest path.
    wire = bytes.fromhex(wire_hex).decode("utf-8")
    parsed = json.loads(wire)
    assert fb.canonical_bytes(parsed).hex() == canon_hex, "JS canonical bytes differ from Python"
    assert fb.pack_digest(parsed) == digest, "JS packDigest differs from Python"


@requires_node
def test_numeric_canonicalisation_fuzz(cfg: Config) -> None:
    import math
    import random
    import struct

    boundary = [
        0.0, -0.0, 1.0, -1.0, 0.1, 0.2, 0.3, 0.5, 1.0 / 3.0, 2.0 / 3.0,
        1e-4, 9.999e-5, 1e-5, 1e15, 1e16, 9.999999999999999e15, 1e21,
        1e-323, 5e-324, 1.7976931348623157e308, 2200.0, 400.0,
        123456789.123456789, 0.6666666666666666,
    ]
    rng = random.Random(1729)
    sample: list[float] = list(boundary)
    while len(sample) < 600:
        # Uniform mantissa/exponent doubles, plus realistic [0,1] geometry.
        bits = rng.getrandbits(64)
        d = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if math.isfinite(d):
            sample.append(d)
        sample.append(rng.random())

    canon_hex, wire_hex, _ = _node_canon(sample)
    parsed = json.loads(bytes.fromhex(wire_hex).decode("utf-8"))
    assert fb.canonical_bytes(parsed).hex() == canon_hex


@requires_node
def test_string_canonicalisation_fuzz(cfg: Config) -> None:
    strings = [
        "", "plain", "caf\u00e9", "\u2264\u2265", "\U0001f600\U0001f4a9",
        "</script>", "a\"b\\c", "tab\tnew\nline", "\u007f\u0080\u009f",
        "\u2028\u2029", "quote'apos", "<b>&amp;</b>",
    ]
    canon_hex, wire_hex, _ = _node_canon(strings)
    parsed = json.loads(bytes.fromhex(wire_hex).decode("utf-8"))
    assert fb.canonical_bytes(parsed).hex() == canon_hex


@requires_node
def test_seam_exports_only_the_pure_functions() -> None:
    # A defensive check that the browser wiring did not leak into the seam.
    proc = subprocess.run(
        [
            NODE, "-e",
            (
                "const m=require(process.argv[1]);"
                "process.stdout.write(Object.keys(m).sort().join(','));"
            ),
            str(ASSET),
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "canonicalize,numToken,pyFloatRepr"


# ---------------------------------------------------------------------------
# The assembled pack survives ingest
# ---------------------------------------------------------------------------


def test_representative_pack_validates() -> None:
    pack = _representative_pack()
    pack["packDigest"] = fb.pack_digest(pack)
    pack["reportDigest"] = "sha256:" + "a" * 64
    ok, errors = is_valid("human-feedback-pack", pack)
    assert ok, errors


def test_accept_with_blocker_is_a_conflict(cfg: Config) -> None:
    # The page surfaces this before submit; ingest refuses it. Both must agree,
    # and this asserts against the *same* rule ingest uses.
    rd = make_run(cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA)
    pack = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "2026-08-20-c0de",
        "candidateSha": CANDIDATE_SHA,
        "submittedAt": "2026-08-20T12:00:00.000Z",
        "verdict": "accept",
        "route": "outer",
        "summary": "Looks good.",
        "annotations": [
            {
                "id": "an-block",
                "artifactSha256": "c" * 64,
                "shape": "rect",
                "comment": "This blocks shipping.",
                "severity": "blocker",
            }
        ],
        "diffDecisions": [
            {"id": "dd-rej", "targetKind": "measurement", "targetId": "lcp", "decision": "reject"}
        ],
    }
    assert fb.blocking_conflicts(pack) == ["an-block", "dd-rej"]

    result = fb.apply_feedback(cfg, rd, pack)
    assert result["applied"] is False
    assert "blocking" in result["reason"].lower()


def test_accept_without_blockers_is_applied(cfg: Config) -> None:
    # An accept with no blockers is the lightest apply path (ship, no successor).
    # When node is present we attach the JS-computed packDigest, proving ingest
    # accepts the browser's digest end to end.
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
    # The digest is honest but the pack targets code the run does not record.
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


# ---------------------------------------------------------------------------
# Mirror constants + accessibility hooks + JS structure
# ---------------------------------------------------------------------------


def test_server_constants_match() -> None:
    from adlc import serve

    assert feedback_section._SUBMIT_PATH == serve.SUBMIT_PATH
    assert feedback_section._NONCE_HEADER == serve.NONCE_HEADER
    assert feedback_section._MAX_BODY_BYTES == serve.MAX_BODY_BYTES


def test_accessibility_hooks_are_present(cfg: Config) -> None:
    html = _render(cfg)
    # Every control has a real label.
    for target in ("adlc-verdict", "adlc-route", "adlc-summary", "adlc-submitted-by"):
        assert f'<label for="{target}">' in html, target
    # Live regions: assertive for failures, polite for success.
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert 'role="alert"' in html and 'aria-live="assertive"' in html
    # The error region is focusable so failure can steal focus.
    assert 'id="adlc-error"' in html and 'tabindex="-1"' in html
    # Keyboard focus is visible; buttons are real buttons.
    assert ":focus-visible" in html
    assert html.count('type="button"') >= 3
    # Disabled egress controls state WHY via aria-describedby -> the live guidance
    # (download + copy), and the submit button adds its own not-served note.
    assert html.count('aria-describedby="adlc-guidance"') == 2
    assert 'aria-describedby="adlc-guidance adlc-submit-note"' in html
    # Guidance is a live region so the gating reason is announced when it changes.
    assert 'id="adlc-guidance" role="status" aria-live="polite"' in html


def test_conflict_is_not_conveyed_by_colour_alone(cfg: Config) -> None:
    html = _render(cfg)
    # The conflict region carries a symbol + text, never colour only.
    assert 'id="adlc-conflict"' in html
    assert "\\u26a0" in _asset_text()  # warning sign prefixes the conflict text


def test_asset_is_ascii_and_has_no_script_terminator() -> None:
    raw = ASSET.read_bytes()
    assert all(b < 128 for b in raw), "asset must be pure ASCII"
    assert "</script>" not in raw.decode("ascii"), "a literal </script> would end the block early"


def test_asset_uses_the_shared_registry_initialiser() -> None:
    text = _asset_text()
    # The exact lazy idempotent initialiser, so load order vs the sibling
    # sections does not matter.
    assert "window.adlcFeedback = window.adlcFeedback ||" in text
    assert "annotations: [], critiques: [], diffDecisions: [], listeners: []" in text
    assert "subscribe(fn)" in text


def test_post_is_a_simple_same_origin_request() -> None:
    text = _asset_text()
    # A relative path keeps the POST same-origin, so the custom nonce header
    # triggers no CORS preflight (the server answers OPTIONS with 405).
    assert "cfg.submitPath" in text
    assert "text/plain;charset=UTF-8" in text
    assert "cfg.nonceHeader" in text
    assert "URLSearchParams" in text  # nonce read from location.search
    # Submit is only enabled on the loopback origin the server actually accepts,
    # so no other http host carrying a stray ?nonce= can absorb a POST.
    assert 'location.hostname === "127.0.0.1"' in text
    assert 'location.hostname === "localhost"' in text


def test_no_failure_is_dressed_up_as_success() -> None:
    text = _asset_text()
    # Success requires BOTH resp.ok and the server's own applied flag.
    assert "resp.ok && result && result.applied === true" in text
    # A refusal is surfaced verbatim, loudly, and never read as success.
    assert "REFUSED" in text
    # A dropped connection is reported as unknown -- never as "sent" -- because
    # the idempotent ingest may already have applied it.
    assert "outcome is unknown" in text
    assert "resubmitting is safe" in text


@requires_node
def test_owned_field_scrub_keeps_the_pack_applicable(cfg: Config) -> None:
    # scrubText replaces unpaired surrogates with U+FFFD in the fields this
    # section owns. That is the one input where a naive JS pack would diverge from
    # Python (which raises UnicodeEncodeError and refuses). The scrubbed U+FFFD
    # form canonicalises identically on both sides and is accepted by ingest.
    assert "function scrubText" in _asset_text()
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


def test_egress_has_a_clipboard_fallback_and_size_guard() -> None:
    text = _asset_text()
    assert "adlc-copy-fallback" in text  # selectable textarea fallback
    assert "cfg.maxBodyBytes" in text  # size checked before POST
    assert "REFUSED" in text  # server refusal surfaced verbatim
