# Building a review GUI for ADLC

This document is for whoever writes the *next* evidence page. It describes the
contract a review surface must satisfy, and nothing about how the current one
looks.

ADLC's human-feedback loop is deliberately split at a seam so that replacing the
GUI does not mean rewriting the loop:

```
run directory ──► feedback-targets.json ──► [ANY GUI] ──► human-feedback pack ──► adlc feedback apply ──► successor run
                  adlc-feedback-targets/v1                adlc-human-feedback/v1                          route=outer
                  (input contract)                        (output contract)
```

Everything to the left of `[ANY GUI]` is ADLC's problem. Everything to the right
is ADLC's problem. The GUI's entire job is: **render the input document, and emit
a valid output pack.** It needs no Python, no access to the run directory, and no
knowledge of ADLC internals.

There are two reference consumers, and they share zero code with each other:

| Consumer | Built by | Purpose |
|---|---|---|
| `report.html` | `adlc report` | The self-contained evidence page you can email |
| `feedback-console.html` | `adlc feedback console` | Proof the contract is GUI-agnostic |

If a change breaks only one of them, it is a GUI bug. If it breaks both, it is a
contract bug.

## Step 1 — get the input document

```console
$ adlc feedback targets latest --out targets.json
$ adlc feedback targets latest --endpoint http://127.0.0.1:8765/feedback
```

`feedback-targets.json` validates against
[`schemas/feedback-targets.schema.json`](../schemas/feedback-targets.schema.json).
It is a plain JSON file with `additionalProperties: false` throughout, so a
field you do not recognise is a version skew, not an extension.

| Key | What a GUI does with it |
|---|---|
| `run` | Identity: `runId`, `candidateSha`, `baselineRunId`, `reportDigest`. Display it; echo `runId`/`candidateSha` back in the pack. |
| `requirements[]` | The things evidence is *for*. Offer them as linkable ids on annotations and critiques. |
| `artifacts[]` | Evidence files. `inline` is a `data:` URI when it fit the budget; otherwise `inline` is `null` and `inlineOmittedReason` says why. **Never drop an omitted artifact** — the reviewer must still be able to annotate it as a whole. |
| `reasoning[]` | Every agent-authored argument: squad findings, personas, rubric rationales, ADR justifications. Each carries a `sourceDigest` over the exact text shown. |
| `diff` | `measurements`, `coverage`, `screenshots` against `baselineRunId`. Each row is normalised to a `targetId` and carries a pre-computed `regression` flag. A changed screenshot row's own `inline` is `null` by design — the candidate image is inlined once under `artifacts[]`; recover it by matching the row's `sha256`. |
| `submission` | The output contract, machine-readable: enums, limits, id pattern, and the endpoint (if any). **Read your enums from here, never hard-code them.** |
| `budgets` | What was inlined and what was skipped, so a GUI can be honest about it. |

### The `submission` block is the whole point

```json
{
  "packSchemaVersion": "adlc-human-feedback/v1",
  "reviewFence": "adlc-human-feedback",
  "enums": { "verdict": [...], "route": [...], "severity": [...], "shape": [...],
             "critiqueTargetKind": [...], "critiqueStance": [...],
             "diffTargetKind": [...], "diffDecision": [...] },
  "limits": { "commentChars": 4000, "annotations": 500, ... },
  "idPattern": "^[A-Za-z0-9._-]+$",
  "endpoint": "http://127.0.0.1:8765/feedback",
  "nonceHeader": "X-ADLC-Nonce",
  "nonce": "…",
  "maxBodyBytes": 8388608
}
```

These values are *derived from the pack schema at build time*, not copied. If the
schema gains a stance or tightens a limit, every GUI that reads `submission`
follows automatically, and one that hard-coded the old list starts producing
packs the CLI rejects. Derivation fails loudly (`SchemaDerivationError`) rather
than emitting an empty enum, because an empty enum is the same rot as a stale
hand-copied one, just discovered later.

The key names matter as much as the values. `enums.shape` is spelled exactly
that, because it is the `shape` property of an annotation in the pack schema. A
GUI reading `enums.annotationShape` gets `undefined`, renders an empty control,
and fails at submit — which is precisely how the reference console shipped
broken until a test compared it against the real manifest instead of a
hand-written fixture. Do not retype these names from this document; read them
from the file.

## Step 2 — use the SDK (or reimplement it exactly)

```console
$ adlc feedback sdk --out ./assets
wrote ./assets/adlc-feedback.js, ./assets/adlc-feedback.mjs
```

Both files are generated from one source, so no two consumers can disagree about
the canonical digest. `adlc-feedback.js` is a UMD build (classic `<script>`,
CommonJS); `adlc-feedback.mjs` is the ES-module surface.

The SDK is **DOM-free**. It has no opinion about your markup, your framework, or
your styling. It does exactly the things that are easy to get subtly, silently
wrong:

```js
const session = AdlcFeedbackSDK.createSession(targets);

session.addAnnotation({
  artifactSha256: targets.artifacts[0].sha256,
  shape: "rect",
  points: [[0.10, 0.20], [0.42, 0.55]],   // fractions of natural size
  severity: "major",
  comment: "The empty state renders above the fold.",
  requirementIds: ["US1-AC1"],
});
session.critiqueFor(targets.reasoning[0].id, "needs_evidence", "No trace supports this.");
session.decide({ targetKind: "measurement", targetId: "lcp_ms", decision: "reject" });

session.setVerdict("revise");
const pack = session.buildPack();          // throws if the pack contradicts itself
await session.toBlob();                    // download
await session.submit();                    // POST, when an endpoint exists
```

### What the SDK enforces, and why it enforces it *there*

Validation happens at authorship time, in front of the reviewer, not at ingest
time in a CI log the reviewer will never read.

* **Citation-or-discard.** An annotation naming an `artifactSha256` that is not
  in the manifest throws immediately. Uncited feedback is not evidence.
* **Enum membership.** Every enum value is checked against `submission.enums`.
* **Caps.** Array and text limits come from `submission.limits`.
* **Geometry normalisation.** Coordinates are fractions of *natural* image size,
  quantised to 4 decimals. A reviewer's viewport width must never change what an
  annotation means.
* **Self-contradiction.** `blockingConflicts()` reports a verdict of `accept`
  that coexists with blocker-severity items or rejected diff rows. `buildPack()`
  refuses to build one.

### The canonical-JSON rule (read this before reimplementing)

`packDigest` must agree with Python's `pack_digest` byte for byte. The
non-obvious parts:

1. **Keys are sorted recursively.** `JSON.stringify` preserves insertion order;
   Python's `sort_keys=True` does not.
2. **All numbers are quantised to 4 decimals.** Python switches to exponent
   notation at `1e-4`, JavaScript at `1e-6`. On a 4-decimal grid the two agree
   exactly. `canonicalize` **throws** on an off-grid non-integer rather than
   digesting a value the two languages render differently.
3. **`1.0` and `1` are the same number in JavaScript.** The invariant that holds
   is the *wire round-trip*: a pack reaches Python as JSON text and is parsed
   before hashing, so `js_canonical == canonical_bytes(json.loads(js_wire))`.
   That is what `tests/l11_feedback/test_sdk_parity.py` asserts, under a real
   node process.
4. **Lone surrogates survive `JSON.stringify` and kill Python's UTF-8 encode.**
   `cleanText` strips them.

`packDigest` is **optional** on ingest. A page opened from `file://` may have no
`SubtleCrypto`; the SDK omits the digest rather than faking one.

## Step 3 — get the pack back into ADLC

Two paths. Both end in the same code.

**Export (the contract path, always available):**

```console
$ adlc feedback apply --pack feedback-2026-08-20.json
```

Works from `file://` with no server, no ports, and no browser permissions.

**Loopback POST (a convenience wrapper over the same CLI):**

```console
$ adlc report serve latest
```

Binds `127.0.0.1` only, requires a one-time nonce carried in the report URL,
rejects non-loopback `Origin`, and answers `OPTIONS` with `405` on purpose —
without a nonce, any page in the browser could POST to localhost. Because there
is no preflight, a custom nonce header only works same-origin, which means the
GUI must be served *by* the loopback server. From `file://`, `submission.endpoint`
is absent and export is the only egress. That is the designed path, not a
degraded one.

### Authority does not travel with the file

A downloaded JSON file carries no permission. Locally, `adlc feedback apply` is
fine — it is your machine. In CI a pack must arrive through something that
already proves write access: a native PR review or `workflow_dispatch`. Never an
unauthenticated issue comment.

**Review transport (the authorised path in CI):**

```js
const body = await session.toReviewBody();  // paste as a PR review
```

That wraps the pack in a fenced block tagged with `submission.reviewFence`.
The fence string is published in the manifest for one reason: CI finds the pack
by matching that tag, and a GUI that had to learn it by reading
`.github/workflows/adlc-feedback.yml` would be hard-coding exactly the thing
this manifest exists to delete. Read it from `submission.reviewFence`.

Both ends of the fence are anchored to the start of a line, in the workflow and
in the SDK. That is load-bearing: an unanchored closing fence stops at the first
backtick run *anywhere*, so a reviewer who typed a code block into their own
summary would silently truncate their pack. `tests/l11_feedback/test_review_fence.py`
extracts the regex from the real workflow file and runs it over a body the real
SDK produced, so the two cannot drift apart.

## What happens on submit

`adlc feedback apply`:

1. validates the pack against `adlc-human-feedback/v1`;
2. **refuses a stale pack** whose `candidateSha` is not the run's `headSha`;
3. **discards uncited items** — an annotation naming a hash absent from
   `run.artifacts` is dropped *and recorded as dropped*;
4. sanitises every free-text field (length caps, control-character stripping)
   because the pack becomes input to an agent;
5. takes an `O_EXCL` claim on the pack identity **before** doing any work, so two
   identical submissions cannot both fork the lineage;
6. writes an immutable feedback record and a `feedback` stage result;
7. creates a successor run with `referencesRun` and **`route=outer`**, whose
   brief carries the rendered feedback under an explicit provenance header.

`route=outer` is the retrigger. It is persisted on the run — in
`schemas/adlc-run.schema.json`, in `Run`, in `RunDir.create` and in `reduce_run` —
not merely written into stage data, so CI can branch on it. `OUTER_LOOP_STAGES`
is `("qualify", "spec", "enrich", "graph")`: the successor re-specs rather than
just rebuilding.

History is never rewritten. Feedback always moves forward.

## Dry-run it first

`adlc feedback apply` is a one-way door. Before you spend an hour annotating,
or before your GUI POSTs something it built, ask what would happen:

```bash
adlc feedback validate --pack pack.json --run 2026-08-20-c0de
```

This reports `wouldApply`, the refusal reason if any, the verdict and route, and
— most usefully — **which annotations would be discarded as uncited**, which is
the failure a reviewer otherwise discovers only by noticing their work is not in
the successor brief. It writes nothing and takes no claim.

There is exactly one implementation of the refusal rules (`plan_feedback`), and
both the dry run and the real apply call it, so the prediction cannot drift from
the decision.

## Accessibility is part of the contract, not a polish pass

Both reference GUIs shipped the same class of defect independently, which is how
you know it is a property of the *problem* rather than of one implementation. An
adversarial review found, in both: annotations that could be created but never
listed, read back, edited or deleted; `disabled` applied to the button the user
had just activated, blurring focus to `<body>` at the exact moment the task
completed; live regions styled `:empty { display:none }`, which removes them from
the accessibility tree so nothing they ever say is announced; and a changed
screenshot whose only disclosure was a CSS blend mode — asking a reviewer to make
a loop-retriggering judgement about a difference they cannot perceive.

Treat these as requirements:

- **A visual mark is not a record.** Every annotation must also exist in a list
  that states its severity, its position in words (`region from 10%, 20% to
  35%, 45%`), and its comment. Geometry is data; if it exists only as SVG inside
  a `role="presentation"` overlay, it does not exist.
- **Creation is a third of the job.** Edit and delete must be reachable too, or a
  mis-clicked mark is permanent in the pack.
- **Never disable the focused element.** Use `aria-disabled` plus an early return
  that announces the blocking reason, and `aria-busy` for in-flight work. A
  `disabled` button is unfocusable, so the `aria-describedby` explaining why it
  is unavailable can never be reached.
- **Hide live regions with the clip recipe**, never `display:none`, `hidden`, or
  `:empty`.
- **Summarise, do not enumerate.** A freehand annotation has up to 400 points;
  announce its extent, not its path.
- **Every difference needs a non-visual form.** Render the hashes and byte deltas
  you already hold as text.
- **Reserve space for sticky headers** with `scroll-margin-top`, or the browser
  scrolls each newly focused control underneath one.
- **Do not rewrite an assertive live region on every keystroke.** Guard the
  assignment on the text actually changing, or the alert interrupts the user's
  own typing echo and the field becomes impossible to compose.

## A checklist for a new GUI

- [ ] Reads `feedback-targets.json`; imports nothing from `adlc.stages.report`.
- [ ] Takes enums, limits and the id pattern from `submission`, not from source.
- [ ] Takes the PR-review fence from `submission.reviewFence`, never from workflow YAML.
- [ ] Renders omitted artifacts with their reason and still allows a `whole` annotation.
- [ ] Stores geometry as `0..1` fractions of natural size.
- [ ] Shows `sourceDigest` reasoning text verbatim — the digest pins what the human read.
- [ ] Every action reachable by keyboard alone; state changes announced via `aria-live`.
- [ ] Annotations are listed, described in words, editable and deletable — not just drawn.
- [ ] No control is ever `disabled` while focused; `aria-disabled`/`aria-busy` instead.
- [ ] Live regions are clipped, never `display:none`, and are not rewritten per keystroke.
- [ ] Severity and regression conveyed by more than colour.
- [ ] Never uses `innerHTML` on manifest values; escapes `<` in any JSON island.
- [ ] Surfaces `blockingConflicts()` before submit rather than after rejection.
- [ ] Falls back to download/copy when there is no endpoint.
- [ ] Checked against `adlc feedback validate --run` before trusting its output.

Run `adlc feedback console --out console.html` and read
`src/adlc/assets/feedback-console/console.js` for a complete worked example in
~600 lines of framework-free JavaScript.

## Related

* [`docs/feedback.md`](feedback.md) — the feedback loop end to end
* [`docs/evidence.md`](evidence.md) — what counts as evidence
* [`schemas/feedback-targets.schema.json`](../schemas/feedback-targets.schema.json)
* [`schemas/human-feedback-pack.schema.json`](../schemas/human-feedback-pack.schema.json)
