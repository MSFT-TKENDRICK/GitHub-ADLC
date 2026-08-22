---
status: accepted
date: 2026-08-20
decision-makers: ADLC maintainers
consulted: rubber-duck, code-review, performance-adversary, security-adversary, accessibility-adversary
informed: —
adlc-run: —
adlc-review-sha: —
---

# Bind human feedback to the evidence snapshot it was authored against

## Context and Problem Statement

The human-feedback layer lets a reviewer annotate artifacts, critique agent
reasoning, and accept or reject evidence diffs, then submit the whole body of that
work as one pack that retriggers the outer loop. Review of the completed stack
surfaced a family of findings that share one root cause and therefore should share
one decision rather than being patched individually.

A pack is bound to a *run* (`runId`) and to a *commit* (`candidateSha`). It is not
bound to the **evidence snapshot** the reviewer actually looked at. Between the
moment a report or manifest is generated and the moment a pack is applied, the run
directory can gain artifacts, re-run collectors, or have reasoning regenerated.
Nothing detects that.

The individual symptoms:

1. **No `targetsDigest`.** A pack cannot state which manifest it was authored
   against, so ingestion cannot tell "this reviewer saw a different set of
   targets" from "this reviewer saw exactly these targets".
2. **The successor run records the checkout HEAD, not the reviewed candidate
   SHA.** The next design cycle can therefore begin from a tree the human never
   reviewed.
3. **The `inner` route creates a successor that cannot be driven.** It is
   reachable from the schema but produces a run no stage will pick up.
4. **Replay identity includes `submittedAt`.** A genuine retry of the same
   submission — a dropped connection, a re-clicked button — mints a *second*
   successor run instead of being recognised as the same act.
5. **The outer loop receives a lossy projection.** Feedback reaches the next cycle
   as markdown truncated at 64 000 characters, so the structured pack that was so
   carefully validated degrades to prose at the exact moment it is meant to drive
   behaviour.

Individually these look like five bugs. They are one: **the pack's identity is
under-specified, so neither side can prove they are talking about the same
evidence.**

## Decision Drivers

* Feedback that is silently dropped or silently misapplied is worse than feedback
  that is loudly refused — the reviewer has already spent the effort either way,
  and only one of those outcomes tells them.
* The framework already refuses on stale `candidateSha` and discards uncited
  findings. Failing closed on identity mismatch is the established pattern here,
  not a new one.
* `report.html` must keep working from `file://` with no backend. Any binding has
  to be computable by a page that cannot call anything.
* The GUI is being replaced shortly. Anything encoded only in the current report's
  behaviour, rather than in the schema and the manifest, will not survive that.
* Twenty layers of stacked PRs are in flight. A change that touches the schema,
  the manifest, the report, the SDK, the console and ingestion at once cannot be
  landed safely as a nineteenth rebase.

## Considered Options

* **A — fix all five now**, as one contract change across schema, manifest,
  report, SDK, console and ingestion.
* **B — fix the drift that silently destroys feedback now; defer the identity
  binding to its own layer with this ADR recording why.**
* **C — leave all of it, on the grounds that no user has hit it yet.**

## Decision Outcome

Chosen option: "B", because the two classes of defect have genuinely different
risk profiles and lumping them together serves neither.

The **drift** defects — the report and the manifest disagreeing about what a
target *is* — are already fixed, because they destroy feedback with no signal at
all and the fix is contained. A critique whose `targetRef` matches nothing, or
whose `sourceDigest` disagrees between two GUIs describing identical text, is
discarded as stale when nothing is stale. That is a silent loss of work a human
already did, and it is now pinned by a parity test that reads the emitted
artifacts rather than the functions that build them.

The **identity binding** is deferred, deliberately and with a stated shape rather
than as a vague intention:

* `feedback-targets.json` gains a `targetsDigest` over its own canonical form,
  and the report embeds the same value.
* The pack schema gains an optional `targetsDigest`, which ingestion **requires**
  once producers emit it — optional in the schema, mandatory in the gate, so
  older packs are refused explicitly rather than accepted quietly.
* A mismatch is refused the way a stale `candidateSha` already is, naming both
  digests, because "your evidence moved" is actionable and "invalid pack" is not.
* Replay identity drops `submittedAt` in favour of `packDigest`, so a retry is
  recognised as the same act while a genuinely different pack is not.
* The successor run seeds from the reviewed `candidateSha`.
* The `inner` route is either implemented or removed from the enum. Shipping an
  enum value that produces an unusable run is worse than not offering it.
* The successor brief carries the structured pack alongside the prose projection.

This is not "we will get to it". It is a specified layer with a defined
acceptance test: a pack authored against manifest *A* and applied against manifest
*B* must be refused, and the refusal must name what moved.

### Consequences

Good: the silent-loss defects are gone now, and the parity test makes the
GUI-agnostic claim enforceable instead of aspirational — a replacement GUI that
disagrees with the report fails the suite rather than losing a reviewer's work in
production.

Bad: until the binding lands, a pack applied against a run whose evidence changed
underneath it is accepted. The exposure is bounded — `candidateSha` still has to
match, so the code under review cannot have moved — but the *evidence* can have.
This is a known, written-down gap, not an unknown one.

Also accepted: `route: "inner"` remains in the schema and remains unusable until
that layer lands. It is recorded here so it is not mistaken for a working feature.

### Confirmation

The drift half is confirmed by `tests/l11_feedback/test_report_manifest_parity.py`,
which asserts the report and the manifest publish identical `targetKind`,
`targetRef` and `sourceDigest` for every reasoning target, identical annotatable
artifact hashes, and identical requirement ids — each guarded against passing
vacuously on empty input.

The deferred half is confirmed when a pack carrying a `targetsDigest` that does not
match the run's current manifest is refused with both digests named, and when
submitting the same pack twice yields one successor run rather than two.
