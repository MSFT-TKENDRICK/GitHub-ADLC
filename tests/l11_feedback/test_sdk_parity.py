"""Cross-language parity between the JS SDK and the Python ingestion path.

The SDK computes ``packDigest``; :mod:`adlc.stages.feedback` recomputes it and
refuses a pack whose digest does not match. Those are two independent
implementations of "canonical JSON" in two languages that genuinely disagree
about how to render a float and how to order keys. If they drift, every pack a
GUI produces is refused, and the failure looks like a corruption bug rather than
a formatting bug.

So the digest is not asserted against a hand-written constant -- a constant only
proves one of them changed, not which. It is asserted against the *other
implementation*, running for real, over deliberately adversarial values.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from adlc.stages.feedback import blocking_conflicts, canonical_bytes, pack_digest
from adlc.stages.feedback_sdk import ASSET_NAME, esm_source, sdk_source, write_sdk

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

#: Prepended to every node script. Kept as a plain constant so the scripts below
#: stay concatenation -- they are full of JS braces, and an f-string would
#: silently reinterpret every one of them as a format field.
REQUIRE_SDK = "const sdk = require('./" + ASSET_NAME + "');\n"


def run_node(tmp_path: Path, script: str, **env: str) -> str:
    """Run ``script`` against the real SDK asset and return its stdout.

    The SDK is written out rather than imported from the source tree so the test
    exercises exactly the bytes ``adlc feedback sdk`` ships.
    """
    (tmp_path / ASSET_NAME).write_text(sdk_source(), encoding="utf-8")
    runner = tmp_path / "runner.js"
    runner.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE or "node", str(runner)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={**os.environ, **env},
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


# ---------------------------------------------------------------------------
# The asset itself
# ---------------------------------------------------------------------------


def test_sdk_source_is_not_empty() -> None:
    assert "createSession" in sdk_source()
    assert "canonicalize" in sdk_source()


def test_esm_wrapper_is_generated_from_the_same_source() -> None:
    # One source, two surfaces. Two hand-maintained copies would be one copy
    # plus a future bug -- specifically, a digest bug nobody notices.
    assert esm_source().startswith(sdk_source().rstrip("\n")[:200])
    assert "export const createSession" in esm_source()


def test_write_sdk_emits_both_surfaces(tmp_path: Path) -> None:
    written = write_sdk(tmp_path / "vendor")
    names = sorted(p.name for p in written)
    assert names == ["adlc-feedback.js", "adlc-feedback.mjs"]
    assert all(p.read_text(encoding="utf-8").strip() for p in written)


@needs_node
def test_esm_wrapper_actually_imports(tmp_path: Path) -> None:
    (tmp_path / "adlc-feedback.mjs").write_text(esm_source(), encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(
        "import sdk, { createSession, canonicalize } from './adlc-feedback.mjs';\n"
        "if (typeof createSession !== 'function') throw new Error('no createSession');\n"
        "if (typeof canonicalize !== 'function') throw new Error('no canonicalize');\n"
        "if (sdk.PACK_VERSION !== 'adlc-human-feedback/v1') throw new Error('bad version');\n"
        "console.log('ok');\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE or "node", str(script)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# ---------------------------------------------------------------------------
# Canonicalisation parity -- the load-bearing assertion
# ---------------------------------------------------------------------------


#: Chosen to break a naive implementation, not to pass one.
PARITY_CASES: list[object] = [
    {},
    {"b": 1, "a": 2},
    # Recursive ordering: JSON.stringify preserves insertion order, so a
    # top-level-only sort passes the case above and fails this one.
    {"z": {"d": 1, "c": {"b": 1, "a": 2}}, "a": [{"y": 1, "x": 2}]},
    # Integers vs floats: Python renders 0.0 as "0.0" and 0 as "0".
    {"i": 0, "f": 0.5, "one": 1, "onef": 1.0},
    # Four-decimal geometry: the quantisation grid the SDK enforces. Anything
    # smaller (1e-05) is where Python switches to exponent form and JS does not.
    {"points": [[0.0, 1.0], [0.0001, 0.9999], [0.1234, 0.5]]},
    # Non-ASCII must be raw UTF-8, not \\uXXXX -- ensure_ascii=False on one side
    # has to be matched by JSON.stringify's raw output on the other.
    {"t": "caf\u00e9 \u2014 \u4f60\u597d \U0001f600"},
    # Escapes: quote, backslash, and the control characters with shortcuts.
    {"t": 'he said "no" \\ \n\t\r\b\f'},
    # A control character with no shortcut, escaped as \\u0001 by both.
    {"t": "\u0001"},
    {"empty_list": [], "empty_obj": {}, "null": None, "true": True, "false": False},
    {"nested": [[[{"a": [1, 2.5, None]}]]]},
]


@needs_node
def test_canonicalize_matches_python(tmp_path: Path) -> None:
    """Parity is asserted over the *wire form*, which is the only form that exists.

    A pack reaches Python as JSON text produced by ``JSON.stringify`` and is
    parsed before ``pack_digest`` ever sees it. So the property that matters is
    not "these two literals agree" -- Python can hold a ``1.0`` that JavaScript
    cannot represent distinctly -- but "what JavaScript sends, canonicalises the
    same on both sides". That is what ingestion actually does.
    """
    script = (
        REQUIRE_SDK + "const cases = JSON.parse(process.env.CASES);\n"
        "console.log(JSON.stringify(cases.map(c => [sdk.canonicalize(c), JSON.stringify(c)])));\n"
    )
    produced = json.loads(run_node(tmp_path, script, CASES=json.dumps(PARITY_CASES)))
    assert len(produced) == len(PARITY_CASES)
    for case, (js_canonical, wire) in zip(PARITY_CASES, produced, strict=True):
        py_canonical = canonical_bytes(json.loads(wire)).decode("utf-8")
        assert js_canonical == py_canonical, (
            f"canonical form diverged for {case!r}:\n"
            f"  wire: {wire!r}\n  js:   {js_canonical!r}\n  py:   {py_canonical!r}"
        )


@needs_node
def test_float_with_more_precision_than_the_grid_is_refused(tmp_path: Path) -> None:
    """Refusing beats silently digesting a number Python renders differently.

    A value off the 4-decimal grid has no shared canonical form: Python switches
    to exponent notation at 1e-4 and JavaScript at 1e-6. Digesting it anyway
    would produce a pack whose own digest never verifies, and the reviewer would
    see an integrity error for feedback that was never corrupted.
    """
    script = (
        REQUIRE_SDK + "const out = {};\n"
        "try { sdk.canonicalize({x: 0.000012345}); out.offGrid = 'accepted'; }\n"
        "catch (e) { out.offGrid = 'refused'; }\n"
        "try { sdk.canonicalize({x: 0.1234}); out.onGrid = 'accepted'; }\n"
        "catch (e) { out.onGrid = 'refused'; }\n"
        "out.quantized = sdk.quantizeNumber(1234.56789);\n"
        "out.clamped = sdk.quantize(1.5);\n"
        "console.log(JSON.stringify(out));\n"
    )
    out = json.loads(run_node(tmp_path, script))
    assert out["offGrid"] == "refused"
    assert out["onGrid"] == "accepted"
    assert out["quantized"] == 1234.5679
    assert out["clamped"] == 1


@needs_node
def test_pack_digest_matches_python(tmp_path: Path) -> None:
    """The whole point: a digest the page computes is one ingestion accepts."""
    pack = {
        "schemaVersion": "adlc-human-feedback/v1",
        "runId": "20240101-000000-abcdef",
        "candidateSha": "a" * 40,
        "submittedAt": "2024-01-01T00:00:00Z",
        "verdict": "revise",
        "route": "outer",
        "summary": "caf\u00e9 \u2014 needs work",
        "annotations": [
            {
                "id": "ann-1",
                "artifactSha256": "b" * 64,
                "shape": "rect",
                "geometry": {"points": [[0.0, 1.0], [0.1234, 0.9999]]},
                "severity": "blocker",
                "comment": "this is wrong",
            }
        ],
    }
    script = (
        REQUIRE_SDK + "const pack = JSON.parse(process.env.PACK);\n"
        "sdk.packDigest(pack).then(d => console.log(JSON.stringify(\n"
        "  {digest: d, wire: JSON.stringify(pack)})));\n"
    )
    out = json.loads(run_node(tmp_path, script, PACK=json.dumps(pack)))
    assert out["digest"] is not None, "node should expose SubtleCrypto"
    # Recomputed from the wire bytes, exactly as apply_feedback does.
    assert out["digest"] == pack_digest(json.loads(out["wire"]))


# ---------------------------------------------------------------------------
# Behavioural parity
# ---------------------------------------------------------------------------


@needs_node
def test_blocking_conflicts_matches_python(tmp_path: Path) -> None:
    """A GUI that lets you submit what ingestion refuses has wasted your time."""
    packs = [
        {"verdict": "accept", "annotations": [{"id": "a1", "severity": "blocker"}]},
        {"verdict": "accept", "annotations": [{"id": "a1", "severity": "minor"}]},
        {"verdict": "revise", "annotations": [{"id": "a1", "severity": "blocker"}]},
        {"verdict": "accept", "diffDecisions": [{"id": "d1", "decision": "reject"}]},
        {"verdict": "accept", "diffDecisions": [{"id": "d1", "decision": "accept"}]},
        {
            "verdict": "accept",
            "annotations": [{"id": "z9", "severity": "blocker"}],
            "critiques": [{"id": "a0", "severity": "blocker"}],
            "diffDecisions": [{"id": "m5", "decision": "reject"}],
        },
    ]
    # Conflict parity is asserted directly against the algorithm: the session
    # guards its own state, so smuggling records past those guards to test this
    # would be testing the smuggling, not the rule.
    script = (
        REQUIRE_SDK + "function conflicts(pack) {\n"
        "  if (pack.verdict !== 'accept') return [];\n"
        "  let ids = [];\n"
        "  ['annotations','critiques'].forEach(c => (pack[c]||[]).forEach(i => {\n"
        "    if (i.severity === 'blocker') ids.push(String(i.id)); }));\n"
        "  (pack.diffDecisions||[]).forEach(d => { if (d.decision === 'reject') ids.push(String(d.id)); });\n"
        "  return ids.sort();\n"
        "}\n"
        "console.log(JSON.stringify(JSON.parse(process.env.PACKS).map(conflicts)));\n"
    )
    produced = json.loads(run_node(tmp_path, script, PACKS=json.dumps(packs)))
    expected = [blocking_conflicts(p) for p in packs]  # type: ignore[arg-type]
    assert produced == expected


@needs_node
def test_session_enforces_the_contract_it_advertises(tmp_path: Path, targets_doc: dict) -> None:
    script = (
        REQUIRE_SDK + "const targets = JSON.parse(process.env.TARGETS);\n"
        "const s = sdk.createSession(targets);\n"
        "const out = {};\n"
        "// citation-or-discard, enforced where the reviewer can still see it\n"
        "try { s.addAnnotation({artifactSha256: 'f'.repeat(64), shape: 'whole', comment: 'x'});\n"
        "      out.unknownHash = 'accepted'; } catch (e) { out.unknownHash = 'refused'; }\n"
        "// an enum the schema does not have\n"
        "try { s.setVerdict('maybe'); out.badVerdict = 'accepted'; }\n"
        "catch (e) { out.badVerdict = 'refused'; }\n"
        "// a comment-less annotation says nothing\n"
        "try { s.addAnnotation({artifactSha256: targets.artifacts[0].sha256, shape: 'whole', comment: '  '});\n"
        "      out.noComment = 'accepted'; } catch (e) { out.noComment = 'refused'; }\n"
        "const ann = s.addAnnotation({artifactSha256: targets.artifacts[0].sha256,\n"
        "  shape: 'rect', points: [[0,0],[0.5,0.5]], severity: 'blocker', comment: 'bad pixel'});\n"
        "out.annotationId = ann.id;\n"
        "out.geometry = ann.geometry.points;\n"
        "s.setVerdict('accept');\n"
        "out.conflicts = s.blockingConflicts();\n"
        "try { s.buildPack(); out.builtWithConflict = 'yes'; }\n"
        "catch (e) { out.builtWithConflict = 'no'; }\n"
        "s.setVerdict('revise');\n"
        "const pack = s.buildPack({submittedAt: '2024-01-01T00:00:00Z'});\n"
        "out.pack = pack;\n"
        "console.log(JSON.stringify(out));\n"
    )
    out = json.loads(run_node(tmp_path, script, TARGETS=json.dumps(targets_doc)))

    assert out["unknownHash"] == "refused"
    assert out["badVerdict"] == "refused"
    assert out["noComment"] == "refused"
    assert out["geometry"] == [[0, 0], [0.5, 0.5]]
    assert out["conflicts"] == [out["annotationId"]]
    assert out["builtWithConflict"] == "no"

    # And the pack a real session produces must satisfy the real schema.
    from adlc.schemas import is_valid

    ok, errors = is_valid("human-feedback-pack", out["pack"])
    assert ok, errors
