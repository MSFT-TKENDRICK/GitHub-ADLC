# Agent runners (`adlc.agents`)

An `AgentRunner` executes **one task node** from `taskgraph.json` and hands back
a patch. The contract is frozen in [`adlc.ports`](../../src/adlc/ports.py) and
described in [`docs/PLAN.md`](../PLAN.md) §4.4:

```python
async def run_task(self, node: TaskNode, worktree: Path, cfg: Config) -> TaskOutcome
```

Every runner in this document guarantees the same four things:

1. the task executes against an **isolated git worktree** checked out at `baseSha`;
2. the output is `patches/<task-id>.patch`, a unified diff **anchored to that
   exact base SHA** (proven in the tests by `git apply --check` after a hard
   reset to the base);
3. a write outside `node["writeSet"]` is **refused** — the edit is reverted and
   the task fails; `PROTECTED_PATHS` (`.github/**`, `.adlc/**`, `schemas/**`,
   `docs/decisions/**`, `pyproject.toml`) can never be written even if a graph
   mistakenly declares one;
4. `tokensIn` / `tokensOut` / `cost` are reported **only when the backend
   actually reports them**. An unreported figure is absent from `TaskOutcome`,
   never a fabricated zero.

| Runner | Entry point | Module | Where the work happens | Status |
|---|---|---|---|---|
| `CopilotSdkRunner` | `copilot-sdk` | `adapters/agents/copilot_sdk.py` | Locally, in the worktree | SDK is GA; needs a Copilot entitlement |
| `AgentTaskRunner` | `agent-task` | `adapters/agents/agent_task.py` | GitHub cloud agent, remote branch | **Public preview** |
| `GhAwRunner` | `gh-aw` | `adapters/agents/gh_aw.py` | GitHub Actions, via an agentic workflow | gh-aw is GA; the workflow is yours to write |

The spine default is `fake` — deterministic, credential-free, and what the
conformance suite (§8.1) runs. **None of the runners below is on the
credential-free path**; when unavailable they return
`detect() -> (False, "<specific reason>")` and the framework carries on with
`fake`.

---

## Selection: these runners are explicit-only

`adlc.config.select_adapter` resolves an agent runner in this order:

1. an explicit override — `adlc build RUN --runner copilot-sdk`, or
   `adapters: {agents: …}` in `.adlc/config.yaml`. **`detect()` is not consulted
   for an explicit override**, so you can always force a runner;
2. otherwise the spine default, `fake`.

`agents` is in `adlc.config.EXPLICIT_ONLY_KINDS`, so unlike observational
adapters (evidence, evals, telemetry, flags, gates, export) an agent runner is
**never** auto-selected just because `detect()` succeeds. That matters because
`GITHUB_TOKEN` and `GITHUB_REPOSITORY` are present on every GitHub Actions
runner, so ambient auto-selection would silently switch a plain `adlc build`
onto a paid cloud agent that opens pull requests.

`detect()` therefore answers "**could** this run here?", which is what
`capabilities.json` reports — not "should this be used". Turning one on is
always a deliberate act:

```yaml
# .adlc/config.yaml
adapters:
  agents: copilot-sdk
```

## `detect()` rules these adapters follow

Per `CONTRIBUTING.md`, `detect()` is cheap, never raises and never makes a
network call. It also never shells out — including `GhAwRunner`, which
establishes that the `gh-aw` extension is installed by looking for its
installation directory rather than by running `gh extension list`. Every reason
string is specific because it is surfaced verbatim in `capabilities.json` and in
any `not_run` gate.

---

## 1. `copilot-sdk` — `CopilotSdkRunner`

Drives the **GitHub Copilot SDK** (`github-copilot-sdk`, imported as `copilot`)
in-process. The SDK talks JSON-RPC to a Copilot CLI runtime that it bundles and
downloads on first use, and the session's working directory is set to the task
worktree, so the agent edits files in place and the patch is extracted from the
worktree afterwards.

**Install**

```bash
pip install 'adlc[copilot]'          # or: pip install github-copilot-sdk
python -m copilot download-runtime   # caches the CLI runtime; otherwise lazy
```

**Auth** — one of `GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_CLI_TOKEN`. The token is
passed to the client explicitly. The account needs a **Copilot entitlement**;
that is why this adapter cannot be part of the credential-free suite.

**Configuration**

| Variable | Default | Meaning |
|---|---|---|
| `ADLC_COPILOT_MODEL` | `auto` | Model passed to `create_session` (`gpt-5`, `claude-sonnet-4.5`, …) |
| `limits.taskTimeoutSeconds` | `1800` | Wall-clock budget per task node |
| `ADLC_PATCH_DIR` | *(derived)* | Overrides where `<task-id>.patch` is written |

**`detect()` outcomes**

| Situation | Reason |
|---|---|
| package absent | `Copilot SDK not installed: no module 'copilot' (pip install 'adlc[copilot]' / github-copilot-sdk)` |
| a different `copilot` package shadows it | `a module named 'copilot' is installed but it is not the GitHub Copilot SDK (copilot.session is missing)` |
| no credential | `Copilot SDK installed but no credential found in GH_TOKEN/GITHUB_TOKEN/COPILOT_CLI_TOKEN` |
| ready | `Copilot SDK importable; credential from $GH_TOKEN` |

**Cost profile.** Copilot premium requests / SDK usage against the token's
entitlement, billed to that account. One task node is one session and typically
many model turns; it is the most expensive of the three per node in model terms
but consumes no Actions minutes. The SDK does not currently expose token counts
in a documented field, so `tokensIn`/`tokensOut`/`cost` are populated
opportunistically (`usage.input_tokens`, `prompt_tokens`, … are all recognised)
and simply omitted when the runtime reports nothing.

**Permissions.** The session runs with `PermissionHandler.approve_all`, i.e. the
agent may run tools without prompting — the run is unattended. Containment comes
from the isolated worktree plus write-set enforcement, not from the permission
prompt. Do not point this runner at a worktree you care about.

**Compatibility.** `run_task` prefers `session.send_and_wait(prompt)` when the
installed SDK exposes it and otherwise falls back to `session.send(prompt)` plus
the `session.idle` event, which is the surface documented for
`github-copilot-sdk` 1.x. Either way the transcript and any usage figures come
back through the same path.

---

## 2. `agent-task` — `AgentTaskRunner`

> [!IMPORTANT]
> **The Agent Tasks REST API is a GitHub public preview.** The endpoint, its
> request body and its response shape may change without notice. This adapter
> reads every response field defensively and degrades to a specific `fail`
> reason — never an exception — when it sees a shape it does not recognise
> (`agent-task create returned no task id — the public-preview response shape is
> not recognised: …`).

Delegates the node to the **Copilot cloud agent**:

```http
POST https://api.github.com/agents/repos/{owner}/{repo}/tasks
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Authorization: Bearer $GITHUB_TOKEN

{"prompt": "…", "base_ref": "…", "create_pull_request": true, "model": "auto"}
```

then polls `GET /agents/repos/{owner}/{repo}/tasks/{task_id}` until the status
leaves `queued`/`in_progress`. Recognised statuses: `queued`, `in_progress`,
`completed`, `failed`, `idle`, `waiting_for_user`, `timed_out`, `cancelled`.
**Only `completed` is a success** — `waiting_for_user` fails with the reason
"the cloud agent asked a question; ADLC runs unattended", because the inner loop
is not interactive.

Because the work happens remotely, the adapter then:

1. reads the result ref from the task record — `pull_request.head_ref`,
   `pull_request.head.ref`, `head_ref`, `branch`, `ref`, or `refs/pull/<n>/head`
   derived from the PR number;
2. `git fetch`es it into the local worktree;
3. produces the patch as `git diff <baseSha> <fetched-ref>` scoped to the write
   set, **after** checking the full name-only diff for violations.

So even though the agent ran elsewhere, the artifact is still a patch anchored to
the local base SHA, and an overreaching cloud agent is refused exactly like a
local one.

**No new dependencies.** HTTP uses the standard library (`urllib.request`) on a
worker thread; `httpx` is deliberately *not* required, so `detect()` has no
"missing HTTP client" failure mode at all. Non-HTTPS URLs are refused outright.

**Auth** — `GITHUB_TOKEN` or `GH_TOKEN`, for an account with Copilot
coding-agent access **and write permission on the target repository**. The
default `GITHUB_TOKEN` minted by GitHub Actions does not carry Copilot scope.

**Repository resolution** (offline, no subprocess): `$ADLC_REPO` →
`$GITHUB_REPOSITORY` → `repo:` in `.adlc/config.yaml` → the `origin` URL parsed
straight out of the git config file (worktree `.git` files and `commondir`
indirection are followed).

**Configuration**

| Variable | Default | Meaning |
|---|---|---|
| `ADLC_AGENT_TASK_MODEL` | `auto` | `model` in the create-task body |
| `ADLC_BASE_REF` | branch name, else base SHA | `base_ref` in the create-task body |
| `ADLC_GIT_REMOTE` | `origin` | Remote to fetch the result from |
| `limits.pollSeconds` | `15` | Poll interval |
| `limits.taskTimeoutSeconds` | `1800` | Total budget, including queue time |

**Side effects — read this before enabling.** `create_pull_request` is `true`, so
a successful task **pushes a branch and opens a pull request in the target
repository**: visible to the whole repo, may trigger CI, may notify reviewers.
ADLC then produces its *own* PR from the reduced run, so you get two. This is
inherent to the preview API's shape, not something the adapter can undo while
still being able to fetch a ref back.

**Cost profile.** Copilot coding-agent premium requests billed to the token's
account, plus any Actions minutes the created PR's CI consumes. Latency is
minutes, not seconds, because the task queues. The API reports no token or cost
figures today, so those `TaskOutcome` fields are omitted.

---

## 3. `gh-aw` — `GhAwRunner`

Borrows the **event-driven half** of the ADLC design for one inner-loop node:
dispatch a compiled [GitHub Agentic Workflow](https://github.com/githubnext/gh-aw),
let it run the agent inside Actions with least privilege, `safe-outputs` and the
network firewall, then collect the patch it uploaded.

**Contract with the workflow.** The workflow is dispatched with four inputs and
must upload an artifact containing `<task_id>.patch`:

| Input | Content |
|---|---|
| `task_id` | `node["id"]` — also the required patch filename |
| `base_sha` | The worktree's base SHA; the patch **must** be anchored to it |
| `write_set` | Newline-separated allowed paths |
| `prompt` | The rendered task prompt (truncated to 60 000 characters) |

The workflow must also set `run-name: ${{ inputs.task_id }}`. `workflow_dispatch`
returns no run id and GitHub does not expose dispatch inputs on the run, so the
adapter correlates by diffing the run list against a snapshot taken *before*
dispatch and then matching `displayTitle` to the task id. Level-0 nodes run
concurrently against the *same* workflow, so without a distinguishing run name
two nodes could adopt each other's runs; when several new runs appear and none
is identifiable, the adapter fails with that instruction rather than guessing.

The artifact must be named `<task_id>.patch`. There is no "any `.patch` will do"
fallback, for the same reason.

**How the patch is validated.** The artifact is produced by a remote workflow, so
none of it is trusted and none of it is parsed by hand:

1. `git apply --numstat -z` + `git apply --summary` enumerate the paths — git
   unquotes C-quoted headers and reports both sides of a rename, neither of which
   a regex scanner can see;
2. those paths are checked against the write set and `PROTECTED_PATHS`;
3. `git apply --index` applies it, which is what proves it is anchored to
   `base_sha` — an unanchored patch simply will not apply;
4. `git status` reports what actually landed and the check is repeated. If
   anything is outside the write set the patch is reversed out of the worktree
   and the task fails.

**Prerequisites**

```bash
gh auth login
gh extension install githubnext/gh-aw
# author .github/workflows/adlc-task.md, then:
gh aw compile          # produces adlc-task.lock.yml
```

**`detect()`** checks, in order and entirely offline: `gh` on `PATH`; the `gh-aw`
extension directory present (`$GH_CONFIG_DIR/extensions`,
`%LOCALAPPDATA%\GitHub CLI\extensions`, `$XDG_DATA_HOME/gh/extensions`,
`~/.local/share/gh/extensions`); authentication via `$GH_TOKEN`/`$GITHUB_TOKEN`
or a non-empty `hosts.yml`; and a resolvable `owner/repo`.

**Configuration**

| Variable | Default | Meaning |
|---|---|---|
| `ADLC_GHAW_WORKFLOW` | `adlc-task.lock.yml` | Workflow to dispatch |
| `ADLC_GHAW_REF` | *(repo default)* | Ref to dispatch the workflow on |
| `ADLC_GHAW_MODE` | `workflow` | `workflow` → `gh workflow run`; `aw` → `gh aw run` |
| `limits.pollSeconds` | `10` | Poll interval while the run executes |
| `limits.taskTimeoutSeconds` | `1800` | Total budget, including queue time |

**Run correlation.** See the workflow contract above — the adapter snapshots run
ids before dispatching, refuses to dispatch at all if that snapshot fails (an
unknown baseline would make every pre-existing run look new), and identifies its
run by `displayTitle`.

**Cost profile.** GitHub Actions minutes plus whatever the workflow's configured
engine spends (Copilot premium requests, or a third-party model key held as a
repo secret). Slowest of the three — queue time plus runner startup plus the
agent — but the only one where the agent's network access, permissions and
outputs are constrained by gh-aw rather than by trust. `gh` reports no token
accounting, so those fields are omitted.

---

## How the write set is actually enforced

git does the parsing, never a regex. This is deliberate: `git apply` honours
C-quoted headers (`diff --git "a/…" "b/…"`, emitted for any path containing a
non-ASCII byte, quote or backslash) and `rename from` / `rename to` records that
can name a completely different path from the `diff --git` line. A hand-written
diff scanner misses both, and code review demonstrated working escapes through
each — a patch that reported only `src/theme.ts` while creating
`.github/workflows/pwné.yml`, and one that deleted an ADR by renaming it.

| Runner | Enumeration | Enforcement point |
|---|---|---|
| `copilot-sdk` | `git status --porcelain -z -uall` (reports both sides of a rename) | before the diff is taken; violations are reverted |
| `agent-task` | `git diff --name-only -z --no-renames base..head` | before the patch is written or applied |
| `gh-aw` | `git apply --numstat -z` + `git apply --summary`, then `git status` after applying | before *and* after applying; violations are reversed out |

`--no-renames` matters for `agent-task`: a rename record names only its
destination in `--name-only`, so a rename *out of* `docs/decisions/**` would
otherwise be invisible to the check while still deleting the source.

`changed_paths()` returns `None` — not `[]` — when the probe itself fails, and
every caller fails closed on it. Conflating "git status failed" with "nothing
changed" would silently disable enforcement entirely.

`paths_in_patch()` still exists for log output and is documented as unsafe for
enforcement.

## Failure semantics (all three)

Fail closed. Every one of these produces `TaskOutcome{"status": "fail"}` with the
reason as the first line of `log`, and never raises into the executor:

| Situation | Behaviour |
|---|---|
| adapter unavailable | `"<name> unavailable: <detect reason>"` |
| worktree is not a git repo / HEAD unresolvable | fail, naming the path |
| `node["writeSet"]` empty | fail — an empty write set permits nothing |
| edit outside the write set | edits **reverted**, fail, violations listed |
| edit to a `PROTECTED_PATHS` entry | refused even if the write set declares it |
| agent produced no changes | fail — "produced no file changes", not a silent pass |
| backend timeout / non-success status | fail with the status and the budget |
| backend exception | caught, reported as `ClassName: message` |

Gitignored files are never counted as agent authorship, so a task that happens to
populate `node_modules/` or `build/` is not spuriously refused.

## Where the patch is written, and who owns it

The spine's `adlc.executor.Executor` extracts the **canonical**
`patches/<task-id>.patch` itself, by diffing the worktree after `run_task`
returns (`Worktree.diff()`), and it does not read `TaskOutcome["patchPath"]`.
That has one consequence every runner here has to respect:

> **When `run_task` returns, the changes must be present in the worktree.**

For `copilot-sdk` that is automatic — the agent edits the worktree in place. For
`agent-task` and `gh-aw` the agent worked *somewhere else*, so the fetched or
downloaded patch is `git apply --index`-ed into the worktree before returning.
Without that, the executor would diff an unchanged worktree, record `"no changes
produced"`, and silently drop the work. A side benefit: a successful apply is
independent proof that the patch really is anchored to the worktree's base SHA.

The runner *also* writes its own copy and returns it as `patchPath`, per the L1
contract. That location is resolved as: `$ADLC_PATCH_DIR` → `$ADLC_RUN_ID`
through `RunDir(cfg, run_id).patches_dir` → the nearest ancestor that is a run
directory (a direct child of `<root>/.adlc/runs`, or a directory holding
`run.json` / `taskgraph.json`) → a sibling of the worktree named
`<worktree-name>.patches`.

That last fallback is deliberately not `<worktree>/../patches`: the executor
creates worktrees with `tempfile.mkdtemp()`, so `..` is the shared system temp
root, where two concurrent runs of the same graph would collide on
`<task-id>.patch`. It is also never *inside* the worktree, which would make the
patch part of its own diff.

## Tests

`tests/l1_copilot/` runs with **no credentials and no network**:

```bash
python -m pytest tests/l1_copilot -q
ruff check src/adlc/adapters/agents/
```

It asserts the `detect() -> (False, reason)` path for all three runners, that
`detect()` never raises even against a config object that throws on attribute
access, and that `GhAwRunner.detect()` starts no subprocess. Patch extraction and
write-set enforcement run against a **real throwaway git repository**, so the
anchoring claim is verified by `git apply --check` at the base SHA rather than by
string matching. `test_executor_integration.py` drives the **real**
`adlc.executor.Executor` — worktrees, level barriers, patch application, `baseSha`
advance — which is what proves the "changes must be in the worktree" contract
above. `test_bypasses.py` holds a regression test for every escape found in code
review, each built from real patch bytes and checked with real git. The backends
themselves — the SDK session, the REST calls, every `gh` invocation — are mocked.

> [!NOTE]
> The whole fleet shares one system Python, so `pip install -e .` writes a single
> editable pointer and the last session to run it wins. Use `PYTHONPATH` instead
> of relying on the install, and confirm before trusting a run:
>
> ```powershell
> $env:PYTHONPATH = "<your worktree>\src"
> python -c "import adlc.executor as e; print(e.__file__)"   # must be YOUR worktree
> ```

The opt-in real-agent smoke test (`PLAN.md` §8.2) is the only thing that needs
credentials, and it is skipped and reported as `not_run` with a reason when they
are absent.

## Implementation note

The shared execution core — write-set matching, git plumbing, patch production,
patch-path resolution, prompt rendering — lives in
[`copilot_sdk.py`](../../src/adlc/adapters/agents/copilot_sdk.py) and is imported
by the other two runners. Workstream L1 owns exactly three modules
(`PLAN.md` §6) and may not add a fourth, so one of them has to carry it. Nothing
in that module imports the Copilot SDK at import time — the SDK is imported
lazily inside `CopilotSdkRunner._converse` — so importing those helpers from
`agent_task.py` and `gh_aw.py` costs nothing and cannot fail when the optional
dependency is absent.
