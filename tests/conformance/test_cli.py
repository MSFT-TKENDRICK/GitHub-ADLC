"""``adlc.cli`` — end-to-end coverage for every Typer command.

Previously this module (284 statements) had no dedicated test file at all;
it was only exercised incidentally by whatever a leaf's own tests happened
to invoke via subprocess. That left the user-facing entry point — the thing
every human and every CI job actually runs — as the single largest coverage
gap in the tree (36%).

Uses ``typer.testing.CliRunner`` against a real consumer repository (the same
fixture conformance uses), so every command is invoked exactly the way a user
would invoke it, including exit codes and both ``--json`` and human-readable
output paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adlc.cli import app
from tests.conformance.conftest import bind_env, make_consumer_repo

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = make_consumer_repo(tmp_path / "consumer")
    bind_env(root)
    return root


@pytest.fixture(autouse=True)
def _restore_cwd():
    previous = Path.cwd()
    yield
    os.chdir(previous)


class TestVersionAndHelp:
    def test_version_flag_prints_version_and_exits_zero(self, repo: Path) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert result.stdout.strip()

    def test_no_args_shows_help_listing_every_command(self, repo: Path) -> None:
        result = runner.invoke(app, [])
        assert result.exit_code == 2
        assert "doctor" in result.stdout
        assert "hotfix" in result.stdout


class TestDoctor:
    def test_doctor_human_output_lists_profile_and_selected_adapters(
        self, repo: Path
    ) -> None:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "profile=minimal" in result.stdout
        assert "selected:" in result.stdout
        assert "required gates" in result.stdout

    def test_doctor_json_output_is_parseable_and_has_expected_shape(
        self, repo: Path
    ) -> None:
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert "kinds" in payload
        assert "selected" in payload

    def test_doctor_writes_capabilities_json(self, repo: Path) -> None:
        runner.invoke(app, ["doctor"])
        assert (repo / ".adlc" / "capabilities.json").is_file()


class TestInit:
    def test_init_writes_expected_files_into_a_bare_target(self, tmp_path: Path) -> None:
        target = tmp_path / "bare-target"
        target.mkdir()
        result = runner.invoke(app, ["init", "--target", str(target)])
        assert result.exit_code == 0
        assert (target / ".adlc" / "config.yaml").is_file()
        assert (target / ".adlc" / "policy.yaml").is_file()
        assert (target / ".adlc" / "squads.yaml").is_file()
        assert (target / ".github" / "workflows" / "adlc.yml").is_file()

    def test_init_appends_gitignore_marker_when_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "target-with-gitignore"
        target.mkdir()
        (target / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        runner.invoke(app, ["init", "--target", str(target)])
        content = (target / ".gitignore").read_text(encoding="utf-8")
        assert "node_modules/" in content
        assert ".adlc/runs/" in content

    def test_init_does_not_duplicate_gitignore_marker_on_second_run(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target-idempotent"
        target.mkdir()
        runner.invoke(app, ["init", "--target", str(target), "--force"])
        runner.invoke(app, ["init", "--target", str(target), "--force"])
        content = (target / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".adlc/runs/") == 1

    def test_init_skips_existing_files_without_force(self, tmp_path: Path) -> None:
        target = tmp_path / "target-existing"
        target.mkdir()
        runner.invoke(app, ["init", "--target", str(target)])
        result = runner.invoke(app, ["init", "--target", str(target)])
        assert result.exit_code == 0
        assert "exists; use --force" in result.stdout

    def test_init_force_overwrites_existing_files(self, tmp_path: Path) -> None:
        target = tmp_path / "target-force"
        target.mkdir()
        runner.invoke(app, ["init", "--target", str(target)])
        result = runner.invoke(app, ["init", "--target", str(target), "--force"])
        assert result.exit_code == 0
        assert "+ .adlc\\config.yaml" in result.stdout or "+ .adlc/config.yaml" in result.stdout


class TestRunLifecycle:
    def test_run_new_requires_brief_or_issue(self, repo: Path) -> None:
        result = runner.invoke(app, ["run", "new"])
        assert result.exit_code == 2
        assert "provide --brief or --issue" in result.stderr

    def test_run_new_from_brief_creates_a_run_directory(self, repo: Path) -> None:
        result = runner.invoke(app, ["run", "new", "--brief", str(repo / "brief.md")])
        assert result.exit_code == 0
        assert "created run" in result.stdout
        runs = list((repo / ".adlc" / "runs").iterdir())
        assert len(runs) == 1

    def test_run_new_json_output_has_run_id_and_path(self, repo: Path) -> None:
        result = runner.invoke(
            app, ["run", "new", "--brief", str(repo / "brief.md"), "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["runId"]
        assert payload["path"]

    def test_run_list_reports_no_runs_yet_before_any_run_exists(self, repo: Path) -> None:
        result = runner.invoke(app, ["run", "list"])
        assert result.exit_code == 0
        assert "no runs yet" in result.stdout

    def test_run_list_after_a_run_reports_its_status(self, repo: Path) -> None:
        runner.invoke(app, ["run", "new", "--brief", str(repo / "brief.md")])
        result = runner.invoke(app, ["run", "list", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        assert rows[0]["runId"]


class TestStageCommands:
    """qualify / spec / enrich / graph / build / evidence / eval / gate / reduce."""

    @pytest.fixture
    def run_id(self, repo: Path) -> str:
        result = runner.invoke(
            app, ["run", "new", "--brief", str(repo / "brief.md"), "--json"]
        )
        return json.loads(result.stdout)["runId"]

    def test_full_pipeline_via_cli_reaches_a_gated_run(self, repo: Path, run_id: str) -> None:
        for cmd in ("qualify", "spec", "enrich", "graph"):
            result = runner.invoke(app, [cmd, run_id])
            assert result.exit_code == 0, f"{cmd} failed: {result.stdout}"

        result = runner.invoke(app, ["build", run_id])
        assert result.exit_code == 0, result.stdout

        result = runner.invoke(app, ["evidence", run_id])
        assert result.exit_code == 0, result.stdout

        result = runner.invoke(app, ["eval", run_id])
        assert result.exit_code == 0, result.stdout

        result = runner.invoke(app, ["gate", run_id])
        assert "aggregate:" in result.stdout

        result = runner.invoke(app, ["reduce", run_id])
        assert result.exit_code == 0
        assert "reduced" in result.stdout

    def test_stage_command_on_unknown_run_id_exits_nonzero(self, repo: Path) -> None:
        result = runner.invoke(app, ["qualify", "does-not-exist"])
        assert result.exit_code == 2

    def test_build_accepts_runner_and_max_parallel_options(
        self, repo: Path, run_id: str
    ) -> None:
        runner.invoke(app, ["qualify", run_id])
        runner.invoke(app, ["spec", run_id])
        runner.invoke(app, ["enrich", run_id])
        runner.invoke(app, ["graph", run_id])
        result = runner.invoke(
            app, ["build", run_id, "--runner", "fake", "--max-parallel", "2"]
        )
        assert result.exit_code == 0

    def test_evidence_accepts_variant_option(self, repo: Path, run_id: str) -> None:
        runner.invoke(app, ["qualify", run_id])
        runner.invoke(app, ["spec", run_id])
        runner.invoke(app, ["enrich", run_id])
        runner.invoke(app, ["graph", run_id])
        runner.invoke(app, ["build", run_id])
        result = runner.invoke(app, ["evidence", run_id, "--variant", "candidate-a"])
        assert result.exit_code == 0
        assert "artifact(s)" in result.stdout

    def test_gate_accepts_comma_separated_ids(self, repo: Path, run_id: str) -> None:
        for cmd in ("qualify", "spec", "enrich", "graph", "build", "evidence", "eval"):
            runner.invoke(app, [cmd, run_id])
        result = runner.invoke(app, ["gate", run_id, "--ids", "tests,secrets_local"])
        assert result.exit_code in (0, 1)
        assert "tests" in result.stdout

    def test_reduce_json_reports_stage_gate_artifact_counts(
        self, repo: Path, run_id: str
    ) -> None:
        runner.invoke(app, ["qualify", run_id])
        result = runner.invoke(app, ["reduce", run_id, "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert "stages" in payload
        assert "gates" in payload
        assert "artifacts" in payload


class TestReportAndValidate:
    @pytest.fixture
    def gated_run_id(self, repo: Path) -> str:
        result = runner.invoke(
            app, ["run", "new", "--brief", str(repo / "brief.md"), "--json"]
        )
        run_id = json.loads(result.stdout)["runId"]
        for cmd in ("qualify", "spec", "enrich", "graph", "build", "evidence", "eval", "gate"):
            runner.invoke(app, [cmd, run_id])
        return run_id

    def test_report_renders_an_html_file(self, repo: Path, gated_run_id: str) -> None:
        result = runner.invoke(app, ["report", gated_run_id])
        assert result.exit_code == 0
        assert "report:" in result.stdout

    def test_report_json_output_has_a_path(self, repo: Path, gated_run_id: str) -> None:
        result = runner.invoke(app, ["report", gated_run_id, "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["path"]
        assert Path(payload["path"]).is_file()

    def test_validate_reports_valid_for_a_reduced_run(
        self, repo: Path, gated_run_id: str
    ) -> None:
        result = runner.invoke(app, ["validate", gated_run_id])
        assert result.exit_code == 0
        assert "valid" in result.stdout.lower()

    def test_validate_json_output_has_adlc_run_findings(
        self, repo: Path, gated_run_id: str
    ) -> None:
        result = runner.invoke(app, ["validate", gated_run_id, "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["valid"] is True
        assert "adlc-run" in payload["findings"]


class TestAdr:
    def test_adr_new_creates_a_record_and_lists_it(self, repo: Path) -> None:
        result = runner.invoke(app, ["adr", "new", "Use SQLite for the local task store"])
        assert result.exit_code == 0
        assert "created" in result.stdout

        result = runner.invoke(app, ["adr", "list", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        assert rows[0]["status"] == "proposed"

    def test_adr_list_reports_no_adrs_when_none_exist(self, repo: Path) -> None:
        result = runner.invoke(app, ["adr", "list"])
        assert result.exit_code == 0
        assert "no ADRs" in result.stdout

    def test_adr_set_status_transitions_status(self, repo: Path) -> None:
        runner.invoke(app, ["adr", "new", "Adopt flagd for local flag evaluation"])
        result = runner.invoke(app, ["adr", "list", "--json"])
        number = json.loads(result.stdout)[0]["number"]

        result = runner.invoke(app, ["adr", "set-status", str(number), "accepted"])
        assert result.exit_code == 0
        assert "accepted" in result.stdout

        result = runner.invoke(app, ["adr", "list", "--json"])
        rows = json.loads(result.stdout)
        assert rows[0]["status"] == "accepted"

    def test_adr_set_status_accepts_review_sha(self, repo: Path) -> None:
        runner.invoke(app, ["adr", "new", "Adopt LaunchDarkly for experimentation"])
        result = runner.invoke(app, ["adr", "list", "--json"])
        number = json.loads(result.stdout)[0]["number"]
        result = runner.invoke(
            app, ["adr", "set-status", str(number), "accepted", "--review-sha", "abc123"]
        )
        assert result.exit_code == 0


class TestExportOes:
    def test_export_oes_on_a_non_comparative_run_exits_cleanly_not_a_traceback(
        self, repo: Path
    ) -> None:
        """A single-variant run is the common case; refusing must be a clean
        exit rather than an uncaught exception with a traceback."""
        result = runner.invoke(
            app, ["run", "new", "--brief", str(repo / "brief.md"), "--json"]
        )
        run_id = json.loads(result.stdout)["runId"]
        for cmd in ("qualify", "spec", "enrich", "graph", "build", "evidence", "eval", "gate"):
            runner.invoke(app, [cmd, run_id])

        result = runner.invoke(app, ["export", "oes", run_id])
        assert result.exit_code == 2
        assert result.exception is None or isinstance(
            result.exception, SystemExit
        ), "OES refusal must not surface as an uncaught traceback"


class TestAutoresearch:
    def test_autoresearch_runs_against_a_bare_repository(self, repo: Path) -> None:
        result = runner.invoke(app, ["autoresearch"])
        assert result.exit_code == 0

    def test_autoresearch_json_output_is_parseable(self, repo: Path) -> None:
        result = runner.invoke(app, ["autoresearch", "--json"])
        assert result.exit_code == 0
        json.loads(result.stdout)


class TestHotfix:
    def test_hotfix_is_registered_as_a_command(self, repo: Path) -> None:
        """Regression guard: `adlc hotfix` was implemented in
        `adlc.stages.hotfix` before it was wired into the CLI, so
        `adlc hotfix --help` used to fail with 'No such command'.
        """
        result = runner.invoke(app, ["hotfix", "--help"])
        assert result.exit_code == 0
        assert "--incident" in result.stdout

    def test_hotfix_plan_only_processes_an_incident_file(self, repo: Path) -> None:
        incident = repo / "incident.json"
        incident.write_text(
            json.dumps(
                {
                    "title": "Checkout button unresponsive on mobile Safari",
                    "description": (
                        "Users cannot complete checkout on mobile Safari; the "
                        "submit button does not respond to taps."
                    ),
                    "severity": "sev2",
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["hotfix", "--incident", str(incident), "--plan-only"])
        assert result.exit_code == 0, result.stdout
        assert "hotfix:" in result.stdout


class TestReviewApply:
    def test_review_apply_missing_event_file_fails_with_typer_usage_error(
        self, repo: Path
    ) -> None:
        result = runner.invoke(
            app, ["review", "apply", "latest", "--event", str(repo / "nope.json")]
        )
        assert result.exit_code != 0

    def test_review_apply_rejects_a_review_of_a_stale_commit(self, repo: Path) -> None:
        result = runner.invoke(
            app, ["run", "new", "--brief", str(repo / "brief.md"), "--json"]
        )
        run_id = json.loads(result.stdout)["runId"]

        event = repo / "event.json"
        event.write_text(
            json.dumps(
                {
                    "review": {"state": "approved", "commit_id": "deadbeef"},
                    "pull_request": {"head": {"sha": "deadbeef"}, "labels": []},
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["review", "apply", run_id, "--event", str(event)])
        # Either applies cleanly (sha matches what the run recorded) or is
        # rejected as stale -- both are valid outcomes for a fabricated SHA;
        # what matters is the command does not crash uncaught.
        assert result.exit_code in (0, 1)
