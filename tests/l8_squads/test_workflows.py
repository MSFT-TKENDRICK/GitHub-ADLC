"""Structural assertions on the gh-aw workflow sources and compiled locks.

These are the tests that keep the evidence squad's sandbox honest. The sandbox
is a property of the *compiled job*, so it is asserted against the compiled job
-- not against a promise in a prompt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
AGENTS = REPO_ROOT / ".github" / "agents"

SOURCES = (
    "adlc-autoresearch",
    "adlc-intake",
    "adlc-adversarial",
    "adlc-evidence-review",
    "adlc-feature-completeness",
)
PROFILES = (
    "security-adversary",
    "performance-adversary",
    "accessibility-adversary",
    "requirements-auditor",
    "completeness-auditor",
    "grounding-auditor",
    "relevance-auditor",
)

FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n", re.DOTALL)


def frontmatter(path: Path) -> dict:
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name} has no YAML frontmatter"
    loaded = yaml.safe_load(match.group("yaml"))
    assert isinstance(loaded, dict), f"{path.name} frontmatter is not a mapping"
    # YAML 1.1 resolves a bare `on:` key to the boolean True. GitHub Actions
    # relies on the YAML 1.2 reading, so normalise it back.
    if True in loaded:
        loaded["on"] = loaded.pop(True)
    return loaded


def body_text(path: Path) -> str:
    """Workflow body with whitespace collapsed, so assertions survive re-wrapping."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def agent_job(lock: Path) -> str:
    """Return just the `agent:` job block of a compiled lock file."""
    lines = lock.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if re.fullmatch(r"  [A-Za-z_][\w-]*:", line)]
    for idx, start in enumerate(starts):
        if lines[start].strip() == "agent:":
            end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
            return "\n".join(lines[start:end])
    raise AssertionError(f"{lock.name} has no `agent` job")


@pytest.fixture(scope="module")
def evidence_fm() -> dict:
    return frontmatter(WORKFLOWS / "adlc-evidence-review.md")


@pytest.fixture(scope="module")
def evidence_agent() -> str:
    return agent_job(WORKFLOWS / "adlc-evidence-review.lock.yml")


@pytest.fixture(scope="module")
def completeness_fm() -> dict:
    return frontmatter(WORKFLOWS / "adlc-feature-completeness.md")


@pytest.fixture(scope="module")
def completeness_agent() -> str:
    return agent_job(WORKFLOWS / "adlc-feature-completeness.lock.yml")


@pytest.fixture(scope="module")
def squads() -> dict:
    path = REPO_ROOT / "templates" / ".adlc" / "squads.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SOURCES)
class TestEveryWorkflow:
    def test_source_and_lock_are_both_committed(self, name: str) -> None:
        assert (WORKFLOWS / f"{name}.md").is_file()
        assert (WORKFLOWS / f"{name}.lock.yml").is_file(), (
            f"{name}.lock.yml missing -- run `gh aw compile` and commit the result"
        )

    def test_lock_was_compiled_from_the_committed_source(self, name: str) -> None:
        lock = (WORKFLOWS / f"{name}.lock.yml").read_text(encoding="utf-8")
        assert "gh-aw-metadata" in lock.splitlines()[0]
        assert '"strict":true' in lock.splitlines()[0]

    def test_every_cost_cap_is_set(self, name: str) -> None:
        fm = frontmatter(WORKFLOWS / f"{name}.md")
        for cap in ("timeout-minutes", "max-turns", "max-ai-credits"):
            assert cap in fm, f"{name} does not cap {cap}"
            assert isinstance(fm[cap], int) and fm[cap] > 0

    def test_agent_job_holds_no_write_permission(self, name: str) -> None:
        fm = frontmatter(WORKFLOWS / f"{name}.md")
        permissions = fm.get("permissions") or {}
        assert permissions, f"{name} does not declare permissions"
        assert all(
            value == "read" for value in permissions.values()
        ), f"{name} grants a write scope to the agent job: {permissions}"

    def test_writes_go_through_safe_outputs(self, name: str) -> None:
        fm = frontmatter(WORKFLOWS / f"{name}.md")
        assert fm.get("safe-outputs"), f"{name} declares no safe-outputs"

    def test_strict_mode_and_a_bounded_network(self, name: str) -> None:
        fm = frontmatter(WORKFLOWS / f"{name}.md")
        assert fm.get("strict") is True
        allowed = (fm.get("network") or {}).get("allowed")
        assert allowed, f"{name} does not constrain the egress firewall"
        assert "*" not in allowed


class TestAutoresearch:
    def test_is_scheduled_and_manually_dispatchable(self) -> None:
        on = frontmatter(WORKFLOWS / "adlc-autoresearch.md")["on"]
        assert "schedule" in on
        assert "workflow_dispatch" in on

    def test_emits_at_most_one_capped_deduplicated_brief(self) -> None:
        create = frontmatter(WORKFLOWS / "adlc-autoresearch.md")["safe-outputs"]["create-issue"]
        assert create["max"] == 1
        assert "adlc:brief" in create["labels"]
        assert create["close-older-issues"] is True
        assert create["deduplicate-by-title"]

    def test_does_not_request_a_file_editing_tool(self) -> None:
        assert frontmatter(WORKFLOWS / "adlc-autoresearch.md")["tools"]["edit"] is False


class TestIntake:
    def test_triggers_on_issues_and_filters_to_briefs(self) -> None:
        fm = frontmatter(WORKFLOWS / "adlc-intake.md")
        assert "issues" in fm["on"]
        assert "adlc:brief" in fm["if"]

    def test_comments_the_qualification_result(self) -> None:
        add_comment = frontmatter(WORKFLOWS / "adlc-intake.md")["safe-outputs"]["add-comment"]
        assert add_comment["max"] == 1
        assert add_comment["target"] == "triggering"

    def test_categorisation_labels_are_allowlisted(self) -> None:
        labels = frontmatter(WORKFLOWS / "adlc-intake.md")["safe-outputs"]["add-labels"]
        assert "adlc:qualified" in labels["allowed"]
        assert labels["blocked"]


class TestAdversarial:
    def test_runs_on_pull_requests(self) -> None:
        assert "pull_request" in frontmatter(WORKFLOWS / "adlc-adversarial.md")["on"]

    def test_uploads_the_verdict_files_the_gate_reads(self) -> None:
        fm = frontmatter(WORKFLOWS / "adlc-adversarial.md")
        uploads = [s for s in fm["post-steps"] if "upload-artifact" in str(s.get("uses", ""))]
        assert uploads, "the adversarial squad must publish its verdict files"
        assert uploads[0]["with"]["name"] == "adlc-reviews-adversarial"

    def test_body_states_the_citation_or_discard_rule(self) -> None:
        body = body_text(WORKFLOWS / "adlc-adversarial.md")
        assert "discarded by the gate before the quorum is counted" in body

    def test_agent_job_does_check_out_because_it_reviews_code(self) -> None:
        assert "actions/checkout@" in agent_job(WORKFLOWS / "adlc-adversarial.lock.yml")


class TestEvidenceReviewSandbox:
    """The sandbox must be structural. Each assertion is a load-bearing control."""

    def test_source_disables_checkout(self, evidence_fm: dict) -> None:
        assert evidence_fm["checkout"] is False

    def test_compiled_agent_job_contains_no_checkout_step(self, evidence_agent: str) -> None:
        # The load-bearing control: there is no source tree on the runner, so
        # the reviewer cannot read the code even if it decided to try.
        assert "actions/checkout@" not in evidence_agent

    def test_no_file_editing_tool_is_requested(self, evidence_fm: dict) -> None:
        assert evidence_fm["tools"]["edit"] is False

    def test_no_web_access_is_requested(self, evidence_fm: dict) -> None:
        assert "web-fetch" not in evidence_fm["tools"]
        assert "web-search" not in evidence_fm["tools"]
        assert "playwright" not in evidence_fm["tools"]

    def test_github_access_is_read_only_issues(self, evidence_fm: dict) -> None:
        github = evidence_fm["tools"]["github"]
        assert github["toolsets"] == ["issues"], "the `repos` toolset would restore file reads"
        assert github["read-only"] is True

    def test_compiled_mcp_server_is_scoped_to_issues(self, evidence_agent: str) -> None:
        assert '"X-MCP-Toolsets": "issues"' in evidence_agent

    def test_bash_allowlist_is_trivial_and_read_only(self, evidence_fm: dict) -> None:
        allowed = evidence_fm["tools"]["bash"]
        assert len(allowed) <= 6
        commands = {entry.split()[0] for entry in allowed}
        assert commands <= {"cat", "jq", "head", "wc"}

    def test_compiled_shell_allowlist_excludes_every_egress_command(
        self, evidence_agent: str
    ) -> None:
        # Trailing marker matters: `shell(github:*)` is the MCP bridge, not git.
        for forbidden in (
            "shell(git ", "shell(git)", "shell(curl", "shell(wget",
            "shell(find", "shell(python", "shell(node", "shell(gh ",
        ):
            assert forbidden not in evidence_agent, f"{forbidden} would defeat the sandbox"

    def test_the_pack_is_fetched_by_a_deterministic_pre_step(self, evidence_fm: dict) -> None:
        # The agent never chooses its own input. A workflow step does.
        names = [str(step.get("name", "")) for step in evidence_fm["pre-steps"]]
        assert any("evidence review pack" in n for n in names)
        assert any("allowlisted" in n for n in names)

    def test_the_pre_step_rejects_non_allowlisted_pack_keys(self) -> None:
        body = (WORKFLOWS / "adlc-evidence-review.md").read_text(encoding="utf-8")
        assert "non-allowlisted top-level keys" in body
        assert "refusing to hand raw evidence to the reviewer" in body

    def test_the_pre_step_screens_the_same_leak_markers_as_the_spine(self) -> None:
        # Kept in lockstep with the spine's producer-side conformance test
        # `tests/conformance/test_pipeline.py::test_review_pack_leaks_no_raw_evidence`.
        # The two header markers are matched WITH a colon here on purpose: this
        # runs against real packs, and a security requirement whose prose says
        # "the Authorization header" must not hard-fail the workflow.
        body = (WORKFLOWS / "adlc-evidence-review.md").read_text(encoding="utf-8")
        for marker in ("'<html'", "'#!/usr/bin/env'", "'await page.'",
                       "'Set-Cookie:'", "'Authorization:'", "'\"headers\":'"):
            assert marker in body, f"consumer-side screen does not cover {marker}"

    def test_the_only_write_path_is_one_comment(self, evidence_fm: dict) -> None:
        safe = evidence_fm["safe-outputs"]
        assert safe["add-comment"]["max"] == 1
        assert "upload-artifact" not in safe
        assert "create-pull-request" not in safe

    def test_body_forbids_a_blocking_verdict(self) -> None:
        body = (WORKFLOWS / "adlc-evidence-review.md").read_text(encoding="utf-8")
        assert "Never emit `block`" in body
        assert "advisory" in body


class TestFeatureCompletenessSandbox:
    """The blocking squad's sandbox. Same shape as evidence review, higher stakes.

    This squad can fail a run, so its isolation matters more, not less: a
    reviewer that could see the implementation would grade the implementation,
    and its verdict would carry an independence it no longer has.
    """

    def test_source_disables_checkout(self, completeness_fm: dict) -> None:
        assert completeness_fm["checkout"] is False

    def test_compiled_agent_job_contains_no_checkout_step(self, completeness_agent: str) -> None:
        # The load-bearing control: no source tree on the runner, so the code is
        # absent rather than merely off-limits.
        assert "actions/checkout@" not in completeness_agent

    def test_no_file_editing_tool_is_requested(self, completeness_fm: dict) -> None:
        assert completeness_fm["tools"]["edit"] is False

    def test_no_web_access_is_requested(self, completeness_fm: dict) -> None:
        assert "web-fetch" not in completeness_fm["tools"]
        assert "web-search" not in completeness_fm["tools"]
        assert "playwright" not in completeness_fm["tools"]

    def test_github_access_is_read_only_issues(self, completeness_fm: dict) -> None:
        github = completeness_fm["tools"]["github"]
        assert github["toolsets"] == ["issues"], "the `repos` toolset would restore file reads"
        assert github["read-only"] is True

    def test_compiled_mcp_server_is_scoped_to_issues(self, completeness_agent: str) -> None:
        assert '"X-MCP-Toolsets": "issues"' in completeness_agent

    def test_bash_allowlist_is_trivial_and_read_only(self, completeness_fm: dict) -> None:
        allowed = completeness_fm["tools"]["bash"]
        assert len(allowed) <= 6
        commands = {entry.split()[0] for entry in allowed}
        assert commands <= {"cat", "jq", "head", "wc"}

    def test_compiled_shell_allowlist_excludes_every_egress_command(
        self, completeness_agent: str
    ) -> None:
        for forbidden in (
            "shell(git ", "shell(git)", "shell(curl", "shell(wget",
            "shell(find", "shell(python", "shell(node", "shell(gh ",
        ):
            assert forbidden not in completeness_agent, f"{forbidden} would defeat the sandbox"

    def test_the_pack_is_fetched_by_a_deterministic_pre_step(self, completeness_fm: dict) -> None:
        names = [str(step.get("name", "")) for step in completeness_fm["pre-steps"]]
        assert any("completeness pack" in n for n in names)
        assert any("allowlisted" in n for n in names)

    def test_the_pre_step_rejects_non_allowlisted_pack_keys(self) -> None:
        body = (WORKFLOWS / "adlc-feature-completeness.md").read_text(encoding="utf-8")
        assert "non-allowlisted top-level keys" in body
        assert "refusing to hand code or raw evidence to the reviewer" in body

    def test_the_pre_step_screens_code_as_well_as_raw_evidence(self) -> None:
        # Kept in lockstep with the spine's producer-side screen,
        # `adlc.stages.complete.LEAK_MARKERS`. The diff markers are the addition
        # that matters here: this reviewer must never see the implementation.
        body = (WORKFLOWS / "adlc-feature-completeness.md").read_text(encoding="utf-8")
        for marker in ("'diff --git'", "'@@ -'", "'<html'", "'#!/usr/bin/env'",
                       "'await page.'", "'Set-Cookie:'", "'Authorization:'"):
            assert marker in body, f"consumer-side screen does not cover {marker}"

    def test_the_pack_must_declare_its_own_exclusions(self) -> None:
        # A reviewer that is not told what it cannot see will guess instead of
        # saying "I cannot judge that from here".
        body = (WORKFLOWS / "adlc-feature-completeness.md").read_text(encoding="utf-8")
        assert "pack declares no exclusions" in body

    def test_the_only_write_path_is_one_comment_per_member(self, completeness_fm: dict) -> None:
        safe = completeness_fm["safe-outputs"]
        assert safe["add-comment"]["max"] == 3
        assert "upload-artifact" not in safe
        assert "create-pull-request" not in safe

    def test_body_permits_a_blocking_verdict_and_names_the_outer_loop(self) -> None:
        # The deliberate inversion of the evidence-review contract: nothing
        # deterministic sits underneath this gate, so an advisory verdict would
        # make it a comment rather than a gate.
        body = (WORKFLOWS / "adlc-feature-completeness.md").read_text(encoding="utf-8")
        assert "You may emit `block`" in body
        assert "outer loop" in body

    def test_body_names_all_three_member_lenses(self) -> None:
        body = (WORKFLOWS / "adlc-feature-completeness.md").read_text(encoding="utf-8")
        for member in ("completeness-auditor", "grounding-auditor", "relevance-auditor"):
            assert member in body


@pytest.mark.parametrize("name", PROFILES)
class TestAgentProfiles:
    def test_profile_exists_with_the_required_frontmatter(self, name: str) -> None:
        fm = frontmatter(AGENTS / f"{name}.agent.md")
        assert fm["name"] == name
        assert fm["description"].strip()
        assert fm["model"]
        assert fm["tools"]

    def test_profile_states_the_citation_or_discard_rule(self, name: str) -> None:
        body = (AGENTS / f"{name}.agent.md").read_text(encoding="utf-8")
        assert "discarded" in body.lower()

    def test_profile_declares_a_verdict_contract(self, name: str) -> None:
        body = (AGENTS / f"{name}.agent.md").read_text(encoding="utf-8")
        assert "verdict:" in body
        assert "abstain" in body


class TestSquadsTemplate:
    def test_declares_every_squad(self, squads: dict) -> None:
        assert set(squads["squads"]) == {
            "adversarial_review", "evidence_review", "feature_completeness",
        }

    def test_every_member_has_a_committed_agent_profile(self, squads: dict) -> None:
        for squad in squads["squads"].values():
            for member in squad["members"]:
                assert (REPO_ROOT / member["agent"]).is_file(), member

    def test_adversarial_blocks_and_evidence_squad_does_not(self, squads: dict) -> None:
        assert squads["squads"]["adversarial_review"]["blocking"] is True
        # The LLM half of evidence review is advisory; the deterministic
        # coverage check is what blocks.
        assert squads["squads"]["evidence_review"]["blocking"] is False

    def test_feature_completeness_blocks_and_routes_to_the_outer_loop(self, squads: dict) -> None:
        # Nothing deterministic sits underneath this one, so an advisory verdict
        # would make it a comment rather than a gate.
        squad = squads["squads"]["feature_completeness"]
        assert squad["blocking"] is True
        assert squad["routesTo"] == "outer"

    def test_citation_kinds_match_what_the_gates_parse(self, squads: dict) -> None:
        assert squads["squads"]["adversarial_review"]["citation"] == "file-line"
        assert squads["squads"]["evidence_review"]["citation"] == "artifact-sha256"
        # Code-blind squads can only cite artifacts; a file:line citation would
        # require source they are structurally denied.
        assert squads["squads"]["feature_completeness"]["citation"] == "artifact-sha256"

    def test_coverage_rules_are_declared(self, squads: dict) -> None:
        coverage = squads["squads"]["evidence_review"]["coverage"]
        assert coverage["minArtifactsPerRequirement"] >= 1
        assert coverage["requireShaMatch"] is True
        assert coverage["requireHashVerification"] is True
