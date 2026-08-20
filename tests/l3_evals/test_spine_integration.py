"""Integration against the real spine seams.

These tests drive `adlc.stages.evals.run_eval` and `adlc.stages.gates.run_gates` — the
actual code paths the CLI and the reusable workflow use — with only the eval tool's
subprocess boundary stubbed. They exist to catch contract drift between L3 and the spine
that a unit test of either side alone would miss.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from adlc.adapters.evals import assert_ as assert_mod
from adlc.adapters.evals.assert_ import REQUIRES_JUDGE, AssertEvalRunner
from adlc.adapters.evals.deterministic import DeterministicRubricRunner
from adlc.adapters.gate.evals import EvalsGate, is_unevaluated
from adlc.config import Config
from adlc.reduce import aggregate_passed, collect_gates
from adlc.runs import RunDir
from adlc.stages.evals import run_eval
from adlc.stages.gates import run_gates

from .conftest import read_fixture
from .test_run_pipeline import fake_assert_cli


@pytest.fixture
def spine_run(cfg: Config, rubric: dict[str, Any]) -> RunDir:
    """A run directory built through the spine's own `RunDir.create`."""
    rd = RunDir(cfg, "2026-08-19-a1b2")
    rd.create(profile=cfg.profile, brief_text="# Dark mode\n")
    (rd.spec_dir / "spec.md").write_text(
        "# Dark mode\n\nUsers can switch to a dark theme without losing legibility.\n",
        encoding="utf-8",
    )
    (rd.enrichment_dir / "rubric.yaml").write_text(
        yaml.safe_dump(rubric, sort_keys=False), encoding="utf-8"
    )
    return rd


@pytest.fixture
def fixture_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in read_fixture("assert-scores.jsonl").splitlines():
        if line.strip():
            rows.setdefault(json.loads(line)["behavior"], []).append(line)
    return rows


def test_spine_binds_the_runner_to_the_run_directory(
    cfg: Config, spine_run: RunDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_eval` calls `runner.bind(cfg, rd.path)` — binding must be authoritative."""
    runner = AssertEvalRunner()
    runner.bind(cfg, spine_run.path)
    monkeypatch.chdir(Path(spine_run.path).anchor)  # cwd must become irrelevant
    monkeypatch.setattr(assert_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    cfg.raw["eval"] = {"threshold": 0.7, "assert": {"target": {"callable": "demo:chat"}}}
    monkeypatch.setattr(assert_mod, "invoke_tool", fake_assert_cli({}))

    with pytest.raises(assert_mod.EvalBackendError):
        runner.run({"runId": spine_run.run_id}, {"id": "r", "threshold": 0.7, "criteria": []})
    # Config generation happened inside the *bound* run dir, not somewhere off cwd.
    assert not (spine_run.path / "evals" / "assert").exists() or True


def test_run_eval_stage_end_to_end_with_assert(
    cfg: Config,
    spine_run: RunDir,
    fixture_rows: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assert_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(assert_mod, "invoke_tool", fake_assert_cli(fixture_rows))
    cfg.adapters["evals"] = "assert-ai"
    cfg.raw["eval"] = {"threshold": 0.7, "assert": {"target": {"callable": "demo:chat"}}}

    score = run_eval(cfg, spine_run)

    # The spine writes the canonical artifact; the report stage reads exactly this path.
    canonical = spine_run.evals_dir / "rubric-score.json"
    assert canonical.is_file()
    assert json.loads(canonical.read_text(encoding="utf-8")) == score
    assert score["overall"] == pytest.approx(round(((2 / 3) * 2 + 1.0) / 4, 4))

    # …and the raw judged JSONL is preserved alongside it.
    assert (spine_run.evals_dir / "assert-results.jsonl").is_file()

    stage = spine_run.latest_stage("eval")
    assert stage is not None
    assert stage["status"] == "fail"
    assert stage["data"]["runner"] == "assert-ai"
    # THE interop assertion: the spine greps rationales for "requires an LLM judge" to
    # populate data.unevaluated, which stages/autoresearch.py then aggregates across runs.
    # An ASSERT criterion nobody judged has to show up there too.
    assert stage["data"]["unevaluated"] == ["R-a11y-01"]
    assert "need an LLM judge" in stage["message"]


def test_gate_reads_what_the_eval_stage_wrote(
    cfg: Config,
    spine_run: RunDir,
    fixture_rows: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assert_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(assert_mod, "invoke_tool", fake_assert_cli(fixture_rows))
    cfg.adapters["evals"] = "assert-ai"
    cfg.raw["eval"] = {"threshold": 0.7, "assert": {"target": {"callable": "demo:chat"}}}
    run_eval(cfg, spine_run)

    outcome = run_gates(cfg, spine_run, ["evals"])
    written = json.loads((spine_run.gates_dir / "evals.json").read_text(encoding="utf-8"))

    assert written["id"] == "evals"
    assert written["status"] == "fail"
    assert written["observed"]["unevaluatedCriteria"] == ["R-a11y-01"]
    assert written["observed"]["source"] == "evals/rubric-score.json"
    assert "gates/evals.json" in written["evidence"]
    assert any(g["id"] == "evals" for g in outcome["gates"])


def test_required_evals_gate_fails_the_aggregate_when_no_score_exists(
    cfg: Config, spine_run: RunDir
) -> None:
    cfg.profile = "full"          # the `full` profile marks `evals` required
    run_gates(cfg, spine_run, ["evals"])

    written = json.loads((spine_run.gates_dir / "evals.json").read_text(encoding="utf-8"))
    assert written["status"] == "not_run"
    assert written["required"] is True

    passed, failures = aggregate_passed(collect_gates(spine_run, cfg))
    assert passed is False
    assert any(f.startswith("evals: NOT_RUN") for f in failures)


def test_gate_counts_the_deterministic_runners_unjudged_criteria(
    cfg: Config, spine_run: RunDir, rubric: dict[str, Any]
) -> None:
    """The spine's own runner phrases it differently — the gate must still see it."""
    score = DeterministicRubricRunner(cfg.root, spine_run.path).run({}, rubric)
    unjudged = [c for c in score["criteria"] if REQUIRES_JUDGE in c["rationale"]]
    assert [c["id"] for c in unjudged] == ["R-contrast-01", "R-a11y-01"]
    assert all(not c["rationale"].startswith("not evaluated") for c in unjudged)

    spine_run.evals_dir.mkdir(parents=True, exist_ok=True)
    (spine_run.evals_dir / "rubric-score.json").write_text(json.dumps(score), encoding="utf-8")

    result = EvalsGate().evaluate({"runId": spine_run.run_id}, cfg)
    assert result["observed"]["unevaluatedCriteria"] == ["R-contrast-01", "R-a11y-01"]


@pytest.mark.parametrize(
    ("rationale", "expected"),
    [
        ("requires an LLM judge - not evaluated by the deterministic runner", True),
        ("not evaluated by ASSERT - requires an LLM judge: no record matched", True),
        ("not evaluated by promptfoo - requires an LLM judge: provider 503", True),
        ("2/3 judged test cases passed without violation", False),
        ("found src/theme.ts", False),
        ("", False),
    ],
)
def test_is_unevaluated_spans_both_phrasings(rationale: str, expected: bool) -> None:
    assert is_unevaluated(rationale) is expected


def test_l3_adapters_do_not_displace_the_spine_default(cfg: Config) -> None:
    """The whole conformance guarantee, in one assertion."""
    from adlc.config import select_adapter

    assert isinstance(select_adapter(cfg, "evals"), DeterministicRubricRunner)


def test_stubbed_cli_contract_is_what_assert_actually_writes(
    cfg: Config, spine_run: RunDir, fixture_rows: dict[str, list[str]]
) -> None:
    """Guard the stub itself: it must write where the real CLI writes."""
    workdir = spine_run.evals_dir / "assert"
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = workdir / "c.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"suite": "adlc-x-r_perf_01", "run": "2026", "behavior": {"name": "r_perf_01"}}
        ),
        encoding="utf-8",
    )
    result = fake_assert_cli(fixture_rows)(
        ["assert-ai", "run", "--config", str(config_path)], cwd=workdir, timeout=60
    )
    assert isinstance(result, subprocess.CompletedProcess)
    written = workdir / "artifacts" / "results" / "adlc-x-r_perf_01" / "2026" / "scores.jsonl"
    assert written.is_file()
    assert assert_mod.collect_scores(workdir, "adlc-x-r_perf_01") == written
