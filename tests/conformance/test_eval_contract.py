"""The unevaluated-criterion contract between eval backends and autoresearch.

This is the seam that the L3 workstream found broken. ``run_eval`` records which
criteria a backend could not judge, and ``stages/autoresearch.py`` aggregates
that across runs to propose making them machine-checkable. The first version
inferred it by grepping the rationale for a literal phrase, which coupled every
backend, the stage and the outer loop to one another's *prose* -- a backend that
worded its message differently vanished from the feedback loop entirely.

These tests pin the structured contract and the legacy fallback, so neither can
regress silently.
"""

from __future__ import annotations

from pathlib import Path

from adlc.config import Config
from adlc.ports import RubricScore
from adlc.reduce import load_run
from adlc.runs import RunDir, new_run_id
from adlc.stages.evals import _unevaluated_ids


def _score(*criteria: dict) -> RubricScore:
    return {"overall": 0.0, "threshold": 0.7, "passed": False, "criteria": list(criteria)}


def test_structured_flag_is_authoritative() -> None:
    """A backend that sets requiresJudge is reported regardless of its wording."""
    score = _score(
        {"id": "R-1", "score": 0.0, "requiresJudge": True, "rationale": "wibble"},
        {"id": "R-2", "score": 1.0, "requiresJudge": False, "rationale": "checked"},
    )
    assert _unevaluated_ids(score) == ["R-1"]


def test_legacy_prose_still_recognised() -> None:
    """Backends written before the flag existed keep working."""
    score = _score(
        {"id": "R-1", "score": 0.0, "rationale": "requires an LLM judge - not evaluated"},
        {"id": "R-2", "score": 0.0, "rationale": "not evaluated by ASSERT: judge errored"},
        {"id": "R-3", "score": 1.0, "rationale": "found spec/spec.md"},
    )
    assert _unevaluated_ids(score) == ["R-1", "R-2"]


def test_a_differently_worded_backend_is_not_lost() -> None:
    """The original defect: novel wording plus no flag meant silent invisibility.

    With the structured flag set this is reported no matter how it reads, which
    is the whole point of replacing the prose match.
    """
    prose_only = _score({"id": "R-1", "score": 0.0, "rationale": "judge unavailable"})
    assert _unevaluated_ids(prose_only) == [], "documents the legacy gap"

    with_flag = _score(
        {"id": "R-1", "score": 0.0, "requiresJudge": True, "rationale": "judge unavailable"}
    )
    assert _unevaluated_ids(with_flag) == ["R-1"]


def test_deterministic_runner_sets_the_flag(cfg: Config, brief_file: Path) -> None:
    """The spine's own runner must honour the contract it defines."""
    from adlc.stages.enrich import run_enrich
    from adlc.stages.evals import run_eval
    from adlc.stages.intake import run_intake
    from adlc.stages.spec import run_spec

    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_spec(cfg, rd)
    run_enrich(cfg, rd)

    # Add a criterion the deterministic runner cannot possibly judge.
    import yaml

    rubric_path = rd.enrichment_dir / "rubric.yaml"
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    rubric["criteria"].append(
        {
            "id": "R-judge-01",
            "statement": "The change reads as though a careful engineer wrote it.",
            "weight": 1,
            "kind": "llm-rubric",
        }
    )
    rubric_path.write_text(yaml.safe_dump(rubric, sort_keys=False), encoding="utf-8")

    run_eval(cfg, rd)

    stage = [s for s in rd.stage_results() if s["stage"] == "eval"][-1]
    assert "R-judge-01" in stage["data"]["unevaluated"]

    judged = next(c for c in stage["data"]["criteria"] if c["id"] == "R-judge-01")
    assert judged["requiresJudge"] is True
    # An unevaluated criterion must only ever pull the verdict down.
    assert judged["score"] == 0.0
    assert judged["passed"] is False


def test_autoresearch_consumes_the_same_field(cfg: Config, brief_file: Path) -> None:
    """The outer loop reads `data.unevaluated`; prove the wiring end to end."""
    from adlc.reduce import reduce_run
    from adlc.stages.autoresearch import propose
    from adlc.stages.enrich import run_enrich
    from adlc.stages.evals import run_eval
    from adlc.stages.intake import run_intake
    from adlc.stages.spec import run_spec

    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_spec(cfg, rd)
    run_enrich(cfg, rd)

    import yaml

    rubric_path = rd.enrichment_dir / "rubric.yaml"
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    rubric["criteria"].append(
        {"id": "R-judge-02", "statement": "Reads well.", "weight": 1, "kind": "llm-rubric"}
    )
    rubric_path.write_text(yaml.safe_dump(rubric, sort_keys=False), encoding="utf-8")

    run_eval(cfg, rd)
    reduce_run(cfg, rd)
    assert load_run(rd)["stages"], "reduce must fold the eval stage in"

    result = propose(cfg)
    surfaced = dict(result["signals"]["runs"]["unevaluatedCriteria"])
    assert "R-judge-02" in surfaced, result["signals"]["runs"]
