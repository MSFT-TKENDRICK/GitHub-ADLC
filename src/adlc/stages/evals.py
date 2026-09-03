"""Eval stage -- score the candidate against the rubric."""

from __future__ import annotations

from typing import Any

import yaml

from adlc.config import Config, select_adapter
from adlc.ports import Rubric, RubricScore
from adlc.reduce import load_run
from adlc.runs import RunDir, utcnow, write_json
from adlc.schemas import is_valid


def load_rubric(rd: RunDir) -> Rubric:
    path = rd.enrichment_dir / "rubric.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found - run `adlc enrich` first")
    rubric: Rubric = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    valid, errors = is_valid("rubric", rubric)
    if not valid:
        raise ValueError(f"invalid rubric.yaml: {errors}")
    return rubric


#: Legacy fallback. Before ``RubricCriterion.requiresJudge`` existed, an
#: unevaluated criterion was detected by grepping the rationale for this phrase.
#: Backends written against the old contract still work, but a backend that
#: words its message differently would be invisible to the autoresearch loop --
#: which is exactly why the structured flag replaced it.
_LEGACY_JUDGE_MARKERS = ("requires an llm judge", "not evaluated")


def _unevaluated_ids(score: RubricScore) -> list[str]:
    """Criteria the backend could not actually judge.

    Prefers the structured ``requiresJudge`` flag and falls back to the legacy
    prose markers, so old and new backends both report correctly.
    """
    ids: list[str] = []
    for criterion in score.get("criteria", []):
        if criterion.get("requiresJudge"):
            ids.append(criterion.get("id", "?"))
            continue
        rationale = (criterion.get("rationale") or "").lower()
        if any(marker in rationale for marker in _LEGACY_JUDGE_MARKERS):
            ids.append(criterion.get("id", "?"))
    return ids


def run_eval(cfg: Config, rd: RunDir, *, runner_name: str | None = None) -> dict[str, Any]:
    started = utcnow()
    rubric = load_rubric(rd)
    runner = select_adapter(cfg, "evals", runner_name)
    if hasattr(runner, "bind"):
        runner.bind(cfg, rd.path)

    run = load_run(rd)
    score = runner.run(run, rubric)

    rd.evals_dir.mkdir(parents=True, exist_ok=True)
    write_json(rd.evals_dir / "rubric-score.json", score)

    unevaluated = _unevaluated_ids(score)

    rd.write_stage(
        "eval",
        status="ok" if score.get("passed") else "fail",
        outputs=["evals/rubric-score.json"],
        message=(
            f"score {score.get('overall')} vs threshold {score.get('threshold')} "
            f"via '{getattr(runner, 'name', type(runner).__name__)}'"
            + (f"; {len(unevaluated)} criterion/criteria need an LLM judge" if unevaluated else "")
        ),
        data={
            "runner": getattr(runner, "name", type(runner).__name__),
            "overall": score.get("overall"),
            "threshold": score.get("threshold"),
            "passed": score.get("passed"),
            "unevaluated": unevaluated,
            "criteria": score.get("criteria", []),
        },
        started_at=started,
    )
    return score
