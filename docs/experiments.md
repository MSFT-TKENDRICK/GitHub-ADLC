# Experiments, feature flags and the OES exporter

> Workstream **L7**. Covers `src/adlc/stages/experiment.py`,
> `src/adlc/adapters/export/oes.py` and `src/adlc/adapters/flags/launchdarkly.py`.

---

## 1. `adlc-run/v1` is canonical. OES is an exporter.

The [Open Experiment Specification](https://www.openexperiment.org/) v0.1.0 is real
and its JSON Schema is published at
`https://openexperiment.org/schema/openexperiment-0.1.0.schema.json`. ADLC exports to
it, and ADLC is **not** built on it. Two concrete reasons:

**It models an online A/B experiment.** Its vocabulary is traffic allocation,
randomization units, hash attributes and salts, sample-ratio mismatch, p-values,
credible intervals, statistical power and minimum detectable effect. An ADLC run is
usually a build/evaluation run: two build artifacts at two commits, exercised by the
same deterministic evidence suite, with no live traffic and frequently only one
candidate. Filling `design.trafficAllocation`, `results.sampleSizes` or
`metricResults[].pValue` for such a run would not be a lossy translation — it would be
a fabrication.

**Its artifact vocabulary cannot describe ADLC evidence.** `artifacts[].type` is a
closed enum:

```
chart | screenshot | sql | notebook | csv | dashboard | slide | image | html_report
```

There is no member for a Playwright trace, a HAR, a JSONL console log, a video, or a
k6/axe result — which are exactly the artifacts ADLC evidence capture produces and the
evidence gate depends on.

So `run.json` (`adlc-run/v1`) stays canonical and append-only, and
`adlc export oes RUN` produces a **conditional, lossy interchange document**. A run
carries an optional `experimentRef`; **a run is not an experiment.**

### Corrections to the design notes

While implementing this, several details in the original notes turned out to be wrong
against the live schema. The tests in `tests/l7_experiment/test_oes_schema_fidelity.py`
re-derive every one of these from the schema document itself, so they cannot drift
again:

| Note said | The schema actually says |
|---|---|
| 14 top-level keys | **19**: `schemaVersion`, `objectType`, `exportedAt`, `sourceSystem`, `sourceSystemVersion`, `canonicalUrl`, `externalIds`, `experiment`, `design`, `variants`, `metrics`, `analysis`, `results`, `scorecard`, `decision`, `qualityChecks`, `artifacts`, `provenance`, `extensions` |
| `experiment` is `{id, title, hypothesis, status}` | `experiment` **requires only `id` and `title`**. `hypothesis` and `status` are optional, alongside `slug`, `summary`, `description`, `learningGoal`, `businessGoal`, `productArea`, `tags`, `owner`, `stakeholders`, `links` |
| `schemaVersion` is the constant `"0.1.0"` | It is a **semver pattern**, `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`. `objectType` is the enum with the single member `experiment` |
| — | `metrics[]` requires `id` **and `name`**; `variants[]` requires `id` **and `key`**; `artifacts[]` requires `type` **and `uri`**; `results.metricResults[]` requires `metricId` and `comparison{baselineVariantId, variantId}` |
| — | `analysis.method` is `frequentist\|bayesian\|sequential\|cuped\|diff_in_diff\|custom`. There is **no** "deterministic" or "none" member, so ADLC uses `custom` with `model: "adlc-deterministic-comparison"` |
| — | `decision.outcome` spells it **`rollback`**; `scorecard.recommendedAction` spells it **`roll_back`**, and has no member meaning `partial_rollout`. That outcome is dropped from the scorecard rather than coerced |
| — | `qualityChecks[].observed` / `expected` are **untyped** (`{}`), so ADLC's `observed`/`expected` dicts pass through unchanged |
| — | `externalIds` values must be **strings** (`additionalProperties: {"type": "string"}`) |
| — | The top level and every sub-object set `additionalProperties: true`, which is what makes `adlc:`-prefixed sibling keys legal |

The schema is **vendored verbatim** inside `oes.py` so `adlc export oes` validates its
own output offline, in an air-gapped runner, and inside the credential-free conformance
suite. `tests/l7_experiment/data/openexperiment-0.1.0.schema.json` holds an independent
copy used as the test oracle; a test asserts the two are identical. Set
`ADLC_OES_SCHEMA=/path/to/schema.json` (or `export.oes.schemaPath` in
`.adlc/config.yaml`) to validate against a newer draft without a code change. Run
`ADLC_TEST_NETWORK=1 pytest tests/l7_experiment` to re-fetch the published schema and
diff it against the vendored copy.

---

## 2. The refusal rule

> `adlc export oes RUN` emits a document **only when the run is genuinely
> comparative**, and otherwise refuses with a specific reason.

Comparative means **all** of:

1. the run declares **≥ 2 variants**, and
2. at least one metric has a **measured value on ≥ 2 of them**, so a comparison exists.

Anything else raises `NotComparativeError` (a `ValueError` subclass, via
`OesExportError`) and **writes no file**. The refusal names what is missing:

```
refusing to export OES for run '2026-08-19-c4d5': the run declares 1 variant(s);
OES describes a comparison, so at least 2 variants are required. adlc-run/v1 remains
the canonical record for this run.
```

```
refusing to export OES for run '2026-08-19-e6f7': the run has no measured outcomes;
run the experiment stage's `analyze` phase (or provide experiment/measurements.json)
before exporting
```

This is the **normal** outcome for most ADLC runs and is not an error condition in the
pipeline sense — `run.json`, `report.html` and the gates are unaffected. Callers can
pre-check without exception handling:

```python
from adlc.adapters.export.oes import is_comparative

ok, reason = is_comparative(run)
```

**Nothing is ever fabricated.** The exporter emits no `pValue`, `qValue`,
`standardError`, `confidenceInterval`, `credibleInterval`,
`statisticalPowerObserved`, `probabilityOfImprovement`, `expectedLoss`, `power`,
`minimumDetectableEffect`, `trafficAllocation`, `variantAllocation`,
`randomizationUnit`, `sampleSizes` or `exposures` unless the source data genuinely
supplied it. `sampleSizes` and `exposures` are passed through verbatim when a real
online experiment provides them, and are otherwise absent. A test asserts these keys
never appear in output built from a deterministic run.

Instead of leaving those fields conspicuously empty, the exporter states the situation
outright as a quality check:

```json
{
  "checkType": "adlc:statistical_inference",
  "status": "not_run",
  "observed": { "randomizationUnit": null, "inference": "none" },
  "expected": { "inference": "frequentist_or_bayesian" },
  "message": "no randomization and no live traffic: variants are build artifacts at commits compared by deterministic measurement, so p-values, statistical power and sample-ratio checks do not exist for this run and were not fabricated"
}
```

---

## 3. ADLC → OES field mapping

| OES field | Source | Notes |
|---|---|---|
| `schemaVersion` | constant `"0.1.0"` | matches the schema's semver pattern |
| `objectType` | constant `"experiment"` | the only enum member |
| `exportedAt` | export time, UTC | honours `SOURCE_DATE_EPOCH` for reproducible output |
| `sourceSystem` | constant `"adlc"` | |
| `sourceSystemVersion` | `adlc.__version__` | |
| `canonicalUrl` | `https://github.com/{repo}/pull/{prNumber}` | emitted only when both are known |
| `externalIds.github_pr` | `run.prNumber` | stringified |
| `externalIds.github_issue` | intake stage `data.issue` | stringified |
| `externalIds.github_repo` / `adlc_run` | `run.repo` / `run.runId` | |
| `experiment.id` | pre-registration `experiment.id`, else `run.experimentRef`, else `run.runId` | |
| `experiment.title` / `hypothesis` / `summary` / `tags` | pre-registration | |
| `experiment.status` | `decided` when `run.decision` exists, else mapped from `run.status` (`draft→draft`, `specced→planned`, `built→running`, `evaluated\|gated\|reported→analyzed`, `decided→decided`, `abandoned→archived`) | |
| `design.type` | pre-registration, default **`quasi_experiment`** | the only honest default: no randomization |
| `design.analysisUnit` | `build_run` | |
| `design.assignmentMethod` | `deterministic_build_variant` | |
| `design.exposureDefinition` | "each variant is a build artifact at a commit; exposure is a CI evaluation, not live user traffic" | |
| `design.*` (traffic, power, alpha, randomizationUnit, …) | pre-registration **only** | never synthesized |
| `variants[].id` / `key` | `run.variants[].key` | ADLC has one identifier; both OES fields carry it |
| `variants[].role` | `run.variants[].role` | ADLC allows `control\|treatment`; OES also allows `holdout\|baseline` |
| `variants[].featureFlagKeys` | `run.variants[].flagKeys` | usually empty — see §5 |
| `variants[].codeReferences` | `[{type: "git_commit", value: <commit>, repo: <repo>}]` | this is where "a candidate is a commit" lands |
| `metrics[]` | pre-registration + `enrichment/benchmarks.yaml` + `enrichment/rubric.yaml` criteria | `id`, `name`, `role`, `direction`, `type`, `unit`, `description` |
| `metrics[].adlc:budget` / `adlc:source` | benchmark budget, defining file | OES metrics have no budget field, so it is namespaced rather than smuggled in |
| `analysis.method` | pre-registration, default `custom` | with `model: "adlc-deterministic-comparison"` |
| `results.metricResults[]` | analyze phase, or recomputed from `measurements` | `baselineValue`, `variantValue`, `absoluteDifference`, `relativeDifference`, `resultStatus`, `decisionImpact` |
| `results.metricResults[].adlc:measurementBasis` | `deterministic_single_measurement` | on every comparison ADLC computes itself |
| `results.sampleSizes` / `exposures` | measurement source **only** | absent for a deterministic run |
| `qualityChecks[]` | **every gate in `run.gates`** | see §4 |
| `artifacts[]` | `run.artifacts` whose `kind` fits the enum | see §6 |
| `decision.outcome` / `rationale` / `decidedAt` | `run.decision` | |
| `decision.decidedBy` | `{name: <decidedBy>, role: "reviewer"}` | ADLC records a string; OES wants an object |
| `decision.adlc:reviewSha` / `adlc:adr` | `run.decision` | no OES equivalent |
| `scorecard` | derived | `summary`, `overallResult`, `qualityStatus`, `recommendedAction`, `keyFindings`, `risks` |
| `provenance.codeVersion` | `run.headSha` | |
| `provenance.createdBy` / `exportedBy` / `analysisGeneratedBy` | `{system: "adlc"}`, `adlc/<version>` | |
| `provenance.resultHash` / `attachmentsHash` | sha256 over the canonicalized results / artifact hashes | |
| `extensions["adlc:*"]` | everything else | see §7 |

### Result classification

`resultStatus` is a **descriptive** statement about two measured values, never a
statistical claim:

| Metric `direction` | Rule |
|---|---|
| `increase_is_good` | higher than baseline → `positive`, lower → `negative` |
| `decrease_is_good` | lower than baseline → `positive`, higher → `negative` |
| `no_change_expected` | any change → `negative` |
| `two_sided` or undeclared | `inconclusive` — we genuinely do not know which direction is better |
| value equal to baseline | `neutral` |

`decisionImpact` then follows: a guardrail that regressed or blew its budget is
`blocks_ship`; a primary metric that improved is `supports_ship`; any other regression
is `needs_followup`; everything else is `informational`.

---

## 4. Gates → `qualityChecks[]`

This is the one place where ADLC and OES line up almost exactly. Both model a named
check with a status, a severity, and observed vs expected values — and the severity
enums are identical (`low|medium|high|critical`).

`checkType` is a **free string** in the schema (no enum), so ADLC namespaces its own:

```
adlc:tests            adlc:secrets_local        adlc:deps_local
adlc:evidence_completeness                      adlc:security
adlc:code_quality     adlc:evals                adlc:governance
adlc:adversarial_review                         adlc:evidence_review
adlc:pre_registration adlc:spec_coverage
```

Status maps 1:1 (`pass`/`fail`/`not_run` are all legal OES check statuses; OES also has
`warn`, which ADLC gates never emit). Requiredness has no OES home, so it is carried as
`adlc:required`, and gate evidence paths as `adlc:evidence`.

Three checks are **synthesized** by the exporter:

- **`adlc:aggregate`** — the single fail-closed verdict ADLC uses as its
  branch-protection target. `fail` when any *required* gate is `fail` or `not_run`,
  with the offending gate ids in `observed.failingRequiredGates`.
- **`adlc:pre_registration`** — see §5 below.
- **`adlc:statistical_inference`** — see §2 above.

---

## 5. The experiment stage

`adlc.stages.experiment` has three phases. Each appends a new immutable attempt at
`runs/<run>/stages/experiment.<attempt>.json`; **none of them writes `run.json`** —
only `adlc reduce` may, which is what makes parallel Actions jobs race-free. The stage
enforces this itself: its writer refuses any path named `run.json`.

### `plan` — the pre-registration

Writes `runs/<run>/experiment/plan.json` declaring **variants, metrics and design
before anything is measured**, sourced from an operator-authored `experiment.yaml`, or
derived from the run's variants plus `enrichment/`. It records the plan's sha256 digest
and the repository HEAD sha in the stage result.

This is a genuine trust check rather than paperwork. Because stage results are
committed and immutable, "the metrics were declared before they were measured" becomes
timestamp-verifiable via git, and because the digest is recorded, editing the plan
afterwards is detectable. `analyze` re-hashes the file and reports the comparison; the
exporter turns it into a quality check:

```json
{
  "checkType": "adlc:pre_registration",
  "status": "pass",
  "observed": { "plannedAt": "…", "analyzedAt": "…", "digest": "sha256:…", "unchanged": true, "gitSha": "…" },
  "expected": { "unchanged": true, "plannedBeforeAnalyzed": true }
}
```

A run with fewer than two variants gets a `skipped` result, not a failure. It is simply
not an experiment.

### `run` — exposure

Records which flag keys back which variant, which `FlagProvider` materialized them, and
any evaluations actually performed. **Live evaluation is opt-in** (`evaluate=True`);
by default the phase records the intended exposure without contacting a flag backend. A
missing or broken flag adapter is recorded, never raised — and when the auto-selected
adapter reports itself *unavailable*, `exposure.providerNote` says so, so the record
never implies flags were served when they were not.

### `analyze` — measured results

Loads measurements — from an argument, `experiment/measurements.json`, or
`evidence/<variant>/metrics.json` — re-verifies the pre-registration digest, and
computes per-metric comparisons against the baseline variant (`control`, else
`baseline`, else the first declared variant). Writes `experiment/analysis.json`. Returns
`fail` if the pre-registration changed after it was written.

### Candidates are commits, not flag variants

> A candidate is a **build artifact at a commit**, not automatically a flag variant.

OpenFeature wiring is **opt-in** and only meaningful when the application genuinely
exposes **both code paths in one binary** — that is, when a single deployed build can
serve either behaviour depending on a flag evaluation. Two builds from two commits
cannot be switched between by a flag; they are separate artifacts, and the flag key
would be decorative. That is why:

- `variants[].flagKeys` is empty for a normal ADLC run,
- the `run` phase returns `skipped` when no variant declares a flag key, and
- `design.exposureDefinition` says out loud that exposure is a CI evaluation.

### Flag telemetry uses current OpenTelemetry semantic conventions

`adlc.stages.experiment.flag_evaluation_attributes()` is the single vendor-neutral
builder every provider funnels through, so attribute names cannot drift apart:

| Attribute | Meaning |
|---|---|
| `feature_flag.key` | the flag key |
| `feature_flag.provider.name` | **dotted** — the older `feature_flag.provider_name` spelling is obsolete |
| `feature_flag.result.variant` | the variant returned |
| `feature_flag.result.value` | the value returned |
| `feature_flag.result.reason` | e.g. `TARGETING_MATCH`, `STATIC`, `ERROR` |
| `feature_flag.context.id` | the evaluation context's targeting key |
| `feature_flag.set.id` | the flag set — ADLC uses the experiment id |

---

## 6. The artifact-enum limitation

`artifacts[].type` is a closed enum with no member for a trace, HAR, JSONL, video or
zip. Mislabelling a Playwright trace as `image` to satisfy the enum would make the
document lie, so the exporter splits the artifact list:

**Promoted to `artifacts[]`** — screenshots, the HTML report, CSVs, charts, notebooks,
SQL, dashboards, slides and images, matched first by ADLC `kind` and then by
`mimeType`. Each keeps its ADLC identity via `adlc:kind`, `adlc:bytes` and
`hash: "sha256:…"`.

**Referenced under `extensions["adlc:artifacts"]`** — everything else, with the reason
recorded:

```json
{
  "uri": "evidence/candidate-a/trace.zip",
  "source": "adlc",
  "adlc:kind": "playwright_trace",
  "mimeType": "application/zip",
  "hash": "sha256:d2a84f4b…",
  "adlc:bytes": 481233,
  "adlc:reason": "ADLC kind 'playwright_trace' has no member in the OES artifacts[].type enum"
}
```

Nothing is lost: paths and verified hashes survive in full, and the OES spec requires
importers to safely ignore extensions they do not recognise.

---

## 7. `extensions["adlc:*"]`

Everything ADLC-specific with no OES home:

| Key | Contents |
|---|---|
| `adlc:canonicalRecord` | a pointer back to `adlc-run/v1`, stating plainly that this export is lossy |
| `adlc:run` | `runId`, `repo`, `baseSha`, `headSha`, `prNumber`, `status`, `profile`, `referencesRun`, `experimentRef`, `createdAt` |
| `adlc:capabilities` | the resolved adapter set for the run |
| `adlc:gates` | the verbatim `GateResult[]`, beyond the `qualityChecks[]` projection |
| `adlc:artifacts` | evidence the artifact enum cannot name (§6) |
| `adlc:measurements` | the raw measurement rows, with collector and artifact hash |
| `adlc:exposure` | flag provider, manifest and variant → flag-key mapping |
| `adlc:experimentStage` | per-phase attempt number, status and timestamp |
| `adlc:statistics` | `{"inference": "none", …}` with the reason |

---

## 8. LaunchDarkly: delivery and metrics only, never a gate

`adlc.adapters.flags.launchdarkly.LaunchDarklyProvider` implements the frozen
`FlagProvider` port through the **OpenFeature** provider
(`launchdarkly-openfeature-server`, `ld_openfeature`) rather than the raw LaunchDarkly
SDK, so the application-facing API stays vendor-neutral — swapping it for the spine's
flagd file provider changes no call site.

> **LaunchDarkly is not a gate authority in this design.** Its experiment-results read
> API is unverified, so nothing in ADLC gates a merge on a LaunchDarkly verdict. Gate
> decisions come from `adlc.adapters.gate.*`, are recorded in `adlc-run/v1`, and are
> aggregated by the fail-closed `ADLC / required` check. LaunchDarkly's role is flag
> **delivery** and metric **emission**.

### Availability

`detect()` is cheap, offline and non-raising. It checks only for
`LAUNCHDARKLY_SDK_KEY` in the environment and for importable modules — no network
call, and deliberately **no SDK initialization**, which would open a streaming
connection.

| Condition | Result |
|---|---|
| `LAUNCHDARKLY_SDK_KEY` unset or blank | `(False, "…not set; …the spine's credential-free flagd-file provider will be used instead")` |
| key set, packages missing | `(False, "…openfeature-sdk, launchdarkly-server-sdk, launchdarkly-openfeature-server is not installed (pip install 'adlc[flags]' …)")` |
| key set, packages present | `(True, "…flag delivery and metric emission only — LaunchDarkly never gates a run")` |

With no key the spine's `flagd-file` provider takes over and the credential-free
conformance suite is unaffected. This adapter is *"documented + disabled example"* on
the KISS ladder in `PLAN.md` §7.

### `materialize()`

LaunchDarkly flags live server-side and an SDK key is read-only, so unlike the flagd
file provider this **cannot create flags**. It writes a declarative manifest —
`flags.launchdarkly.json` — recording the variant → flag-key mapping, the target
project/environment, and the expected variations. That is what an operator (or a
Terraform / LD API step) provisions from, and it is hashable evidence of what the run
intended to serve. The manifest names `LAUNCHDARKLY_SDK_KEY`; it never contains its
value.

### `evaluate()`

Evaluates through the OpenFeature client, choosing the typed accessor from the type of
`ctx["default"]`. It **never raises**: a backend outage returns the default value with
`reason: "ERROR"`, because a flag outage must not fail a build. Every evaluation emits
the semantic-convention attributes in §5 to the injected `Telemetry`, if any.

```python
provider = LaunchDarklyProvider(run_dir=run_dir, telemetry=telemetry)
provider.materialize(run)
provider.evaluate("adlc.exp.a1b2", {"targetingKey": "ci-runner-7", "default": "control"})
```

---

## 9. Tests

`tests/l7_experiment/` runs with **no credentials and no network**:

- `test_oes_schema_fidelity.py` — the vendored schema is the published document, and
  every enum constant is re-derived from it. Includes an opt-in network test
  (`ADLC_TEST_NETWORK=1`) that re-fetches the schema and diffs it.
- `test_oes_export.py` — a golden `adlc-run/v1` fixture (itself validated against the
  frozen `schemas/adlc-run.schema.json`) is exported and validated against the real OES
  schema, with the full mapping asserted field by field, plus a scan proving no
  statistical quantity was fabricated.
- `test_oes_refusal.py` — single-variant, zero-variant, unmeasured and
  measured-on-one-variant runs are all refused, and no partial file is left behind.
- `test_launchdarkly_provider.py` — `detect()` returns `(False, reason)` with no key,
  never raises, and opens no socket; manifest and evaluation behaviour is exercised
  through an injected fake OpenFeature client.
- `test_experiment_stage.py` — append-only attempts, `run.json` untouched, tamper
  detection, stage results validated against the frozen `stageResult` schema, and an
  end-to-end plan → run → analyze → reduce → export that validates as OES.

```bash
python -m pytest tests/l7_experiment -q
ruff check src/adlc/adapters/flags src/adlc/adapters/export src/adlc/stages/experiment.py
```
