"""The experiment stage: pre-registration, exposure, analysis — and its limits.

The load-bearing assertions here are the boring ones: the stage appends immutable
attempts, it never writes ``run.json`` (only ``adlc reduce`` may), and its output
is exactly what the OES exporter needs with no extra plumbing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from adlc.adapters.export.oes import OesExporter, is_comparative
from adlc.config import Config
from adlc.stages import experiment

MEASUREMENTS = [
    {"metricId": "lcp_ms", "variantKey": "control", "value": 2000, "collector": "lighthouse"},
    {"metricId": "lcp_ms", "variantKey": "candidate-a", "value": 1750, "collector": "lighthouse"},
    {"metricId": "bundle_kb", "variantKey": "control", "value": 200, "collector": "build"},
    {"metricId": "bundle_kb", "variantKey": "candidate-a", "value": 250, "collector": "build"},
]

BENCHMARKS = {
    "metrics": [
        {
            "id": "lcp_ms",
            "name": "Largest Contentful Paint",
            "role": "primary",
            "direction": "decrease_is_good",
            "type": "duration",
            "unit": "ms",
            "budget": 2500,
        },
        {
            "id": "bundle_kb",
            "name": "Client bundle size",
            "role": "secondary",
            "direction": "decrease_is_good",
            "type": "count",
            "unit": "kB",
            "budget": 240,
        },
    ]
}

RUN_DOCUMENT: dict[str, Any] = {
    "schemaVersion": "adlc-run/v1",
    "runId": "2026-08-19-a1b2",
    "createdAt": "2026-08-19T09:00:00Z",
    "repo": "octo-org/octo-app",
    "baseSha": "3f1a9c7e2b4d6081a5c3e7f902b4d6081a5c3e7f",
    "headSha": "c7e2b4d6081a5c3e7f902b4d6081a5c3e7f90213",
    "prNumber": 42,
    "status": "evaluated",
    "profile": "full",
    "stages": [],
    "variants": [
        {
            "key": "control",
            "role": "control",
            "commit": "3f1a9c7e2b4d6081a5c3e7f902b4d6081a5c3e7f",
            "flagKeys": [],
        },
        {
            "key": "candidate-a",
            "role": "treatment",
            "commit": "c7e2b4d6081a5c3e7f902b4d6081a5c3e7f90213",
            "flagKeys": ["adlc.exp.a1b2"],
        },
    ],
    "gates": [
        {
            "id": "tests",
            "required": True,
            "status": "pass",
            "message": "all tests passed",
        }
    ],
}


class _FakeProvider:
    """A flag provider that behaves like the spine's file-based default."""

    name = "fake-flags"
    kind = "flags"

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.materialized: list[dict[str, Any]] = []

    def materialize(self, run: dict[str, Any]) -> Path:
        self.materialized.append(run)
        path = self.run_dir / "flags.fake.json"
        path.write_text(json.dumps(run), encoding="utf-8")
        return path

    def evaluate(self, key: str, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": key,
            "value": "candidate-a",
            "variant": "candidate-a",
            "reason": "STATIC",
        }


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def emit(self, span: dict[str, Any]) -> None:
        self.spans.append(span)


@pytest.fixture
def run_dir(cfg: Config) -> Path:
    directory = Path(cfg.run_dir(RUN_DOCUMENT["runId"]))
    (directory / "enrichment").mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps(RUN_DOCUMENT, indent=2), encoding="utf-8")
    (directory / "enrichment" / "benchmarks.yaml").write_text(
        yaml.safe_dump(BENCHMARKS), encoding="utf-8"
    )
    return directory


@pytest.fixture
def run_id() -> str:
    return str(RUN_DOCUMENT["runId"])


def _stage_files(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "stages").glob("experiment.*.json"))


# -- plan ---------------------------------------------------------------------


def test_plan_writes_the_pre_registration(cfg: Config, run_dir: Path, run_id: str) -> None:
    result = experiment.plan(cfg, run_id)

    assert result["stage"] == "experiment"
    assert result["attempt"] == 1
    assert result["status"] == "ok"
    assert result["outputs"] == ["experiment/plan.json"]
    assert result["digest"].startswith("sha256:")
    assert result["data"]["phase"] == "plan"

    plan_document = json.loads((run_dir / "experiment" / "plan.json").read_text(encoding="utf-8"))
    assert plan_document["schemaVersion"] == "adlc-experiment-plan/v1"
    assert [v["key"] for v in plan_document["variants"]] == ["control", "candidate-a"]
    assert plan_document["experiment"]["status"] == "planned"
    assert plan_document["design"]["type"] == "quasi_experiment"
    assert plan_document["analysis"]["method"] == "custom"


def test_plan_picks_up_metrics_from_enrichment(cfg: Config, run_dir: Path, run_id: str) -> None:
    (run_dir / "enrichment" / "rubric.yaml").write_text(
        yaml.safe_dump({"criteria": [{"id": "R-perf-01", "name": "Perceived performance"}]}),
        encoding="utf-8",
    )
    result = experiment.plan(cfg, run_id)
    metrics = {m["id"]: m for m in result["data"]["metrics"]}
    assert set(metrics) == {"lcp_ms", "bundle_kb", "R-perf-01"}
    assert metrics["lcp_ms"]["budget"] == 2500
    assert metrics["lcp_ms"]["source"] == "benchmarks.yaml"
    assert metrics["R-perf-01"]["role"] == "secondary"
    assert metrics["R-perf-01"]["source"] == "rubric.yaml"


def test_plan_reads_an_operator_authored_preregistration(
    cfg: Config, run_dir: Path, run_id: str
) -> None:
    (run_dir / "experiment.yaml").write_text(
        yaml.safe_dump(
            {
                "experiment": {
                    "id": "exp-dark-mode",
                    "title": "Dark mode",
                    "hypothesis": "Tokens beat duplicated CSS",
                },
                "design": {"type": "ab", "randomizationUnit": "user"},
            }
        ),
        encoding="utf-8",
    )
    result = experiment.plan(cfg, run_id)
    assert result["data"]["experiment"]["id"] == "exp-dark-mode"
    assert result["data"]["experiment"]["hypothesis"] == "Tokens beat duplicated CSS"
    # A genuine online A/B test is honoured when the operator declares one.
    assert result["data"]["design"]["type"] == "ab"
    assert result["data"]["design"]["randomizationUnit"] == "user"


def test_plan_skips_a_single_candidate_run(cfg: Config, run_dir: Path, run_id: str) -> None:
    """A run is not an experiment; one candidate is not a comparison."""
    document = dict(RUN_DOCUMENT, variants=[RUN_DOCUMENT["variants"][0]])
    (run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")

    result = experiment.plan(cfg, run_id)
    assert result["status"] == "skipped"
    assert "not an experiment" in result["message"]
    assert not (run_dir / "experiment" / "plan.json").exists()


def test_plan_records_a_git_anchor(cfg: Config, run_dir: Path, run_id: str) -> None:
    experiment.plan(cfg, run_id)
    plan_document = json.loads((run_dir / "experiment" / "plan.json").read_text(encoding="utf-8"))
    assert "gitSha" in plan_document
    assert plan_document["gitSha"] is None or isinstance(plan_document["gitSha"], str)


# -- run ----------------------------------------------------------------------


def test_run_skips_when_no_variant_declares_a_flag(
    cfg: Config, run_dir: Path, run_id: str
) -> None:
    """OpenFeature wiring is opt-in: a candidate is a build artifact, not a flag."""
    document = json.loads(json.dumps(RUN_DOCUMENT))
    for variant in document["variants"]:
        variant["flagKeys"] = []
    (run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")

    experiment.plan(cfg, run_id)
    result = experiment.run(cfg, run_id)
    assert result["status"] == "skipped"
    assert "no variant declares a feature flag key" in result["message"]


def test_run_records_exposure_via_the_provider(
    cfg: Config, run_dir: Path, run_id: str
) -> None:
    experiment.plan(cfg, run_id)
    provider = _FakeProvider(run_dir)
    result = experiment.run(cfg, run_id, provider=provider)

    assert result["status"] == "ok"
    exposure = result["data"]["exposure"]
    assert exposure["provider"] == "fake-flags"
    assert exposure["flagKeys"] == ["adlc.exp.a1b2"]
    assert exposure["variants"] == {"control": [], "candidate-a": ["adlc.exp.a1b2"]}
    assert exposure["manifest"] == "flags.fake.json"
    assert provider.materialized[0]["variants"][1]["flagKeys"] == ["adlc.exp.a1b2"]


def test_run_emits_semconv_flag_spans_when_evaluating(
    cfg: Config, run_dir: Path, run_id: str
) -> None:
    experiment.plan(cfg, run_id)
    telemetry = _RecordingTelemetry()
    result = experiment.run(
        cfg,
        run_id,
        provider=_FakeProvider(run_dir),
        telemetry=telemetry,
        evaluate=True,
        context={"targetingKey": "ci-runner-7"},
    )
    span = next(s for s in telemetry.spans if s["name"] == "feature_flag.evaluation")
    assert span["attributes"] == {
        "feature_flag.key": "adlc.exp.a1b2",
        "feature_flag.provider.name": "fake-flags",
        "feature_flag.result.value": "candidate-a",
        "feature_flag.result.variant": "candidate-a",
        "feature_flag.result.reason": "STATIC",
        "feature_flag.context.id": "ci-runner-7",
        "feature_flag.set.id": "2026-08-19-a1b2",
    }
    assert result["data"]["exposure"]["evaluations"][0]["error"] is None


def test_run_survives_a_missing_flag_adapter(
    cfg: Config, run_dir: Path, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no flag adapter installed the phase still records intent, never crashes."""

    def _none_registered(*_args: object, **_kwargs: object) -> None:
        raise LookupError("no adapters registered for kind 'flags'")

    monkeypatch.setattr("adlc.config.select_adapter", _none_registered)
    experiment.plan(cfg, run_id)
    result = experiment.run(cfg, run_id)
    assert result["status"] == "ok"
    assert result["data"]["exposure"]["provider"] == "none"
    assert "no flag provider available" in result["data"]["exposure"]["providerNote"]
    assert result["data"]["exposure"]["flagKeys"] == ["adlc.exp.a1b2"]


def test_run_flags_an_unavailable_auto_selected_provider(
    cfg: Config, run_dir: Path, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter that reports itself unavailable must not look like it served flags."""

    class _Unavailable(_FakeProvider):
        name = "unavailable-flags"

        @staticmethod
        def detect(_cfg: Config) -> tuple[bool, str]:
            return False, "SOME_KEY is not set"

    monkeypatch.setattr("adlc.config.select_adapter", lambda *_a, **_k: _Unavailable(run_dir))
    experiment.plan(cfg, run_id)
    result = experiment.run(cfg, run_id)
    assert "the provider is unavailable: SOME_KEY is not set" in (
        result["data"]["exposure"]["providerNote"]
    )


# -- analyze ------------------------------------------------------------------


def test_analyze_computes_comparisons(cfg: Config, run_dir: Path, run_id: str) -> None:
    experiment.plan(cfg, run_id)
    result = experiment.analyze(cfg, run_id, MEASUREMENTS)

    assert result["status"] == "ok"
    assert result["data"]["baselineVariantKey"] == "control"
    results = {r["metricId"]: r for r in result["data"]["results"]["metricResults"]}
    assert results["lcp_ms"]["resultStatus"] == "positive"
    assert results["lcp_ms"]["decisionImpact"] == "supports_ship"
    assert results["lcp_ms"]["relativeDifference"] == -0.125
    assert results["bundle_kb"]["resultStatus"] == "negative"
    assert results["bundle_kb"]["adlc:budgetPassed"] is False
    assert result["data"]["preRegistration"]["unchanged"] is True


def test_analyze_reads_measurements_from_disk(cfg: Config, run_dir: Path, run_id: str) -> None:
    experiment.plan(cfg, run_id)
    (run_dir / "experiment" / "measurements.json").write_text(
        json.dumps({"measurements": MEASUREMENTS}), encoding="utf-8"
    )
    result = experiment.analyze(cfg, run_id)
    assert len(result["data"]["results"]["metricResults"]) == 2


def test_analyze_falls_back_to_evidence_metrics(cfg: Config, run_dir: Path, run_id: str) -> None:
    experiment.plan(cfg, run_id)
    for variant, lcp in (("control", 2000), ("candidate-a", 1750)):
        directory = run_dir / "evidence" / variant
        directory.mkdir(parents=True)
        (directory / "metrics.json").write_text(json.dumps({"lcp_ms": lcp}), encoding="utf-8")
    result = experiment.analyze(cfg, run_id)
    assert [r["metricId"] for r in result["data"]["results"]["metricResults"]] == ["lcp_ms"]


def test_analyze_passes_sample_sizes_through_but_never_invents_them(
    cfg: Config, run_dir: Path, run_id: str
) -> None:
    experiment.plan(cfg, run_id)
    plain = experiment.analyze(cfg, run_id, MEASUREMENTS)
    assert "sampleSizes" not in plain["data"]["results"]

    supplied = experiment.analyze(
        cfg,
        run_id,
        {"measurements": MEASUREMENTS, "sampleSizes": {"control": 1200, "candidate-a": 1198}},
    )
    assert supplied["data"]["results"]["sampleSizes"] == {"control": 1200, "candidate-a": 1198}


def test_analyze_detects_a_tampered_pre_registration(
    cfg: Config, run_dir: Path, run_id: str
) -> None:
    """Editing the plan after the fact is the failure mode pre-registration exists for."""
    experiment.plan(cfg, run_id)
    plan_path = run_dir / "experiment" / "plan.json"
    tampered = json.loads(plan_path.read_text(encoding="utf-8"))
    tampered["metrics"] = [m for m in tampered["metrics"] if m["id"] != "bundle_kb"]
    plan_path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")

    result = experiment.analyze(cfg, run_id, MEASUREMENTS)
    assert result["status"] == "fail"
    assert result["data"]["preRegistration"]["unchanged"] is False
    assert "changed after it was planned" in result["message"]


def test_analyze_without_a_plan_is_skipped(cfg: Config, run_dir: Path, run_id: str) -> None:
    result = experiment.analyze(cfg, run_id, MEASUREMENTS)
    assert result["status"] == "skipped"
    assert "run the `plan` phase before `analyze`" in result["message"]


# -- immutability -------------------------------------------------------------


def test_attempts_are_append_only(cfg: Config, run_dir: Path, run_id: str) -> None:
    experiment.plan(cfg, run_id)
    experiment.run(cfg, run_id, provider=_FakeProvider(run_dir))
    experiment.analyze(cfg, run_id, MEASUREMENTS)
    assert [p.name for p in _stage_files(run_dir)] == [
        "experiment.1.json",
        "experiment.2.json",
        "experiment.3.json",
    ]

    before = {p.name: p.read_bytes() for p in _stage_files(run_dir)}
    experiment.analyze(cfg, run_id, MEASUREMENTS)
    after = {p.name: p.read_bytes() for p in _stage_files(run_dir)}

    assert "experiment.4.json" in after
    for name, payload in before.items():
        assert after[name] == payload, f"{name} was mutated by a re-run"


def test_the_stage_never_writes_run_json(cfg: Config, run_dir: Path, run_id: str) -> None:
    """Only ``adlc reduce`` may write ``run.json``; that is what makes CI safe."""
    before = (run_dir / "run.json").read_bytes()
    experiment.plan(cfg, run_id)
    experiment.run(cfg, run_id, provider=_FakeProvider(run_dir))
    experiment.analyze(cfg, run_id, MEASUREMENTS)
    assert (run_dir / "run.json").read_bytes() == before


def test_writing_run_json_is_refused_outright(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="only `adlc reduce`"):
        experiment._write_json(tmp_path / "run.json", {})


def test_stage_results_match_the_frozen_schema(
    cfg: Config, run_dir: Path, run_id: str, adlc_run_schema: dict[str, Any]
) -> None:
    schema = {"$defs": adlc_run_schema["$defs"], "$ref": "#/$defs/stageResult"}
    experiment.plan(cfg, run_id)
    experiment.run(cfg, run_id, provider=_FakeProvider(run_dir))
    experiment.analyze(cfg, run_id, MEASUREMENTS)
    for path in _stage_files(run_dir):
        jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), schema)


# -- dispatcher and helpers ---------------------------------------------------


def test_execute_dispatches_by_phase(cfg: Config, run_dir: Path, run_id: str) -> None:
    assert experiment.execute(cfg, run_id, "plan")["data"]["phase"] == "plan"
    assert experiment.execute(cfg, run_id, "run")["data"]["phase"] == "run"
    assert (
        experiment.execute(cfg, run_id, "analyze", measurements=MEASUREMENTS)["data"]["phase"]
        == "analyze"
    )


def test_execute_rejects_an_unknown_phase(cfg: Config, run_dir: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="unknown experiment phase"):
        experiment.execute(cfg, run_id, "publish")


def test_baseline_prefers_control_then_baseline() -> None:
    assert experiment.baseline_variant_key([{"key": "b", "role": "treatment"}]) == "b"
    assert (
        experiment.baseline_variant_key(
            [{"key": "b", "role": "treatment"}, {"key": "a", "role": "control"}]
        )
        == "a"
    )
    assert (
        experiment.baseline_variant_key(
            [{"key": "b", "role": "treatment"}, {"key": "c", "role": "baseline"}]
        )
        == "c"
    )


def test_undeclared_direction_is_inconclusive_not_guessed() -> None:
    results = experiment.compare_measurements(
        [{"id": "m1", "name": "M1"}],
        [
            {"metricId": "m1", "variantKey": "control", "value": 10},
            {"metricId": "m1", "variantKey": "b", "value": 12},
        ],
        "control",
    )
    assert results[0]["resultStatus"] == "inconclusive"


def test_malformed_enrichment_degrades_to_no_metrics(run_dir: Path) -> None:
    (run_dir / "enrichment" / "benchmarks.yaml").write_text("{[not: yaml", encoding="utf-8")
    assert experiment.metrics_from_enrichment(run_dir) == []


# -- end to end ---------------------------------------------------------------


def test_stage_output_exports_as_valid_oes(
    cfg: Config, run_dir: Path, run_id: str, oes_schema: dict[str, Any], tmp_path: Path
) -> None:
    """The whole point: plan → run → analyze → reduce → export, with no glue code."""
    experiment.plan(cfg, run_id)
    experiment.run(cfg, run_id, provider=_FakeProvider(run_dir))
    experiment.analyze(cfg, run_id, MEASUREMENTS)

    # Stand in for `adlc reduce`, the only writer of run.json.
    reduced = dict(RUN_DOCUMENT)
    reduced["stages"] = [
        json.loads(path.read_text(encoding="utf-8")) for path in _stage_files(run_dir)
    ]
    reduced["status"] = "gated"

    assert is_comparative(reduced)[0]
    out = OesExporter(run_dir=run_dir).export(reduced, tmp_path / "oes.json")
    document = json.loads(out.read_text(encoding="utf-8"))
    jsonschema.validate(document, oes_schema)

    assert document["experiment"]["status"] == "analyzed"
    assert {v["key"] for v in document["variants"]} == {"control", "candidate-a"}
    assert {m["id"] for m in document["metrics"]} == {"lcp_ms", "bundle_kb"}
    assert len(document["results"]["metricResults"]) == 2
    assert document["extensions"]["adlc:exposure"]["flagKeys"] == ["adlc.exp.a1b2"]
    pre_registration = next(
        c for c in document["qualityChecks"] if c["checkType"] == "adlc:pre_registration"
    )
    assert pre_registration["status"] == "pass"


def test_a_skipped_plan_still_refuses_export(cfg: Config, run_dir: Path, run_id: str) -> None:
    document = dict(RUN_DOCUMENT, variants=[RUN_DOCUMENT["variants"][0]])
    (run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")
    experiment.plan(cfg, run_id)
    experiment.analyze(cfg, run_id, MEASUREMENTS)

    reduced = dict(document)
    reduced["stages"] = [
        json.loads(path.read_text(encoding="utf-8")) for path in _stage_files(run_dir)
    ]
    ok, reason = is_comparative(reduced)
    assert not ok
    assert "1 variant" in reason
