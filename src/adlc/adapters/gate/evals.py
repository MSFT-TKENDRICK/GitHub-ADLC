"""The ``evals`` gate — turns a :class:`~adlc.ports.RubricScore` into a gate verdict.

This gate is deliberately dumb: it does not evaluate anything itself. It reads the
``RubricScore`` produced by whichever :class:`~adlc.ports.EvalRunner` the config selected
— the spine's deterministic runner, ASSERT, promptfoo or Azure — and passes iff
``overall >= threshold``.

That is the payoff of normalising every backend onto one shape: the gate and the report
never learn which backend ran.

Fail-closed rules
-----------------
* No score artifact at all ⇒ ``not_run`` (which a *required* gate turns into a build
  failure), never ``pass``.
* A score whose criteria were stamped :data:`~adlc.adapters.evals.assert_.NOT_EVALUATED`
  is reported honestly: the unevaluated count lands in ``observed`` and those criteria
  contribute 0.0, so they can only ever pull the verdict down.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adlc.adapters.evals.assert_ import NOT_EVALUATED
from adlc.config import Config
from adlc.ports import GateResult, RubricScore, Run

__all__ = ["EvalsGate", "find_rubric_score"]

#: Score filenames we look for under ``runs/<run>/evals/``, in priority order. The first
#: entries are what the spine writes; the ``*-score.json`` entries are the side artifacts
#: the L3 backends write so the gate still works when a backend is driven directly.
SCORE_FILENAMES: tuple[str, ...] = (
    "score.json",
    "rubric-score.json",
    "rubric_score.json",
    "evals.json",
    "result.json",
    "assert-score.json",
    "promptfoo-score.json",
    "azure-score.json",
)

#: Keys under a stage's ``data`` that may hold the score.
_STAGE_DATA_KEYS: tuple[str, ...] = ("score", "rubricScore", "rubric_score", "result")


def _is_rubric_score(value: Any) -> bool:
    """Structural check — a RubricScore has an ``overall`` and a ``criteria`` list."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("overall"), (int, float))
        and not isinstance(value.get("overall"), bool)
        and isinstance(value.get("criteria"), list)
    )


def _from_stages(run: Run) -> tuple[RubricScore, str] | None:
    """Latest successful ``eval`` stage result carrying a score, if any."""
    stages = [s for s in (run.get("stages") or []) if isinstance(s, dict)]
    evals = [s for s in stages if s.get("stage") == "eval"]
    for stage in sorted(evals, key=lambda s: int(s.get("attempt") or 0), reverse=True):
        data = stage.get("data")
        if not isinstance(data, dict):
            continue
        candidates = [data, *(data.get(key) for key in _STAGE_DATA_KEYS)]
        for candidate in candidates:
            if _is_rubric_score(candidate):
                attempt = stage.get("attempt", 1)
                return candidate, f"stages/eval.{attempt}.json"
    return None


def _from_files(evals_dir: Path) -> tuple[RubricScore, str] | None:
    """First RubricScore-shaped JSON document under ``evals/``."""
    if not evals_dir.is_dir():
        return None
    named = [evals_dir / name for name in SCORE_FILENAMES]
    rest = sorted(p for p in evals_dir.glob("*.json") if p not in named)
    for path in [*named, *rest]:
        if not path.is_file():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _is_rubric_score(blob):
            return blob, f"evals/{path.name}"
    return None


def find_rubric_score(run: Run, cfg: Config) -> tuple[RubricScore, str] | None:
    """Locate the run's ``RubricScore`` and the run-relative path it came from.

    Stage results win over loose files: they are the immutable record of what the eval
    stage actually produced.
    """
    found = _from_stages(run)
    if found:
        return found
    run_id = str(run.get("runId") or "").strip()
    if not run_id:
        return None
    return _from_files(cfg.run_dir(run_id) / "evals")


class EvalsGate:
    """Pass iff the rubric's weighted ``overall`` meets its threshold."""

    id = "evals"
    name = "evals"
    kind = "gate"
    #: Optional by default; the ``full`` profile promotes it to required
    #: (see ``adlc.config.PROFILE_REQUIRED_GATES``).
    required_by_default = False

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        # Reading a JSON document needs nothing: no binary, no key, no network. Whether a
        # score actually exists is an `evaluate()` question, not a capability question.
        return True, "reads the RubricScore written by the selected eval runner"

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        required = cfg.is_required(self.id)
        configured = (cfg.raw.get("eval") or {}).get("threshold", 0.7)
        try:
            configured_threshold = float(configured)
        except (TypeError, ValueError):
            configured_threshold = 0.7

        found = find_rubric_score(run, cfg)
        if found is None:
            return {
                "id": self.id,
                "required": required,
                "status": "not_run",
                "severity": "high" if required else "medium",
                "observed": {"score": None},
                "expected": {"overall": f">= {configured_threshold}"},
                "message": (
                    "no RubricScore found for this run — expected an eval stage result or a "
                    f"score document under runs/{run.get('runId', '?')}/evals/. "
                    "Run `adlc eval <run>` first."
                ),
                "evidence": [],
            }

        score, source = found
        threshold = score.get("threshold")
        try:
            threshold = float(threshold)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            threshold = configured_threshold
        overall = float(score.get("overall") or 0.0)

        criteria = [c for c in (score.get("criteria") or []) if isinstance(c, dict)]
        breakdown = [
            {
                "id": c.get("id"),
                "score": c.get("score"),
                "weight": c.get("weight"),
                "passed": bool(c.get("passed")),
                "rationale": str(c.get("rationale") or "")[:400],
            }
            for c in criteria
        ]
        unevaluated = [
            str(c.get("id"))
            for c in criteria
            if str(c.get("rationale") or "").startswith(NOT_EVALUATED)
        ]
        failed = [str(c.get("id")) for c in criteria if not c.get("passed")]
        passed = overall >= threshold

        if not criteria:
            message = "the RubricScore has no criteria — nothing was actually evaluated"
            status: str = "not_run"
            passed = False
        elif passed:
            message = (
                f"rubric overall {overall:.2f} >= threshold {threshold:.2f} "
                f"({len(criteria) - len(failed)}/{len(criteria)} criteria passed)"
            )
            status = "pass"
        else:
            message = (
                f"rubric overall {overall:.2f} < threshold {threshold:.2f}; "
                f"failing criteria: {', '.join(failed) or 'none'}"
            )
            status = "fail"
        if unevaluated:
            message += f"; {len(unevaluated)} criteria were {NOT_EVALUATED}: " + ", ".join(
                unevaluated
            )

        severity = "low" if status == "pass" else ("high" if required else "medium")
        return {
            "id": self.id,
            "required": required,
            "status": status,  # type: ignore[typeddict-item]
            "severity": severity,  # type: ignore[typeddict-item]
            "observed": {
                "overall": overall,
                "threshold": threshold,
                "passed": passed,
                "source": source,
                "criteriaCount": len(criteria),
                "failedCriteria": failed,
                "unevaluatedCriteria": unevaluated,
                "criteria": breakdown,
            },
            "expected": {"overall": f">= {threshold}", "unevaluatedCriteria": []},
            "message": message,
            "evidence": [source],
        }
