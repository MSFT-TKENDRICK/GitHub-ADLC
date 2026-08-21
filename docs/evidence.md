# Evidence collectors — Lighthouse, k6, axe (L6)

> Leaf **L6** of `docs/PLAN.md` §6. Three optional `EvidenceCollector` adapters
> that turn a running candidate build into hash-verified, budget-checked
> evidence.

Evidence is the *proof* that a candidate implementation delivered what the spec
promised. Every file these collectors write becomes an `ArtifactRef` in
`run.json`'s `artifacts[]` (`{path, kind, mimeType, sha256, bytes}`) and feeds
the **deterministic** half of the evidence gate. Budgets come from
`.adlc/runs/<run-id>/enrichment/benchmarks.yaml`.

## The rules these collectors obey

1. **All three are optional.** With none installed, the spine's Playwright
   collector alone satisfies the credential-free conformance suite (§8.1).
   `detect()` returns `(False, "<specific reason>")` and the framework carries on.
2. **`detect()` is cheap and cannot hang.** It inspects `PATH`, a nearby
   `node_modules/.bin`, and the filesystem. No network. No subprocess.
3. **No measurement is ever fabricated.** If a tool did not run, timed out,
   crashed, or simply did not report a metric, that metric appears in
   `unmeasured[]` with `status: "not_run"` and a cause — never as a `0`, never
   as a default, never as a silent pass. A measured `0` (e.g. zero critical a11y
   violations) is a real measurement; an *absent* metric is not.
4. **No secrets leave in evidence.** Everything written is passed through the
   redactor first — see [Redaction](#redaction).
5. **Evidence describes the current attempt.** Each collector deletes its own
   prior outputs before running, so a stale green report can never be re-hashed
   and presented as current.

## The three collectors

| Entry point | Class | Requires | Emits |
|---|---|---|---|
| `lighthouse` | `LighthouseCollector` | `lhci` + Chrome/Chromium | `lighthouse.json`, `lighthouserc.json`, `lighthouse-measurements.json` |
| `k6` | `K6Collector` | `k6` binary | `k6.json`, `k6-script.js`, `k6-measurements.json` |
| `axe` | `AxeCollector` | Node + `@axe-core/playwright` + `playwright` | `axe.json`, `axe-scan.cjs`, `axe-scan.config.json`, `axe-measurements.json` |

Each also writes `<collector>-unmeasured.json` when a declared budget could not
be measured. All are pre-registered in `pyproject.toml` under
`[project.entry-points."adlc.evidence"]`. Files land in
`.adlc/runs/<run-id>/evidence/<variant>/` (plan §4.1).

> **Selection note.** The spine default is `local` (always available, no
> browser); `playwright` takes over when installed. `evidence` is deliberately
> **not** in `adlc.config.EXPLICIT_ONLY_KINDS` — it is an observational,
> read-only kind, so `select_adapter` is allowed to upgrade to the first
> *detected* collector, and `adlc.stages.evidence.run_evidence` runs exactly
> **one** collector per run. On a machine that happens to have `lhci` or `k6`
> installed that means one of these is selected instead of `local`/`playwright`,
> and the run gets budget evidence but no trace/console bundle. Pin the choice
> explicitly in `.adlc/config.yaml` when you want both kinds of evidence:
>
> ```yaml
> adapters:
>   evidence: local   # or playwright | lighthouse | k6 | axe
> ```
>
> On a credential-free CI runner none of the three is detected, so `local` is
> used and the conformance suite is unaffected.

### `lighthouse` — Lighthouse CI

**Install:** `npm i -g @lhci/cli` (or a repo-local `node_modules/.bin/lhci`),
plus a Chrome/Chromium binary. See <https://github.com/GoogleChrome/lighthouse-ci>.

**What it runs.** Writes a `lighthouserc.json` derived from `benchmarks.yaml` —
the configured URLs, run count, preset and Chrome flags, plus `assert.assertions`
translated from your budgets — then runs `lhci autorun --config=<rc>`. The lhci
assertions are **advisory**: ADLC's own comparison against `benchmarks.yaml` is
the authority, so an lhci upgrade cannot silently change a gate outcome.

The first configured URL produces `lighthouse.json` (a standard Lighthouse
Result document); additional URLs produce `lighthouse-1.json`, `lighthouse-2.json`, …
The `.lighthouseci/` working directory is **deleted** after harvesting, because
its HTML reports embed page source and full network detail.

**Built-in metric ids** (no `source` needed):

`lcp_ms` · `fcp_ms` · `si_ms` · `tti_ms` · `tbt_ms` · `ttfb_ms` · `cls` ·
`dom_size` · `total_byte_weight_kb` · `performance_score` ·
`accessibility_score` · `best_practices_score` · `seo_score` · `pwa_score`

Category scores are reported on a **0–100** scale (Lighthouse's native 0–1 value
× 100), so a budget reads naturally as `budget: 90, direction: higher_is_better`.

**Typical runtime/cost.** ~20–45 s per URL per run on a warm runner (Chrome cold
start dominates the first ~5 s). `numberOfRuns: 3` triples it. No credentials, no
API cost — just CI minutes.

### `k6` — load & performance

**Install:** the `k6` binary — <https://k6.io/docs/get-started/installation/>.

**What it runs.** `k6 run --summary-export=<out>/k6.json --summary-trend-stats=avg,min,med,max,p(90),p(95),p(99) --no-usage-report <script>`.
The trend-stats flag is what makes `p(95)`/`p(99)` present in the export.

The script is either the one you declare (`collectors.k6.script`, resolved against
the run directory then the repo root) or a generated constant-VU GET script
written to `k6-script.js` and emitted as evidence so the load profile is
reproducible. `collectors.k6.env` values are forwarded as `--env KEY=VALUE`;
**never put credentials there** — `benchmarks.yaml` lives in the run directory.

**Built-in metric ids:**

`p50_latency_ms` · `p90_latency_ms` · `p95_latency_ms` · `p99_latency_ms` ·
`avg_latency_ms` · `min_latency_ms` · `max_latency_ms` · `p95_wait_ms` ·
`p95_connect_ms` · `rps` · `requests_total` · `error_rate` (0–1) ·
`error_rate_pct` (0–100) · `failed_requests` · `checks_failed` ·
`checks_passed` · `check_pass_rate` · `iterations` · `iteration_rate` ·
`data_received_kb` · `data_sent_kb` · `vus_max`

**Typical runtime/cost.** The configured `duration` plus ~5 s of start-up, so a
`30s` profile costs ~35 s. Load tests generate real traffic — point them at a
preview/candidate deployment, never at production.

### `axe` — accessibility

**Install:** Node 18+, then `npm i -D @axe-core/playwright playwright` and
`npx playwright install chromium`.

**What it runs.** Generates `axe-scan.cjs` — deliberately CommonJS so it resolves
identically whether or not the nearest `package.json` declares `"type": "module"`,
and so it tolerates both export shapes of `@axe-core/playwright` — plus an
`axe-scan.config.json` describing the URLs, tags, disabled rules, browser and
navigation timeout. Node drives Playwright to each URL and runs
`new AxeBuilder({ page }).withTags(...).analyze()`.

The first URL produces `axe.json` (a standard axe-core results document);
additional URLs produce `axe-1.json`, `axe-2.json`, … Both the scan script and
its config are emitted as evidence so a reviewer can reproduce the scan. The
intermediate raw payload is deleted once the redacted reports are written.

**Built-in metric ids:**

`a11y_critical_violations` · `a11y_serious_violations` ·
`a11y_moderate_violations` · `a11y_minor_violations` ·
`a11y_blocking_violations` (critical + serious) · `a11y_total_violations` ·
`a11y_violation_nodes` · `a11y_critical_nodes` · `a11y_serious_nodes` ·
`a11y_incomplete` · `a11y_passes` · `a11y_inapplicable`

axe reports `impact: null` for some rules. Those violations count towards
`a11y_total_violations` but towards **none** of the per-impact buckets —
assigning them a severity would be invented data.

**Typical runtime/cost.** ~5–15 s per URL (browser launch plus axe injection).
The Chromium download is a one-off ~150 MB.

## `benchmarks.yaml`

Schema: [`schemas/benchmarks.schema.json`](../schemas/benchmarks.schema.json).
Location: `.adlc/runs/<run-id>/enrichment/benchmarks.yaml`.

```yaml
version: 1

target:
  url: http://localhost:3000/     # default for every collector
  timeoutSeconds: 300             # wall-clock cap per collector subprocess

collectors:                       # all optional
  lighthouse:
    urls: [http://localhost:3000/, http://localhost:3000/checkout]
    numberOfRuns: 1
    preset: desktop               # desktop | mobile
    chromeFlags: "--headless=new --no-sandbox --disable-gpu"
  k6:
    url: http://localhost:3000/api/health
    vus: 5
    duration: 30s
    # script: enrichment/k6/load.js   # use your own instead of the generated one
  axe:
    urls: [http://localhost:3000/checkout]
    tags: [wcag2a, wcag2aa]
    browser: chromium             # chromium | firefox | webkit

metrics:
  - id: lcp_ms
    collector: lighthouse
    budget: 2500
    direction: lower_is_better
    unit: ms
  - id: a11y_critical_violations
    collector: axe
    budget: 0
    direction: lower_is_better
  - id: p95_latency_ms
    collector: k6
    budget: 400
    direction: lower_is_better
```

### Metric fields

| Field | Required | Meaning |
|---|---|---|
| `id` | ✅ | `^[a-z][a-z0-9_]*$`. A built-in id is extracted automatically; anything else **must** declare `source`. |
| `collector` | ✅ | Which collector produces it. Known values: `lighthouse`, `k6`, `axe` (this workstream), `playwright` and `local` (spine defaults). A pattern rather than an enum, so a new evidence adapter cannot invalidate an existing file. |
| `budget` | ✅ | The threshold the measured value is compared against. |
| `direction` | ✅ | `lower_is_better` (passes when `value <= budget`) or `higher_is_better` (passes when `value >= budget`). Required on purpose — there is no safe default, and a wrong direction is a silent false pass. |
| `source` | | RFC 6901 JSON Pointer into the collector's raw output, e.g. `/audits/dom-size/numericValue` or `/metrics/http_req_duration/p(95)`. Overrides the built-in catalogue. |
| `scale` | | Multiplier applied before comparison (e.g. `100` to turn a 0–1 score into 0–100). |
| `aggregate` | | How to combine values across several URLs: `worst` (default), `best`, `mean`, `sum`, `first`. `worst` is max for `lower_is_better` and min for `higher_is_better`. |
| `unit`, `description` | | Reporting only. |
| `optional` | | Advisory: a failing optional metric is reported but is not intended to block the gate. **Absence is still `not_run`, never a pass.** |

**Target resolution order:** `ADLC_TARGET_URL` env var → `collectors.<name>.urls`
/ `collectors.<name>.url` → `target.url` → `run["capabilities"]["targetUrl"]`
(the same field the spine's Playwright collector reads; `about:blank` is treated
as unset). With none set the collector reports `not_run` rather than guessing an
address.

The spine's `adlc enrich` seeds this file with `lcp_ms` (lighthouse),
`a11y_critical_violations` (axe) and `console_errors` (playwright). All three are
built-in ids for their collector, and a test asserts the seeded document
validates against this schema.

## Normalised measurements

Each collector writes **two** files next to its raw output.

### `<collector>-measurements.json` — a bare JSON array

This is the file the spine reads. `adlc.stages.evidence.collect_measurements`
and the deterministic rubric runner's `metric_within_budget` check both glob
`*-measurements.json` and parse it as a **list**, matching the shape the spine's
own `local` collector emits — so the top level is an array, not an object:

```jsonc
[
  { "metricId": "lcp_ms", "value": 1820.4, "budget": 2500, "passed": true,
    "collector": "lighthouse", "artifactSha256": "…" }
]
```

Entries are **key-for-key identical** to `evidence-review-pack.schema.json`
`#/properties/measurements/items` (which is `additionalProperties: false`), so
the spine can copy them into the sanitised pack verbatim. `artifactSha256` is
the hash of the raw artifact the value was read from, which is what makes an LLM
squad's citation checkable.

Only *real* measurements appear here. A metric with no value never does.

### `<collector>-unmeasured.json` — why a budget could not be measured

```jsonc
{
  "schemaVersion": "adlc-unmeasured/v1",
  "collector": "lighthouse",
  "runId": "2026-08-19-a1b2",
  "variant": "candidate-a",
  "generatedAt": "2026-08-19T18:00:00Z",
  "tool": { "ran": false, "cause": "tool_unavailable",
            "reason": "lhci not on PATH — install with `npm i -g @lhci/cli` …" },
  "unmeasured": [
    { "metricId": "lcp_ms", "collector": "lighthouse", "budget": 2500,
      "direction": "lower_is_better", "status": "not_run",
      "cause": "tool_unavailable",
      "reason": "lhci not on PATH — install with `npm i -g @lhci/cli` …" }
  ]
}
```

The filename deliberately does **not** match the spine's `*-measurements.json`
glob. A not-run entry has no `value`, so putting it in the measurements array
would either be dropped or emit `value: null` and invalidate the review pack.
Its **absence** from the measurements array is what fails the budget:
`metric_within_budget` scores `0.0` with "no measurement recorded for
'<id>'". This sidecar records *why*, hash-verified, so the failure is
actionable rather than mute.

Causes:

| `cause` | Meaning |
|---|---|
| `tool_unavailable` | The binary/package is not installed — the `detect()` reason is copied verbatim. |
| `tool_timeout` | The subprocess exceeded `timeoutSeconds` and was killed. |
| `tool_failed` | The tool ran and exited non-zero. |
| `output_missing` | No target URL, no script, or the tool produced no artifact. |
| `output_unreadable` | The artifact existed but could not be parsed. |
| `metric_absent` | The tool ran but did not report this metric. |
| `metric_unmapped` | Unknown metric id with no `source` pointer declared. |

A metric declared in `benchmarks.yaml` is therefore **always** accounted for:
either it is in `<collector>-measurements.json` backed by a hashed artifact, or
it is in `<collector>-unmeasured.json` as `not_run`. Per plan §4.2, `required +
not_run ⇒ the aggregate fails`, so a missing measurement fails closed.

When a collector has **no** budgets declared for it and its tool is missing, it
writes nothing at all and returns an empty artifact list.

> **Known spine limitation.** `collect_measurements` and `metric_within_budget`
> both compare with `value <= budget` regardless of `direction`, so a
> `higher_is_better` budget (e.g. `performance_score: 90`) is reported as failed
> in the review pack even when it passed. The `passed` field these collectors
> write is direction-correct; the spine recomputes it. The divergence is
> fail-closed (a false negative, never a false positive) and is reported to the
> spine workstream. All three metric ids the spine seeds — `lcp_ms`,
> `a11y_critical_violations`, `console_errors` — are `lower_is_better`, so the
> default path is unaffected.

## Redaction

HAR files, Lighthouse network audits and axe HTML snippets routinely carry
bearer tokens, session cookies and signed URLs. Evidence is uploaded as a build
artifact and shown to reviewers, so **everything these collectors write is
redacted first** — including the recorded command line and captured
stdout/stderr, which frequently echo request URLs.

What is replaced with `[REDACTED]`:

1. **Mapping values under a credential-bearing key** — `authorization`,
   `proxy-authorization`, `cookie`/`cookies`, `set-cookie`, `api-key`/`x-api-key`,
   `access_token`, `id_token`, `refresh_token`, `auth_token`, `session_id`,
   `session_token`, `client_secret`, `secret`, `password`, `token`, `bearer`,
   `credential(s)`, `signature`, `sas`, `csrf_token`, `xsrf_token` (case- and
   `-`/`_`-insensitive, optional `x-` prefix). This covers Lighthouse's
   `configSettings.extraHeaders`, which can literally contain your
   `Authorization` header.
2. **HAR-shaped `{"name": …, "value": …}` pairs** whose `name` is credential-
   bearing — covers HAR `headers[]`, `cookies[]` and `queryString[]`. When the
   *key* is sensitive but the value is such a list (e.g. `cookies`), the
   structure is kept and only the values are replaced, so a reviewer still sees
   which cookies were present.
3. **URL query-parameter values** matching `access_token`, `refresh_token`,
   `id_token`, `session_token`, `auth_token`, `api_key`, `token`, `secret`,
   `password`, `credential`, `signature`, `sig`, `sas`, `auth`, `code`, `key`,
   `session`, `sid` (matched on `-`/`_` word boundaries). The URL keeps its
   shape; non-sensitive parameters are preserved.
4. **`Bearer` / `Basic` / `Token` prefixed values**, JWTs (`eyJ….….…`), GitHub
   tokens (`ghp_`, `github_pat_`), AWS access key ids (`AKIA…`), Slack tokens
   (`xox[abprs]-`) and `sk-…` API keys, anywhere in any string.
5. **HTML attribute values** whose attribute name looks credential-bearing
   (`data-access-token="…"`), and the classic hidden-input pair
   `<input name="csrf_token" value="…">` in either attribute order.
6. **`KEY=value` assignments** whose key looks credential-bearing, at the start
   of a string or log line — this covers the recorded command line (`--env
   API_TOKEN=…`) and environment-shaped output. Non-sensitive assignments such
   as `VUS=5` are preserved.

Deliberately **not** redacted: base64 `data:` URIs (Lighthouse screenshots) —
they carry no credentials, and rewriting megabytes of base64 would be slow and
would corrupt the image.

Additionally, axe `nodes[].html` snippets are **truncated to 512 characters**.
They are attacker-controlled page markup; the sanitised review pack must not
carry raw HTML at all (§4.6), and truncation limits the blast radius of anything
that reaches a log.

Redaction is a best-effort defence in depth, not a licence to send credentials
into a candidate build. Keep secrets out of the URLs and scripts you point these
collectors at.

## Artifact kinds

The collectors in this document name their own kinds:

| `kind` | File |
|---|---|
| `lighthouse` | `lighthouse.json`, `lighthouse-N.json` |
| `lighthouse_config` | `lighthouserc.json` |
| `k6` | `k6.json` |
| `k6_script` | `k6-script.js` |
| `axe` | `axe.json`, `axe-N.json` |
| `axe_script` / `axe_config` | `axe-scan.cjs` / `axe-scan.config.json` |
| `evidence_measurements` | `<collector>-measurements.json`, `<collector>-unmeasured.json` |

`RunDir.scan_artifacts` assigns the rest from the file suffix, so anything a
collector drops in the evidence directory is hashed and classified without the
collector having to declare it:

| `kind` | Matched by |
|---|---|
| `playwright_trace` | `.zip` |
| `har` | `.har` |
| `video` | `.webm` |
| `screenshot` | `.png` |
| `jsonl` | `.jsonl` |
| `replay_script` | `.ts` |
| `json` | `.json` |
| `html_report` | `.html`, plus the run's own `report.html` |
| `file` | anything else |

Two kinds are decided by **location**, not suffix:

| `kind` | Matched by |
|---|---|
| `persona_feedback` | `.json` directly under `evidence/personas/` |

A persona walkthrough is evidence about the *experience* rather than another
anonymous JSON blob, and both the report and the completeness pack need to treat
it as its own class — the report renders it as a reasoning trace, and the pack
counts it separately from measured evidence. Naming the kind at scan time is what
lets them do that without re-opening every JSON file to guess what it is. See
`docs/PLAN.md` §4.6b.

Every entry carries a verified `sha256` and a `path` relative to the run
directory, e.g. `evidence/candidate-a/lighthouse.json`. Note that
`adlc.stages.evidence.run_evidence` **re-hashes every file it finds on disk**
rather than trusting the returned list, and derives `kind`/`mimeType` from the
suffix — so these `ArtifactRef`s must (and do) point at files that were really
written. It also means anything left in the evidence directory becomes an
artifact, which is why the `.lighthouseci/` working directory and the
intermediate axe scan payload are deleted before returning.

## Tests

`tests/l6_evidence/` passes with **no tools installed and no credentials**:

```powershell
# Do NOT use `pip install -e .` -- the whole fleet shares one Python and a
# single editable pointer, so the last installer wins and every other
# workstream silently imports the wrong source tree.
$env:PYTHONPATH = "<your worktree>\src"
python -m pytest tests/l6_evidence -q
python -m pytest tests/conformance -q     # 41 tests, must stay green
ruff check src/adlc/adapters/evidence/
```

Verify the isolation with
`python -c "import adlc.executor as e; print(e.__file__)"` — it must print your
own worktree.

It asserts the `detect() -> (False, reason)` path for all three collectors (with
`PATH` isolated, so the result is the same on a developer machine that happens to
have the tools), proves `detect()` spawns no subprocess and never raises, and
unit-tests the raw-JSON → normalised-measurement mapping against checked-in
fixture outputs from each tool in `tests/l6_evidence/fixtures/`. The `collect()`
path is exercised end-to-end with only the subprocess boundary stubbed, so
harvesting, redaction, budget comparison and artifact hashing all really run.

`tests/l6_evidence/test_spine_interop.py` calls the **real** spine functions —
`adlc.stages.evidence.collect_measurements` and
`DeterministicRubricRunner.run` with a `metric_within_budget` criterion — against
files these collectors actually wrote, so a change to the spine's measurement
contract fails loudly here instead of silently yielding zero measurements. It
also asserts the "tool did not run ⇒ failing budget check, never a pass"
guarantee end-to-end.

## Implementation note

L6's exclusive paths are exactly three modules, so the shared toolkit (budget
loading, JSON-pointer extraction, redaction, subprocess execution, measurement
emission) lives in `lighthouse.py` and is imported by `k6.py` and `axe.py`
rather than in a fourth file. The blast radius is contained to L6:
`adlc.config.load_adapters` swallows `ImportError`, so a broken leaf is simply
undiscoverable, never fatal.
