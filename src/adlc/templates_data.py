"""Template payloads vendored by ``adlc init``.

Deliberately tiny. ``adlc init`` installs one pinned caller workflow plus
namespaced config -- it never copies the framework and never touches existing
CI, so upgrading is changing a single ref.
"""

from __future__ import annotations

CONFIG_YAML = """# ADLC configuration
# Installed by adlc {version}. Docs: https://github.com/MSFT-TKENDRICK/GitHub-ADLC
version: 1
profile: {profile}          # minimal | full

# Commands ADLC runs on your behalf. `test` is required by the `tests` gate --
# leaving it blank makes that gate report not_run, which fails a required gate.
commands:
  test: ""
  lint: ""
  build: ""

# Adapter overrides. Omit to use capability detection, which falls back to the
# built-in credential-free defaults.
adapters: {{}}
  # agents: copilot-sdk
  # taskstore: github
  # evals: assert-ai

limits:
  maxParallel: 4
  maxInnerIterations: 2
  maxOuterIterations: 1
  maxTurns: 200
  maxAiCredits: 500

gates:
  # Omit `required` to use the profile default. Listing it here overrides that.
  required: null
  depsMaxSeverity: high

qualify:
  minScore: 50

eval:
  threshold: 0.7
"""

POLICY_YAML = """# Agent Governance Toolkit policy for ADLC.
# Enforced deterministically in application code before a tool call reaches the
# wire -- not by asking the model to behave.
apiVersion: governance.toolkit/v1
name: adlc-default
default_action: allow

rules:
  - name: block-destructive
    condition: "action.type in ['drop', 'delete', 'truncate', 'force_push']"
    action: deny
    description: "Destructive operations require a human."

  - name: protect-framework-paths
    condition: >-
      action.type == 'write_file' and (
        action.path.startswith('.github/') or
        action.path.startswith('.adlc/') or
        action.path.startswith('schemas/') or
        action.path.startswith('docs/decisions/')
      )
    action: deny
    description: "Agent-authored patches must not rewrite CI, config, schemas or ADRs."

  - name: require-approval-for-external-network
    condition: "action.type == 'http_request' and not action.host.endswith('github.com')"
    action: require_approval
    approvers: ["repository-maintainers"]
"""

SQUADS_YAML = """# Reviewer squads. Each member is a .github/agents/<name>.agent.md profile.
squads:
  adversarial:
    description: "Adversarial review of the CODE."
    blocking: true
    quorum: "2/3"
    members:
      - agent: security-adversary
      - agent: performance-adversary
      - agent: accessibility-adversary

  evidence:
    description: >-
      Reviews EVIDENCE against requirements without code access. Sandboxed
      structurally: no checkout, no file editing, issues toolset only, and its
      sole input is the sanitised evidence-review-pack.json.
    blocking: true
    quorum: "1/1"
    members:
      - agent: requirements-auditor

# A verdict that cites no artifactSha256 from the review pack is discarded.
rules:
  requireArtifactCitation: true
"""

CALLER_WORKFLOW = """# ADLC - thin caller. Pinned so upgrades are a one-line change.
# The reusable workflow lives in the framework repo; this file stays tiny.
name: ADLC

on:
  pull_request:
  workflow_dispatch:
    inputs:
      brief:
        description: "Path to a brief markdown file"
        required: false
        type: string

permissions:
  contents: read

concurrency:
  group: adlc-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  adlc:
    uses: MSFT-TKENDRICK/GitHub-ADLC/.github/workflows/adlc.yml@{ref}
    with:
      profile: minimal
      brief: ${{{{ inputs.brief }}}}
    permissions:
      contents: read
      pull-requests: write
      checks: write
      security-events: read
"""
