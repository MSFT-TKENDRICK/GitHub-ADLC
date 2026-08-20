"""End-to-end plumbing for the L3 backends, with the subprocess stubbed out.

The mapping tests cover JSONL/JSON → ``RubricScore``. These cover everything *around* it:
config generation, where each tool's artifacts are collected from, what gets written into
``runs/<run>/evals/``, and that the ``evals`` gate can read the result back. No network, no
credentials, no real CLI — only the subprocess boundary is faked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from adlc.adapters.evals import assert_ as assert_mod
from adlc.adapters.evals import promptfoo as promptfoo_mod
from adlc.adapters.evals.assert_ import AssertEvalRunner, EvalBackendError
from adlc.adapters.evals.promptfoo import PromptfooEvalRunner
from adlc.adapters.gate.evals import EvalsGate
from adlc.config import Config
from adlc.ports import EvalRunner, GateRunner

from .conftest import read_fixture


@pytest.fixture
def assert_cfg(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A config where assert-ai looks installed, credentialed and targeted."""
    monkeypatch.setattr(assert_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    cfg.raw["eval"] = {
        "threshold": 0.7,
        "assert": {"target": {"callable": "demo.app:chat"}, "model": "azure/gpt-4o"},
    }
    return cfg


def fake_assert_cli(rows_by_behavior: dict[str, list[str]]):
    """Stand in for ``assert-ai run --config …``: write the judge stage's scores.jsonl."""

    def _run(argv, *, cwd: Path, timeout: int, env=None):
        config_path = Path(argv[argv.index("--config") + 1])
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        suite, run_id = config["suite"], config["run"]
        rows = rows_by_behavior.get(config["behavior"]["name"], [])
        out = Path(cwd) / "artifacts" / "results" / suite / str(run_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "scores.jsonl").write_text("\n".join(rows) + "\n" if rows else "", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "judge: done\n", "")

    return _run


@pytest.fixture
def fixture_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in read_fixture("assert-scores.jsonl").splitlines():
        if line.strip():
            rows.setdefault(json.loads(line)["behavior"], []).append(line)
    return rows


def test_adapters_satisfy_the_frozen_protocols() -> None:
    assert isinstance(AssertEvalRunner(), EvalRunner)
    assert isinstance(PromptfooEvalRunner(), EvalRunner)
    assert isinstance(EvalsGate(), GateRunner)


def test_assert_run_writes_artifacts_and_normalises(
    assert_cfg: Config,
    run_dir: Path,
    run_doc: dict[str, Any],
    rubric: dict[str, Any],
    fixture_rows: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assert_mod, "invoke_tool", fake_assert_cli(fixture_rows))

    score = AssertEvalRunner(assert_cfg).run(run_doc, rubric)

    # One eval_config.yaml per criterion, each carrying the spec as ASSERT's `context`.
    configs = sorted((run_dir / "evals" / "assert").glob("*.eval_config.yaml"))
    assert [p.name for p in configs] == [
        "r_a11y_01.eval_config.yaml",
        "r_contrast_01.eval_config.yaml",
        "r_perf_01.eval_config.yaml",
    ]
    first = yaml.safe_load(configs[0].read_text(encoding="utf-8"))
    assert "Users can switch to a dark theme" in first["context"]
    assert first["pipeline"]["inference"]["target"] == {"callable": "demo.app:chat"}

    # Raw judged JSONL is kept verbatim and cited by every criterion.
    raw = run_dir / "evals" / "assert-results.jsonl"
    assert raw.is_file()
    assert len([line for line in raw.read_text(encoding="utf-8").splitlines() if line]) == 5
    assert all("evals/assert-results.jsonl" in c["evidence"] for c in score["criteria"])

    assert score["overall"] == pytest.approx(round(((2 / 3) * 2 + 1.0) / 4, 4))
    assert score["passed"] is False

    # …and the side artifact the gate can read back.
    written = json.loads((run_dir / "evals" / "assert-score.json").read_text(encoding="utf-8"))
    assert written == score

    gate = EvalsGate().evaluate(run_doc, assert_cfg)
    assert gate["status"] == "fail"
    assert gate["observed"]["unevaluatedCriteria"] == ["R-a11y-01"]
    assert gate["evidence"] == ["gates/evals.json", "evals/assert-score.json"]


def test_assert_run_refuses_when_nothing_was_judged(
    assert_cfg: Config,
    run_dir: Path,
    run_doc: dict[str, Any],
    rubric: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assert_mod, "invoke_tool", fake_assert_cli({}))
    with pytest.raises(EvalBackendError, match=r"no scores\.jsonl"):
        AssertEvalRunner(assert_cfg).run(run_doc, rubric)
    # Nothing was written, so the gate reports not_run rather than a fabricated verdict.
    assert EvalsGate().evaluate(run_doc, assert_cfg)["status"] == "not_run"


def test_assert_run_requires_a_spec(
    assert_cfg: Config, run_dir: Path, run_doc: dict[str, Any], rubric: dict[str, Any]
) -> None:
    (run_dir / "spec" / "spec.md").unlink()
    with pytest.raises(EvalBackendError, match="adlc spec"):
        AssertEvalRunner(assert_cfg).run(run_doc, rubric)


def test_promptfoo_run_generates_config_and_reads_results(
    cfg: Config,
    run_dir: Path,
    run_doc: dict[str, Any],
    rubric: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promptfoo_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    results = read_fixture("promptfoo-results.json")

    def _run(argv, *, cwd: Path, timeout: int, env=None):
        Path(argv[argv.index("--output") + 1]).write_text(results, encoding="utf-8")
        # promptfoo exits 100 when assertions fail — a legitimate outcome, not an error.
        return subprocess.CompletedProcess(argv, 100, "", "")

    monkeypatch.setattr(promptfoo_mod, "invoke_tool", _run)

    score = PromptfooEvalRunner(cfg).run(run_doc, rubric)

    config = yaml.safe_load(
        (run_dir / "evals" / "promptfoo" / "promptfoo.yaml").read_text(encoding="utf-8")
    )
    assert [t["description"] for t in config["tests"]] == [
        "R-contrast-01",
        "R-perf-01",
        "R-a11y-01",
    ]
    assert "Users can switch to a dark theme" in config["tests"][0]["vars"]["context"]

    assert score["overall"] == pytest.approx(0.6)
    assert score["passed"] is False
    assert json.loads(
        (run_dir / "evals" / "promptfoo-score.json").read_text(encoding="utf-8")
    ) == score

    gate = EvalsGate().evaluate(run_doc, cfg)
    assert gate["status"] == "fail"
    assert gate["observed"]["unevaluatedCriteria"] == ["R-a11y-01"]


def test_promptfoo_run_fails_loudly_when_no_results_file_appears(
    cfg: Config,
    run_dir: Path,
    run_doc: dict[str, Any],
    rubric: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promptfoo_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        promptfoo_mod,
        "invoke_tool",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "config error"),
    )
    with pytest.raises(EvalBackendError, match=r"results\.json"):
        PromptfooEvalRunner(cfg).run(run_doc, rubric)
