# ADLC — Agentic Development Lifecycle

ADLC is a reusable Python framework for running a governed, evidence-producing
agentic software development lifecycle in a GitHub repository. It turns a
brief into a specified, decomposed, tested, measured, and reviewable change:

```text
brief -> qualify -> spec -> enrich -> graph -> build -> evidence
      -> eval -> gates -> completeness review -> report -> PR review -> ADR
```

The framework is deliberately small and composable. It has credential-free
defaults for local development and optional adapters for GitHub, Copilot,
Azure, governance, evaluation, feature flags, and richer evidence collection.

## Start here

| Goal | Read |
| --- | --- |
| Install ADLC in another repository | [Installation](#installation) |
| Run the complete local pipeline | [Quick start](#quick-start) |
| Understand the architecture and invariants | [`docs/PLAN.md`](docs/PLAN.md) |
| Configure adapters and gates | [Configuration](#configuration) and [`docs/`](docs/) |
| Add an adapter or stage | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Operate incidents and hotfixes | [`docs/day2-operations.md`](docs/day2-operations.md) |

## Requirements

- Python 3.11 or newer
- Git
- A Git repository for `adlc init` and pipeline runs
- `pytest` and `ruff` for development (install with `.[dev]`)

Optional integrations are detected by `adlc doctor`; missing optional tools do
not prevent the credential-free path from running.

## Installation

### Reusable GitHub Actions workflow

This is the normal cross-repository integration. Add a thin caller workflow
that pins ADLC to a tag or commit:

```yaml
name: ADLC

on:
  pull_request:

jobs:
  adlc:
    uses: MSFT-TKENDRICK/GitHub-ADLC/.github/workflows/adlc.yml@v0
    with:
      profile: minimal
```

Use a commit SHA instead of `v0` when your repository requires immutable
third-party references. The reusable workflow checks out the caller repository,
installs ADLC, runs the pipeline, and uploads the run directory as an artifact.

### CLI

Install from the repository with `uv` or `pip`:

```bash
uv tool install git+https://github.com/MSFT-TKENDRICK/GitHub-ADLC@v0
# or:
python -m pip install "git+https://github.com/MSFT-TKENDRICK/GitHub-ADLC@v0"

adlc init
```

`adlc init` writes only the ADLC caller workflow, `.adlc/config.yaml`,
`.adlc/policy.yaml`, `.adlc/squads.yaml`, and the `.adlc/runs/` gitignore entry.
It does not copy the framework or overwrite existing files unless `--force` is
provided.

### Side-load for Codespaces or dotfiles

```bash
curl -fsSL https://raw.githubusercontent.com/MSFT-TKENDRICK/GitHub-ADLC/v0/bootstrap.sh | bash
```

The script installs the CLI and runs `adlc init` in the current repository. Set
`ADLC_REF`, `ADLC_REPO`, `ADLC_TARGET`, or `ADLC_PROFILE` to customize it.

## Quick start

From a repository containing a brief such as
[`examples/briefs/dark-mode.md`](examples/briefs/dark-mode.md):

```bash
adlc doctor
adlc run new --brief examples/briefs/dark-mode.md
adlc qualify latest
adlc spec latest
adlc enrich latest
adlc graph latest
adlc build latest --runner fake --max-parallel 4
adlc evidence latest --variant candidate-a
adlc eval latest
adlc gate latest
adlc reduce latest
adlc personas latest
adlc complete latest
adlc report latest --open
adlc validate latest
```

Every command accepts a run id; `latest` resolves to the newest run. Commands
that emit structured output also support `--json`. A failed required gate
returns a non-zero exit code.

For automation, capture the run id without parsing human output:

```bash
RUN_ID=$(adlc run new --brief examples/briefs/dark-mode.md --json \
  | python -c "import json,sys; print(json.load(sys.stdin)['runId'])")
adlc qualify "$RUN_ID" && adlc spec "$RUN_ID" && adlc enrich "$RUN_ID"
adlc graph "$RUN_ID" && adlc build "$RUN_ID" --runner fake
adlc evidence "$RUN_ID" --variant candidate-a
adlc eval "$RUN_ID" && adlc gate "$RUN_ID"
adlc reduce "$RUN_ID" && adlc personas "$RUN_ID" && adlc complete "$RUN_ID"
adlc report "$RUN_ID" && adlc validate "$RUN_ID"
```

## CLI reference

| Command | Purpose |
| --- | --- |
| `adlc init` | Install the thin workflow and repository configuration |
| `adlc doctor` | Detect available adapters and record `capabilities.json` |
| `adlc run new --brief FILE` | Create a run from Markdown |
| `adlc run new --issue NUMBER` | Create a run from a GitHub issue |
| `adlc run list` | List local runs and their gate status |
| `adlc qualify`, `spec`, `enrich`, `graph` | Prepare the run and compile its task graph |
| `adlc build` | Execute graph levels with isolated worktrees and patch barriers |
| `adlc evidence` | Collect variant evidence and create the sanitized review pack |
| `adlc eval` | Score the candidate against its rubric |
| `adlc gate` | Run selected gates and enforce fail-closed aggregation |
| `adlc reduce` | Fold immutable stage results into canonical `run.json` |
| `adlc personas` | Walk the scenarios as each persona and record the reasoning as evidence |
| `adlc complete` | Build the code-blind completeness pack and run the feature-completeness gate |
| `adlc report` | Render a standalone HTML report |
| `adlc validate` | Validate run artifacts against JSON Schemas |
| `adlc adr new/list/set-status` | Create and manage MADR decision records |
| `adlc review apply` | Apply a native GitHub pull-request review event |
| `adlc export oes` | Export genuinely comparative runs to OES |
| `adlc autoresearch` | Propose the next brief from repository history |
| `adlc hotfix --incident FILE` | Run the day-2 incident-to-hotfix path |

Run `adlc --help` or `adlc COMMAND --help` for the complete option list.

## Configuration

`adlc` searches upward for the repository root and loads `.adlc/config.yaml`.
The checked-in example at [`.adlc/config.yaml`](.adlc/config.yaml) is also the
configuration used to dogfood this repository.

```yaml
version: 1
profile: minimal
commands:
  test: "python -m pytest tests/conformance -q"
  lint: "ruff check src/"
limits:
  maxParallel: 4
  maxInnerIterations: 2
  maxOuterIterations: 1
  maxTurns: 200
  maxAiCredits: 500
gates:
  required: null
  depsMaxSeverity: high
qualify:
  minScore: 50
eval:
  threshold: 0.7
```

`minimal` requires local tests, secrets, dependency, and evidence-completeness
gates. `full` additionally requires the optional security, quality, evaluation,
governance, and reviewer-squad gates. Required gates that are unavailable return
`not_run` and fail the aggregate; ADLC never treats an unverified check as
green.

Select an optional adapter explicitly:

```yaml
adapters:
  agents: copilot-sdk
  evals: assert-ai
  taskstore: github
```

Agent runners and task stores are intentionally explicit-only because they can
spend money or write to live GitHub resources. See the adapter guides in
[`docs/`](docs/) for prerequisites and failure semantics.

## Architecture and safety model

- **Immutable stages:** stages write
  `.adlc/runs/<run>/stages/<stage>.<attempt>.json`; `adlc reduce` is the only
  writer of canonical `run.json`.
- **Isolated builds:** graph nodes run in worktrees at an exact base SHA and
  produce patches restricted to their declared write sets.
- **Bounded context:** task capsules use file limits, line ranges, and blob
  SHAs so agents do not receive unbounded or stale source.
- **Fail-closed gates:** `required + not_run` is a failure, and one aggregate
  check can be used for branch protection.
- **Sanitized evidence review:** the evidence reviewer receives hashes,
  measurements, and coverage claims, not raw traces, HAR files, or console
  logs.
- **Code-blind completeness review:** every other gate checks the *change*;
  `feature_completeness` checks the *run*. A squad that has seen the brief and
  the evidence and nothing else -- no code, no diffs, no agent sessions, no
  chains of thought -- decides whether the evidence demonstrates what was asked
  for. The isolation is structural (`checkout: false`, no repository toolset, a
  pack built by allowlist that is refused if a leak marker survives), and a
  failure routes back to the **outer** loop, because if the evidence does not
  answer the brief then patching the code is guessing.
- **Native decisions:** GitHub pull-request reviews become the human decision;
  revisions create new runs instead of mutating history.

The canonical artifact and permission contracts are documented in
[`docs/PLAN.md`](docs/PLAN.md). The run directory is intentionally local and
should not be committed; CI uploads it as an artifact.

## Repository layout

```text
src/adlc/          Python package: CLI, stages, adapters, ports, schemas
tests/             conformance, integration, and adapter tests
docs/              architecture and adapter guides
schemas/           versioned JSON Schemas for run artifacts
templates/         files used by adlc init
examples/          briefs and optional Azure integration examples
.github/workflows/ reusable workflow and gh-aw sources/locks
.adlc/             this repository's dogfooding configuration
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/conformance -q
ruff check src/
```

The conformance suite is credentialless. Optional adapter tests should describe
their unavailable path and skip or degrade with a specific reason when the
external service is not configured.

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before changing ports, schemas,
entry-point registrations, or protected paths.

## Documentation

[`docs/README.md`](docs/README.md) is the documentation index. Start with the
architecture plan, then use the focused adapter guides for the integration you
are enabling. Documentation changes should keep commands, paths, profiles,
and claims aligned with the implementation and should label preview or
example-only integrations clearly.

## License

MIT. See the project metadata in [`pyproject.toml`](pyproject.toml).
