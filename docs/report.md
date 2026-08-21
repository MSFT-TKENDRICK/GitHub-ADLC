# The evidence report (`report.html`)

> Source: `src/adlc/report/` · summaries: `src/adlc/summarize.py`
> Command: `adlc report RUN [--open]` · tests: `tests/l11_report/`

One self-contained HTML file, written at the end of a run, that answers "what
happened, and should I believe it?" without a server, a network connection, or a
build step. It opens from `file://` off a USB stick in six months' time.

---

## 1. The constraint that shapes everything

**No backend, no network, no bundler, one file.** An evidence artifact that needs
infrastructure to read is not an evidence artifact; it is a service that will be
switched off before anyone needs it. So every asset is inlined, every dependency
is absent, and the only external reference is a relative link the reader may
choose to follow.

`tests/conformance` asserts this directly: the file starts with `<!doctype html>`
and the strings `src="./` and `href="./` do not appear anywhere in it.

The second-order consequence is the more interesting one. With no build step
there is also no client-side pipeline: **every number, summary, diff hunk and
layout coordinate is computed in Python at render time** and shipped as one JSON
blob in `<script id="adlc-model">`. The page is a viewer, not a compiler.

```
adlc.report.model.build_model()      → one dict, fully resolved
adlc.report.html.render()            → static shell + embedded model
adlc.report.assets                   → all CSS and JS, inlined
```

That is what keeps a 60-file, 200-artifact run instant on a laptop. Diffing
60 patches in JavaScript on load would not be.

---

## 2. The eight panes

| Tab | Answers |
|---|---|
| **Overview** | Did it pass, and what is the one recording that shows it? |
| **Task graph** | What work happened, in what order, and what came out of each piece? |
| **Visuals** | What changed on screen? |
| **Diff** | What changed in the code? |
| **Evidence** | What was captured, and which requirement does each piece back? |
| **Personas** | What did a user make of it, and what were they thinking? |
| **Decisions** | What was decided, why, and what informed it? |
| **Completeness** | Does the evidence actually demonstrate what was asked for? |

---

## 3. The task graph is a gitgraph, and levels are columns

`adlc.report.model._lane_layout` places each node at `column = level`,
`row = lane`. That is not a visual preference. **A level *is* a parallel wave** —
every node in one ran concurrently in an isolated worktree, and every node in the
next waited for the patch barrier. Drawing it any other way misrepresents how the
run actually executed.

Lanes are assigned by trying to keep a node in the same lane as its first
dependency, so a chain of work draws as a straight horizontal line — the shape a
reader can follow — and a fork visibly diverges instead of the whole graph
zig-zagging.

Clicking a node is a **lookup, not a search**: each node ships pre-joined with the
gates, ADRs, artifacts, requirements and diff files that belong to it.

### Every node carries a ≤150-character summary

`schemas/taskgraph.schema.json` sets `tldr.maxLength: 150`, and
`adlc.summarize` is the only thing that writes one.

150 characters is a forcing function, not a formatting rule. It is too short to
restate a node's title and too short to hedge, so the only thing that fits is the
*outcome*. `summarize.compose()` allocates the budget across clauses and
`clamp()` truncates on a word boundary, never mid-word.

The property that matters is enforced in `tests/l11_report/test_summaries.py`:
**two different things must not produce the same sentence.** A summariser that
degrades to "Task completed successfully" for every node is worse than no
summariser, because it costs a reader attention and returns nothing.

---

## 4. The recording is front and centre

`adlc.report.media.build_media` picks a **hero**: the single longest video in the
run. Length is the proxy for completeness — the full end-to-end journey is almost
always the longest capture, and a 400 ms element-level clip almost never is.

The hero renders at the top of Overview, above everything else, because a person
opening a report wants to watch the thing work before they read about it.

> **Gotcha for maintainers.** `build_media` returns `hero` *separately* from
> `videos`, and `videos` is `videos[1:]` — the non-hero remainder. Inspecting
> `videos` alone makes a correctly-embedded hero look missing.

Budgets: 6 MiB per video, 1500 KB per image, 24 MiB total. Anything over budget
is **linked with the reason stated** rather than silently dropped, so the reader
knows the capture exists and why it is not inline.

---

## 5. Before/after pairing is stated, never implied

The slideshow pairs screenshots by three ordered rules, and **the report always
says which rule it used and how confident it is**:

| Rule | Signal | Confidence |
|---|---|---|
| 1 | Filenames differing only by a marker word (`settings-before.png` / `settings-after.png`) | high |
| 2 | Baseline/candidate variant directories holding the same filename | high |
| 3 | Consecutive captures in timeline order | low — "these are adjacent, not necessarily a pair" |

A pair is labelled with its shared *subject* ("Settings"), not the marker word
("Settings before"), because the marker is already the axis of the comparison.
Rule 3 keeps its `A → B` timeline label, since there the ordering *is* the claim.

No capture is used in two pairs, and every capture appears somewhere — an
unpaired screenshot goes to the gallery rather than vanishing. Rule 1 buckets the
`after` candidates by pairing key before matching, so a run with hundreds of
captures stays linear; the earlier per-`before` rescan went quadratic precisely
when the filenames *didn't* correspond, which is the messy case that actually
occurs.

Four view modes: side-by-side, before-only, after-only, and difference. The
difference blend is a composite whose meaning is entirely visual, so it is
exposed as a single labelled image that says so and points at the two modes that
aren't — side-by-side, and the diff tab.

---

## 6. The diff is computed in Python, rendered as a table

`adlc.report.diff` parses `patches/*.patch` and emits classified lines, both
gutters, and **word-level segments within a changed line** so the eye lands on
the token that moved rather than the whole row.

Bounds are hard, and the reason is the same one as everywhere else — a report
that hangs is a report nobody opens:

| Bound | Value | Guards against |
|---|---|---|
| `MAX_LINES_PER_FILE` | 900 | a runaway hunk |
| `MAX_FILES` | 60 | a runaway patch set |
| `MAX_LINE_CHARS` | 2000 | a minified bundle with 200 KB lines |

The parser is deliberately tolerant: a missing `diff --git` header, commit prose
above the hunks, a mid-hunk truncation, or outright junk all degrade to "show
what parsed" rather than throwing. A patch file that a strict parser rejects is
still the best record of what changed.

---

## 7. Decisions link both ways, and cite their sources

### The link lives on the ADR, because only the ADR can know

`taskgraph.json` has always had `adrRefs` on each node, and `stages/graph.py` has
always written `[]` into it. That was not an oversight — it is an ordering
problem. **The graph is planned before any decision is taken.** A node cannot
name the ADR that will govern it, because that ADR does not exist yet.

The ADR is authored with the graph already in front of it, so the ADR is the only
party that can state the relationship. `stages/adr.py` therefore writes an
`adlc-tasks:` front-matter field:

```markdown
---
adlc-run: 2026-08-20-abcd
adlc-review-sha: cafebabe
adlc-tasks: T001, T002
decision-makers: …
---
```

`report/adr.py::build_adrs` resolves the relationship from **both** directions and
de-duplicates, and `report/model.py` builds the reverse map so a task node shows
the decisions made while doing it. A task reference that is not in the graph is
**kept and flagged `inGraph: false`**, not dropped: a decision can outlive the
plan that prompted it, and silently discarding the reference would hide that.

### The citations pane

`## Links` in a MADR is parsed and classified into seven groups, so "what
informed this" is navigable instead of a wall of URLs:

`Requirements` · `Evidence artifacts` · `Other decisions` · `Files` ·
`External sources` · `Runs` · `Within this document`

Trailing punctuation is trimmed, duplicates collapse on `(kind, ref)` — the same
URL cited twice is still one source — and a reference to an ADR that does not
exist is dropped rather than rendered as a dead end.

---

## 8. Personas show their reasoning

The Personas pane renders `evidence/personas/*.json` (see `docs/PLAN.md` §4.6b).
Each record is one persona in one scenario, with a ≤150-char summary, a verdict,
a sentiment, friction points — and a **step trace where every step carries a
`thought`**.

Showing the reasoning is the point. A verdict on its own is an assertion; a
verdict with the reasoning attached is something a reader can disagree with, and
disagreeing with it is the whole reason to render it.

Two labels are never blurred:

- `simulated: true` — a deterministic walkthrough derived from the spec and the
  captured evidence. Genuinely useful, and clearly not a human.
- `simulated: false` — an ingested record from a real session. Never overwritten
  by a regeneration.

---

## 9. Completeness is shown as a verdict *about the evidence*

The last pane renders the `feature_completeness` gate (`docs/squads.md` §8.3):
the pack the code-blind squad saw, what it was structurally prevented from
seeing (`excluded[]`), the verdicts, and — when a quorum blocked — the statement
that this is an **outer-loop** failure.

That framing is load-bearing. Every other red gate in this report means "the
change is wrong". This one means "we may have built the wrong thing", and the
reader should not confuse the two.

---

## 10. Accessibility and degradation

- Tabs are real `role="tab"` buttons with `aria-selected` and `aria-controls`,
  and there is a skip link to the panel content.
- **Anything that rewrites itself in place is a live region.** The task detail
  panel, the slide label and the pairing-rule line all carry
  `aria-live="polite"`, because a control whose only feedback is text changing
  silently elsewhere on the page does nothing at all for a screen-reader user.
- **A composite is described as one thing.** The difference blend sets
  `role="img"` with a single `aria-label` and marks its two layers
  `aria-hidden`; announcing "Before" and "After, blended" as separate images
  describes a result the reader cannot perceive. Ordinary captures keep their own
  alt text — only the blend is decorative.
- Every table exists in the static HTML, so gate results, requirements and
  artifacts are readable with JavaScript disabled. The interactive panes (graph,
  slideshow, diff viewer) are the enhancement.
- Mermaid is embedded as `escape(source)` inside a div and read back via
  `el.textContent` — a round-trip `tests/l9_enrich/test_diagrams.py` asserts. Do
  not "simplify" it into an attribute; the escaping is what stops a diagram
  source from becoming markup.

---

## 11. Tests

```bash
PYTHONPATH=src python -m pytest tests/l11_report -q     # 154 passed
```

| File | Covers |
|---|---|
| `test_summaries.py` | budget and word-boundary truncation; every `*_tldr` helper; the discrimination property |
| `test_diff.py` | line classification, gutters, word-level segments, file status, malformed input tolerance, all three bounds |
| `test_decisions.py` | citation classification and dedupe, MADR section parsing, `adlc-tasks` shape tolerance, all five linkage cases |
| `test_media.py` | hero selection, inline vs linked-with-reason, all three pairing rules and their confidence, budget accounting, linear pairing cost, self-pairing guard |
| `test_accessibility.py` | slideshow live regions; the difference blend exposed as one described image with decorative layers |

`tests/conformance/test_pipeline.py::test_report_is_self_contained` owns the
one-file guarantee.
