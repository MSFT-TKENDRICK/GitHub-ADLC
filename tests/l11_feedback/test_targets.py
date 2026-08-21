"""The GUI-agnostic feedback manifest.

These tests defend one property above all others: a review GUI reading
``feedback-targets.json`` should never need to know anything about ADLC that the
document does not tell it. Every assertion here is really the same question --
"could a GUI author get this wrong, and would they find out only after a
reviewer's work was thrown away?"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.runs import read_json, write_json
from adlc.schemas import is_valid, load_schema
from adlc.stages.evidence_diff import run_evidence_diff
from adlc.stages.feedback_targets import (
    SCHEMA_VERSION,
    SchemaDerivationError,
    compute_targets,
    run_feedback_targets,
    submission_contract,
    targets_path,
)

from .conftest import BASELINE_SHA, CANDIDATE_SHA, make_run


@pytest.fixture
def run(cfg: Config):
    return make_run(
        cfg,
        "2026-08-20-c0de",
        head_sha=CANDIDATE_SHA,
        screenshots={"home.png": (10, 20, 30)},
        measurements=[
            {"metricId": "lcp_ms", "value": 2200.0, "budget": 2500.0, "passed": True,
             "collector": "lighthouse"}
        ],
        coverage=[
            {"requirementId": "US1-AC1", "present": True, "evidenceKinds": ["screenshot"],
             "artifactSha256": ["c" * 64]}
        ],
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_targets_validate_against_their_schema(cfg: Config, run) -> None:
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run)
    ok, errors = is_valid("feedback-targets", targets)
    assert ok, errors
    assert targets["schemaVersion"] == SCHEMA_VERSION


def test_run_identity_is_echoed_verbatim(cfg: Config, run) -> None:
    """A GUI must never re-derive these; it would eventually derive them wrong."""
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run)
    assert targets["run"]["runId"] == run.run_id
    assert targets["run"]["candidateSha"] == CANDIDATE_SHA


def test_run_stage_writes_the_document_and_a_stage_result(cfg: Config, run) -> None:
    reduce_run(cfg, run)
    run_feedback_targets(cfg, run)
    assert targets_path(run).is_file()
    written = read_json(targets_path(run))
    assert written["schemaVersion"] == SCHEMA_VERSION
    stage = run.latest_stage("feedback-targets")
    assert stage is not None
    assert stage["status"] == "ok"
    assert "feedback-targets.json" in stage["outputs"][0]


def test_empty_run_still_produces_a_valid_document(cfg: Config) -> None:
    """Backwards compatibility: nothing to review is a valid state, not an error."""
    bare = make_run(cfg, "2026-08-20-bare", head_sha=CANDIDATE_SHA)
    reduce_run(cfg, bare)
    targets = compute_targets(cfg, bare)
    ok, errors = is_valid("feedback-targets", targets)
    assert ok, errors
    assert targets["diff"] is None
    assert targets["artifacts"] == []


# ---------------------------------------------------------------------------
# The submission contract -- derived, never hand-copied
# ---------------------------------------------------------------------------


def test_enums_are_derived_from_the_pack_schema() -> None:
    """The point of derivation: these cannot silently disagree with ingestion."""
    pack = load_schema("human-feedback-pack")
    contract = submission_contract()
    assert contract["enums"]["verdict"] == pack["properties"]["verdict"]["enum"]
    assert contract["enums"]["route"] == pack["properties"]["route"]["enum"]
    assert contract["enums"]["severity"] == pack["$defs"]["severity"]["enum"]
    assert (
        contract["enums"]["shape"]
        == pack["$defs"]["annotation"]["properties"]["shape"]["enum"]
    )
    assert (
        contract["enums"]["critiqueStance"]
        == pack["$defs"]["critique"]["properties"]["stance"]["enum"]
    )
    assert (
        contract["enums"]["diffDecision"]
        == pack["$defs"]["diffDecision"]["properties"]["decision"]["enum"]
    )


def test_limits_are_derived_from_the_pack_schema() -> None:
    pack = load_schema("human-feedback-pack")
    limits = submission_contract()["limits"]
    assert limits["annotations"] == pack["properties"]["annotations"]["maxItems"]
    assert (
        limits["geometryPoints"]
        == pack["$defs"]["annotation"]["properties"]["geometry"]["properties"]["points"][
            "maxItems"
        ]
    )
    assert (
        limits["commentChars"]
        == pack["$defs"]["annotation"]["properties"]["comment"]["maxLength"]
    )
    assert limits["idChars"] == pack["$defs"]["id"]["maxLength"]


def test_id_pattern_is_published_so_a_gui_can_mint_valid_ids() -> None:
    pack = load_schema("human-feedback-pack")
    assert submission_contract()["idPattern"] == pack["$defs"]["id"]["pattern"]


def test_schema_drift_fails_loudly_rather_than_emitting_an_empty_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GUI handed an empty dropdown shows the reviewer nothing and explains nothing.

    The build that broke it should fail instead -- somebody is looking at that.
    """
    import adlc.stages.feedback_targets as ft

    monkeypatch.setattr(ft, "load_schema", lambda name: {"properties": {}, "$defs": {}})
    with pytest.raises(SchemaDerivationError):
        ft.submission_contract()


def test_endpoint_is_null_when_there_is_no_server(cfg: Config, run) -> None:
    reduce_run(cfg, run)
    assert compute_targets(cfg, run)["submission"]["endpoint"] is None


def test_endpoint_is_carried_through_when_supplied(cfg: Config, run) -> None:
    reduce_run(cfg, run)
    targets = compute_targets(
        cfg, run, endpoint="http://127.0.0.1:9/feedback", nonce_header="X-ADLC-Nonce", nonce="n"
    )
    assert targets["submission"]["endpoint"] == "http://127.0.0.1:9/feedback"
    assert targets["submission"]["nonceHeader"] == "X-ADLC-Nonce"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def test_screenshots_are_inlined_with_natural_size(cfg: Config, run) -> None:
    """Natural size is what makes normalised geometry mean anything."""
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run)
    shots = [a for a in targets["artifacts"] if a["kind"] == "screenshot"]
    assert shots, targets["artifacts"]
    shot = shots[0]
    assert shot["annotatable"] is True
    assert shot["inline"].startswith("data:image/png;base64,")
    assert shot["inlineOmittedReason"] is None
    assert (shot["width"], shot["height"]) == (8, 6)


def test_over_budget_artifacts_degrade_with_a_stated_reason(cfg: Config, run) -> None:
    """Never a silent drop: the reviewer must be able to see what they cannot see."""
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run, per_artifact_bytes=1)
    shot = next(a for a in targets["artifacts"] if a["kind"] == "screenshot")
    assert shot["inline"] is None
    assert "not inlined" in shot["inlineOmittedReason"]
    assert shot["sha256"] and shot["bytes"], "hash and size still identify it"
    assert targets["budgets"]["omittedCount"] >= 1
    assert targets["budgets"]["inlinedBytes"] == 0


def test_total_budget_stops_inlining_across_artifacts(cfg: Config) -> None:
    many = make_run(
        cfg,
        "2026-08-20-many",
        head_sha=CANDIDATE_SHA,
        screenshots={f"s{i}.png": (i, i, i) for i in range(5)},
    )
    reduce_run(cfg, many)
    targets = compute_targets(cfg, many, total_bytes=1)
    assert targets["budgets"]["inlinedCount"] == 0
    assert targets["budgets"]["omittedCount"] >= 5
    assert all(
        a["inlineOmittedReason"] for a in targets["artifacts"] if a["kind"] == "screenshot"
    )


def test_unannotatable_artifacts_are_listed_not_hidden(cfg: Config, run) -> None:
    """A filtered evidence list is a lie by omission."""
    (run.evidence_dir / "candidate-a" / "trace.zip").write_bytes(b"PK\x03\x04not-a-real-zip")
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run)
    traces = [a for a in targets["artifacts"] if a["path"].endswith("trace.zip")]
    assert traces, [a["path"] for a in targets["artifacts"]]
    assert traces[0]["annotatable"] is False
    assert traces[0]["inline"] is None


def test_budgets_are_reported_so_a_bloated_document_is_visible(cfg: Config, run) -> None:
    reduce_run(cfg, run)
    budgets = compute_targets(cfg, run)["budgets"]
    assert budgets["inlinedCount"] >= 1
    assert budgets["inlinedBytes"] > 0
    assert budgets["perArtifactBytes"] > 0


def test_config_can_override_the_budget(cfg: Config, run) -> None:
    cfg.raw = {"feedback": {"perArtifactBytes": 1}}
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run)
    assert targets["budgets"]["perArtifactBytes"] == 1
    assert targets["budgets"]["inlinedCount"] == 0


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


def _write_review(run, name: str, body: str) -> None:
    run.reviews_dir.mkdir(parents=True, exist_ok=True)
    (run.reviews_dir / name).write_text(body, encoding="utf-8")


def test_squad_findings_become_critique_targets(cfg: Config, run) -> None:
    _write_review(
        run,
        "adversarial_review.security-adversary.md",
        "---\n"
        "squad: adversarial_review\n"
        "member: security-adversary\n"
        "verdict: block\n"
        "---\n\n"
        "## [high] Unescaped slug\n\n"
        "The repo slug is interpolated raw into an href.\n"
        "See src/adlc/stages/report/sections/decisions.py:L41 for the site.\n",
    )
    reduce_run(cfg, run)
    targets = compute_targets(cfg, run)
    findings = [r for r in targets["reasoning"] if r["targetKind"] == "squad_finding"]
    assert findings, targets["reasoning"]
    finding = findings[0]
    assert "raw into an href" in finding["text"]
    assert finding["author"] == "security-adversary"
    assert finding["targetRef"].startswith("reviews/")
    assert "#finding-" in finding["targetRef"], "a critique must locate a span, not a file"
    assert finding["sourceDigest"].startswith("sha256:")


def test_personas_become_critique_targets(cfg: Config, run) -> None:
    run.enrichment_dir.mkdir(parents=True, exist_ok=True)
    (run.enrichment_dir / "personas.md").write_text(
        "## Keyboard-only operator\n\nCannot use a mouse; relies on focus order.\n\n"
        "## Screen-reader user\n\nNeeds every state change announced.\n",
        encoding="utf-8",
    )
    reduce_run(cfg, run)
    personas = [r for r in compute_targets(cfg, run)["reasoning"] if r["targetKind"] == "persona"]
    assert len(personas) == 2
    assert {p["targetTitle"] for p in personas} == {
        "Keyboard-only operator",
        "Screen-reader user",
    }
    assert all("#" in p["targetRef"] for p in personas)


def test_rubric_rationales_become_critique_targets(cfg: Config, run) -> None:
    run.evals_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        run.evals_dir / "rubric-score.json",
        {
            "overall": 0.5,
            "threshold": 0.7,
            "backend": "deterministic",
            "criteria": [
                {
                    "id": "C1",
                    "statement": "Acceptance scenarios are executable.",
                    "passed": False,
                    "rationale": "No feature file was found in the run.",
                }
            ],
        },
    )
    reduce_run(cfg, run)
    crits = [
        r for r in compute_targets(cfg, run)["reasoning"] if r["targetKind"] == "rubric_criterion"
    ]
    assert len(crits) == 1
    assert crits[0]["text"] == "No feature file was found in the run."
    assert crits[0]["targetRef"] == "evals/rubric-score.json#C1"


def test_reasoning_without_text_is_dropped_not_offered_empty(cfg: Config, run) -> None:
    """An empty card invites a critique of nothing."""
    run.evals_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        run.evals_dir / "rubric-score.json",
        {"criteria": [{"id": "C1", "statement": "x", "rationale": ""}]},
    )
    reduce_run(cfg, run)
    assert compute_targets(cfg, run)["reasoning"] == []


def test_a_malformed_review_does_not_sink_the_manifest(cfg: Config, run) -> None:
    """One bad file must not cost the reviewer every other target in the run."""
    _write_review(run, "broken.md", "no frontmatter here, just prose\n")
    _write_review(
        run,
        "good.md",
        "---\nsquad: s\nmember: m\nverdict: pass\n---\n\n"
        "## [low] T\n\nSomething real.\n",
    )
    reduce_run(cfg, run)
    texts = [r["text"] for r in compute_targets(cfg, run)["reasoning"]]
    assert any("Something real." in t for t in texts)


def test_reasoning_ids_are_unique(cfg: Config, run) -> None:
    run.enrichment_dir.mkdir(parents=True, exist_ok=True)
    (run.enrichment_dir / "personas.md").write_text(
        "## A\n\nfirst\n\n## B\n\nsecond\n", encoding="utf-8"
    )
    run.evals_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        run.evals_dir / "rubric-score.json",
        {"criteria": [{"id": "C1", "statement": "s", "rationale": "why"}]},
    )
    reduce_run(cfg, run)
    ids = [r["id"] for r in compute_targets(cfg, run)["reasoning"]]
    assert len(ids) == len(set(ids)) == 3


# ---------------------------------------------------------------------------
# Diff -- decision-ready rows
# ---------------------------------------------------------------------------


@pytest.fixture
def diffed(cfg: Config):
    baseline = make_run(
        cfg,
        "2026-08-19-a1b2",
        head_sha=BASELINE_SHA,
        screenshots={"home.png": (10, 20, 30), "gone.png": (1, 2, 3)},
        measurements=[
            {"metricId": "lcp_ms", "value": 1800.0, "budget": 2500.0, "passed": True},
        ],
        coverage=[{"requirementId": "US1-AC1", "present": True, "evidenceKinds": ["screenshot"]}],
    )
    reduce_run(cfg, baseline)
    candidate = make_run(
        cfg,
        "2026-08-20-c0de",
        head_sha=CANDIDATE_SHA,
        references_run=baseline.run_id,
        screenshots={"home.png": (99, 99, 99)},
        measurements=[
            {"metricId": "lcp_ms", "value": 2600.0, "budget": 2500.0, "passed": False},
        ],
        coverage=[{"requirementId": "US1-AC1", "present": False, "evidenceKinds": []}],
    )
    reduce_run(cfg, candidate)
    run_evidence_diff(cfg, candidate)
    return candidate


def test_diff_rows_carry_the_identifiers_a_decision_must_name(cfg: Config, diffed) -> None:
    """A GUI that derives a targetId will eventually derive one ingestion rejects."""
    diff = compute_targets(cfg, diffed)["diff"]
    assert diff is not None
    pack = load_schema("human-feedback-pack")
    allowed = pack["$defs"]["diffDecision"]["properties"]["targetKind"]["enum"]
    for key in ("measurements", "coverage", "screenshots"):
        for row in diff[key]:
            assert row["targetKind"] in allowed
            assert row["targetId"], row


def test_regression_is_precomputed_once(cfg: Config, diffed) -> None:
    """Otherwise every GUI invents its own rule and they disagree with each other."""
    diff = compute_targets(cfg, diffed)["diff"]
    lcp = next(m for m in diff["measurements"] if m["targetId"] == "lcp_ms")
    assert lcp["budgetCrossed"] == "entered_breach"
    assert lcp["regression"] is True
    lost = [c for c in diff["coverage"] if c["change"] == "lost"]
    assert lost and lost[0]["regression"] is True


def test_removed_screenshot_is_a_regression_but_changed_is_a_question(
    cfg: Config, diffed
) -> None:
    """A changed pixel is exactly what the human is being asked about."""
    diff = compute_targets(cfg, diffed)["diff"]
    by_id = {s["targetId"]: s for s in diff["screenshots"]}
    assert by_id["gone.png"]["change"] == "removed"
    assert by_id["gone.png"]["regression"] is True
    assert by_id["home.png"]["change"] == "changed"
    assert by_id["home.png"]["regression"] is False


def test_baseline_screenshot_is_inlined_for_side_by_side(cfg: Config, diffed) -> None:
    """The baseline lives in another run dir, so nothing else in the document has it."""
    diff = compute_targets(cfg, diffed)["diff"]
    home = next(s for s in diff["screenshots"] if s["targetId"] == "home.png")
    assert home["baselineInline"].startswith("data:image/png;base64,")
    # The candidate is already inlined once under `artifacts`; duplicating it
    # here would double the document for nothing.
    assert home["inline"] is None


def test_diffed_targets_validate(cfg: Config, diffed) -> None:
    ok, errors = is_valid("feedback-targets", compute_targets(cfg, diffed))
    assert ok, errors


def test_a_corrupt_diff_file_omits_the_section_rather_than_crashing(
    cfg: Config, diffed
) -> None:
    from adlc.stages.evidence_diff import diff_path

    diff_path(diffed).write_text("{not json", encoding="utf-8")
    targets = compute_targets(cfg, diffed)
    assert targets["diff"] is None
    ok, _ = is_valid("feedback-targets", targets)
    assert ok


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------


def test_requirements_are_offered_for_linkage(cfg: Config, run) -> None:
    run.spec_dir.mkdir(parents=True, exist_ok=True)
    (run.spec_dir / "spec.md").write_text(
        "- **US1-AC1**: A theme toggle exists.\n- **US1-AC2**: It applies immediately.\n",
        encoding="utf-8",
    )
    reduce_run(cfg, run)
    reqs = compute_targets(cfg, run)["requirements"]
    assert {r["id"] for r in reqs} >= {"US1-AC1", "US1-AC2"}
    assert all(r["source"] for r in reqs)


# ---------------------------------------------------------------------------
# End to end -- the manifest is genuinely enough to build an accepted pack
# ---------------------------------------------------------------------------


def test_a_pack_built_only_from_the_manifest_is_accepted(cfg: Config, diffed) -> None:
    """The real proof. No ADLC internals were consulted to build this pack.

    Everything below comes out of the manifest: the run identity, an artifact
    hash to cite, a reasoning target's digest, and a diff row's target pair. If a
    GUI can do this, it does not matter what the GUI is written in or what it
    looks like -- which is the entire point of this layer.
    """
    (diffed.enrichment_dir / "personas.md").parent.mkdir(parents=True, exist_ok=True)
    (diffed.enrichment_dir / "personas.md").write_text(
        "## Keyboard-only operator\n\nRelies on a visible focus order.\n", encoding="utf-8"
    )
    reduce_run(cfg, diffed)
    targets = compute_targets(cfg, diffed)

    artifact = next(a for a in targets["artifacts"] if a["annotatable"])
    reasoning = targets["reasoning"][0]
    row = next(
        r
        for r in targets["diff"]["measurements"] + targets["diff"]["screenshots"]
        if r["change"] != "unchanged"
    )
    enums = targets["submission"]["enums"]

    pack: dict[str, Any] = {
        "schemaVersion": targets["submission"]["packSchemaVersion"],
        "runId": targets["run"]["runId"],
        "candidateSha": targets["run"]["candidateSha"],
        "reportDigest": targets["run"]["reportDigest"],
        "submittedAt": "2026-08-20T12:00:00Z",
        "verdict": "revise",
        "route": enums["route"][0],
        "summary": "Assembled from the manifest alone.",
        "annotations": [
            {
                "id": "an-1",
                "artifactSha256": artifact["sha256"],
                "artifactPath": artifact["path"],
                "artifactKind": artifact["kind"],
                "shape": enums["shape"][0],
                "geometry": {"points": [[0.1, 0.1], [0.4, 0.35]]},
                "severity": enums["severity"][1],
                "comment": "The focus ring is invisible here.",
                "requirementIds": [r["id"] for r in targets["requirements"][:1]],
            }
        ],
        "critiques": [
            {
                "id": "cr-1",
                "targetKind": reasoning["targetKind"],
                "targetRef": reasoning["targetRef"],
                "sourceDigest": reasoning["sourceDigest"],
                "stance": enums["critiqueStance"][1],
                "comment": "This misses the keyboard path entirely.",
            }
        ],
        "diffDecisions": [
            {
                "id": "dd-1",
                "targetKind": row["targetKind"],
                "targetId": row["targetId"],
                "decision": "reject",
                "comment": "Not acceptable.",
                "annotationIds": ["an-1"],
            }
        ],
    }
    if pack["reportDigest"] is None:
        del pack["reportDigest"]

    ok, errors = is_valid("human-feedback-pack", pack)
    assert ok, errors

    # And ingestion actually accepts it: no stale SHA, no discarded citation.
    from adlc.stages.feedback import apply_feedback

    result = apply_feedback(cfg, diffed, pack, retrigger=False)
    assert result["applied"] is True, result
    assert not result.get("discarded"), result["discarded"]


def test_cli_writes_the_manifest(cfg: Config, run, tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from adlc.cli import app

    reduce_run(cfg, run)
    monkeypatch.setattr("adlc.cli._cfg", lambda: cfg)
    out = tmp_path / "targets.json"
    result = CliRunner().invoke(app, ["feedback", "targets", run.run_id, "--out", str(out)])
    assert result.exit_code == 0, result.output
    ok, errors = is_valid("feedback-targets", json.loads(out.read_text(encoding="utf-8")))
    assert ok, errors


def test_cli_exports_the_sdk(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from adlc.cli import app

    result = CliRunner().invoke(app, ["feedback", "sdk", "--out", str(tmp_path / "vendor")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "vendor" / "adlc-feedback.js").is_file()
    assert (tmp_path / "vendor" / "adlc-feedback.mjs").is_file()
