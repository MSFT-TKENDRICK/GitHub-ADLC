"""The review transport must be published, not memorised.

A pack that travels by download proves nothing about who produced it. The one
channel that carries real authority in CI is a native PR review, and CI finds
the pack there by looking for a fenced code block with a particular info string.

That info string used to live in exactly one place: a regex inside
``.github/workflows/adlc-feedback.yml``. Any GUI wanting to use the authorised
transport had to read the workflow YAML and hard-code the magic word -- which is
precisely the coupling ``feedback-targets.json`` exists to delete. So the fence
is now a published field of the submission contract.

Publishing it only helps if the published value is *the same value CI matches*.
These tests do not assert the fence against a literal. They extract the regex
from the real workflow file, compile it, and run it over a review body the real
SDK produced in a real node process. Drift in either direction fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from adlc.stages.feedback import REVIEW_FENCE
from adlc.stages.feedback_console import console_asset
from adlc.stages.feedback_sdk import sdk_source
from adlc.stages.feedback_targets import submission_contract

from .test_sdk_parity import REQUIRE_SDK, needs_node, run_node

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "adlc-feedback.yml"

#: Finds the workflow's own extraction regex inside the YAML. Deliberately
#: anchored on the ``fence_re`` name so it cannot latch onto some other
#: unrelated regex that happens to appear in the file.
_EXTRACTOR_RE = re.compile(
    r"fence_re\s*=\s*re\.compile\(\s*r\"(?P<pattern>[^\"]+)\"\s*,\s*(?P<flags>[^)]+?),?\s*\)",
    re.DOTALL,
)

_FLAGS = {
    "re.DOTALL": re.DOTALL,
    "re.MULTILINE": re.MULTILINE,
    "re.IGNORECASE": re.IGNORECASE,
}


def workflow_pattern() -> tuple[str, int]:
    """The regex source *and flags* CI uses to pull a pack out of a review body.

    The flags are not decoration. ``re.MULTILINE`` is what anchors the closing
    fence to the start of a line, and without that anchor a backtick inside a
    reviewer's comment truncates the pack.
    """
    if not WORKFLOW.exists():  # pragma: no cover - the workflow is committed
        pytest.skip(f"{WORKFLOW} is not present")
    match = _EXTRACTOR_RE.search(WORKFLOW.read_text(encoding="utf-8"))
    assert match, (
        "could not find the pack-extraction regex in the workflow. If the "
        "workflow changed how it finds a pack, this test must be taught the "
        "new shape -- silently skipping would leave the fence unpinned."
    )
    flags = 0
    for name in match.group("flags").split("|"):
        name = name.strip()
        assert name in _FLAGS, f"unrecognised regex flag {name!r} in the workflow"
        flags |= _FLAGS[name]
    return match.group("pattern"), flags


def extract(body: str) -> str | None:
    """Run the workflow's extraction over ``body``, exactly as CI would."""
    pattern, flags = workflow_pattern()
    match = re.search(pattern, body, flags)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# The constant, the workflow, and the manifest
# ---------------------------------------------------------------------------


def test_the_workflow_matches_the_published_fence() -> None:
    """The single assertion that makes the manifest field trustworthy."""
    pattern, _ = workflow_pattern()
    assert "```" + REVIEW_FENCE in pattern, (
        f"workflow regex {pattern!r} does not look for the published fence "
        f"{REVIEW_FENCE!r}; a GUI following the manifest would be ignored by CI"
    )


def test_the_workflow_anchors_the_closing_fence() -> None:
    """Without this anchor a reviewer's own backticks truncate the pack.

    Regression pin for a real defect: the first version of this workflow used
    ``(.*?)`` followed by an unanchored fence, so any code block a reviewer
    typed into their summary or a comment ended the pack early.
    """
    pattern, flags = workflow_pattern()
    assert flags & re.MULTILINE, "the extraction regex needs re.MULTILINE"
    assert flags & re.DOTALL, "the pack spans lines, so it needs re.DOTALL"
    assert pattern.endswith("^```"), (
        f"closing fence in {pattern!r} is not anchored to the start of a line"
    )


def test_the_manifest_publishes_the_fence() -> None:
    contract = submission_contract()
    assert contract["reviewFence"] == REVIEW_FENCE


def test_the_fence_is_safe_in_a_fence_info_string() -> None:
    """A fence tag with a backtick or a newline could not close its own block."""
    assert re.fullmatch(r"[A-Za-z0-9-]+", REVIEW_FENCE), REVIEW_FENCE


def test_the_workflow_does_not_apply_a_review_without_a_pack() -> None:
    """A normal human review is not an error, and must not become one."""
    assert extract("Looks good to me, shipping.") is None


# ---------------------------------------------------------------------------
# The SDK's output, through the workflow's own regex
# ---------------------------------------------------------------------------


REVIEW_SCRIPT = (
    REQUIRE_SDK + "const targets = JSON.parse(process.env.TARGETS);\n"
    "const s = sdk.createSession(targets);\n"
    "s.setVerdict('revise');\n"
    "s.setRoute('outer');\n"
    "s.setSummary(process.env.SUMMARY);\n"
    "s.addAnnotation({artifactSha256: targets.artifacts[0].sha256,\n"
    "  shape: 'rect', points: [[0,0],[0.5,0.5]], severity: 'blocker',\n"
    "  comment: process.env.COMMENT});\n"
    "s.toReviewBody().then(b => process.stdout.write(b));\n"
)


@needs_node
def test_the_workflow_extracts_a_pack_the_sdk_produced(tmp_path: Path, targets_doc: dict) -> None:
    """End to end across two languages: SDK writes it, CI's regex reads it."""
    body = run_node(
        tmp_path,
        REVIEW_SCRIPT,
        TARGETS=json.dumps(targets_doc),
        SUMMARY="the hero image regressed",
        COMMENT="this pixel is wrong",
    )
    raw = extract(body)
    assert raw is not None, f"the workflow found no pack in:\n{body}"
    pack = json.loads(raw)
    assert pack["runId"] == targets_doc["run"]["runId"]
    assert pack["candidateSha"] == targets_doc["run"]["candidateSha"]
    assert pack["verdict"] == "revise"
    assert pack["route"] == "outer"
    assert pack["annotations"][0]["comment"] == "this pixel is wrong"


@needs_node
def test_the_body_is_readable_by_a_human_too(tmp_path: Path, targets_doc: dict) -> None:
    """It is posted as a PR review, so it must not be an opaque wall of JSON."""
    body = run_node(
        tmp_path,
        REVIEW_SCRIPT,
        TARGETS=json.dumps(targets_doc),
        SUMMARY="s",
        COMMENT="c",
    )
    head = body.split("```", 1)[0]
    assert targets_doc["run"]["runId"] in head
    assert targets_doc["run"]["candidateSha"] in head


@needs_node
def test_reviewer_text_cannot_close_the_fence_early(tmp_path: Path, targets_doc: dict) -> None:
    """The adversarial case: a reviewer types a code fence into their comment.

    If that could terminate the block, CI would parse a truncated prefix -- and
    a truncated pack is either a JSON error or, far worse, a *valid* pack that
    silently drops the findings after the injected fence.
    """
    body = run_node(
        tmp_path,
        REVIEW_SCRIPT,
        TARGETS=json.dumps(targets_doc),
        SUMMARY="closing early:\n```\nnope\n```\n```" + REVIEW_FENCE + "\n{}\n```",
        COMMENT='```\n{"verdict": "accept"}\n```',
    )
    raw = extract(body)
    assert raw is not None, "an injected fence truncated the block"
    pack = json.loads(raw)
    # Not merely parseable -- still carrying everything the reviewer wrote.
    assert pack["verdict"] == "revise", "an injected fence must not flip the verdict"
    assert len(pack["annotations"]) == 1
    assert "```" in pack["summary"], "the reviewer's literal text is preserved"


@needs_node
def test_a_pack_that_travelled_by_review_still_verifies(tmp_path: Path, targets_doc: dict) -> None:
    """The fence is transport, not transformation.

    ``apply_feedback`` recomputes ``packDigest`` over the bytes it received. If
    the review round-trip perturbed so much as a float, every packet a GUI sent
    through the authorised channel would be refused for "corruption".
    """
    from adlc.stages.feedback import pack_digest

    body = run_node(
        tmp_path,
        REVIEW_SCRIPT,
        TARGETS=json.dumps(targets_doc),
        SUMMARY="caf\u00e9 \u2014 needs work",
        COMMENT="0.1234 is on the grid",
    )
    raw = extract(body)
    assert raw is not None
    pack = json.loads(raw)
    declared = pack.pop("packDigest", None)
    if declared is not None:
        assert declared == pack_digest(pack)


# ---------------------------------------------------------------------------
# The fence is never hard-coded downstream
# ---------------------------------------------------------------------------


def test_the_sdk_reads_the_fence_from_the_manifest(tmp_path: Path) -> None:
    """If the SDK embedded the literal, publishing it would be theatre.

    Checked against the *fenced* form rather than the bare word: the pack
    schema version legitimately contains the same substring, and a test that
    banned that would be a test nobody could satisfy honestly.
    """
    source = sdk_source()
    assert "submission.reviewFence" in source
    assert "```" + REVIEW_FENCE not in source, (
        "the SDK hard-codes the fence instead of reading it from the manifest"
    )


def test_the_console_uses_the_review_transport() -> None:
    html = console_asset("console.html")
    js = console_asset("console.js")
    assert 'id="copy-review"' in html
    assert "toReviewBody()" in js
    assert "```" + REVIEW_FENCE not in js, "the console must not hard-code it either"


@needs_node
def test_a_manifest_without_a_fence_fails_loudly(tmp_path: Path, targets_doc: dict) -> None:
    """A missing fence must not become a guessed one.

    Guessing would produce a review body that looks right, posts cleanly, and is
    then silently ignored by CI -- the reviewer's work lost with no error
    anywhere.
    """
    targets_doc["submission"].pop("reviewFence")
    script = (
        REQUIRE_SDK + "const targets = JSON.parse(process.env.TARGETS);\n"
        "const s = sdk.createSession(targets);\n"
        "s.setSummary('x');\n"
        "Promise.resolve().then(() => s.toReviewBody())\n"
        "  .then(() => console.log('accepted'))\n"
        "  .catch(() => console.log('refused'));\n"
    )
    out = run_node(tmp_path, script, TARGETS=json.dumps(targets_doc)).strip()
    assert out == "refused"
