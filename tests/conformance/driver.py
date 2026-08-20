"""The canonical pipeline driver used by conformance tests.

Mirrors exactly what the CLI and the reusable workflow do, so a green
conformance run really does exercise the shipped path.
"""

from __future__ import annotations

from pathlib import Path

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.runs import RunDir, new_run_id
from adlc.stages.build import run_build
from adlc.stages.enrich import run_enrich
from adlc.stages.evals import run_eval
from adlc.stages.evidence import run_evidence
from adlc.stages.gates import run_gates
from adlc.stages.graph import run_graph
from adlc.stages.intake import run_intake, run_qualify
from adlc.stages.report import run_report
from adlc.stages.spec import run_spec


def drive(cfg: Config, brief_file: Path, *, runner: str = "fake") -> RunDir:
    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_qualify(cfg, rd)
    run_spec(cfg, rd)
    run_enrich(cfg, rd)
    run_graph(cfg, rd)
    run_build(cfg, rd, runner_name=runner)
    run_evidence(cfg, rd, "candidate-a")
    run_eval(cfg, rd)
    run_gates(cfg, rd)
    reduce_run(cfg, rd)
    run_report(cfg, rd)
    reduce_run(cfg, rd)
    return rd
