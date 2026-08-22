"""L11 -- the evidence-vs-baseline diff engine.

The evidence page is a snapshot; what a reviewer decides on is *movement*. These
tests pin the movement semantics that matter and would otherwise regress
silently: a budget that was crossed, a requirement that stopped being evidenced,
a screenshot that changed under a renamed variant. They assert on concrete
values rather than truthiness, because a diff that is merely "non-empty" can
still be wrong in exactly the way that misleads a human.
"""

from __future__ import annotations

from typing import Any

from adlc.config import Config
from adlc.schemas import is_valid
from adlc.stages.evidence_diff import ARTIFACT_NAME, compute_diff, diff_path, run_evidence_diff
from tests.l11_feedback.conftest import BASELINE_SHA, CANDIDATE_SHA, make_run

BASE_ID = "2026-08-19-a1b2"
CAND_ID = "2026-08-20-c0de"


# ---------------------------------------------------------------------------
# Builders for review-pack sub-documents (the shape make_run writes verbatim)
# ---------------------------------------------------------------------------


def _m(
    metric_id: str,
    value: float | None,
    *,
    budget: float | None = None,
    passed: bool | None = None,
    collector: str = "lighthouse",
    sha: str = "a" * 64,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "metricId": metric_id,
        "value": value,
        "collector": collector,
        "artifactSha256": sha,
    }
    if budget is not None:
        entry["budget"] = budget
    if passed is not None:
        entry["passed"] = passed
    return entry


def _c(req_id: str, present: bool, *, kinds: tuple[str, ...] = ("screenshot",)) -> dict[str, Any]:
    return {
        "requirementId": req_id,
        "present": present,
        "evidenceKinds": list(kinds),
        "artifactSha256": ["a" * 64] if present else [],
    }


def _by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def test_measurement_unchanged_has_zero_delta(cfg: Config) -> None:
    """An equal value is unchanged, and the report must be able to filter it out."""
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("lcp", 2000.0, budget=2500.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("lcp", 2000.0, budget=2500.0, passed=True)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["lcp"]
    assert m["change"] == "unchanged"
    assert m["delta"] == 0
    assert m["budgetCrossed"] == "none"


def test_measurement_changed_reports_signed_delta(cfg: Config) -> None:
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("lcp", 1800.0, budget=2500.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("lcp", 2200.0, budget=2500.0, passed=True)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["lcp"]
    assert m["change"] == "changed"
    assert m["value"] == 2200.0
    assert m["baselineValue"] == 1800.0
    assert m["delta"] == 400.0
    # Still inside its 2500 budget on both sides -- movement, not a decision.
    assert m["budgetCrossed"] == "none"


def test_measurement_added_and_removed(cfg: Config) -> None:
    """A metric on only one side is added/removed, never a spurious change."""
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("only_base", 5.0, budget=10.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("only_cand", 42.0)],
    )
    by_id = _by(compute_diff(cfg, cand)["measurements"], "metricId")

    added = by_id["only_cand"]
    assert added["change"] == "added"
    assert added["value"] == 42.0
    assert added["baselineValue"] is None
    assert added["delta"] is None
    assert added["baselinePassed"] is None
    assert added["budgetCrossed"] == "none"

    removed = by_id["only_base"]
    assert removed["change"] == "removed"
    assert removed["value"] is None
    assert removed["baselineValue"] == 5.0
    assert removed["passed"] is None
    assert removed["baselinePassed"] is True
    assert removed["budgetCrossed"] == "none"


def test_entered_breach_when_baseline_passed_and_candidate_fails(cfg: Config) -> None:
    """The whole point of the diff: a budget crossed is surfaced distinctly."""
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("lcp", 2000.0, budget=2500.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("lcp", 3000.0, budget=2500.0, passed=False)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["lcp"]
    assert m["change"] == "changed"
    assert m["budgetCrossed"] == "entered_breach"
    assert m["passed"] is False
    assert m["baselinePassed"] is True


def test_left_breach_when_baseline_failed_and_candidate_passes(cfg: Config) -> None:
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("tbt", 300.0, budget=200.0, passed=False)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("tbt", 150.0, budget=200.0, passed=True)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["tbt"]
    assert m["change"] == "changed"
    assert m["delta"] == -150.0
    assert m["budgetCrossed"] == "left_breach"


def test_budget_crossed_is_none_when_a_side_lacks_budget_or_verdict(cfg: Config) -> None:
    """No budget on one side means 'crossed' is unknowable -- report none, not a guess."""
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("lcp", 1.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("lcp", 2.0, budget=5.0, passed=False)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["lcp"]
    assert m["change"] == "changed"
    assert m["budgetCrossed"] == "none"


def test_budget_crossed_can_fire_even_when_value_is_unchanged(cfg: Config) -> None:
    """Same value but a tightened budget still crosses -- classification is by verdict."""
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("lcp", 2200.0, budget=2500.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("lcp", 2200.0, budget=2000.0, passed=False)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["lcp"]
    assert m["change"] == "unchanged"
    assert m["budgetCrossed"] == "entered_breach"


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_coverage_lost_is_flagged(cfg: Config) -> None:
    """`lost` is the single most important signal: evidence silently disappeared."""
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, coverage=[_c("US1-AC1", True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        coverage=[_c("US1-AC1", False)],
    )
    c = _by(compute_diff(cfg, cand)["coverage"], "requirementId")["US1-AC1"]
    assert c["change"] == "lost"
    assert c["present"] is False
    assert c["baselinePresent"] is True


def test_coverage_gained_is_flagged(cfg: Config) -> None:
    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, coverage=[_c("US1-AC1", False)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        coverage=[_c("US1-AC1", True, kinds=("screenshot", "har"))],
    )
    c = _by(compute_diff(cfg, cand)["coverage"], "requirementId")["US1-AC1"]
    assert c["change"] == "gained"
    assert c["present"] is True
    assert c["baselinePresent"] is False
    assert c["evidenceKinds"] == ["screenshot", "har"]


def test_coverage_unchanged_added_removed(cfg: Config) -> None:
    make_run(
        cfg, BASE_ID, head_sha=BASELINE_SHA,
        coverage=[_c("US1-AC1", True), _c("US1-AC9", True)],
    )
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        coverage=[_c("US1-AC1", True), _c("US1-AC5", True)],
    )
    by_id = _by(compute_diff(cfg, cand)["coverage"], "requirementId")
    assert by_id["US1-AC1"]["change"] == "unchanged"
    assert by_id["US1-AC5"]["change"] == "added"
    assert by_id["US1-AC5"]["baselinePresent"] is None
    assert by_id["US1-AC9"]["change"] == "removed"
    assert by_id["US1-AC9"]["present"] is None
    assert by_id["US1-AC9"]["baselinePresent"] is True


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


def test_screenshot_change_classes(cfg: Config) -> None:
    make_run(
        cfg, BASE_ID, head_sha=BASELINE_SHA,
        screenshots={"home.png": (10, 20, 30), "about.png": (0, 0, 0), "gone.png": (1, 1, 1)},
    )
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        screenshots={"home.png": (10, 20, 30), "about.png": (9, 9, 9), "new.png": (7, 7, 7)},
    )
    by_path = _by(compute_diff(cfg, cand)["screenshots"], "path")

    assert by_path["home.png"]["change"] == "unchanged"
    assert by_path["home.png"]["sha256"] == by_path["home.png"]["baselineSha256"]

    assert by_path["about.png"]["change"] == "changed"
    assert by_path["about.png"]["sha256"] != by_path["about.png"]["baselineSha256"]
    assert by_path["about.png"]["bytes"] > 0

    assert by_path["new.png"]["change"] == "added"
    assert by_path["new.png"]["baselineSha256"] is None
    assert by_path["new.png"]["baselineBytes"] is None

    assert by_path["gone.png"]["change"] == "removed"
    assert by_path["gone.png"]["sha256"] is None
    assert by_path["gone.png"]["bytes"] is None


def test_variant_rename_is_not_a_wholesale_replacement(cfg: Config) -> None:
    """A rename candidate-a -> candidate-b must not read as remove-all + add-all.

    The screenshot identity is the path *inside* the variant dir, so an identical
    image under a renamed variant is `unchanged`, and only the genuinely different
    image is `changed`.
    """
    make_run(
        cfg, BASE_ID, head_sha=BASELINE_SHA, variant="candidate-a",
        screenshots={"home.png": (10, 20, 30), "dash.png": (1, 2, 3)},
    )
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID, variant="candidate-b",
        screenshots={"home.png": (10, 20, 30), "dash.png": (4, 5, 6)},
    )
    shots = compute_diff(cfg, cand)["screenshots"]
    by_path = _by(shots, "path")

    # Keyed on the intra-variant path, so exactly two entries, not four.
    assert set(by_path) == {"home.png", "dash.png"}
    assert by_path["home.png"]["change"] == "unchanged"
    assert by_path["dash.png"]["change"] == "changed"
    assert all("candidate-" not in s["path"] for s in shots)


# ---------------------------------------------------------------------------
# Absence is stated, never silent
# ---------------------------------------------------------------------------


def test_no_references_run_states_the_reason(cfg: Config) -> None:
    rd = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=None)
    diff = compute_diff(cfg, rd)
    assert diff["baselineRunId"] is None
    assert "referencesRun" in diff["reason"]
    assert diff["measurements"] == diff["coverage"] == diff["screenshots"] == []
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_missing_baseline_directory_states_the_reason(cfg: Config) -> None:
    rd = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run="2026-01-01-dead")
    diff = compute_diff(cfg, rd)
    assert diff["baselineRunId"] is None
    assert "2026-01-01-dead" in diff["reason"]
    assert "no run directory" in diff["reason"]
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_baseline_without_a_review_pack_states_the_reason(cfg: Config) -> None:
    base = make_run(cfg, BASE_ID, head_sha=BASELINE_SHA)
    base.review_pack.unlink()
    rd = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID)
    diff = compute_diff(cfg, rd)
    assert diff["baselineRunId"] is None
    assert "evidence-review-pack.json" in diff["reason"]
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_unreadable_baseline_pack_states_the_reason(cfg: Config) -> None:
    """A baseline that crashed mid-way leaves a truncated pack; say so, don't crash."""
    base = make_run(cfg, BASE_ID, head_sha=BASELINE_SHA)
    base.review_pack.write_text("{ this is not json", encoding="utf-8")
    rd = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID)
    diff = compute_diff(cfg, rd)
    assert diff["baselineRunId"] is None
    assert "unreadable" in diff["reason"]
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_truncated_multibyte_baseline_pack_states_the_reason(cfg: Config) -> None:
    """A pack sliced through a multi-byte UTF-8 char raises UnicodeDecodeError, not
    JSONDecodeError (both are ValueErrors, but only the latter is a JSON error). It
    must still be reported as unreadable -- and the run's own diff artifact and stage
    must still be written, or the very diagnostic that says "the baseline is broken"
    is itself lost."""
    base = make_run(cfg, BASE_ID, head_sha=BASELINE_SHA)
    # ``\xc3`` opens a 2-byte sequence with no continuation byte -> undecodable UTF-8.
    base.review_pack.write_bytes(b'{"runId": "x", "coverage": [], "note": "caf\xc3')
    rd = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID)

    diff = compute_diff(cfg, rd)
    assert diff["baselineRunId"] is None
    assert "unreadable" in diff["reason"]
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors

    # The whole point of stating absence: run_evidence_diff still succeeds and leaves a
    # persisted, valid artifact + stage rather than crashing with nothing on disk.
    written = run_evidence_diff(cfg, rd)
    assert written["baselineRunId"] is None
    assert diff_path(rd).is_file()
    stage = rd.latest_stage("evidence_diff")
    assert stage is not None and stage["status"] == "ok"
    assert "unreadable" in stage["data"]["reason"]


def test_truncated_multibyte_candidate_pack_degrades_to_removals(cfg: Config) -> None:
    """A half-written candidate pack must not crash the diff. With no candidate
    evidence left to read, everything the baseline recorded reads faithfully as
    ``removed`` instead of throwing an unhandled UnicodeDecodeError."""
    make_run(
        cfg, BASE_ID, head_sha=BASELINE_SHA,
        measurements=[_m("lcp", 2000.0, budget=2500.0, passed=True)],
        coverage=[_c("US1-AC1", True)],
    )
    cand = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID)
    cand.review_pack.write_bytes(b'{"measurements": [{"metricId": "lcp", "value": "caf\xc3')

    diff = compute_diff(cfg, cand)
    assert diff["baselineRunId"] == BASE_ID
    assert _by(diff["measurements"], "metricId")["lcp"]["change"] == "removed"
    assert _by(diff["coverage"], "requirementId")["US1-AC1"]["change"] == "removed"
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_explicit_baseline_argument_is_used(cfg: Config) -> None:
    """`compute_diff` accepts a baseline directly, bypassing referencesRun."""
    base = make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, measurements=[_m("lcp", 1000.0, budget=2500.0, passed=True)])
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=None,
        measurements=[_m("lcp", 1200.0, budget=2500.0, passed=True)],
    )
    diff = compute_diff(cfg, cand, baseline=base)
    assert diff["baselineRunId"] == BASE_ID
    assert _by(diff["measurements"], "metricId")["lcp"]["delta"] == 200.0


# ---------------------------------------------------------------------------
# Whole-document properties
# ---------------------------------------------------------------------------


def _rich_pair(cfg: Config) -> Any:
    """A run pair exercising every collection at once."""
    make_run(
        cfg, BASE_ID, head_sha=BASELINE_SHA,
        measurements=[
            _m("lcp", 2000.0, budget=2500.0, passed=True),
            _m("tbt", 300.0, budget=200.0, passed=False),
            _m("cls", 0.1, budget=0.1, passed=True),
            _m("dropped", 9.0, budget=10.0, passed=True),
        ],
        coverage=[_c("US1-AC1", True), _c("US1-AC2", True), _c("US1-AC3", False), _c("US1-AC9", True)],
        screenshots={"home.png": (10, 20, 30), "about.png": (0, 0, 0), "gone.png": (1, 1, 1)},
    )
    return make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[
            _m("lcp", 3000.0, budget=2500.0, passed=False),  # entered_breach
            _m("tbt", 150.0, budget=200.0, passed=True),     # left_breach
            _m("cls", 0.1, budget=0.1, passed=True),         # unchanged
            _m("fresh", 42.0),                               # added
        ],
        coverage=[_c("US1-AC1", True), _c("US1-AC2", False), _c("US1-AC3", True), _c("US1-AC5", True)],
        screenshots={"home.png": (10, 20, 30), "about.png": (9, 9, 9), "new.png": (7, 7, 7)},
    )


def test_emitted_document_validates_against_the_schema(cfg: Config) -> None:
    cand = _rich_pair(cfg)
    diff = compute_diff(cfg, cand)
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_summary_counters_match_the_collections(cfg: Config) -> None:
    """A counter that disagrees with its table would mislead every reader."""
    cand = _rich_pair(cfg)
    diff = compute_diff(cfg, cand)
    s = diff["summary"]

    assert s["measurementsChanged"] == sum(1 for m in diff["measurements"] if m["change"] == "changed")
    assert s["budgetsEntered"] == sum(1 for m in diff["measurements"] if m["budgetCrossed"] == "entered_breach")
    assert s["budgetsLeft"] == sum(1 for m in diff["measurements"] if m["budgetCrossed"] == "left_breach")
    assert s["coverageLost"] == sum(1 for c in diff["coverage"] if c["change"] == "lost")
    assert s["coverageGained"] == sum(1 for c in diff["coverage"] if c["change"] == "gained")
    assert s["screenshotsChanged"] == sum(1 for x in diff["screenshots"] if x["change"] == "changed")
    assert s["screenshotsAdded"] == sum(1 for x in diff["screenshots"] if x["change"] == "added")
    assert s["screenshotsRemoved"] == sum(1 for x in diff["screenshots"] if x["change"] == "removed")

    # And the concrete tally for this fixture, so a miscount cannot pass.
    assert s == {
        "measurementsChanged": 2,
        "budgetsEntered": 1,
        "budgetsLeft": 1,
        "coverageLost": 1,
        "coverageGained": 1,
        "screenshotsChanged": 1,
        "screenshotsAdded": 1,
        "screenshotsRemoved": 1,
    }


def test_collections_are_sorted_by_key(cfg: Config) -> None:
    """Deterministic ordering is what makes two runs byte-identical."""
    cand = _rich_pair(cfg)
    diff = compute_diff(cfg, cand)
    assert [m["metricId"] for m in diff["measurements"]] == sorted(m["metricId"] for m in diff["measurements"])
    assert [c["requirementId"] for c in diff["coverage"]] == sorted(c["requirementId"] for c in diff["coverage"])
    assert [s["path"] for s in diff["screenshots"]] == sorted(s["path"] for s in diff["screenshots"])


def test_compute_diff_is_deterministic(cfg: Config) -> None:
    """Same inputs -> byte-identical JSON (compute_diff never stamps a clock)."""
    import json

    cand = _rich_pair(cfg)
    first = compute_diff(cfg, cand)
    second = compute_diff(cfg, cand)
    assert first == second
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)
    assert "generatedAt" not in first


# ---------------------------------------------------------------------------
# run_evidence_diff: persistence + stage record
# ---------------------------------------------------------------------------


def test_run_evidence_diff_writes_artifact_and_stage(cfg: Config) -> None:
    import json

    cand = _rich_pair(cfg)
    diff = run_evidence_diff(cfg, cand)

    out = diff_path(cand)
    assert out.name == ARTIFACT_NAME
    assert out.parent == cand.path  # alongside evidence-review-pack.json
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8")) == diff
    assert "generatedAt" in diff  # stamped at write time

    stage = cand.latest_stage("evidence_diff")
    assert stage is not None
    assert stage["stage"] == "evidence_diff"
    assert stage["status"] == "ok"
    assert ARTIFACT_NAME in stage["outputs"]
    assert stage["data"]["baselineRunId"] == BASE_ID
    assert stage["data"]["budgetsEntered"] == 1


def test_run_evidence_diff_records_a_stage_even_with_no_baseline(cfg: Config) -> None:
    """An empty-but-stated diff is a success, and it is still persisted."""
    rd = make_run(cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=None)
    diff = run_evidence_diff(cfg, rd)

    assert diff["baselineRunId"] is None
    assert diff_path(rd).is_file()
    stage = rd.latest_stage("evidence_diff")
    assert stage is not None
    assert stage["status"] == "ok"
    assert stage["data"].get("reason")


def test_run_evidence_diff_output_is_schema_valid(cfg: Config) -> None:
    cand = _rich_pair(cfg)
    diff = run_evidence_diff(cfg, cand)
    ok, errors = is_valid("evidence-diff", diff)
    assert ok, errors


def test_provenance_is_carried_from_the_review_pack(cfg: Config) -> None:
    """Collector + artifact hash flow through so the report can cite them."""
    make_run(
        cfg, BASE_ID, head_sha=BASELINE_SHA,
        measurements=[_m("lcp", 2000.0, budget=2500.0, passed=True, collector="lighthouse", sha="b" * 64)],
    )
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        measurements=[_m("lcp", 2500.0, budget=2500.0, passed=True, collector="lighthouse", sha="c" * 64)],
    )
    m = _by(compute_diff(cfg, cand)["measurements"], "metricId")["lcp"]
    assert m["collector"] == "lighthouse"
    assert m["artifactSha256"] == "c" * 64


def test_non_image_evidence_is_ignored(cfg: Config) -> None:
    """Only images are screenshot-diffed; traces/HAR/console are out of scope here."""
    base = make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, screenshots={"home.png": (1, 2, 3)})
    (base.evidence_dir / "candidate-a" / "trace.zip").write_bytes(b"PK\x03\x04not-a-real-zip")
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        screenshots={"home.png": (1, 2, 3)},
    )
    (cand.evidence_dir / "candidate-a" / "console.jsonl").write_text("{}", encoding="utf-8")
    shots = compute_diff(cfg, cand)["screenshots"]
    assert [s["path"] for s in shots] == ["home.png"]


def test_symlink_escaping_the_evidence_tree_is_not_hashed(cfg: Config) -> None:
    """Defence in depth: a symlink planted in the bundle that points *outside*
    evidence/ (say, at a secret) must be skipped, not silently followed and hashed
    into the diff. Skipped where the OS forbids unprivileged symlink creation."""
    import os

    import pytest

    make_run(cfg, BASE_ID, head_sha=BASELINE_SHA, screenshots={"home.png": (1, 2, 3)})
    cand = make_run(
        cfg, CAND_ID, head_sha=CANDIDATE_SHA, references_run=BASE_ID,
        screenshots={"home.png": (1, 2, 3)},
    )
    secret = cand.path / "secret.png"  # lives at the run root, outside evidence/
    secret.write_bytes(b"\x89PNG\r\n\x1a\nsecret-bytes")
    link = cand.evidence_dir / "candidate-a" / "leak.png"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without privilege
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    paths = [s["path"] for s in compute_diff(cfg, cand)["screenshots"]]
    assert "leak.png" not in paths  # escaping symlink excluded
    assert "home.png" in paths  # the genuine artifact still diffed
