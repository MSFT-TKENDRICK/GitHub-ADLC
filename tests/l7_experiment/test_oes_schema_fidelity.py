"""The vendored OES schema must be the published one, and our enums must match it.

This is the test that keeps the exporter honest. Every enum the exporter uses to
decide what it is allowed to emit is re-derived here *from the schema document*
and compared with the module constant, so a hand-typed value that does not exist
in OES 0.1.0 fails immediately rather than at a consumer's import step.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from adlc.adapters.export import oes
from adlc.stages import experiment


def _enum(schema: dict[str, Any], *path: str) -> frozenset[str]:
    """Pull an ``enum`` out of the schema by walking a property path."""
    node: Any = schema
    for part in path:
        if part == "[]":
            node = node["items"]
        else:
            node = node["properties"][part]
    return frozenset(node["enum"])


def test_vendored_copy_matches_the_module_copy(oes_schema: dict[str, Any]) -> None:
    """The schema embedded in ``oes.py`` is the file downloaded from the standard."""
    assert oes.load_oes_schema() == oes_schema


def test_vendored_copy_is_the_published_document(oes_schema: dict[str, Any]) -> None:
    assert oes_schema["$id"] == oes.OES_SCHEMA_URL
    assert oes_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert oes_schema["required"] == ["schemaVersion", "objectType", "experiment"]
    # 19 top-level keys, not 14: the exporter must be written against the real
    # document rather than a summary of it.
    assert len(oes_schema["properties"]) == 19
    assert set(oes_schema["properties"]) == {
        "schemaVersion",
        "objectType",
        "exportedAt",
        "sourceSystem",
        "sourceSystemVersion",
        "canonicalUrl",
        "externalIds",
        "experiment",
        "design",
        "variants",
        "metrics",
        "analysis",
        "results",
        "scorecard",
        "decision",
        "qualityChecks",
        "artifacts",
        "provenance",
        "extensions",
    }


def test_schema_version_is_accepted_by_its_own_pattern(oes_schema: dict[str, Any]) -> None:
    import re

    pattern = oes_schema["properties"]["schemaVersion"]["pattern"]
    assert re.match(pattern, oes.OES_SCHEMA_VERSION)


@pytest.mark.parametrize(
    ("constant", "path"),
    [
        (oes.EXPERIMENT_STATUSES, ("experiment", "status")),
        (oes.DECISION_OUTCOMES, ("decision", "outcome")),
        (oes.DECISION_STATUSES, ("decision", "status")),
        (oes.CHECK_STATUSES, ("qualityChecks", "[]", "status")),
        (oes.CHECK_SEVERITIES, ("qualityChecks", "[]", "severity")),
        (oes.ARTIFACT_TYPES, ("artifacts", "[]", "type")),
        (oes.SCORECARD_RESULTS, ("scorecard", "overallResult")),
        (oes.SCORECARD_ACTIONS, ("scorecard", "recommendedAction")),
        (oes.SCORECARD_QUALITY, ("scorecard", "qualityStatus")),
        (oes.RESULT_STATUSES, ("results", "metricResults", "[]", "resultStatus")),
        (oes.DECISION_IMPACTS, ("results", "metricResults", "[]", "decisionImpact")),
        (experiment.VARIANT_ROLES, ("variants", "[]", "role")),
        (experiment.METRIC_ROLES, ("metrics", "[]", "role")),
        (experiment.METRIC_DIRECTIONS, ("metrics", "[]", "direction")),
        (experiment.METRIC_TYPES, ("metrics", "[]", "type")),
        (experiment.DESIGN_TYPES, ("design", "type")),
        (experiment.ANALYSIS_METHODS, ("analysis", "method")),
    ],
)
def test_enums_match_the_schema(
    oes_schema: dict[str, Any], constant: frozenset[str], path: tuple[str, ...]
) -> None:
    assert constant == _enum(oes_schema, *path)


def test_artifact_enum_really_cannot_name_traces(oes_schema: dict[str, Any]) -> None:
    """The limitation the design is built around, asserted rather than assumed."""
    artifact_types = _enum(oes_schema, "artifacts", "[]", "type")
    for unrepresentable in ("trace", "har", "jsonl", "video", "zip", "log"):
        assert unrepresentable not in artifact_types
    assert not set(oes.ARTIFACT_KIND_TO_OES.values()) - artifact_types
    assert not set(oes.MIME_TYPE_TO_OES.values()) - artifact_types


def test_scorecard_and_decision_disagree_on_rollback(oes_schema: dict[str, Any]) -> None:
    """Guards the one enum mismatch that is easy to get wrong in both directions."""
    assert "rollback" in _enum(oes_schema, "decision", "outcome")
    assert "roll_back" in _enum(oes_schema, "scorecard", "recommendedAction")
    assert oes.DECISION_TO_SCORECARD_ACTION["rollback"] == "roll_back"
    # OES has no scorecard action meaning "partial rollout", so we drop it rather
    # than coerce it into something else.
    assert "partial_rollout" not in oes.DECISION_TO_SCORECARD_ACTION


def test_required_sub_object_fields(oes_schema: dict[str, Any]) -> None:
    props = oes_schema["properties"]
    assert props["experiment"]["required"] == ["id", "title"]
    assert props["variants"]["items"]["required"] == ["id", "key"]
    assert props["metrics"]["items"]["required"] == ["id", "name"]
    assert props["qualityChecks"]["items"]["required"] == ["checkType"]
    assert props["artifacts"]["items"]["required"] == ["type", "uri"]
    metric_result = props["results"]["properties"]["metricResults"]["items"]
    assert metric_result["required"] == ["metricId", "comparison"]
    assert metric_result["properties"]["comparison"]["required"] == [
        "baselineVariantId",
        "variantId",
    ]


def test_extensions_are_free_form(oes_schema: dict[str, Any]) -> None:
    extensions = oes_schema["properties"]["extensions"]
    assert extensions["additionalProperties"] is True
    assert "MUST safely ignore unknown extensions" in extensions["description"]


def test_check_type_is_a_free_string(oes_schema: dict[str, Any]) -> None:
    """``adlc:*`` namespacing is legal precisely because checkType has no enum."""
    check_type = oes_schema["properties"]["qualityChecks"]["items"]["properties"]["checkType"]
    assert check_type["type"] == "string"
    assert "enum" not in check_type


@pytest.mark.skipif(
    os.environ.get("ADLC_TEST_NETWORK") != "1",
    reason="network test; set ADLC_TEST_NETWORK=1 to check the vendored copy is current",
)
def test_vendored_copy_is_current(oes_schema: dict[str, Any]) -> None:  # pragma: no cover
    """Opt-in: re-fetch the published schema and diff it against the vendored copy."""
    import urllib.request

    with urllib.request.urlopen(oes.OES_SCHEMA_URL, timeout=30) as response:
        published = json.loads(response.read().decode("utf-8"))
    assert published == oes_schema, (
        "the published OES schema has changed; re-vendor it in "
        "src/adlc/adapters/export/oes.py and tests/l7_experiment/data/"
    )
