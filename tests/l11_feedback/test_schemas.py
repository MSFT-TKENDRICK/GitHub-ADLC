"""L11 S1 -- the human-feedback and evidence-diff contracts.

The pack is authored in a browser, may be hand-edited, and its rendered form is
read by an agent in the *successor* run. It is therefore untrusted input to a
prompt, and these tests exist to prove the schema actually closes the doors it
claims to close: no unknown properties, no unbounded text, no geometry that
escapes the image, and no field that merely *claims* to be a digest.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from adlc import ports
from adlc.schemas import is_valid, load_schema, schema_dir

PACK = "human-feedback-pack"
DIFF = "evidence-diff"


def _invalid(name: str, payload: Any) -> list[str]:
    ok, errors = is_valid(name, payload)
    assert not ok, f"expected {name} to reject the payload, but it validated"
    return errors


# ---------------------------------------------------------------------------
# The schema files themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [PACK, DIFF])
def test_schema_is_wellformed_and_discoverable(name: str) -> None:
    path = schema_dir() / f"{name}.schema.json"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["type"] == "object"
    # Closed at the top level: an unknown key is a bug or an attack, never noise.
    assert doc["additionalProperties"] is False


def test_every_object_in_the_pack_is_closed() -> None:
    """A single open sub-object would reopen the whole surface."""
    doc = load_schema(PACK)
    open_paths: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                open_paths.append(path or "<root>")
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}")

    walk(doc, "")
    assert open_paths == []


def test_every_free_text_field_is_capped() -> None:
    """Unbounded prose is how a pack becomes a prompt-injection payload."""
    doc = load_schema(PACK)
    uncapped: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            constrained = {"maxLength", "pattern", "enum", "const", "format"}
            if node.get("type") == "string" and not constrained & set(node):
                uncapped.append(path or "<root>")
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}")

    walk(doc, "")
    assert uncapped == []


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_valid_pack_validates(valid_pack: dict[str, Any]) -> None:
    ok, errors = is_valid(PACK, valid_pack)
    assert ok, errors


def test_pack_is_valid_with_only_required_fields(valid_pack: dict[str, Any]) -> None:
    """A reviewer who only clicks 'accept' must produce a legal pack."""
    minimal = {k: valid_pack[k] for k in load_schema(PACK)["required"]}
    ok, errors = is_valid(PACK, minimal)
    assert ok, errors


def test_valid_diff_validates(valid_diff: dict[str, Any]) -> None:
    ok, errors = is_valid(DIFF, valid_diff)
    assert ok, errors


def test_diff_states_why_it_is_empty(valid_diff: dict[str, Any]) -> None:
    """No baseline is a *stated* condition, never a silently empty diff."""
    doc = dict(valid_diff)
    doc.update(
        baselineRunId=None,
        reason="run has no referencesRun",
        measurements=[],
        coverage=[],
        screenshots=[],
    )
    ok, errors = is_valid(DIFF, doc)
    assert ok, errors


# ---------------------------------------------------------------------------
# Negative paths -- the doors the schema claims to close
# ---------------------------------------------------------------------------


def test_unknown_top_level_property_is_rejected(valid_pack: dict[str, Any]) -> None:
    doc = dict(valid_pack, evalScript="rm -rf /")
    assert any("evalScript" in e for e in _invalid(PACK, doc))


def test_unknown_annotation_property_is_rejected(valid_pack: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["html"] = "<script>alert(1)</script>"
    assert any("html" in e for e in _invalid(PACK, doc))


@pytest.mark.parametrize("bad", [[1.4, 0.5], [-0.01, 0.5], [0.5, 2.0]])
def test_geometry_outside_the_image_is_rejected(
    valid_pack: dict[str, Any], bad: list[float]
) -> None:
    """Normalised coordinates are 0..1 by construction; anything else is nonsense."""
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["geometry"]["points"] = [bad]
    _invalid(PACK, doc)


def test_geometry_point_must_be_a_pair(valid_pack: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["geometry"]["points"] = [[0.1, 0.2, 0.3]]
    _invalid(PACK, doc)


def test_freehand_stroke_length_is_bounded(valid_pack: dict[str, Any]) -> None:
    """A megabyte of scribble must not become a megabyte of agent prompt."""
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["shape"] = "freehand"
    doc["annotations"][0]["geometry"]["points"] = [[0.5, 0.5]] * 401
    _invalid(PACK, doc)


@pytest.mark.parametrize("bad", ["not-a-sha", "A" * 64, "", "abc123"])
def test_bad_artifact_hash_is_rejected(valid_pack: dict[str, Any], bad: str) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["artifactSha256"] = bad
    _invalid(PACK, doc)


def test_annotation_without_an_artifact_hash_is_rejected(valid_pack: dict[str, Any]) -> None:
    """Citation-or-discard: markup that cites nothing anchors to nothing."""
    doc = copy.deepcopy(valid_pack)
    del doc["annotations"][0]["artifactSha256"]
    _invalid(PACK, doc)


def test_empty_comment_is_rejected(valid_pack: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["comment"] = ""
    _invalid(PACK, doc)


@pytest.mark.parametrize(
    ("collection", "field"),
    [("annotations", "comment"), ("critiques", "comment"), ("diffDecisions", "comment")],
)
def test_overlong_text_is_rejected(
    valid_pack: dict[str, Any], collection: str, field: str
) -> None:
    doc = copy.deepcopy(valid_pack)
    doc[collection][0][field] = "x" * (ports.FEEDBACK_MAX_TEXT + 1)
    _invalid(PACK, doc)


def test_overlong_summary_is_rejected(valid_pack: dict[str, Any]) -> None:
    doc = dict(valid_pack, summary="x" * (ports.FEEDBACK_MAX_TEXT + 1))
    _invalid(PACK, doc)


def test_too_many_annotations_is_rejected(valid_pack: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["annotations"] = [doc["annotations"][0]] * (ports.FEEDBACK_MAX_ITEMS + 1)
    _invalid(PACK, doc)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("verdict", "approve"),
        ("route", "sideways"),
        ("schemaVersion", "adlc-human-feedback/v2"),
        ("candidateSha", "zzzz"),
        ("runId", "../../etc/passwd"),
        ("reportDigest", "d" * 64),
        ("packDigest", "md5:" + "d" * 32),
    ],
)
def test_bad_scalar_is_rejected(valid_pack: dict[str, Any], field: str, bad: str) -> None:
    _invalid(PACK, dict(valid_pack, **{field: bad}))


@pytest.mark.parametrize("field", ["schemaVersion", "runId", "candidateSha", "verdict", "route"])
def test_missing_required_field_is_rejected(valid_pack: dict[str, Any], field: str) -> None:
    doc = dict(valid_pack)
    del doc[field]
    _invalid(PACK, doc)


@pytest.mark.parametrize("bad", ["maybe", "AGREE", ""])
def test_bad_stance_is_rejected(valid_pack: dict[str, Any], bad: str) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["critiques"][0]["stance"] = bad
    _invalid(PACK, doc)


def test_bad_source_digest_is_rejected(valid_pack: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["critiques"][0]["sourceDigest"] = "trust me"
    _invalid(PACK, doc)


@pytest.mark.parametrize("bad", ["oval", "RECT", ""])
def test_bad_shape_is_rejected(valid_pack: dict[str, Any], bad: str) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["annotations"][0]["shape"] = bad
    _invalid(PACK, doc)


@pytest.mark.parametrize("bad", ["defer", "accepted", ""])
def test_bad_diff_decision_is_rejected(valid_pack: dict[str, Any], bad: str) -> None:
    doc = copy.deepcopy(valid_pack)
    doc["diffDecisions"][0]["decision"] = bad
    _invalid(PACK, doc)


def test_bad_diff_change_is_rejected(valid_diff: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_diff)
    doc["measurements"][0]["change"] = "improved"
    _invalid(DIFF, doc)


def test_unknown_diff_property_is_rejected(valid_diff: dict[str, Any]) -> None:
    doc = copy.deepcopy(valid_diff)
    doc["measurements"][0]["verdict"] = "ship it"
    assert any("verdict" in e for e in _invalid(DIFF, doc))


@pytest.mark.parametrize("field", ["measurements", "coverage", "screenshots"])
def test_diff_requires_every_collection(valid_diff: dict[str, Any], field: str) -> None:
    """An absent collection is indistinguishable from 'nothing changed'."""
    doc = dict(valid_diff)
    del doc[field]
    _invalid(DIFF, doc)


# ---------------------------------------------------------------------------
# Contract mirror: ports.py must not drift from the schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "pointer"),
    [
        ("FeedbackVerdict", "properties/verdict/enum"),
        ("FeedbackRoute", "properties/route/enum"),
        ("AnnotationShape", "$defs/annotation/properties/shape/enum"),
        ("FeedbackSeverity", "$defs/severity/enum"),
        ("CritiqueTargetKind", "$defs/critique/properties/targetKind/enum"),
        ("CritiqueStance", "$defs/critique/properties/stance/enum"),
        ("DiffTargetKind", "$defs/diffDecision/properties/targetKind/enum"),
    ],
)
def test_ports_literals_match_the_schema(literal: str, pointer: str) -> None:
    node: Any = load_schema(PACK)
    for part in pointer.split("/"):
        node = node[part]
    assert set(getattr(ports, literal).__args__) == set(node)


def test_ports_caps_match_the_schema() -> None:
    doc = load_schema(PACK)
    assert doc["properties"]["summary"]["maxLength"] == ports.FEEDBACK_MAX_TEXT
    assert doc["$defs"]["annotation"]["properties"]["comment"]["maxLength"] == (
        ports.FEEDBACK_MAX_TEXT
    )
    assert doc["properties"]["annotations"]["maxItems"] == ports.FEEDBACK_MAX_ITEMS


def test_every_verdict_maps_to_a_decision_outcome() -> None:
    """A verdict with no outcome would be feedback that changes nothing."""
    assert set(ports.FEEDBACK_OUTCOME) == set(ports.FeedbackVerdict.__args__)
    assert set(ports.FEEDBACK_OUTCOME.values()) <= set(ports.DecisionOutcome.__args__)


def test_feedback_stages_are_declared() -> None:
    assert "feedback" in ports.StageName.__args__
    assert "evidence_diff" in ports.StageName.__args__


def test_schema_dir_is_the_repo_schemas_dir() -> None:
    assert schema_dir() == Path(__file__).resolve().parents[2] / "schemas"
