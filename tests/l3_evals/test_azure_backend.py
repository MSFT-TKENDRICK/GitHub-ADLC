"""``adapters.evals.azure`` -- the Azure AI Foundry eval backend (L3).

Previously exercised only via ``registered_adapters``/entry-point smoke checks
(35% coverage). Adds direct coverage of: ``detect()`` (missing SDK, missing
credentials, missing deployment, and the fully-available path), ``run()``
(unavailable backend, empty rubric, all-unjudged rows, and a full mocked-SDK
success path exercising both a built-in evaluator and the generic rubric
evaluator), ``_model_config``, ``_rubric_evaluator``'s fallback-then-raise
behaviour, and ``map_azure_rows``'s normalisation of Likert vs 0-1 scales,
error rows, and rows with no readable score.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from adlc.adapters.evals.assert_ import (
    CriterionSpec,
    EvalBackendError,
    EvalBackendUnavailable,
)
from adlc.adapters.evals.azure import (
    AzureEvalRunner,
    _model_config,
    _rubric_evaluator,
    map_azure_rows,
)
from adlc.config import Config


@pytest.fixture
def fake_sdk_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Register an importable ``azure.ai.evaluation`` stub.

    ``find_spec`` walks real package machinery, so simply stuffing fake
    modules into ``sys.modules`` is not enough to fool it -- patch
    ``has_module`` (used by ``detect()``) directly instead, and register the
    stub in ``sys.modules`` so the real ``import azure.ai.evaluation`` inside
    ``_evaluate`` resolves to it too.
    """
    evaluation_module = types.ModuleType("azure.ai.evaluation")
    azure_pkg = types.ModuleType("azure")
    azure_pkg.__path__ = []  # mark as a package so submodule import machinery works
    azure_ai_pkg = types.ModuleType("azure.ai")
    azure_ai_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.ai", azure_ai_pkg)
    monkeypatch.setitem(sys.modules, "azure.ai.evaluation", evaluation_module)
    monkeypatch.setattr(
        "adlc.adapters.evals.azure.has_module",
        lambda name: name == "azure.ai.evaluation",
    )
    return evaluation_module


class TestDetect:
    def test_false_when_sdk_not_installed(self, no_tools: None, cfg: Config) -> None:
        available, reason = AzureEvalRunner.detect(cfg)
        assert available is False
        assert "not installed" in reason

    def test_false_when_sdk_installed_but_no_credentials(
        self, credential_free: None, cfg: Config, fake_sdk_module: types.ModuleType
    ) -> None:
        available, reason = AzureEvalRunner.detect(cfg)
        assert available is False
        assert "no Azure OpenAI credentials" in reason

    def test_false_when_credentialed_but_no_deployment_configured(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
        available, reason = AzureEvalRunner.detect(cfg)
        assert available is False
        assert "no judge deployment" in reason

    def test_true_when_credentialed_and_deployment_configured(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        available, reason = AzureEvalRunner.detect(cfg)
        assert available is True
        assert "gpt-4o-mini" in reason

    def test_accepts_aad_credential_group_without_api_key(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        available, _reason = AzureEvalRunner.detect(cfg)
        assert available is True


class TestModelConfig:
    def test_uses_default_deployment_when_none_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
        config = _model_config({})
        assert config["azure_deployment"] == "gpt-4o-mini"

    def test_prefers_settings_deployment_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "env-deployment")
        config = _model_config({"deployment": "settings-deployment"})
        assert config["azure_deployment"] == "settings-deployment"

    def test_includes_api_key_only_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        assert "api_key" not in _model_config({})
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
        assert _model_config({})["api_key"] == "secret"

    def test_includes_api_version_from_settings_or_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
        assert "api_version" not in _model_config({})
        config = _model_config({"apiVersion": "2024-06-01"})
        assert config["api_version"] == "2024-06-01"


class TestRubricEvaluatorFallback:
    def test_uses_first_available_generic_evaluator_factory(self) -> None:
        sdk = types.SimpleNamespace(GeneralPromptyEvaluator=lambda model_config: "built")
        spec = CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric")
        assert _rubric_evaluator(sdk, spec, {}) == "built"

    def test_raises_a_clear_error_when_sdk_exposes_no_generic_evaluator(self) -> None:
        sdk = types.SimpleNamespace()
        spec = CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric")
        with pytest.raises(EvalBackendError, match="exposes no generic rubric evaluator"):
            _rubric_evaluator(sdk, spec, {})


class TestMapAzureRows:
    def test_builtin_evaluator_score_scaled_from_likert(self) -> None:
        specs = [CriterionSpec(id="groundedness", statement="x", weight=1, kind="llm-rubric")]
        rows = {"groundedness": {"row": {"gpt_groundedness": 4}, "scale": 5.0}}
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["groundedness"].score == pytest.approx(0.75)

    def test_rubric_evaluator_score_at_native_0_to_1_scale(self) -> None:
        specs = [CriterionSpec(id="R-perf-01", statement="x", weight=1, kind="measurement")]
        rows = {"R-perf-01": {"row": {"score": 0.9}, "scale": 1.0}}
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["R-perf-01"].score == pytest.approx(0.9)

    def test_error_row_becomes_unevaluated_outcome_with_rationale(self) -> None:
        specs = [CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric")]
        rows = {"R-1": {"error": "TimeoutError: judge timed out"}}
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["R-1"].score is None
        assert "TimeoutError" in outcomes["R-1"].rationale

    def test_row_with_only_a_reason_and_no_numeric_value_stays_unevaluated(self) -> None:
        specs = [CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric")]
        rows = {"R-1": {"row": {"gpt_groundedness_reason": "looks fine"}, "scale": 5.0}}
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["R-1"].score is None
        assert outcomes["R-1"].rationale == "looks fine"

    def test_row_with_no_keys_at_all_reports_no_readable_score(self) -> None:
        specs = [CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric")]
        rows = {"R-1": {"row": {}, "scale": 5.0}}
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["R-1"].score is None
        assert "no readable score" in outcomes["R-1"].rationale
        assert "no readable score" in outcomes["R-1"].rationale

    def test_criterion_with_no_row_at_all_is_skipped(self) -> None:
        specs = [
            CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric"),
            CriterionSpec(id="R-2", statement="y", weight=1, kind="llm-rubric"),
        ]
        outcomes = map_azure_rows({"R-1": {"row": {"score": 0.5}, "scale": 1.0}}, specs)
        assert "R-2" not in outcomes

    def test_reason_field_is_captured_as_rationale(self) -> None:
        specs = [CriterionSpec(id="groundedness", statement="x", weight=1, kind="llm-rubric")]
        rows = {
            "groundedness": {
                "row": {"gpt_groundedness": 5, "gpt_groundedness_reason": "fully grounded"},
                "scale": 5.0,
            }
        }
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["groundedness"].rationale == "fully grounded"

    def test_falls_back_to_first_numeric_value_when_no_score_key_present(self) -> None:
        specs = [CriterionSpec(id="R-1", statement="x", weight=1, kind="llm-rubric")]
        rows = {"R-1": {"row": {"weird_metric_name": 0.75}, "scale": 1.0}}
        outcomes = map_azure_rows(rows, specs)
        assert outcomes["R-1"].score == pytest.approx(0.75)


class TestRunUnavailableAndEmptyRubric:
    def test_run_raises_when_backend_unavailable(
        self, no_tools: None, cfg: Config, rubric: dict[str, Any], run_doc: dict[str, Any]
    ) -> None:
        runner = AzureEvalRunner(cfg=cfg)
        with pytest.raises(EvalBackendUnavailable):
            runner.run(run_doc, rubric)

    def test_run_raises_when_rubric_has_no_criteria(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        run_doc: dict[str, Any],
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        runner = AzureEvalRunner(cfg=cfg)
        empty_rubric = {"id": "empty", "threshold": 0.7, "criteria": []}
        with pytest.raises(EvalBackendError, match="declares no criteria"):
            runner.run(run_doc, empty_rubric)


class TestRunFullMockedSuccess:
    def _bind_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

    def test_run_scores_a_builtin_and_a_generic_criterion(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        rubric: dict[str, Any],
        run_doc: dict[str, Any],
        run_dir: Path,
    ) -> None:
        self._bind_credentials(monkeypatch)

        class _GroundednessEvaluator:
            def __init__(self, model_config: dict[str, Any]) -> None:
                self.model_config = model_config

            def __call__(self, **kwargs: Any) -> dict[str, Any]:
                return {"gpt_groundedness": 5, "gpt_groundedness_reason": "solid"}

        class _GenericEvaluator:
            def __init__(self, model_config: dict[str, Any]) -> None:
                self.model_config = model_config

            def __call__(self, **kwargs: Any) -> dict[str, Any]:
                return {"score": 0.8, "reason": "meets the bar"}

        # Rename one rubric criterion to a built-in id so both code paths run.
        local_rubric = {**rubric, "criteria": list(rubric["criteria"])}
        local_rubric["criteria"][0] = {**local_rubric["criteria"][0], "id": "groundedness"}

        fake_sdk_module.GroundednessEvaluator = _GroundednessEvaluator
        fake_sdk_module.GeneralPromptyEvaluator = _GenericEvaluator

        runner = AzureEvalRunner(cfg=cfg, run_dir=run_dir)
        score = runner.run(run_doc, local_rubric)

        assert score["passed"] in (True, False)
        by_id = {c["id"]: c for c in score["criteria"]}
        assert by_id["groundedness"]["score"] == pytest.approx(1.0)
        assert (run_dir / "evals" / "azure-score.json").is_file()

    def test_run_raises_when_every_criterion_errors(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        rubric: dict[str, Any],
        run_doc: dict[str, Any],
        run_dir: Path,
    ) -> None:
        self._bind_credentials(monkeypatch)

        class _BoomEvaluator:
            def __init__(self, model_config: dict[str, Any]) -> None:
                pass

            def __call__(self, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("judge unreachable")

        fake_sdk_module.GeneralPromptyEvaluator = _BoomEvaluator

        runner = AzureEvalRunner(cfg=cfg, run_dir=run_dir)
        with pytest.raises(EvalBackendError, match="returned no usable score"):
            runner.run(run_doc, rubric)

    def test_run_raises_when_sdk_import_fails_inside_evaluate(
        self,
        credential_free: None,
        cfg: Config,
        fake_sdk_module: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        rubric: dict[str, Any],
        run_doc: dict[str, Any],
        run_dir: Path,
    ) -> None:
        """detect() passed (module importable at detect time via has_module's
        find_spec check), but the real import inside `_evaluate` can still
        fail for unrelated reasons (e.g. a broken transitive dependency)."""
        self._bind_credentials(monkeypatch)
        monkeypatch.delitem(sys.modules, "azure.ai.evaluation", raising=False)
        monkeypatch.delitem(sys.modules, "azure.ai", raising=False)
        monkeypatch.delitem(sys.modules, "azure", raising=False)

        runner = AzureEvalRunner(cfg=cfg, run_dir=run_dir)
        with pytest.raises(EvalBackendUnavailable):
            runner.run(run_doc, rubric)
