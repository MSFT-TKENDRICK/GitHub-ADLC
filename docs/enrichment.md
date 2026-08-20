# Enrichment generators (leaf L9)

The spine ships a **minimal** enrichment set — one Gherkin feature file, one
rubric, one benchmarks file. L9 adds the richer *knowledge facets* that make a
spec fully modelled, so a downstream subagent can start building without doing
its own discovery.

Everything lands in `runs/<run-id>/enrichment/` (see `docs/PLAN.md` §4.1).

```
runs/<run-id>/enrichment/
├── architecture.mmd        # L9 — component / container flow (Mermaid)
├── sitemap.mmd             # L9 — route tree (Mermaid)
├── data-model.mmd          # L9 — entity relationships (Mermaid erDiagram)
├── personas.md             # L9 — actors, goals, pains, a11y, owned criteria
├── wireframe.excalidraw    # L9 — low-fidelity page sketch (Excalidraw JSON)
├── features/*.feature      # spine
├── rubric.yaml             # spine
└── benchmarks.yaml         # spine
```

---

## The contract with the spine

Each generator is one module exposing exactly one public function:

```python
def generate(run_dir: Path, spec_text: str, cfg: Config) -> list[Path]:
    """Write artifacts into run_dir/'enrichment' and return the paths written."""
```

| Module | Facet id | Writes |
| --- | --- | --- |
| `adlc.stages.enrich_diagrams` | `diagrams` | `architecture.mmd`, `sitemap.mmd`, `data-model.mmd` |
| `adlc.stages.enrich_personas` | `personas` | `personas.md` |
| `adlc.stages.enrich_wireframe` | `wireframe` | `wireframe.excalidraw` |

Rules these modules hold themselves to:

1. **`generate()` never raises.** Every failure path — unparseable spec, wrong
   argument type, unwritable directory, missing template, hostile `cfg` — is
   caught, logged through `logging.getLogger(__name__)`, and returns `[]`.
   `stages/enrich.py` wraps the call in `try`/`except` anyway; that branch is a
   backstop, not the design.
2. **Pure, deterministic, offline.** No network, no LLM, no clock, no RNG. The
   same spec always produces byte-identical output, so the spine's stage
   `digest` is stable across re-runs and a re-run produces a clean diff.
3. **Partial output is legitimate.** The return value lists what was *actually*
   written. A spec with no routes gets no `sitemap.mmd`; a spec with no entities
   gets no `data-model.mmd`; a spec with no `As a <role>` clause gets no
   `personas.md`. Emitting a plausible-looking artifact with invented content is
   strictly worse than emitting nothing — a downstream agent cannot tell the
   difference, and will implement the invention.
4. **Nothing is written outside `run_dir/enrichment/`.**
5. **Every facet is individually skippable** via `.adlc/config.yaml`:

   ```yaml
   enrich:
     skip: [wireframe]      # diagrams and personas still run
   ```

### No LLM path

There is deliberately **no** LLM-assisted mode, not even an opt-in one. The
conformance suite runs with no credentials, these generators are on the default
path, and a facet that silently degrades between "the model was reachable" and
"the model was not" would make the run non-reproducible. Everything here is
template-and-heuristic. If a future leaf wants model-authored enrichment it
should be a *new* module with its own facet id, so `capabilities.json` can
report it honestly and a `not_run` stays visible.

---

## Why Mermaid, not images

Three reasons, in order of weight:

1. **It renders where the artifacts are read.** GitHub renders ```` ```mermaid ````
   fences natively in Markdown files, issues, PR descriptions and PR comments,
   and the spine's `report.html` renders them with mermaid.js. A PNG has to be
   uploaded, hosted and linked before anyone sees it.
2. **It diffs.** `architecture.mmd` is text under `runs/<run-id>/`, so a
   re-run's change to the model shows up as a readable line diff in review. A
   re-rendered binary image diffs as "binary files differ", which tells a
   reviewer nothing and makes the evidence trail useless.
3. **It needs no toolchain.** Generating a PNG/SVG means Graphviz, or Puppeteer,
   or a rendering service — a dependency, a container, or a network call, in a
   framework whose whole premise is a credential-free default path.

The cost is that Mermaid is a real grammar you can get wrong, and a malformed
diagram **fails silently**: GitHub shows an error box, mermaid.js renders
nothing. So the diagrams are validated before they are written.

### The Mermaid validator

`enrich_diagrams.validate_mermaid(source) -> tuple[bool, list[str]]` is a
structural linter, not a full parser. It targets the failure modes that actually
occur when a diagram is assembled from prose:

| Check | Why |
| --- | --- |
| Non-empty, not comments-only | An empty `.mmd` renders as nothing at all |
| Known diagram header (`MERMAID_HEADERS`) | A typo like `flowchat TB` is a silent no-render |
| `flowchart`/`graph` direction, **if present**, is one of `TB TD BT RL LR` | `flowchart XY` is a lexical error. A *missing* direction is legal — mermaid defaults it — so it is not rejected |
| Balanced `[]`, `()`, `{}` and `"` | The single most common generation bug |
| Balanced `subgraph` / `end` | An unclosed subgraph swallows the rest of the diagram |
| No `\|` inside node labels | `\|` delimits edge labels; a pipe in a label ends the node |
| No stray `"` inside node labels | Terminates the label early, corrupting everything after |
| Even count of `\|` per line | An unclosed `-->\|label\|` |
| No dangling edge (`a -->` at EOL) | Parse error |
| No reserved node id (`end`, `graph`, `subgraph`, …) | `end` as a node id is a classic Mermaid trap |
| ER relationship cardinality grammar | Only `\|\|`, `\|o`, `}\|`, `}o` on the left and `\|\|`, `o\|`, `\|{`, `o{` on the right are legal |
| ER attribute blocks are `type name [PK\|FK\|UK] ["comment"]` and closed | A malformed attribute kills the whole diagram |

Entity names may be bare words or quoted strings in both relationships and
attribute blocks, because mermaid accepts both.

Two carve-outs worth knowing about:

* The generic bracket scan is **skipped for `erDiagram`**, because cardinality
  tokens (`||--o{`, `}o--||`) contain intentionally unbalanced braces.
  `_validate_er` balances the attribute blocks instead.
* The label scan peels nested shape delimiters before judging quoting, so
  `svc1(["Theme Service"])` and `db1[("Reader")]` are accepted.

#### The validator was checked against the real parser

A linter that is *stricter* than Mermaid is its own bug: it makes the generator
silently drop a diagram that would have rendered. So the corpora in
`tests/l9_enrich/test_diagrams.py` (`VALID` and `INVALID`) were cross-checked
against **mermaid 11's own `mermaid.parse()`**, run headlessly under jsdom, and
`validate_mermaid` agrees with it on every case — plus the three generated
artifacts, which mermaid parses.

That cross-check caught a real defect: the validator originally required a
direction after `flowchart`/`graph`, but mermaid accepts `flowchart` bare and
defaults it. Only a *present but unrecognised* direction (`flowchart XY`) is an
error. That check is now behaviour-matched rather than assumed.

The cross-check is a **development-time** tool, not part of the suite: it needs
npm and a mermaid install, and `tests/l9_enrich` must pass with no network. Re-run
it by hand if you change the validator or the diagram builders.

### Surviving `report.py`

`src/adlc/stages/report.py` embeds Mermaid as `escape(source)` inside
`<div class="mermaid">`, then either lets mermaid.js read `el.textContent` or —
when the CDN is unreachable — copies that same `textContent` into a `<pre>`.
Both paths HTML-unescape, so the round-trip must be lossless. It is, and
`test_diagrams_survive_the_report_html_escape_roundtrip` holds that line.

This is why `sanitize_label()` has the allowlist it does. Everything it strips
(`< > # | " [ ] { } ( ) `` ; \ ~`) breaks either the Mermaid grammar or the HTML
embed; everything it keeps (`& + % ? ! ' : / , . -`) was confirmed to parse
inside a quoted label by mermaid 11's own parser, and to survive
`html.escape`/`unescape` unchanged. `#` is stripped because it opens a Mermaid
entity code; `&` is safe because a quoted label tolerates it and the escape
round-trip restores it exactly.

> **Not yet wired up:** `report.py` currently renders only the *task graph*
> diagram. It does not read `enrichment/*.mmd`, so these diagrams do not appear
> in `report.html` yet. That file is spine-owned; the artifacts are valid and
> ready whenever the spine adds the section.

Generated labels are pushed through `sanitize_label()`, which strips everything
outside `[\w \-.,:/'&+%?!]`, collapses whitespace and truncates. Node ids come
from `mermaid_id()`, which slugifies and prefixes so a label can never collide
with a keyword.

### What each diagram contains

**`architecture.mmd`** — a C4-ish container view: `Actors → UI surfaces →
Services and logic → Data`, one `subgraph` per layer. Edges are drawn
**between layers, not between individual nodes**. The spec reliably says which
things exist; it almost never says which service reads which store, and
inventing that wiring produces a confidently wrong diagram that a downstream
agent would then implement. Actors come from `As a <role>` clauses, surfaces
from routes, services from `<Proper Noun> (Service|API|Worker|Gateway|…)`
phrases in `plan.md`/`spec.md`, and data nodes from the extracted entities.

**`sitemap.mmd`** — `flowchart LR` route tree. Routes come from `spec/contracts/`
(OpenAPI `paths:` keys) and from backticked `/…` tokens or route-ish lines in
`spec.md`/`plan.md`. Intermediate segments are materialised, so
`/settings/appearance` also yields a `/settings` node. Path parameters are
rewritten `{readerId}` → `:readerId`, because a brace inside a flowchart label
is a syntax error. Routes are sorted UI-first so the entry point is meaningful.
Skipped entirely when the spec names no routes.

**`data-model.mmd`** — `erDiagram`. Entities and typed attributes come from
`spec/data-model.md` (heading per entity, bullets or a table for fields) and
from the `Key Entities` section of `spec.md` (bold bullets, no types). Names are
camel-split and upper-snaked, so `ThemePreference` becomes `THEME_PREFERENCE`
and matches a prose mention of either spelling. **Relationships are only emitted
when the spec states one** — `has many`, `one-to-many`, `1:N`, `has one`,
`belongs to`, `owned by`, `references`, `many-to-many`. Two entities merely
appearing in the same sentence is not a relationship. Skipped entirely when the
spec names no entities.

---

## `personas.md`

Rendered from `src/adlc/templates/personas.md.j2`. Each persona carries name,
role, goals, pain points, technical proficiency, accessibility needs, and the
acceptance-criteria ids it owns.

The grounding rule is the whole point: **personas are extracted, not invented.**

| Field | Source |
| --- | --- |
| Role | the `As a <role>` clause of a user story |
| Goals | `I want <X> so that <Y>` in the same story |
| Acceptance criteria | `US1-AC2` / `FR-001` / `NFR-002` / `SC-001` ids inside that story's block |
| Pain points | sentences in the story or the spec's `Problem`/`Why`/`Background` section that match friction language (`cannot`, `no way to`, `manual`, `slow`, `error-prone`, …) |
| Technical proficiency | keyword classification of the role, defaulting to *assume the low end* |
| Accessibility needs | a11y signals in the story and spec (screen reader, keyboard, contrast, motion, media, zoom, forms) plus a WCAG 2.2 AA baseline that is always present |
| Quotes | the story sentence itself, so a reader can audit the derivation |

Stories that share a role are merged into one persona. Names are picked from a
fixed list indexed by `sha256(role)`, so they are stable across runs and unique
within a document. Specs hard-wrap, so extraction runs on whitespace-collapsed
paragraphs split at each `As a`, not on raw lines.

Acceptance-criteria ids that appear in the spec but in no user story are
reported at the top of the document as **unclaimed** — that is a real spec
defect (a requirement with no actor), and surfacing it is more useful than
quietly assigning it to whoever happens to be first.

If the spec contains no `As a <role>` clause at all, no file is written.

---

## `wireframe.excalidraw`

A low-fidelity page sketch: browser frame, header, nav row (one box per route),
content blocks (one per user story, captioned with its actor), a primary CTA
whose verb is taken from the story, and an annotation arrow citing the feature
and its acceptance-criteria ids. Plus a footer reminding the reader it is
low-fidelity and generated.

It opens at <https://excalidraw.com>, in the VS Code Excalidraw extension, and
in Obsidian.

### Schema notes (verified against `excalidraw/excalidraw` @ `master`)

Sources read directly, not recalled:

* `packages/element/src/types.ts`
* `packages/common/src/constants.ts`

**Envelope**

```jsonc
{
  "type": "excalidraw",   // EXPORT_DATA_TYPES.excalidraw
  "version": 2,           // VERSIONS.excalidraw
  "source": "…",
  "elements": [ … ],
  "appState": { "gridSize": 20, "gridStep": 5,
                "gridModeEnabled": false, "viewBackgroundColor": "#ffffff" },
  "files": {}
}
```

**`_ExcalidrawElementBase` — required on every element**

`id`, `type`, `x`, `y`, `width`, `height`, `angle`, `strokeColor`,
`backgroundColor`, `fillStyle`, `strokeWidth`, `strokeStyle`, `roundness`,
`roughness`, `opacity`, `seed`, `version`, `versionNonce`, `index`, `isDeleted`,
`groupIds`, `frameId`, `boundElements`, `updated`, `link`, `locked`.

Three of these are missed by most hand-written generators and are emitted here:
`strokeStyle` (`"solid" | "dashed" | "dotted"`), `roundness`
(`null | {type, value?}`), and the pair `index` / `frameId` (both `null` for a
flat, frameless scene). `angle` is `Radians`, not degrees.

**`type: "text"` adds** `text`, `fontSize`, `fontFamily`, `textAlign`,
`verticalAlign`, `containerId`, `originalText`, `autoResize`, `lineHeight`.

* `fontFamily` is a **number**, not a string: `FONT_FAMILY` maps
  `Virgil: 1, Helvetica: 2, Cascadia: 3, Excalifont: 5, Nunito: 6,
  "Lilita One": 7, "Comic Shanns": 8, "Liberation Sans": 9, Assistant: 10`
  (4 is deliberately unused). We use Excalifont (5, the current default), Nunito
  (6) and Cascadia (3).
* `textAlign` ∈ `left | center | right`; `verticalAlign` ∈ `top | middle | bottom`.
* `lineHeight` is **unitless** (1.25 here), not pixels.
* Text is emitted **free-floating** — `containerId: null`, and containers keep
  `boundElements: null`. Bound text requires a matching
  `boundElements: [{id, type: "text"}]` on the container and is re-laid-out by
  `redrawTextBoundingBox` on load; free text is simpler and always survives.

**`type: "arrow" | "line"` adds** `points`, `startBinding`, `endBinding`,
`startArrowhead`, `endArrowhead`; arrows additionally need `elbowed`, lines
`polygon`. `points` is a list of `[x, y]` pairs **relative to the element
origin**, and the first point is `[0, 0]`.

### Determinism

`id`, `seed` and `versionNonce` are derived from
`sha256(feature_title + "#" + element_index)` rather than randomised, and
`updated` is a fixed epoch (`FIXED_UPDATED`). Excalidraw only uses these for
collaborative reconciliation, so fixing them is safe — and it means a re-run
with an unchanged spec produces an identical file.

### Validation

`enrich_wireframe.validate_excalidraw(document)` checks the envelope, every
base field on every element, the extra fields for text and linear elements, id
uniqueness, enum membership for `textAlign`/`verticalAlign`, and that every
numeric field is finite (`json.dumps` will happily write `NaN`/`Infinity`;
`JSON.parse` throws on both). The document is built by rendering the template,
parsing the result with `json.loads`, and validating — so a broken template
fails loudly here instead of producing a file nobody can open.

---

## Adding a new facet

1. Create `src/adlc/stages/enrich_<facet>.py` with the exact signature above,
   a module-level `FACET = "<facet>"`, and a `log = logging.getLogger(__name__)`.
2. Wrap the entire body of `generate()` in `try`/`except Exception`, log with
   `log.exception(...)`, and `return []`. Never let an exception escape.
3. Honour the skip list — copy `_skipped(cfg)`; it must tolerate `cfg` being
   `None` or an object whose `.raw` raises.
4. Derive content from `spec_text` and `run_dir/spec/**` only. No network, no
   clock, no RNG. If you cannot ground a section in the spec, omit it.
5. **Validate before writing.** If your format has a grammar, write a validator
   and unit-test it against known-bad input. An artifact that fails to parse is
   worse than a missing artifact, because nothing downstream reports it.
6. Add `tests/l9_enrich/test_<facet>.py`, and add the module to `MODULES` in
   `tests/l9_enrich/test_contract.py` so it inherits the never-raises,
   never-writes-outside-`enrichment/`, and individually-skippable suites.
7. Document the artifact in the table at the top of this file.

Run the suite with:

```powershell
# All ten leaves share one system Python, so never `pip install -e .` --
# whoever ran it last wins and every other session imports the wrong code.
$env:PYTHONPATH = "<your worktree>\src"
python -m pytest tests/l9_enrich -q
ruff check src/adlc/stages/
```

(`tests/l9_enrich/conftest.py` also prepends the worktree's `src` to
`sys.path`, so the suite is correct even without `PYTHONPATH` set.)

Both must pass with **no credentials and no network**.
