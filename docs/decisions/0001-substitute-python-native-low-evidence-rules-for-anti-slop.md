---
status: proposed
date: 2026-08-20
decision-makers: pending owner approval
consulted: adversarial review
informed: repository maintainers
adlc-run: n/a
adlc-review-sha: n/a
---

# Substitute Python-native low-evidence rules for anti-slop

## Context and Problem Statement

We were asked to install and configure
[`@dmmulroy/anti-slop`](https://github.com/dmmulroy/anti-slop) and address its
findings.

`anti-slop` is a set of Oxlint rules for **TypeScript and JavaScript**. All
fifteen generic rules target TS constructs: `unknown` parameters and returns,
chained type assertions, `Record<string, unknown>`, `Reflect.get`/`Reflect.apply`,
and Vitest/Jest module mocks. Its own install skill targets a TypeScript or
JavaScript repository.

This repository is Python: 162 tracked `.py` files, zero tracked `.ts`, `.js`,
`.tsx` or `.jsx` files, no `package.json`, no `tsconfig.json`. The only
JavaScript is a `<script>` block in a generated HTML report and a
`replay.spec.ts` template rendered into gitignored run directories.

## Decision Drivers

* The intent behind the request — catching low-evidence patterns in
  largely agent-authored code — is worth serving.
* Adding a Node toolchain to a Python repository has ongoing cost.
* A check should report a result about something it actually examined.

## Considered Options

* Vendor `anti-slop` as requested.
* Vendor it scoped to future TypeScript, plus a Python equivalent.
* Substitute Python-native rules now; revisit if real TypeScript appears.

## Decision Outcome

**Proposed:** substitute Python-native rules.

`anti-slop` would examine zero files here. Pre-commit does distinguish
`Skipped (no files to check)` from `Passed`, so this is not automatically a
false green — but a vendored rule set that nothing exercises rots, and the
toolchain cost starts immediately.

**This is a deviation from an explicit instruction, made while the requester was
unavailable, and it is not approved.** It should be reviewed on that basis. If
the owner prefers the tool vendored regardless, that is a reasonable call and
this ADR should be rejected.

### Consequences

* **Good**: rules apply to Python files that exist today.
* **Good**: no Node toolchain in a Python project.
* **Bad**: not rule-for-rule identical to `anti-slop`; the mapping is ours to
  maintain.
* **Bad**: `anti-slop` is a maintained, reviewed rule set. Ours is not yet.
* **Caveat**: Python's `Any` is not the analogue of TypeScript's `unknown`. It
  is closer to `any` — less safe, and it does not force narrowing at the
  boundary. The substitution is weaker than the original in that specific way.

### Confirmation

This ADR is **not yet confirmed**. It moves to `accepted` only when both hold:

1. An owner approves the deviation.
2. The replacement rules exist, have tests that demonstrate each rule failing on
   a known-bad input, and are enforced in CI.

Until then no claim is made that the requested capability has been delivered.

## More Information

Revisit if this repository grows a tracked TypeScript surface. `anti-slop`'s
README recommends vendoring `src/` into the consuming repository rather than
depending on it as a package.
