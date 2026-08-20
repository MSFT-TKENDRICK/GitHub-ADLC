# ADLC — Agentic Development Lifecycle

A reusable framework that runs a **governed, evidence-producing agentic SDLC** in
any GitHub repository. Assembled entirely from existing GitHub, Microsoft, Azure
and CNCF products — nothing invented.

A brief goes in. A specced, decomposed, agent-built, evidence-backed, gated
change comes out, with every decision recorded as an auditable ADR.

```
brief → qualify → spec → enrich → task graph → parallel build → evidence
      → evals → gates → interactive report → native PR review → ADR → merge
```

## Why this exists

Most "AI in the SDLC" tooling produces changes. The hard part is producing
changes you can *trust*: knowing what was promised, what was built, what was
measured, what was checked, who decided, and being able to replay all of it six
months later. ADLC is a thin layer that makes that the default.

## Install

Three ways, all supported.

**1. Reusable workflow** — the normal path. One pinned caller in your repo:

```yaml
# .github/workflows/adlc.yml
name: ADLC
on: [pull_request]
jobs:
  adlc:
    uses: MSFT-TKENDRICK/GitHub-ADLC/.github/workflows/adlc.yml@v0
    with:
      profile: minimal
```

**2. CLI** — vendors that caller plus namespaced config, and nothing else:

```bash
uv tool install git+https://github.com/MSFT-TKENDRICK/GitHub-ADLC@v0
adlc init
```

**3. Side-load** (dotfiles / Codespaces) — installs the CLI and runs `adlc init`
against the repo it finds itself in:

```bash
curl -fsSL https://raw.githubusercontent.com/MSFT-TKENDRICK/GitHub-ADLC/v0/bootstrap.sh | bash
```

`adlc init` never copies the framework into your repo and never touches your
existing CI. Upgrading is changing one pinned ref.

## Use

```bash
adlc doctor                              # what is available here, and why not
adlc run new --brief docs/idea.md        # or --issue 42
adlc qualify latest                      # deterministic readiness score
adlc spec latest                         # GitHub Spec Kit, or built-in templates
adlc enrich latest                       # gherkin, rubric, benchmarks, diagrams
adlc graph latest                        # tasks.md → parallel DAG with context
adlc build latest --max-parallel 4       # isolated worktrees, patch barriers
adlc evidence latest --variant candidate-a
adlc eval latest                         # rubric score
adlc gate latest                         # fail-closed aggregate
adlc report latest --open                # self-contained interactive HTML
```

Everything above runs **offline, free, with no credentials**. Real agents, real
security scanning and real cloud services are opt-in adapters.

## Design

Four decisions carry the weight.

### 1. Immutable stage results, one reducer

Every stage writes a *new* `runs/<run>/stages/<stage>.<attempt>.json`. Only
`adlc reduce` folds them into `run.json`. Nothing else ever writes it.

This is not fastidiousness — GitHub Actions jobs share no filesystem and run
concurrently, so any design that appends to one canonical document loses writes.
Re-runs append `attempt: n+1`, so history is append-only and auditable.

### 2. Fail closed

A gate the profile marks `required` that returns `fail` **or** `not_run` fails
the build. "We could not check" never renders as "it is fine". A single
`ADLC / required` check is the branch-protection target.

### 3. Bounded context capsules

Each task node carries the context its agent needs — but capped at 64 KiB total,
8 KiB per file, 12 files, with blob SHAs and line ranges rather than whole-file
dumps. Capsules are regenerated at every level barrier, and a stale blob SHA
fails the node instead of letting an agent edit against content that has since
changed. Inlining is a cache, never the source of truth.

### 4. The reviewing agent never sees raw evidence

Traces, HAR files, console logs and replay scripts leak source code and are
attacker-controlled — a perfect prompt-injection vector. The evidence-review
agent receives only `evidence-review-pack.json`: requirement ids, normalised
measurements, coverage claims and artifact hashes. The **blocking** check is
deterministic (every requirement backed by a hash-verified artifact); the LLM
verdict is advisory and must cite artifact hashes or it is discarded.

## Task isolation

Nodes at the same topological level run concurrently, each in its own git
worktree at an exact base SHA, and each emits a patch anchored to that SHA.

* Overlapping write-sets at the same level are a **compile-time graph error**,
  caught before any agent runs rather than discovered at merge time.
* At each level barrier patches are applied in id order, tests run, a commit is
  made, and `baseSha` advances.
* Patches touching `.github/**`, `.adlc/**`, `schemas/**` or `docs/decisions/**`
  are rejected — agent-authored code cannot rewrite its own gates.

## Decisions

Human decisions are native GitHub PR reviews. There is no bespoke command
protocol to learn or to abuse.

| Review | Effect |
|---|---|
| Approve | `ship`; ADR accepted, bound to the review's commit SHA |
| Request changes | ADR rejected; a **new** run is created with `referencesRun` |
| Comment | Annotations carried into the successor run's brief |

A review of a stale commit is refused. Revisions never mutate a prior run, so
the audit trail only ever grows.

## Adapters

Every seam has a built-in credential-free default. Everything else is a pure
addition discovered through entry points, and reported honestly by `adlc doctor`.

| Seam | Default (always works) | Opt-in |
|---|---|---|
| Agent runner | `fake` (deterministic) | Copilot SDK, Agent Tasks API, gh-aw, MAF |
| Task store | SQLite | GitHub Issues + sub-issues + Projects |
| Evals | deterministic rubric | ASSERT, promptfoo, Azure AI Evaluation |
| Evidence | `local` | Playwright, Lighthouse CI, k6, axe |
| Flags | flagd file provider | LaunchDarkly via OpenFeature |
| Telemetry | OTel JSONL | Application Insights |
| Gates | tests, secrets, deps, evidence | CodeQL, Code Quality, governance, squads |

`agents` and `taskstore` **never** auto-escalate: they spend money and write to
live repositories, so switching them is always a deliberate choice in
`.adlc/config.yaml`. On a GitHub Actions runner an ambient `GITHUB_TOKEN` would
otherwise silently opt you in.

## Standards

- **[GitHub Spec Kit](https://github.com/github/spec-kit)** — specification and task decomposition
- **[GitHub Agentic Workflows](https://github.com/github/gh-aw)** — event-driven agents with least-privilege safe outputs
- **[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)** — governed agent invocation
- **[Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit)** — deterministic tool-call policy
- **[ASSERT](https://github.com/responsibleai/ASSERT)** — specification-driven evaluation
- **[OpenFeature](https://openfeature.dev) / [flagd](https://flagd.dev)** — vendor-neutral feature flags
- **[Open Experiment Specification](https://www.openexperiment.org/)** — experiment export format
- **[MADR v4](https://adr.github.io/madr/)** — architecture decision records
- **[OpenTelemetry](https://opentelemetry.io/)** — GenAI and feature-flag semantic conventions

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The architecture and its rationale,
including the adversarial critique that reshaped it, are in
[docs/PLAN.md](docs/PLAN.md).

```bash
PYTHONPATH=src python -m pytest tests/conformance -q
```

The conformance suite runs with no credentials and no optional tooling. It
covers the happy path and — more importantly — the refusals: a required gate
that did not run fails the build, write-set conflicts are rejected at graph
time, stale capsules fail their node, protected paths cannot be written, and the
review pack leaks no raw evidence.