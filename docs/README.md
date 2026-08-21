# ADLC documentation

This directory contains the focused guides for using, extending, and operating
ADLC. The root [`README.md`](../README.md) is the short onboarding path.

## Architecture and lifecycle

- [`PLAN.md`](PLAN.md) — canonical architecture, frozen artifact contracts,
  trust boundaries, repository layout, acceptance profiles, and risks.
- [`day2-operations.md`](day2-operations.md) — incident intake, hotfixes,
  Azure SRE Agent integration, and the production feedback loop.
- [`enrichment.md`](enrichment.md) — generated diagrams, personas, wireframes,
  benchmarks, and deterministic enrichment behavior.
- [`experiments.md`](experiments.md) — feature flags, comparative runs, and
  conditional Open Experiment Specification export.

## Integration guides

- [`adapters/agents.md`](adapters/agents.md) — agent runner selection,
  isolation, write sets, and optional Copilot/GitHub runners.
- [`governance.md`](governance.md) — Microsoft Agent Framework middleware and
  Agent Governance Toolkit policy enforcement.
- [`evals.md`](evals.md) — deterministic rubric evaluation plus ASSERT,
  promptfoo, and Azure evaluation adapters.
- [`evidence.md`](evidence.md) — local, Playwright, Lighthouse, k6, and axe
  collectors and their normalized artifacts.
- [`security-gates.md`](security-gates.md) — CodeQL, Code Quality,
  dependency-review, timeouts, and required permissions.
- [`squads.md`](squads.md) — gh-aw reviewer squads, sanitized evidence review,
  citations, quorum, and safe outputs.
- [`taskstore.md`](taskstore.md) — SQLite defaults and GitHub Issues,
  sub-issues, and Projects integration.

## How to use these guides

Start with the root README and `PLAN.md`, then open the guide for the seam you
are enabling. Each integration guide should answer:

1. What the adapter does and what it does not claim to do.
2. How `detect()` and selection behave.
3. Which configuration, permissions, or external tools are required.
4. What artifacts and failure states are produced.
5. Which tests cover the contract.

Preview products and disabled examples are intentionally labeled. If the code
and a guide disagree, treat the implementation and tests as the immediate
source of truth and update the guide in the same change.
