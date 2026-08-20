"""The shipped AGT policy template.

Structural assertions only — we cannot execute AGT's condition language here,
so these tests pin the *contract* the policy is supposed to express: the
framework's protected paths are covered, destructive actions are denied, and
network egress is a closed allowlist rather than an open door.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from adlc.maf.middleware import TEMPLATE_POLICY, resolve_policy_path
from adlc.ports import PROTECTED_PATHS

BLOCKING_ACTIONS = {"deny", "require_approval", "escalate", "block"}


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    assert TEMPLATE_POLICY.is_file(), f"missing {TEMPLATE_POLICY}"
    return yaml.safe_load(TEMPLATE_POLICY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return policy["rules"]


def rule(rules: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for candidate in rules:
        if candidate.get("name") == name:
            return candidate
    raise AssertionError(f"policy has no rule named {name!r}")


class TestSchema:
    def test_parses_as_yaml(self, policy: dict[str, Any]) -> None:
        assert isinstance(policy, dict)

    def test_declares_the_agt_api_version(self, policy: dict[str, Any]) -> None:
        assert policy["apiVersion"] == "governance.toolkit/v1"

    def test_is_named(self, policy: dict[str, Any]) -> None:
        assert policy["name"] == "adlc-default"

    def test_has_a_default_action(self, policy: dict[str, Any]) -> None:
        assert policy["default_action"] in {"allow", "deny"}

    def test_every_rule_is_well_formed(self, rules: list[dict[str, Any]]) -> None:
        assert rules, "an empty rule list would be a rubber stamp"
        for candidate in rules:
            assert candidate["name"], candidate
            assert candidate["condition"].strip(), candidate
            assert candidate["action"] in {"allow", "deny", "warn", "require_approval", "transform"}
            assert candidate.get("description", "").strip(), (
                f"rule {candidate['name']!r} needs a description — "
                "it is surfaced to humans in the denial message"
            )

    def test_rule_names_are_unique(self, rules: list[dict[str, Any]]) -> None:
        names = [candidate["name"] for candidate in rules]
        assert len(names) == len(set(names))

    def test_approval_rules_name_approvers(self, rules: list[dict[str, Any]]) -> None:
        for candidate in rules:
            if candidate["action"] == "require_approval":
                assert candidate.get("approvers"), candidate["name"]


class TestDestructiveOperations:
    def test_destructive_verbs_are_denied(self, rules: list[dict[str, Any]]) -> None:
        block = rule(rules, "block-destructive")
        assert block["action"] == "deny"
        for verb in ("drop", "delete", "truncate"):
            assert f"'{verb}'" in block["condition"]

    def test_destructive_shell_is_denied(self, rules: list[dict[str, Any]]) -> None:
        block = rule(rules, "block-destructive-shell")
        assert block["action"] == "deny"
        assert "rm" in block["condition"]
        assert "sudo" in block["condition"]

    def test_history_rewrite_is_denied(self, rules: list[dict[str, Any]]) -> None:
        """Audit history is append-only (PLAN §1 idea 1)."""
        block = rule(rules, "block-history-rewrite")
        assert block["action"] == "deny"
        assert "--force" in block["condition"]

    def test_credential_files_are_denied(self, rules: list[dict[str, Any]]) -> None:
        block = rule(rules, "block-secret-exfiltration")
        assert block["action"] == "deny"
        for marker in (".env", "id_rsa", ".ssh/"):
            assert marker in block["condition"]


class TestProtectedPaths:
    def test_every_framework_protected_path_is_covered(
        self, rules: list[dict[str, Any]]
    ) -> None:
        """Mirrors ``adlc.ports.PROTECTED_PATHS`` — drift here is a real gap."""
        guard = rule(rules, "require-approval-protected-paths")
        condition = guard["condition"]
        for pattern in PROTECTED_PATHS:
            stem = pattern.replace("/**", "/").replace(".", r"\.")
            assert stem in condition, f"{pattern} is unguarded in the policy"

    def test_protected_paths_are_not_merely_warned_about(
        self, rules: list[dict[str, Any]]
    ) -> None:
        assert rule(rules, "require-approval-protected-paths")["action"] in BLOCKING_ACTIONS

    def test_writes_outside_the_write_set_are_denied(
        self, rules: list[dict[str, Any]]
    ) -> None:
        guard = rule(rules, "deny-write-outside-write-set")
        assert guard["action"] == "deny"
        assert "write_set" in guard["condition"]


class TestNetworkEgress:
    def test_allowlist_precedes_the_catch_all_deny(
        self, rules: list[dict[str, Any]]
    ) -> None:
        """First match wins, so the deny must come after the allowlist."""
        names = [candidate["name"] for candidate in rules]
        assert names.index("allow-network-allowlist") < names.index("block-network-egress")

    def test_egress_is_denied_by_default(self, rules: list[dict[str, Any]]) -> None:
        assert rule(rules, "block-network-egress")["action"] == "deny"

    def test_allowlist_is_a_closed_set_of_known_hosts(
        self, rules: list[dict[str, Any]]
    ) -> None:
        condition = rule(rules, "allow-network-allowlist")["condition"]
        assert "*" not in condition, "a wildcard host is not an allowlist"
        for host in ("api.github.com", "pypi.org", "registry.npmjs.org"):
            assert host in condition

    def test_covers_the_common_egress_verbs(self, rules: list[dict[str, Any]]) -> None:
        condition = rule(rules, "block-network-egress")["condition"]
        for verb in ("http_request", "fetch", "network"):
            assert verb in condition


class TestDelegation:
    def test_spawning_agents_needs_approval(self, rules: list[dict[str, Any]]) -> None:
        """Bounded loops are a framework invariant (PLAN §3)."""
        guard = rule(rules, "require-approval-subagent-spawn")
        assert guard["action"] in BLOCKING_ACTIONS
        assert "spawn_agent" in guard["condition"]


class TestAudit:
    def test_audit_is_enabled(self, policy: dict[str, Any]) -> None:
        assert policy["audit"]["enabled"] is True

    def test_arguments_are_not_logged(self, policy: dict[str, Any]) -> None:
        """Tool arguments can carry source and secrets; keep them out of the log."""
        assert policy["audit"]["include_arguments"] is False


class TestResolution:
    def test_template_is_the_last_resort(self, bare_cfg) -> None:
        assert resolve_policy_path(bare_cfg) == TEMPLATE_POLICY

    def test_repo_policy_wins_over_the_template(self, cfg) -> None:
        assert resolve_policy_path(cfg) == cfg.adlc_dir / "policy.yaml"

    def test_env_override_wins_over_everything(self, monkeypatch, cfg, tmp_path) -> None:
        override = tmp_path / "custom.yaml"
        override.write_text("apiVersion: governance.toolkit/v1\n", encoding="utf-8")
        monkeypatch.setenv("ADLC_POLICY", str(override))
        assert resolve_policy_path(cfg) == override

    def test_missing_override_falls_through(self, monkeypatch, cfg, tmp_path) -> None:
        monkeypatch.setenv("ADLC_POLICY", str(tmp_path / "absent.yaml"))
        assert resolve_policy_path(cfg) == cfg.adlc_dir / "policy.yaml"
