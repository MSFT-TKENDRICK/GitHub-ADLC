"""Deterministic rubric runner -- the spine's credential-free default eval.

Evaluates only the criteria it can check *without* an LLM:

* ``file_exists``            -- an artifact or source file is present
* ``command_exit_zero``      -- a shell command succeeds
* ``metric_within_budget``   -- a measurement satisfies its budget
* ``regex_in_file``          -- required content is present

Criteria marked ``kind: llm-rubric`` cannot be judged here. They are scored
``0.0`` and flagged ``requiresJudge`` rather than being silently skipped or
passed -- inventing a pass for an unevaluated criterion is exactly the
fail-open behaviour this framework exists to prevent.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import Rubric, RubricCriterion, RubricScore, Run


class DeterministicRubricRunner:
    name = "deterministic"
    kind = "evals"

    def __init__(self, root: Path | None = None, run_dir: Path | None = None) -> None:
        self.root = root or Path.cwd()
        self.run_dir = run_dir

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "built-in deterministic rubric runner (no credentials required)"

    def bind(self, cfg: Config, run_dir: Path) -> None:
        self.root = cfg.root
        self.run_dir = run_dir

    # -- checks -----------------------------------------------------------
    def _measurements(self) -> dict[str, dict[str, Any]]:
        """Collect normalised measurements emitted by evidence collectors."""
        out: dict[str, dict[str, Any]] = {}
        if not self.run_dir:
            return out
        for path in (self.run_dir / "evidence").rglob("*-measurements.json"):
            try:
                for item in json.loads(path.read_text(encoding="utf-8")):
                    if "metricId" in item:
                        out[item["metricId"]] = item
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        return out

    def _evaluate(self, criterion: dict[str, Any]) -> tuple[float, str]:
        kind = criterion.get("kind", "deterministic")
        if kind == "llm-rubric":
            return 0.0, "requires an LLM judge - not evaluated by the deterministic runner"

        check = criterion.get("check") or {}
        check_type = check.get("type")

        if check_type == "file_exists":
            target = check.get("target", "")
            for base in filter(None, (self.run_dir, self.root)):
                if (Path(base) / target).exists():
                    return 1.0, f"found {target}"
            return 0.0, f"missing {target}"

        if check_type == "regex_in_file":
            target, pattern = check.get("target", ""), check.get("pattern", "")
            for base in filter(None, (self.run_dir, self.root)):
                candidate = Path(base) / target
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    if re.search(pattern, text):
                        return 1.0, f"pattern matched in {target}"
                    return 0.0, f"pattern not found in {target}"
            return 0.0, f"missing {target}"

        if check_type == "command_exit_zero":
            command = check.get("target", "")
            if not command:
                return 0.0, "no command configured"
            proc = subprocess.run(  # noqa: S602,S603
                command, cwd=str(self.root), shell=True,
                capture_output=True, text=True, check=False,
            )
            if proc.returncode == 0:
                return 1.0, f"`{command}` exited 0"
            return 0.0, f"`{command}` exited {proc.returncode}: {(proc.stderr or '')[-300:]}"

        if check_type == "metric_within_budget":
            metric_id = check.get("metricId", "")
            measurement = self._measurements().get(metric_id)
            if measurement is None:
                return 0.0, f"no measurement recorded for '{metric_id}'"
            budget = check.get("budget", measurement.get("budget"))
            value = measurement.get("value")
            if budget is None or value is None:
                return 0.0, f"metric '{metric_id}' missing value or budget"
            if value <= budget:
                return 1.0, f"{metric_id}={value} within budget {budget}"
            return 0.0, f"{metric_id}={value} exceeds budget {budget}"

        return 0.0, f"unknown check type '{check_type}'"

    # -- entry point -------------------------------------------------------
    def run(self, run: Run, rubric: Rubric) -> RubricScore:
        criteria_out: list[RubricCriterion] = []
        total_weight = 0.0
        weighted = 0.0

        for criterion in rubric.get("criteria", []):
            weight = float(criterion.get("weight", 1.0))
            score, rationale = self._evaluate(criterion)
            total_weight += weight
            weighted += score * weight
            criteria_out.append({
                "id": criterion.get("id", "?"),
                "score": score,
                "weight": weight,
                "passed": score >= 1.0,
                "rationale": rationale,
                "evidence": criterion.get("acceptanceRefs") or [],
            })

        threshold = float(rubric.get("threshold", 0.7))
        overall = (weighted / total_weight) if total_weight else 0.0
        return {
            "overall": round(overall, 4),
            "threshold": threshold,
            "passed": overall >= threshold,
            "criteria": criteria_out,
        }
