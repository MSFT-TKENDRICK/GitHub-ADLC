"""HTML assembly for the evidence report.

The whole report is one file with no build step, no bundler and no network
dependency, because of where it has to work: attached to a CI run, downloaded to
a laptop, opened from ``file://``, forwarded as an email attachment, read on a
plane. Any asset reference that points outside the file is a way for the
evidence to arrive without the proof.

The page is a **shell plus a model**. This module emits static structure -- tabs,
headings, tables that must exist without JavaScript -- and embeds the full data
model in one ``<script type="application/json">`` block. The viewer in
:data:`adlc.report.assets.JS` reads that model to build the interactive parts.
Splitting it this way means the expensive work (diffing, summarising, laying out
the graph) happened in Python long before anyone opened the page.

Templating uses ``{{TOKEN}}`` substitution rather than ``str.format`` so the
inline CSS and JS stay literal; see :mod:`adlc.report.assets`.
"""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any

from adlc.config import Config
from adlc.report.assets import CSS, JS
from adlc.report.model import build_model, graph_mermaid, to_embedded_json
from adlc.report.overlay import inject_overlay
from adlc.runs import RunDir, read_json, utcnow

__all__ = ["fill", "render", "run_report"]

_TOKEN = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def fill(template: str, mapping: dict[str, str]) -> str:
    """Replace ``{{TOKEN}}`` markers. An unknown token is left visible.

    Leaving it visible rather than raising is deliberate: a missing value should
    show up as an obvious defect in the rendered page, not stop a run from
    producing any report at all. The report is the thing a human reads when
    something has gone wrong, so it has to survive its own bugs.
    """
    return _TOKEN.sub(lambda m: mapping.get(m.group(1), m.group(0)), template)


def _gate_rows(gates: list[dict[str, Any]]) -> str:
    if not gates:
        return '<tr><td colspan="5" class="muted">No gates were evaluated.</td></tr>'
    glyphs = {"pass": "&#10003;", "fail": "&#10007;", "not_run": "&#8213;"}
    labels = {"pass": "Pass", "fail": "Fail", "not_run": "Not run"}
    rows = []
    for gate in gates:
        status = gate.get("status", "not_run")
        detail = json.dumps(
            {"observed": gate.get("observed"), "expected": gate.get("expected")},
            indent=2, default=str,
        )
        rows.append(
            f'<tr class="g-{gate["cls"]}">'
            f'<td><span class="pill {gate["cls"]}">{glyphs.get(status, "?")} '
            f'{labels.get(status, "Unknown")}</span></td>'
            f'<td class="mono">{escape(gate.get("id", ""))}'
            f'<p class="tldr">{escape(gate.get("tldr", ""))}</p></td>'
            f'<td><span class="tag">{"Required" if gate.get("required") else "Optional"}</span></td>'
            f'<td>{escape(gate.get("message", ""))}</td>'
            f'<td><details><summary>detail</summary><pre>{escape(detail)}</pre></details></td>'
            f"</tr>"
        )
    return "\n".join(rows)


def _artifact_rows(artifacts: list[dict[str, Any]]) -> str:
    if not artifacts:
        return '<tr><td colspan="5" class="muted">No artifacts captured.</td></tr>'
    rows = []
    for art in artifacts:
        digest = art.get("sha256", "")
        rows.append(
            f'<tr data-sha="{escape(digest)}">'
            f'<td class="mono">{escape(art.get("path", ""))}'
            f'<p class="tldr">{escape(art.get("tldr", ""))}</p></td>'
            f'<td><span class="tag">{escape(art.get("kind", ""))}</span></td>'
            f'<td class="num">{escape(art.get("human", ""))}</td>'
            f'<td class="mono hash" title="{escape(digest)}">{escape(digest[:16])}&hellip;</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def _requirement_rows(requirements: list[dict[str, Any]]) -> str:
    if not requirements:
        return (
            '<tr><td colspan="4" class="muted">No acceptance criteria were '
            "extracted from the spec.</td></tr>"
        )
    rows = []
    for req in requirements:
        cls = "ok" if req.get("covered") else "bad"
        label = "Covered" if req.get("covered") else "No evidence"
        kinds = ", ".join(req.get("evidenceKinds") or []) or "&mdash;"
        rows.append(
            f'<tr><td class="mono">{escape(req.get("id", ""))}</td>'
            f'<td>{escape(req.get("text", ""))}'
            f'<p class="tldr">{escape(req.get("tldr", ""))}</p></td>'
            f'<td><span class="pill {cls}">{label}</span></td>'
            f"<td>{kinds}</td></tr>"
        )
    return "\n".join(rows)


def _timeline(stages: list[dict[str, Any]]) -> str:
    if not stages:
        return '<li class="warn"><p>No stages recorded.</p></li>'
    return "\n".join(
        f'<li class="{stage["cls"]}"><div class="t-head">'
        f'<span class="mono">{escape(stage.get("stage", ""))}</span>'
        f'<span class="tag">attempt {stage.get("attempt", 1)}</span>'
        f'<time>{escape(stage.get("startedAt", ""))}</time></div>'
        f'<p>{escape(stage.get("message", ""))}</p></li>'
        for stage in stages
    )


def _rubric_block(score: dict[str, Any] | None) -> str:
    if not score:
        return '<p class="muted">No rubric score recorded for this run.</p>'
    rows = "".join(
        f'<tr><td class="mono">{escape(crit.get("id", ""))}</td>'
        f'<td><span class="pill {"ok" if crit.get("passed") else "bad"}">'
        f'{crit.get("score", 0):.2f}</span></td>'
        f'<td class="num">{crit.get("weight", 1)}</td>'
        f'<td>{escape(crit.get("rationale", ""))}</td></tr>'
        for crit in score.get("criteria", [])
    )
    overall = float(score.get("overall", 0) or 0)
    threshold = float(score.get("threshold", 0) or 0)
    bar = "ok" if score.get("passed") else "bad"
    return (
        f'<div class="meter"><div class="meter-fill {bar}" '
        f'style="width:{min(100, max(0, overall * 100)):.1f}%"></div>'
        f'<div class="meter-mark" style="left:{threshold * 100:.1f}%"></div></div>'
        f'<p class="muted">Overall <strong>{overall:.2f}</strong> against a threshold of '
        f"<strong>{threshold:.2f}</strong>.</p>"
        f"<table><thead><tr><th>Criterion</th><th>Score</th><th>Weight</th>"
        f"<th>Rationale</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _adr_list(adrs: list[dict[str, Any]]) -> str:
    if not adrs:
        return '<p class="empty">No architecture decisions recorded yet.</p>'
    classes = {"accepted": "ok", "rejected": "bad", "proposed": "warn", "superseded": "info"}
    return "\n".join(
        f'<button class="chip" data-num="{escape(adr["number"])}">'
        f'<span class="pill {classes.get(adr.get("status", ""), "warn")}">'
        f'{escape(adr.get("status", ""))}</span> {escape(adr["number"])} '
        f'{escape(adr.get("title", ""))}</button>'
        for adr in adrs
    )


def _completeness_block(pack: dict[str, Any] | None, gates: list[dict[str, Any]]) -> str:
    """Render the feature-completeness verdict and what the reviewer could see."""
    gate = next((g for g in gates if g.get("id") == "feature_completeness"), None)
    if not pack and not gate:
        return (
            '<p class="empty">This run has not been through feature-completeness '
            "review. Run <code>adlc complete</code> to build the review pack.</p>"
        )
    parts: list[str] = []
    if gate:
        parts.append(
            f'<div class="banner {gate["cls"]}"><strong>{escape(gate.get("tldr", ""))}</strong>'
            f'<div class="sub">{escape(gate.get("message", ""))}</div></div>'
        )
    if pack:
        excluded = "".join(
            f'<li><strong>{escape(item.get("what", ""))}</strong> &mdash; '
            f'{escape(item.get("why", ""))}</li>'
            for item in pack.get("excluded") or []
        )
        parts.append(
            f'<div class="grid">'
            f'<div class="stat"><div class="k">Requirements</div>'
            f'<div class="v">{len(pack.get("requirements") or [])}</div></div>'
            f'<div class="stat"><div class="k">Evidence summaries</div>'
            f'<div class="v">{len(pack.get("evidence") or [])}</div></div>'
            f'<div class="stat"><div class="k">Persona findings</div>'
            f'<div class="v">{len(pack.get("personaFeedback") or [])}</div></div>'
            f'<div class="stat"><div class="k">Uncovered</div>'
            f'<div class="v">{len(pack.get("uncovered") or [])}</div></div>'
            f"</div>"
        )
        if excluded:
            parts.append(
                "<h4>What the completeness reviewer was not allowed to see</h4>"
                f"<ul>{excluded}</ul>"
                '<p class="note">The reviewer judges the evidence against the original '
                "request. Withholding the code, the agent sessions and the internal "
                "reasoning is what makes that judgement independent: it cannot be "
                "talked round by an implementation it never saw.</p>"
            )
    return "".join(parts)


def render(cfg: Config, rd: RunDir) -> str:
    """Render the complete, self-contained report."""
    model = build_model(cfg, rd)
    graph_raw = read_json(rd.taskgraph) if rd.taskgraph.is_file() else None

    qualification = model.get("qualification") or {}
    qual_text = (
        f'{qualification.get("score", "?")}/100 &middot; '
        f'{escape(str(qualification.get("category", "")))} &middot; risk '
        f'{escape(str(qualification.get("risk", "")))}'
        if qualification else "not qualified"
    )

    diff_options = "".join(
        f'<option value="{escape(d["taskId"])}">{escape(d["taskId"])} '
        f'({d["stats"]["files"]} files, +{d["stats"]["additions"]}/'
        f'-{d["stats"]["deletions"]})</option>'
        for d in model["diffs"]
    )
    totals = {
        "files": sum(d["stats"]["files"] for d in model["diffs"]),
        "additions": sum(d["stats"]["additions"] for d in model["diffs"]),
        "deletions": sum(d["stats"]["deletions"] for d in model["diffs"]),
    }

    hero = model["media"].get("hero")
    if hero and hero.get("src"):
        hero_html = (
            f'<video controls preload="metadata" playsinline '
            f'aria-label="End-to-end recording of this run">'
            f'<source src="{hero["src"]}" type="{escape(hero["mime"])}">'
            f"Your browser cannot play this recording. It is stored at "
            f'{escape(hero["path"])}.</video>'
        )
    elif hero:
        hero_html = (
            f'<div class="noplay"><strong>Recording captured but not embedded</strong>'
            f'<p class="sub">{escape(hero.get("reason", ""))}</p>'
            f'<p class="mono sub">{escape(hero["path"])}</p></div>'
        )
    else:
        hero_html = (
            '<div class="noplay"><strong>No end-to-end recording was captured</strong>'
            '<p class="sub">A run with no recording cannot show what a user would have '
            "seen. Install Playwright and re-run the evidence stage.</p></div>"
        )

    hero_meta = ""
    if hero:
        hero_meta = (
            f'<div class="stat"><div class="k">Recording</div>'
            f'<div class="v sm">{escape(hero["human"])} &middot; '
            f'{escape(hero["mime"])}</div></div>'
            f'<div class="stat"><div class="k">SHA-256</div>'
            f'<div class="v sm mono hash" title="{escape(hero.get("sha256", ""))}">'
            f'{escape((hero.get("sha256") or "not hashed")[:24])}</div></div>'
        )
    budget = model["media"]["budget"]
    hero_meta += (
        f'<div class="stat"><div class="k">Media embedded</div>'
        f'<div class="v sm">{budget["embedded"]} inline, {budget["linked"]} linked</div></div>'
    )

    failures = model["failures"]
    return fill(_SHELL, {
        "RUN_ID": escape(model["runId"]),
        "REPO": escape(model["repo"] or "unknown repository"),
        "PROFILE": escape(str(model["profile"])),
        "STATUS": escape(str(model["status"])),
        "BASE_SHA": escape((model["baseSha"] or "")[:12]),
        "HEAD_SHA": escape((model["headSha"] or "")[:12]),
        "GENERATED": escape(model["generated"]),
        "BANNER_CLS": "ok" if model["passed"] else "bad",
        "BANNER_TEXT": (
            "All required gates passed" if model["passed"] else "Required gates did not pass"
        ),
        "REQUIRED_PASS": str(model["requiredPass"]),
        "REQUIRED_TOTAL": str(model["requiredTotal"]),
        "FAILURE_LIST": (
            "<ul>" + "".join(f"<li>{escape(f)}</li>" for f in failures) + "</ul>"
            if failures else ""
        ),
        "QUALIFICATION": qual_text,
        "STAGE_COUNT": str(len(model["stages"])),
        "NODE_COUNT": str(len(model["graph"]["nodes"])),
        "ARTIFACT_COUNT": str(len(model["artifacts"])),
        "PERSONA_COUNT": str(len(model["personas"])),
        "ADR_COUNT": str(len(model["adrs"])),
        "DIFF_FILES": str(totals["files"]),
        "DIFF_ADD": str(totals["additions"]),
        "DIFF_DEL": str(totals["deletions"]),
        "HERO": hero_html,
        "HERO_META": hero_meta,
        "PAIR_COUNT": str(len(model["media"]["pairs"])),
        "GATE_ROWS": _gate_rows(model["gates"]),
        "ARTIFACT_ROWS": _artifact_rows(model["artifacts"]),
        "REQUIREMENT_ROWS": _requirement_rows(model["requirements"]),
        "TIMELINE": _timeline(model["stages"]),
        "RUBRIC": _rubric_block(model["rubric"]),
        "ADR_LIST": _adr_list(model["adrs"]),
        "COMPLETENESS": _completeness_block(model["completeness"], model["gates"]),
        "DIFF_OPTIONS": diff_options,
        "GRAPH_MERMAID": escape(graph_mermaid(graph_raw)),
        "MODEL_JSON": to_embedded_json(model),
        "RUN_JSON": escape(json.dumps(model["run"], indent=2, default=str)),
        "CSS": CSS,
        "JS": JS,
    })


def run_report(cfg: Config, rd: RunDir) -> dict[str, Any]:
    started = utcnow()
    html = render(cfg, rd)
    html = inject_overlay(html, cfg, rd)
    rd.report.write_text(html, encoding="utf-8")
    rd.write_stage(
        "report",
        outputs=[rd.rel(rd.report)],
        message=f"report.html rendered ({len(html) // 1024} KB, self-contained)",
        data={"bytes": len(html)},
        started_at=started,
    )
    return {"path": str(rd.report), "bytes": len(html)}


_SHELL = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<!-- The "no network" promise in this module's docstring, made enforceable. Every
     image and video already travels as a data: URI, the CSS and JS are inline, and
     nothing is fetched at view time, so the report can afford the strictest policy
     there is. This is what stops a future asset reference from quietly turning an
     archived evidence artifact into a live third-party code execution surface. -->
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; media-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>ADLC run {{RUN_ID}}</title>
<style>{{CSS}}</style>
</head>
<body>
<a class="skip" href="#tab-overview">Skip to report content</a>

<div class="topbar"><div class="inner">
  <span class="brand">ADLC run <span class="mono">{{RUN_ID}}</span></span>
  <nav class="tabs" role="tablist" aria-label="Report sections">
    <button role="tab" data-tab="overview" aria-selected="true" aria-controls="tab-overview">Overview</button>
    <button role="tab" data-tab="graph" aria-selected="false" aria-controls="tab-graph">Task graph</button>
    <button role="tab" data-tab="visuals" aria-selected="false" aria-controls="tab-visuals">Visuals</button>
    <button role="tab" data-tab="diff" aria-selected="false" aria-controls="tab-diff">Diff</button>
    <button role="tab" data-tab="evidence" aria-selected="false" aria-controls="tab-evidence">Evidence</button>
    <button role="tab" data-tab="personas" aria-selected="false" aria-controls="tab-personas">Personas</button>
    <button role="tab" data-tab="decisions" aria-selected="false" aria-controls="tab-decisions">Decisions</button>
    <button role="tab" data-tab="completeness" aria-selected="false" aria-controls="tab-completeness">Completeness</button>
  </nav>
  <button id="theme" class="iconbtn">Theme</button>
</div></div>

<div class="wrap">

<section id="tab-overview" class="tabpanel active" role="tabpanel" tabindex="-1" aria-label="Overview">
  <h1>ADLC run <span class="mono">{{RUN_ID}}</span></h1>
  <div class="sub">{{REPO}} &middot; profile <strong>{{PROFILE}}</strong> &middot;
    status <strong>{{STATUS}}</strong> &middot;
    <span class="mono">{{BASE_SHA}}</span> &rarr; <span class="mono">{{HEAD_SHA}}</span></div>
  <div class="sub">Generated {{GENERATED}}</div>

  <div class="banner {{BANNER_CLS}}">
    <strong>{{BANNER_TEXT}}</strong>
    <div class="sub">{{REQUIRED_PASS}} of {{REQUIRED_TOTAL}} required gates passing.
      A required gate that did not run counts as a failure &mdash; absence of evidence
      is not evidence of correctness.</div>
    {{FAILURE_LIST}}
  </div>

  <h2>What this run did, end to end</h2>
  <div class="hero">
    <div>{{HERO}}</div>
    <div class="hero-meta">{{HERO_META}}</div>
  </div>
  <p class="note">This is the recording of the run itself, not a re-enactment.
    Everything below is a way of looking more closely at what it shows.</p>

  <div class="grid">
    <div class="stat"><div class="k">Qualification</div><div class="v sm">{{QUALIFICATION}}</div></div>
    <div class="stat"><div class="k">Tasks</div><div class="v">{{NODE_COUNT}}</div></div>
    <div class="stat"><div class="k">Stages</div><div class="v">{{STAGE_COUNT}}</div></div>
    <div class="stat"><div class="k">Artifacts</div><div class="v">{{ARTIFACT_COUNT}}</div></div>
    <div class="stat"><div class="k">Personas</div><div class="v">{{PERSONA_COUNT}}</div></div>
    <div class="stat"><div class="k">Decisions</div><div class="v">{{ADR_COUNT}}</div></div>
  </div>

  <h2>Gates</h2>
  <table><thead><tr><th>Status</th><th>Gate</th><th>Enforcement</th>
    <th>Message</th><th></th></tr></thead>
  <tbody>{{GATE_ROWS}}</tbody></table>

  <h2>Rubric</h2>
  {{RUBRIC}}

  <h2>Timeline</h2>
  <ol class="timeline">{{TIMELINE}}</ol>

  <h2>Raw run record</h2>
  <details><summary>run.json (adlc-run/v1)</summary><pre>{{RUN_JSON}}</pre></details>
</section>

<section id="tab-graph" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Task graph">
  <h2>Task graph</h2>
  <p class="note">Every column is one parallel wave: those tasks ran concurrently in
    isolated worktrees, and the next wave only began at the patch barrier. Select a node
    to see its summary, its change and the evidence that names it.</p>
  <div class="graphwrap">
    <div>
      <div class="gitgraph" id="gitgraph"></div>
      <div class="legend">
        <span><i style="background:var(--accent)"></i>implement</span>
        <span><i style="background:var(--ok)"></i>test</span>
        <span><i style="background:var(--info)"></i>doc</span>
        <span><i style="background:var(--warn)"></i>infra</span>
      </div>
      <details style="margin-top:12px">
        <summary>Diagram source (Mermaid)</summary>
        <p class="note">Copy this into any Mermaid renderer. It is deliberately not
          rendered here: doing so would mean loading a script from outside this file.</p>
        <div class="mermaid">{{GRAPH_MERMAID}}</div>
      </details>
    </div>
    <aside class="detail" id="node-detail" aria-live="polite">
      <p class="muted">Select a task to see its detail.</p>
    </aside>
  </div>
</section>

<section id="tab-visuals" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Visual comparisons">
  <h2>Before and after</h2>
  <p class="note">{{PAIR_COUNT}} comparison(s) built from the captured screenshots.
    Pairing is inferred, so every slide states the rule that produced it &mdash;
    discount a low-confidence pairing accordingly.</p>
  <div class="slideshow">
    <div class="slide-nav">
      <button class="btn" id="slide-prev" aria-label="Previous comparison">&larr; Prev</button>
      <button class="btn" id="slide-next" aria-label="Next comparison">Next &rarr;</button>
      <span id="slide-label" class="muted" role="status" aria-live="polite"></span>
      <div class="modes" id="shot-modes" role="group" aria-label="Comparison mode">
        <button data-mode="side" aria-pressed="true">Side by side</button>
        <button data-mode="before" aria-pressed="false">Before</button>
        <button data-mode="after" aria-pressed="false">After</button>
        <button data-mode="diff" aria-pressed="false">Difference</button>
      </div>
      <div class="slide-dots" id="slide-dots"></div>
    </div>
    <div class="slide-stage" id="slide-stage"></div>
    <p class="note" id="slide-rule" role="status" aria-live="polite"></p>
  </div>
</section>

<section id="tab-diff" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Code diff">
  <h2>Diff</h2>
  <div class="row">
    <span class="muted">{{DIFF_FILES}} file(s) &middot;
      <span class="plus">+{{DIFF_ADD}}</span> <span class="minus">&minus;{{DIFF_DEL}}</span></span>
    <span class="spacer"></span>
    <label class="muted" for="diff-task">Task</label>
    <select id="diff-task" class="iconbtn"><option value="">All tasks</option>{{DIFF_OPTIONS}}</select>
    <div class="modes" id="diff-modes" role="group" aria-label="Diff layout">
      <button data-mode="unified" aria-pressed="true">Unified</button>
      <button data-mode="split" aria-pressed="false">Split</button>
    </div>
  </div>
  <div id="diff-host" style="margin-top:12px"></div>
  <p class="note">Diffs are parsed and word-highlighted before the page is written, so
    the browser only paints. Large files are truncated here; the complete patch is
    always in the run's <span class="mono">patches/</span> directory.</p>
</section>

<section id="tab-evidence" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Evidence">
  <h2>Requirements and their evidence</h2>
  <table><thead><tr><th>Id</th><th>Acceptance criterion</th><th>Status</th>
    <th>Evidence kinds</th></tr></thead>
  <tbody>{{REQUIREMENT_ROWS}}</tbody></table>

  <h2>Evidence</h2>
  <table><thead><tr><th>Artifact</th><th>Kind</th><th>Size</th><th>SHA-256</th></tr></thead>
  <tbody>{{ARTIFACT_ROWS}}</tbody></table>
  <p class="note">Click a hash to copy it. Hashes are what the gates verify &mdash; a
    reviewing agent sees digests and summaries, never raw traces, HAR or console text.</p>
</section>

<section id="tab-personas" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Persona feedback">
  <h2>What the personas experienced</h2>
  <p class="note">Each card is one persona walking one scenario. Open a card to read the
    reasoning behind each step &mdash; the part a recording cannot show. Records marked
    <em>simulated</em> are derived from the spec and the captured evidence; records marked
    <em>real session</em> came from an actual person.</p>
  <div class="cards" id="persona-host"></div>
</section>

<section id="tab-decisions" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Decisions">
  <h2>Decisions</h2>
  <div class="chips" id="adr-list">{{ADR_LIST}}</div>
  <div class="adrwrap" style="margin-top:14px">
    <article class="panel" id="adr-detail">
      <p class="muted">Select a decision to read it.</p>
    </article>
    <aside class="citations" id="adr-citations" aria-label="Citations for this decision"></aside>
  </div>
  <p class="note">Decisions are recorded through native GitHub pull request reviews:
    <em>Approve</em> accepts the ADR, <em>Request changes</em> rejects it and opens a
    successor run. History is never rewritten.</p>
</section>

<section id="tab-completeness" class="tabpanel" role="tabpanel" tabindex="-1" aria-label="Feature completeness">
  <h2>Feature completeness review</h2>
  <p class="note">The last gate before ship asks a different question from every gate
    before it: not &ldquo;did the checks pass&rdquo; but &ldquo;does the collected
    evidence actually show the thing that was originally asked for&rdquo;.</p>
  {{COMPLETENESS}}
</section>

</div>

<script id="adlc-model" type="application/json">{{MODEL_JSON}}</script>
<script>{{JS}}</script>
</body>
</html>
"""
