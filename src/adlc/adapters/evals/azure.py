"""Azure AI Foundry eval backend — optional, thin (L3).

Uses the ``azure-ai-evaluation`` SDK: the built-in quality evaluators
(groundedness, relevance, coherence, fluency, similarity) plus one custom LLM-judge
evaluator per rubric criterion. Deliberately thin — it is the *documented + detected*
rung of the ladder, not the default path (see ``docs/evals.md``).

The SDK is imported lazily inside :meth:`AzureEvalRunner.run`. ``detect()`` only probes
for the module spec and for Azure credentials, so importing this adapter costs nothing and
never raises on a machine with no Azure at all.

Like every L3 backend, the result is normalised onto the frozen
:class:`~adlc.ports.RubricScore` by ``assert_.py``'s shared core.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from adlc.adapters.evals.assert_ import (
    CriterionOutcome,
    CriterionSpec,
    EvalBackendError,
    EvalBackendUnavailable,
    backend_settings,
    build_rubric_score,
    coerce_score,
    has_module,
    iter_criteria,
    resolve_threshold,
    run_dir_for,
    spec_context,
    write_score,
)
from adlc.config import Config
from adlc.ports import Rubric, RubricScore, Run

__all__ = ["BUILTIN_EVALUATORS", "AzureEvalRunner", "map_azure_rows"]

#: The ``azure-ai-evaluation`` module we require.
SDK_MODULE = "azure.ai.evaluation"

#: Azure OpenAI credential groups the SDK's model config needs. Any one group suffices;
#: ``AZURE_OPENAI_API_KEY`` absent + a managed identity present is the AAD path.
AZURE_CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"),
    ("AZURE_OPENAI_ENDPOINT", "AZURE_CLIENT_ID"),
    ("AZURE_OPENAI_ENDPOINT", "AZURE_TENANT_ID"),
)

#: Built-in quality evaluators, keyed by the criterion ``id`` that opts into them. A
#: rubric criterion whose id matches one of these is scored by the SDK evaluator rather
#: than by a bespoke rubric prompt.
BUILTIN_EVALUATORS: dict[str, str] = {
    "groundedness": "GroundednessEvaluator",
    "relevance": "RelevanceEvaluator",
    "coherence": "CoherenceEvaluator",
    "fluency": "FluencyEvaluator",
    "similarity": "SimilarityEvaluator",
}

#: Built-in evaluators return a 1–5 Likert score; rubric evaluators return 0–1.
BUILTIN_SCALE = 5.0

DEFAULT_DEPLOYMENT = "gpt-4o-mini"


class AzureEvalRunner:
    """Score a rubric with Azure AI Foundry's evaluation SDK."""

    name = "azure"
    kind = "evals"

    def __init__(self, cfg: Config | None = None, run_dir: Path | None = None) -> None:
        self._cfg = cfg
        self._run_dir = run_dir

    def bind(self, cfg: Config, run_dir: Path) -> None:
        """Called by ``adlc.stages.evals.run_eval`` before :meth:`run`."""
        self._cfg = cfg
        self._run_dir = run_dir

    # -- detection --------------------------------------------------------
    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        if not has_module(SDK_MODULE):
            return False, (
                "azure-ai-evaluation is not installed (pip install 'adlc[azure]' or "
                "pip install azure-ai-evaluation)"
            )
        for group in AZURE_CREDENTIAL_GROUPS:
            if all(os.environ.get(name) for name in group):
                break
        else:
            names = " or ".join("+".join(group) for group in AZURE_CREDENTIAL_GROUPS)
            return False, (
                f"azure-ai-evaluation is installed but no Azure OpenAI credentials are in "
                f"the environment (need {names})"
            )
        settings = backend_settings(cfg, "azure")
        deployment = settings.get("deployment") or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        if not deployment:
            return False, (
                "azure-ai-evaluation is installed and credentialed but no judge deployment "
                "is configured (set eval.azure.deployment or AZURE_OPENAI_DEPLOYMENT)"
            )
        return True, f"azure-ai-evaluation available with deployment '{deployment}'"

    # -- execution --------------------------------------------------------
    def run(self, run: Run, rubric: Rubric) -> RubricScore:
        cfg = self._cfg or Config.load()
        available, reason = self.detect(cfg)
        if not available:
            raise EvalBackendUnavailable(reason)

        settings = backend_settings(cfg, "azure")
        specs = iter_criteria(rubric)
        if not specs:
            raise EvalBackendError("rubric declares no criteria; nothing for Azure to judge")

        rdir = self._run_dir or run_dir_for(run, cfg)
        context = spec_context(rdir)
        query = str(settings.get("query") or "Does the delivered change satisfy the specification?")
        response = str(settings.get("response") or context)

        model_config = _model_config(settings)
        rows = _evaluate(specs, model_config, query=query, response=response, context=context)
        outcomes = map_azure_rows(rows, specs)
        if not any(outcome.score is not None for outcome in outcomes.values()):
            raise EvalBackendError(
                "azure-ai-evaluation returned no usable score for any rubric criterion; "
                "refusing to report a score for criteria nothing judged"
            )

        score = build_rubric_score(
            rubric,
            outcomes,
            threshold=resolve_threshold(rubric, cfg),
            backend="azure-ai-evaluation",
            shared_evidence=["evals/azure-score.json"],
        )
        write_score(rdir / "evals" / "azure-score.json", score)
        return score


# ---------------------------------------------------------------------------
# SDK plumbing (imported lazily — never at module import time)
# ---------------------------------------------------------------------------


def _model_config(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Build the SDK's ``AzureOpenAIModelConfiguration`` mapping from env + config."""
    config: dict[str, Any] = {
        "azure_endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
        "azure_deployment": str(
            settings.get("deployment")
            or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            or DEFAULT_DEPLOYMENT
        ),
    }
    if api_key := os.environ.get("AZURE_OPENAI_API_KEY"):
        config["api_key"] = api_key
    if api_version := (settings.get("apiVersion") or os.environ.get("AZURE_OPENAI_API_VERSION")):
        config["api_version"] = str(api_version)
    return config


def _evaluate(
    specs: Sequence[CriterionSpec],
    model_config: Mapping[str, Any],
    *,
    query: str,
    response: str,
    context: str,
) -> dict[str, dict[str, Any]]:
    """Run one evaluator per criterion, returning ``{criterion id: raw row}``.

    A criterion whose id names a built-in evaluator uses that evaluator; everything else
    is judged by ``azure.ai.evaluation``'s generic rubric evaluator. An evaluator that
    raises is recorded as an error row — never as a pass.
    """
    try:
        # Imported lazily by design: module import must stay free of Azure dependencies.
        import azure.ai.evaluation as sdk
    except ImportError as exc:  # pragma: no cover - detect() already guarantees this
        raise EvalBackendUnavailable(f"azure-ai-evaluation import failed: {exc}") from exc

    rows: dict[str, dict[str, Any]] = {}
    for spec in specs:
        evaluator_name = BUILTIN_EVALUATORS.get(spec.id.strip().lower())
        try:
            if evaluator_name:
                evaluator = getattr(sdk, evaluator_name)(model_config=dict(model_config))
                row = evaluator(query=query, response=response, context=context)
                scale = BUILTIN_SCALE
            else:
                evaluator = _rubric_evaluator(sdk, spec, model_config)
                row = evaluator(query=query, response=response, context=context)
                scale = 1.0
        except Exception as exc:  # noqa: BLE001 - a backend error must not become a pass
            rows[spec.id] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        rows[spec.id] = {"row": dict(row) if isinstance(row, Mapping) else {}, "scale": scale}
    return rows


def _rubric_evaluator(sdk: Any, spec: CriterionSpec, model_config: Mapping[str, Any]) -> Any:
    """A custom LLM-judge evaluator for one rubric criterion.

    Prefers the SDK's own general-purpose grader when present and falls back to
    ``GroundednessEvaluator`` semantics only if the SDK exposes nothing suitable — in which
    case we raise rather than silently score something we did not ask for.
    """
    for attr in ("GeneralPromptyEvaluator", "LabelGraderEvaluator", "StringCheckGraderEvaluator"):
        factory = getattr(sdk, attr, None)
        if factory is not None:
            try:
                return factory(model_config=dict(model_config))
            except TypeError:
                continue
    raise EvalBackendError(
        f"azure-ai-evaluation exposes no generic rubric evaluator for criterion "
        f"'{spec.id}'; map it onto a built-in evaluator id "
        f"({', '.join(sorted(BUILTIN_EVALUATORS))}) or use the ASSERT/promptfoo backend"
    )


def map_azure_rows(
    rows: Mapping[str, Mapping[str, Any]], specs: Sequence[CriterionSpec]
) -> dict[str, CriterionOutcome]:
    """Normalise the SDK's ``{metric: value}`` rows onto criterion outcomes.

    Built-in evaluators report a 1–5 Likert score plus a ``*_reason``; rubric evaluators
    report 0–1. Anything unreadable stays unevaluated so it fails closed.
    """
    outcomes: dict[str, CriterionOutcome] = {}
    for spec in specs:
        entry = rows.get(spec.id)
        if not isinstance(entry, Mapping):
            continue
        if error := entry.get("error"):
            outcomes[spec.id] = CriterionOutcome(score=None, rationale=str(error)[:2000])
            continue
        row = entry.get("row")
        row = row if isinstance(row, Mapping) else {}
        try:
            scale = float(entry.get("scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            scale = 1.0

        score: float | None = None
        rationale = ""
        for key, value in row.items():
            lowered = str(key).lower()
            if lowered.endswith("_reason") or lowered == "reason":
                if isinstance(value, str) and value.strip():
                    rationale = value.strip()
            elif score is None and (lowered == "score" or lowered.endswith("_score")):
                score = coerce_score(value, scale=scale)
        if score is None:
            for value in row.values():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    score = coerce_score(value, scale=scale)
                    break
        outcomes[spec.id] = CriterionOutcome(
            score=score,
            rationale=rationale or ("azure evaluator returned no readable score" if score is None else ""),
            evidence=[f"azure:{key}" for key in sorted(row) if not str(key).endswith("_reason")][:10],
        )
    return outcomes
