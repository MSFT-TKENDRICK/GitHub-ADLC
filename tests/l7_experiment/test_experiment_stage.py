"""The experiment stage: pre-registration, exposure, analysis — and its limits.

The load-bearing assertions here are the boring ones: the stage appends immutable
attempts through ``RunDir.write_stage``, it never writes ``run.json`` (only
``adlc reduce`` may), and its output is exactly what the OES exporter needs with
no extra plumbing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from adlc.adapters.export.oes import OesExporter, is_comparative
from adlc.config import Config, select_adapter
from adlc.reduce import reduce_run
from adlc.runs import RunDir, write_json
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

VARIANTS: list[dict[str, Any]] = [
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
]


def _seed(rd: RunDir, variants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Write the run seed the way ``adlc run new`` does."""
    seed = {
        "schemaVersion": "adlc-run/v1",
        "runId": rd.run_id,
        "createdAt": "2026-08-19T09:00:00Z",
        "referencesRun": None,
        "repo": "octo-org/octo-app",
        "baseSha": "3f1a9c7e2b4d6081a5c3e7f902b4d6081a5c3e7f",
        "headSha": "c7e2b4d6081a5c3e7f902b4d6081a5c3e7f90213",
        "prNumber": 42,
        "status": "draft",
        "profile": "minimal",
        "capabilities": {},
        "stages": [],
        "variants": VARIANTS if variants is None else variants,
        "gates": [],
        "artifacts": [],
        "decision": None,
        "experimentRef": None,
    }
    write_json(rd.path / "seed.json", seed)
    return seed


class _FakeProvider:
    """A flag provider shaped like the spine's ``FlagdFileProvider``."""

    name = "fake-flags"
    kind = "flags"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.materialized: list[dict[str, Any]] = []

    @staticmethod
    def detect(_cfg: Config) -> tuple[bool, str]:
        return True, "fake provider"

    def materialize(self, run: dict[str, Any]) -> Path:
        self.materialized.append(run)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(run), encoding="utf-8")
        return self.path

    def evaluate(self, key: str, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": key,
            "value": "candidate-a",
            "variant": "candidate-a",
            "reason": "TARGETING_MATCH",
        }


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def emit(self, span: dict[str, Any]) -> None:
        self.spans.append(span)


@pytest.fixture
def seeded(rd: RunDir) -> RunDir:
    _seed(rd)
    (rd.enrichment_dir / "benchmarks.yaml").write_text(
        yaml.safe_dump(BENCHMARKS), encoding="utf-8"
    )
    return rd


def _stage_files(rd: RunDir) -> list[Path]:
    return sorted(rd.stages_dir.glob("experiment.*.json"))


# -- plan ---------------------------------------------------------------------


def test_plan_writes_the_pre_registration(cfg: Config, seeded: RunDir) -> None:
    result = experiment.plan(cfg, seeded)

    assert result["stage"] == "experiment"
    assert result["attempt"] == 1
    assert result["status"] == "ok"
    assert result["outputs"] == ["experiment/plan.json"]
    assert result["digest"].startswith("sha256:")
    assert result["data"]["phase"] == "plan"
    assert result["data"]["preRegistration"]["digest"].startswith("sha256:")

    plan_document = json.loads(
        (seeded.path / "experiment" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan_document["schemaVersion"] == "adlc-experiment-plan/v1"
    assert [v["key"] for v in plan_document["variants"]] == ["control", "candidate-a"]
    assert plan_document["experiment"]["status"] == "planned"
    assert plan_document["design"]["type"] == "quasi_experiment"
    assert plan_document["analysis"]["method"] == "custom"


def test_plan_picks_up_metrics_from_enrichment(cfg: Config, seeded: RunDir) -> None:
    (seeded.enrichment_dir / "rubric.yaml").write_text(
        yaml.safe_dump({"criteria": [{"id": "R-perf-01", "name": "Perceived performance"}]}),
        encoding="utf-8",
    )
    result = experiment.plan(cfg, seeded)
    metrics = {m["id"]: m for m in result["data"]["metrics"]}
    assert set(metrics) == {"lcp_ms", "bundle_kb", "R-perf-01"}
    assert metrics["lcp_ms"]["budget"] == 2500
    assert metrics["lcp_ms"]["source"] == "benchmarks.yaml"
    assert metrics["R-perf-01"]["role"] == "secondary"


def test_plan_reads_an_operator_authored_preregistration(cfg: Config, seeded: RunDir) -> None:
    (seeded.path / "experiment.yaml").write_text(
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
    result = experiment.plan(cfg, seeded)
    assert result["data"]["experiment"]["id"] == "exp-dark-mode"
    assert result["data"]["experiment"]["hypothesis"] == "Tokens beat duplicated CSS"
    # A genuine online A/B test is honoured when the operator declares one.
    assert result["data"]["design"]["type"] == "ab"
    assert result["data"]["design"]["randomizationUnit"] == "user"


def test_plan_skips_a_single_candidate_run(cfg: Config, rd: RunDir) -> None:
    """A run is not an experiment; one candidate is not a comparison."""
    _seed(rd, [VARIANTS[0]])
    result = experiment.plan(cfg, rd)
    assert result["status"] == "skipped"
    assert "not an experiment" in result["message"]
    assert not (rd.path / "experiment" / "plan.json").exists()


def test_plan_works_before_reduce_has_written_run_json(cfg: Config, seeded: RunDir) -> None:
    """The stage reads the seed when no reduced run.json exists yet."""
    assert not seeded.run_json.exists()
    assert experiment.plan(cfg, seeded)["status"] == "ok"


def test_plan_records_a_git_anchor(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    plan_document = json.loads(
        (seeded.path / "experiment" / "plan.json").read_text(encoding="utf-8")
    )
    assert "gitSha" in plan_document
    assert plan_document["gitSha"] is None or isinstance(plan_document["gitSha"], str)


# -- expose -------------------------------------------------------------------


def test_expose_skips_when_no_variant_declares_a_flag(cfg: Config, rd: RunDir) -> None:
    """OpenFeature wiring is opt-in: a candidate is a build artifact, not a flag."""
    _seed(rd, [{**v, "flagKeys": []} for v in VARIANTS])
    experiment.plan(cfg, rd)
    result = experiment.expose(cfg, rd)
    assert result["status"] == "skipped"
    assert "no variant declares a feature flag key" in result["message"]


def test_expose_records_exposure_via_the_provider(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    provider = _FakeProvider(seeded.path / "flags.fake.json")
    result = experiment.expose(cfg, seeded, provider=provider)

    assert result["status"] == "ok"
    exposure = result["data"]["exposure"]
    assert exposure["provider"] == "fake-flags"
    assert exposure["flagKeys"] == ["adlc.exp.a1b2"]
    assert exposure["variants"] == {"control": [], "candidate-a": ["adlc.exp.a1b2"]}
    assert exposure["manifest"] == "flags.fake.json"


def test_expose_hands_the_provider_the_adlc_run_variant_shape(
    cfg: Config, seeded: RunDir
) -> None:
    """The spine's flagd provider reads ``variants[].key`` / ``.role``, not OES fields."""
    experiment.plan(cfg, seeded)
    provider = _FakeProvider(seeded.path / "flags.fake.json")
    experiment.expose(cfg, seeded, provider=provider)

    handed = provider.materialized[0]
    assert handed["runId"] == seeded.run_id
    assert [v["key"] for v in handed["variants"]] == ["control", "candidate-a"]
    assert handed["variants"][0]["role"] == "control"
    assert handed["variants"][1]["flagKeys"] == ["adlc.exp.a1b2"]
    assert handed["variants"][1]["commit"].startswith("c7e2b4d")


def test_expose_works_with_the_spine_flagd_provider(cfg: Config, seeded: RunDir) -> None:
    """End-to-end against the real credential-free default, not a fake."""
    from adlc.adapters.flags.flagd_file import FlagdFileProvider

    experiment.plan(cfg, seeded)
    provider = FlagdFileProvider(seeded.path / "flags.flagd.json")
    result = experiment.expose(cfg, seeded, provider=provider)

    assert result["status"] == "ok"
    assert result["data"]["exposure"]["manifest"] == "flags.flagd.json"
    document = json.loads((seeded.path / "flags.flagd.json").read_text(encoding="utf-8"))
    flag = document["flags"][f"adlc.exp.{seeded.run_id}"]
    assert flag["defaultVariant"] == "control"
    assert set(flag["variants"]) == {"control", "candidate-a"}


def test_expose_emits_flat_semconv_flag_spans(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    telemetry = _RecordingTelemetry()
    result = experiment.expose(
        cfg,
        seeded,
        provider=_FakeProvider(seeded.path / "flags.fake.json"),
        telemetry=telemetry,
        evaluate=True,
        context={"targetingKey": "ci-runner-7"},
    )
    span = next(s for s in telemetry.spans if s["name"] == "feature_flag.evaluation")
    assert span == {
        "name": "feature_flag.evaluation",
        "feature_flag.key": "adlc.exp.a1b2",
        "feature_flag.provider.name": "fake-flags",
        "feature_flag.result.value": "candidate-a",
        "feature_flag.result.variant": "candidate-a",
        "feature_flag.result.reason": "targeting_match",
        "feature_flag.context.id": "ci-runner-7",
        "feature_flag.set.id": seeded.run_id,
    }
    assert result["data"]["exposure"]["evaluations"][0]["error"] is None


def test_expose_prefers_the_spine_telemetry_builder(cfg: Config, seeded: RunDir) -> None:
    """When the sink offers ``emit_flag_evaluation``, the spine builds the span."""
    from adlc.adapters.telemetry.otel_file import OtelFileTelemetry

    experiment.plan(cfg, seeded)
    telemetry = OtelFileTelemetry(seeded.path / "otel.jsonl")
    experiment.expose(
        cfg,
        seeded,
        provider=_FakeProvider(seeded.path / "flags.fake.json"),
        telemetry=telemetry,
        evaluate=True,
        context={"targetingKey": "ci-runner-7"},
    )
    lines = (seeded.path / "otel.jsonl").read_text(encoding="utf-8").strip().splitlines()
    span = json.loads(lines[-1])
    assert span["name"] == "feature_flag.evaluation"
    assert span["feature_flag.provider.name"] == "fake-flags"
    assert span["feature_flag.result.reason"] == "targeting_match"
    assert span["feature_flag.context.id"] == "ci-runner-7"


def test_expose_survives_a_missing_flag_adapter(
    cfg: Config, seeded: RunDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no flag adapter installed the phase still records intent, never crashes."""

    def _none_registered(*_args: object, **_kwargs: object) -> None:
        raise LookupError("no adapters registered for kind 'flags'")

    monkeypatch.setattr("adlc.config.select_adapter", _none_registered)
    experiment.plan(cfg, seeded)
    result = experiment.expose(cfg, seeded)
    assert result["status"] == "ok"
    assert result["data"]["exposure"]["provider"] == "none"
    assert "no flag provider available" in result["data"]["exposure"]["providerNote"]


def test_expose_flags_an_unavailable_auto_selected_provider(
    cfg: Config, seeded: RunDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter that reports itself unavailable must not look like it served flags."""

    class _Unavailable(_FakeProvider):
        name = "unavailable-flags"

        @staticmethod
        def detect(_cfg: Config) -> tuple[bool, str]:
            return False, "SOME_KEY is not set"

    monkeypatch.setattr(
        "adlc.config.select_adapter",
        lambda *_a, **_k: _Unavailable(seeded.path / "flags.fake.json"),
    )
    experiment.plan(cfg, seeded)
    result = experiment.expose(cfg, seeded)
    assert "the provider is unavailable: SOME_KEY is not set" in (
        result["data"]["exposure"]["providerNote"]
    )


def test_spine_default_is_selected_without_a_launchdarkly_key(cfg: Config) -> None:
    """With no credential the credential-free flagd file provider wins."""
    provider = select_adapter(cfg, "flags")
    assert type(provider).__name__ == "FlagdFileProvider"
    assert provider.name == "flagd-file"


# -- analyze ------------------------------------------------------------------


def test_analyze_computes_comparisons(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    result = experiment.analyze(cfg, seeded, MEASUREMENTS)

    assert result["status"] == "ok"
    assert result["data"]["baselineVariantKey"] == "control"
    results = {r["metricId"]: r for r in result["data"]["results"]["metricResults"]}
    assert results["lcp_ms"]["resultStatus"] == "positive"
    assert results["lcp_ms"]["decisionImpact"] == "supports_ship"
    assert results["lcp_ms"]["relativeDifference"] == -0.125
    assert results["bundle_kb"]["resultStatus"] == "negative"
    assert results["bundle_kb"]["adlc:budgetPassed"] is False
    assert result["data"]["preRegistration"]["unchanged"] is True


def test_analyze_reads_measurements_from_disk(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    write_json(seeded.path / "experiment" / "measurements.json", {"measurements": MEASUREMENTS})
    result = experiment.analyze(cfg, seeded)
    assert len(result["data"]["results"]["metricResults"]) == 2


def test_analyze_reuses_the_evidence_stage_measurements(cfg: Config, seeded: RunDir) -> None:
    """``evidence/<variant>/*-measurements.json`` is the spine's own output format."""
    experiment.plan(cfg, seeded)
    for variant, lcp in (("control", 2000), ("candidate-a", 1750)):
        directory = seeded.evidence_dir / variant
        directory.mkdir(parents=True, exist_ok=True)
        write_json(
            directory / "lighthouse-measurements.json",
            [
                {
                    "metricId": "lcp_ms",
                    "value": lcp,
                    "collector": "lighthouse",
                    "artifactSha256": "b1946ac92492d2347c6235b4d2611184" * 2,
                }
            ],
        )
    result = experiment.analyze(cfg, seeded)
    comparisons = result["data"]["results"]["metricResults"]
    assert [r["metricId"] for r in comparisons] == ["lcp_ms"]
    assert comparisons[0]["adlc:collector"] == "lighthouse"
    assert comparisons[0]["resultStatus"] == "positive"


def test_analyze_falls_back_to_a_simple_metrics_map(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    for variant, lcp in (("control", 2000), ("candidate-a", 1750)):
        directory = seeded.evidence_dir / variant
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "metrics.json", {"lcp_ms": lcp})
    result = experiment.analyze(cfg, seeded)
    assert [r["metricId"] for r in result["data"]["results"]["metricResults"]] == ["lcp_ms"]


def test_analyze_passes_sample_sizes_through_but_never_invents_them(
    cfg: Config, seeded: RunDir
) -> None:
    experiment.plan(cfg, seeded)
    plain = experiment.analyze(cfg, seeded, MEASUREMENTS)
    assert "sampleSizes" not in plain["data"]["results"]

    supplied = experiment.analyze(
        cfg,
        seeded,
        {"measurements": MEASUREMENTS, "sampleSizes": {"control": 1200, "candidate-a": 1198}},
    )
    assert supplied["data"]["results"]["sampleSizes"] == {"control": 1200, "candidate-a": 1198}


def test_analyze_detects_a_tampered_pre_registration(cfg: Config, seeded: RunDir) -> None:
    """Editing the plan after the fact is the failure mode pre-registration exists for."""
    experiment.plan(cfg, seeded)
    plan_path = seeded.path / "experiment" / "plan.json"
    tampered = json.loads(plan_path.read_text(encoding="utf-8"))
    tampered["metrics"] = [m for m in tampered["metrics"] if m["id"] != "bundle_kb"]
    write_json(plan_path, tampered)

    result = experiment.analyze(cfg, seeded, MEASUREMENTS)
    assert result["status"] == "fail"
    assert result["data"]["preRegistration"]["unchanged"] is False
    assert "changed after it was planned" in result["message"]


def test_analyze_without_a_plan_is_skipped(cfg: Config, seeded: RunDir) -> None:
    result = experiment.analyze(cfg, seeded, MEASUREMENTS)
    assert result["status"] == "skipped"
    assert "run the `plan` phase before `analyze`" in result["message"]


# -- immutability -------------------------------------------------------------


def test_attempts_are_append_only(cfg: Config, seeded: RunDir) -> None:
    experiment.plan(cfg, seeded)
    experiment.expose(cfg, seeded, provider=_FakeProvider(seeded.path / "flags.fake.json"))
    experiment.analyze(cfg, seeded, MEASUREMENTS)
    assert [p.name for p in _stage_files(seeded)] == [
        "experiment.1.json",
        "experiment.2.json",
        "experiment.3.json",
    ]

    before = {p.name: p.read_bytes() for p in _stage_files(seeded)}
    experiment.analyze(cfg, seeded, MEASUREMENTS)
    after = {p.name: p.read_bytes() for p in _stage_files(seeded)}

    assert "experiment.4.json" in after
    for name, payload in before.items():
        assert after[name] == payload, f"{name} was mutated by a re-run"


def test_the_stage_never_writes_run_json(cfg: Config, seeded: RunDir) -> None:
    """Only ``adlc reduce`` may write ``run.json``; that is what makes CI safe."""
    reduce_run(cfg, seeded)
    before = seeded.run_json.read_bytes()

    experiment.plan(cfg, seeded)
    experiment.expose(cfg, seeded, provider=_FakeProvider(seeded.path / "flags.fake.json"))
    experiment.analyze(cfg, seeded, MEASUREMENTS)

    assert seeded.run_json.read_bytes() == before


def test_writing_run_json_is_refused_outright(rd: RunDir) -> None:
    with pytest.raises(RuntimeError, match="only `adlc reduce`"):
        experiment._write_output(rd, "run.json", {})


def test_stage_results_match_the_frozen_schema(
    cfg: Config, seeded: RunDir, adlc_run_schema: dict[str, Any]
) -> None:
    schema = {"$defs": adlc_run_schema["$defs"], "$ref": "#/$defs/stageResult"}
    experiment.plan(cfg, seeded)
    experiment.expose(cfg, seeded, provider=_FakeProvider(seeded.path / "flags.fake.json"))
    experiment.analyze(cfg, seeded, MEASUREMENTS)
    for path in _stage_files(seeded):
        jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), schema)


# -- dispatcher and helpers ---------------------------------------------------


def test_run_experiment_dispatches_by_phase(cfg: Config, seeded: RunDir) -> None:
    assert experiment.run_experiment(cfg, seeded, "plan")["data"]["phase"] == "plan"
    assert experiment.run_experiment(cfg, seeded, "run")["data"]["phase"] == "run"
    analyzed = experiment.run_experiment(cfg, seeded, "analyze", measurements=MEASUREMENTS)
    assert analyzed["data"]["phase"] == "analyze"


def test_run_experiment_rejects_an_unknown_phase(cfg: Config, seeded: RunDir) -> None:
    with pytest.raises(ValueError, match="unknown experiment phase"):
        experiment.run_experiment(cfg, seeded, "publish")


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


def test_malformed_enrichment_degrades_to_no_metrics(rd: RunDir) -> None:
    (rd.enrichment_dir / "benchmarks.yaml").write_text("{[not: yaml", encoding="utf-8")
    assert experiment.metrics_from_enrichment(rd.path) == []


# -- end to end ---------------------------------------------------------------


def test_stage_output_exports_as_valid_oes(
    cfg: Config, seeded: RunDir, oes_schema: dict[str, Any]
) -> None:
    """The whole point: plan → expose → analyze → reduce → export, with no glue code."""
    experiment.plan(cfg, seeded)
    experiment.expose(cfg, seeded, provider=_FakeProvider(seeded.path / "flags.fake.json"))
    experiment.analyze(cfg, seeded, MEASUREMENTS)

    reduced = reduce_run(cfg, seeded)
    assert is_comparative(reduced)[0]

    out = OesExporter().export(reduced, seeded.path / "oes.json")
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


def test_reduce_promotes_the_planned_variants(cfg: Config, seeded: RunDir) -> None:
    """``reduce`` picks variants out of stage data, so the exporter sees them."""
    experiment.plan(cfg, seeded)
    experiment.analyze(cfg, seeded, MEASUREMENTS)
    reduced = reduce_run(cfg, seeded)
    assert [v["key"] for v in reduced["variants"]] == ["control", "candidate-a"]


def test_a_skipped_plan_still_refuses_export(cfg: Config, rd: RunDir) -> None:
    _seed(rd, [VARIANTS[0]])
    experiment.plan(cfg, rd)
    experiment.analyze(cfg, rd, MEASUREMENTS)

    reduced = reduce_run(cfg, rd)
    ok, reason = is_comparative(reduced)
    assert not ok
    assert "1 variant" in reason
