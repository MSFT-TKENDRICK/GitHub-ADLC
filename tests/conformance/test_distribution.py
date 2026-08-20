"""Distribution conformance -- criterion 11 and the resume guarantee.

Proves the framework is genuinely reusable: a clean repository with no ADLC
files in it can be installed and driven end to end, and an interrupted build
resumes from the last completed level barrier instead of redoing merged work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from adlc.config import Config
from adlc.reduce import aggregate_passed, load_run
from adlc.runs import RunDir, new_run_id
from adlc.stages.build import _completed_levels, run_build
from adlc.stages.enrich import run_enrich
from adlc.stages.graph import run_graph
from adlc.stages.intake import run_intake, run_qualify
from adlc.stages.spec import run_spec
from tests.conformance.conftest import bind_env

# -- criterion 11: a clean repo can adopt the framework ---------------------

def test_init_installs_a_thin_pinned_caller(tmp_path: Path) -> None:
    """`adlc init` vendors config plus one pinned caller workflow -- nothing else."""
    from typer.testing import CliRunner

    from adlc.cli import app

    target = tmp_path / "fresh"
    target.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)

    result = CliRunner().invoke(app, ["init", "--target", str(target), "--ref", "v0"])
    assert result.exit_code == 0, result.output

    for rel in (".adlc/config.yaml", ".adlc/policy.yaml", ".adlc/squads.yaml",
                ".github/workflows/adlc.yml"):
        assert (target / rel).is_file(), f"init did not create {rel}"

    caller = (target / ".github/workflows/adlc.yml").read_text(encoding="utf-8")
    # Cross-repo reusable workflows must be referenced by full path and pinned.
    assert "uses: MSFT-TKENDRICK/GitHub-ADLC/.github/workflows/adlc.yml@v0" in caller
    assert "contents: read" in caller

    # It must not copy the framework in.
    assert not (target / "src" / "adlc").exists()
    assert not (target / "schemas").exists()


def test_init_never_clobbers_existing_ci(tmp_path: Path) -> None:
    """Adopting ADLC must not damage a repo's existing pipeline."""
    from typer.testing import CliRunner

    from adlc.cli import app

    target = tmp_path / "existing"
    (target / ".github" / "workflows").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)

    existing = target / ".github" / "workflows" / "ci.yml"
    existing.write_text("name: CI\non: push\njobs: {}\n", encoding="utf-8")
    ours = target / ".github" / "workflows" / "adlc.yml"
    ours.write_text("name: pre-existing adlc\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["init", "--target", str(target)])
    assert result.exit_code == 0, result.output
    assert existing.read_text(encoding="utf-8").startswith("name: CI")
    assert ours.read_text(encoding="utf-8") == "name: pre-existing adlc\n"
    assert "exists; use --force" in result.output


def test_installed_repo_runs_the_pipeline(tmp_path: Path) -> None:
    """C11: a clean repo installed by `adlc init` runs the same flow."""
    from typer.testing import CliRunner

    from adlc.cli import app
    from tests.conformance.conftest import BRIEF, _git
    from tests.conformance.driver import drive

    target = tmp_path / "adopter"
    target.mkdir()
    _git("init", "-q", "-b", "main", cwd=target)
    _git("config", "user.email", "a@b.invalid", cwd=target)
    _git("config", "user.name", "Adopter", cwd=target)
    (target / "README.md").write_text("# Adopter\n", encoding="utf-8")
    (target / "src").mkdir()
    (target / "src" / "app.py").write_text("def mount():\n    return 'app'\n", encoding="utf-8")
    _git("add", "-A", cwd=target)
    _git("commit", "-q", "-m", "init", cwd=target)

    assert CliRunner().invoke(app, ["init", "--target", str(target)]).exit_code == 0

    # Supply the test command the installed config leaves blank on purpose.
    config = target / ".adlc" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace('test: ""', 'test: "python -c \\"print(1)\\""'),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-q", "-m", "adopt adlc", cwd=target)

    brief = target / "brief.md"
    brief.write_text(BRIEF, encoding="utf-8")

    previous = Path.cwd()
    try:
        cfg = bind_env(target)
        rd = drive(cfg, brief)
        run = load_run(rd)
        passed, failures = aggregate_passed(run["gates"])
        assert passed, f"gates failed in the adopting repo: {failures}"
        assert (rd.report).is_file()
    finally:
        import os

        os.chdir(previous)


def test_bootstrap_script_is_sane() -> None:
    """The dotfiles/Codespaces side-load path stays minimal and safe."""
    script = Path(__file__).resolve().parents[2] / "bootstrap.sh"
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "adlc init" in text
    # It must not vendor the framework or rewrite the consumer's CI.
    assert "cp -r" not in text
    assert "rm -rf" not in text


# -- criterion 12: kill and resume ------------------------------------------

def test_build_resumes_from_the_last_completed_barrier(cfg: Config, brief_file: Path) -> None:
    """C12: re-running a build continues rather than redoing merged work."""
    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_qualify(cfg, rd)
    run_spec(cfg, rd)
    run_enrich(cfg, rd)
    run_graph(cfg, rd)

    first = run_build(cfg, rd, runner_name="fake")
    assert first["levels"] >= 2
    assert first["completedLevels"] == first["levels"], first["barriers"]

    completed_before = _completed_levels(rd)
    assert completed_before == first["levels"]

    second = run_build(cfg, rd, runner_name="fake")
    assert second["resumedFrom"] == completed_before
    # Nothing re-executed, because every level was already past its barrier.
    assert second["nodes"] == []


def test_build_without_resume_reruns_everything(cfg: Config, brief_file: Path) -> None:
    """`--no-resume` is an explicit escape hatch, not the default."""
    rd = RunDir(cfg, new_run_id())
    rd.create(profile=cfg.profile, brief_text=brief_file.read_text(encoding="utf-8"))
    run_intake(cfg, rd, str(brief_file))
    run_qualify(cfg, rd)
    run_spec(cfg, rd)
    run_enrich(cfg, rd)
    run_graph(cfg, rd)
    run_build(cfg, rd, runner_name="fake")

    again = run_build(cfg, rd, runner_name="fake", resume=False)
    assert again["resumedFrom"] == 0
    assert again["nodes"], "expected nodes to re-execute with resume disabled"


# -- criterion 9: review creates a successor, never mutates history ---------

def test_changes_requested_creates_a_successor_run(cfg: Config, brief_file: Path) -> None:
    """C9: a revision opens a NEW run and leaves the prior run byte-identical."""
    from adlc.stages.review import apply_review
    from tests.conformance.driver import drive

    rd = drive(cfg, brief_file)
    before = rd.run_json.read_bytes()
    head = load_run(rd).get("headSha") or ""

    event = {
        "review": {
            "state": "changes_requested",
            "commit_id": head,
            "body": "The persistence path is untested.",
            "user": {"login": "reviewer"},
        },
        "pull_request": {"head": {"sha": head}, "labels": [{"name": "adlc:route-inner"}]},
    }
    result = apply_review(cfg, rd, event)

    assert result["applied"] is True
    assert result["successorRun"], "no successor run was created"
    successor = RunDir(cfg, result["successorRun"])
    assert successor.exists()
    assert load_run(successor)["referencesRun"] == rd.run_id
    successor_brief = successor.brief.read_text(encoding="utf-8")
    assert "The persistence path is untested." in successor_brief
    assert "Review feedback" in successor_brief

    # The prior run's canonical record is untouched.
    assert rd.run_json.read_bytes() == before


def test_stale_review_is_refused(cfg: Config, brief_file: Path) -> None:
    """A decision must never be applied to code the reviewer did not see."""
    from adlc.stages.review import apply_review
    from tests.conformance.driver import drive

    rd = drive(cfg, brief_file)
    event = {
        "review": {
            "state": "approved",
            "commit_id": "0" * 40,
            "body": "looks good",
            "user": {"login": "reviewer"},
        },
        "pull_request": {"head": {"sha": "1" * 40}, "labels": []},
    }
    result = apply_review(cfg, rd, event)
    assert result["applied"] is False
    assert "stale" in result["reason"]


def test_approval_records_an_adr_bound_to_the_review_sha(cfg: Config, brief_file: Path) -> None:
    """C10: the decision is auditable and tied to a specific commit."""
    from adlc.stages.adr import list_adrs
    from adlc.stages.review import apply_review
    from tests.conformance.driver import drive

    rd = drive(cfg, brief_file)
    head = load_run(rd).get("headSha") or ""
    event = {
        "review": {
            "state": "approved",
            "commit_id": head,
            "body": "Evidence supports every acceptance criterion.",
            "user": {"login": "maintainer"},
        },
        "pull_request": {"head": {"sha": head}, "labels": []},
    }
    result = apply_review(cfg, rd, event)
    assert result["applied"] is True

    adrs = list_adrs(cfg)
    assert adrs, "no ADR was created"
    text = adrs[-1].path.read_text(encoding="utf-8")
    assert "status: accepted" in text
    assert f"adlc-review-sha: {head}" in text
    assert "## Decision Outcome" in text  # MADR v4 structure
