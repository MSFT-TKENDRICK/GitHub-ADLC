"""Report stage -- one self-contained, interactive HTML report per run.

Design constraints that shaped this:

* **Single file, no build step, no framework.** It is uploaded as a CI artifact
  and linked from a PR, so it must open from `file://` with nothing installed.
* **No backend.** Human actions (accept / reject / revise an ADR, annotate
  evidence) are round-tripped through *native GitHub PR reviews* via pre-filled
  deep links, so there is nothing to host and nothing to authenticate.
* **Evidence is auditable, not decorative.** Every artifact is shown with its
  SHA-256 so a reader can verify what they are looking at.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

from adlc.config import Config
from adlc.reduce import aggregate_passed, load_run
from adlc.runs import RunDir, read_json, utcnow
from adlc.stages.adr import list_adrs

_STATUS_META = {
    "pass": ("ok", "&#10003;", "Pass"),
    "fail": ("bad", "&#10007;", "Fail"),
    "not_run": ("warn", "&#8213;", "Not run"),
}


def _gate_rows(gates: list[dict[str, Any]]) -> str:
    rows = []
    for gate in gates:
        cls, glyph, label = _STATUS_META.get(gate.get("status", ""), ("warn", "?", "Unknown"))
        required = "Required" if gate.get("required") else "Optional"
        rows.append(
            f'<tr class="g-{cls}">'
            f'<td><span class="pill {cls}">{glyph} {label}</span></td>'
            f'<td class="mono">{escape(gate.get("id", ""))}</td>'
            f'<td><span class="tag">{required}</span></td>'
            f'<td>{escape(gate.get("message", ""))}</td>'
            f'<td><details><summary>detail</summary>'
            f'<pre>{escape(json.dumps({"observed": gate.get("observed"), "expected": gate.get("expected")}, indent=2))}</pre>'
            f'</details></td></tr>'
        )
    return "\n".join(rows)


def _artifact_rows(artifacts: list[dict[str, Any]]) -> str:
    rows = []
    for art in artifacts:
        size = art.get("bytes", 0)
        human = f"{size/1024:.1f} KB" if size >= 1024 else f"{size} B"
        digest = art.get("sha256", "")
        rows.append(
            f'<tr><td class="mono"><a href="{escape(art.get("path", ""))}">{escape(art.get("path", ""))}</a></td>'
            f'<td><span class="tag">{escape(art.get("kind", ""))}</span></td>'
            f'<td class="num">{human}</td>'
            f'<td class="mono hash" title="{escape(digest)}">{escape(digest[:16])}&hellip;</td></tr>'
        )
    return "\n".join(rows)


def _timeline(stages: list[dict[str, Any]]) -> str:
    items = []
    for stage in stages:
        status = stage.get("status", "ok")
        cls = {"ok": "ok", "fail": "bad", "skipped": "warn"}.get(status, "warn")
        items.append(
            f'<li class="{cls}"><div class="t-head">'
            f'<span class="mono">{escape(stage.get("stage", ""))}</span>'
            f'<span class="tag">attempt {stage.get("attempt", 1)}</span>'
            f'<time>{escape(stage.get("startedAt", ""))}</time></div>'
            f'<p>{escape(stage.get("message", ""))}</p></li>'
        )
    return "\n".join(items)


def _rubric_block(score: dict[str, Any] | None) -> str:
    if not score:
        return '<p class="muted">No rubric score recorded for this run.</p>'
    rows = []
    for crit in score.get("criteria", []):
        cls = "ok" if crit.get("passed") else "bad"
        rows.append(
            f'<tr><td class="mono">{escape(crit.get("id", ""))}</td>'
            f'<td><span class="pill {cls}">{crit.get("score", 0):.2f}</span></td>'
            f'<td class="num">{crit.get("weight", 1)}</td>'
            f'<td>{escape(crit.get("rationale", ""))}</td></tr>'
        )
    overall = score.get("overall", 0)
    threshold = score.get("threshold", 0)
    pct = min(100, max(0, overall * 100))
    bar_cls = "ok" if score.get("passed") else "bad"
    return (
        f'<div class="meter"><div class="meter-fill {bar_cls}" style="width:{pct:.1f}%"></div>'
        f'<div class="meter-mark" style="left:{threshold*100:.1f}%" title="threshold {threshold}"></div></div>'
        f'<p class="muted">Overall <strong>{overall:.2f}</strong> against a threshold of '
        f'<strong>{threshold:.2f}</strong>.</p>'
        f'<table><thead><tr><th>Criterion</th><th>Score</th><th>Weight</th><th>Rationale</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _graph_mermaid(graph: dict[str, Any] | None) -> str:
    if not graph or not graph.get("nodes"):
        return "flowchart LR\n  none[No task graph]"
    lines = ["flowchart LR"]
    levels: dict[int, list[dict]] = {}
    for node in graph["nodes"]:
        levels.setdefault(node.get("level", 0), []).append(node)
    for level in sorted(levels):
        lines.append(f'  subgraph L{level}["level {level}"]')
        for node in levels[level]:
            title = str(node.get("title", ""))[:44].replace('"', "'")
            lines.append(f'    {node["id"]}["{node["id"]}<br/>{title}"]')
        lines.append("  end")
    for node in graph["nodes"]:
        for dep in node.get("dependsOn") or []:
            lines.append(f"  {dep} --> {node['id']}")
    return "\n".join(lines)


def _adr_cards(cfg: Config, run: dict[str, Any], repo: str, pr: int | None) -> str:
    adrs = list_adrs(cfg)
    if not adrs:
        return '<p class="muted">No architecture decisions recorded yet.</p>'
    base = f"https://github.com/{repo}/pull/{pr}/files" if repo and pr else ""
    cards = []
    for adr in adrs:
        cls = {"accepted": "ok", "rejected": "bad", "proposed": "warn"}.get(adr.status, "warn")
        actions = (
            f'<a class="btn ok" href="{base}#submit-review" target="_blank" rel="noopener">Approve &rarr; accept</a>'
            f'<a class="btn bad" href="{base}#submit-review" target="_blank" rel="noopener">Request changes &rarr; revise</a>'
            if base else '<span class="muted">Link a pull request to enable review actions.</span>'
        )
        cards.append(
            f'<article class="card"><header><span class="pill {cls}">{escape(adr.status)}</span>'
            f'<h3>{escape(adr.number)} &mdash; {escape(adr.title)}</h3></header>'
            f'<p class="mono muted">docs/decisions/{escape(adr.path.name)}</p>'
            f'<div class="actions">{actions}</div></article>'
        )
    return "\n".join(cards)


def render(cfg: Config, rd: RunDir) -> str:
    run = load_run(rd)
    gates = run.get("gates") or []
    passed, failures = aggregate_passed(gates)
    artifacts = run.get("artifacts") or []
    stages = run.get("stages") or []

    score = None
    score_path = rd.evals_dir / "rubric-score.json"
    if score_path.is_file():
        score = read_json(score_path)

    graph = read_json(rd.taskgraph) if rd.taskgraph.is_file() else None
    repo = run.get("repo", "")
    pr = run.get("prNumber")

    required_total = sum(1 for g in gates if g.get("required"))
    required_pass = sum(1 for g in gates if g.get("required") and g.get("status") == "pass")
    banner_cls = "ok" if passed else "bad"
    banner_text = "All required gates passed" if passed else "Required gates did not pass"

    qualification = None
    qual_path = rd.path / "qualification.json"
    if qual_path.is_file():
        qualification = read_json(qual_path)

    return _TEMPLATE.format(
        run_id=escape(rd.run_id),
        repo=escape(repo or "unknown repository"),
        generated=escape(utcnow()),
        status=escape(str(run.get("status", ""))),
        profile=escape(str(run.get("profile", ""))),
        base_sha=escape((run.get("baseSha") or "")[:12]),
        head_sha=escape((run.get("headSha") or "")[:12]),
        banner_cls=banner_cls,
        banner_text=banner_text,
        failure_list=(
            "<ul>" + "".join(f"<li>{escape(f)}</li>" for f in failures) + "</ul>"
            if failures else ""
        ),
        required_pass=required_pass,
        required_total=required_total,
        artifact_count=len(artifacts),
        stage_count=len(stages),
        variant_count=len(run.get("variants") or []),
        qualification=(
            f'{qualification["score"]}/100 &middot; {escape(qualification["category"])} '
            f'&middot; risk {escape(qualification["risk"])}'
            if qualification else "not qualified"
        ),
        gate_rows=_gate_rows(gates),
        artifact_rows=_artifact_rows(artifacts) or
        '<tr><td colspan="4" class="muted">No artifacts captured.</td></tr>',
        timeline=_timeline(stages),
        rubric=_rubric_block(score),
        graph_mermaid=escape(_graph_mermaid(graph)),
        adr_cards=_adr_cards(cfg, run, repo, pr),
        run_json=escape(json.dumps(run, indent=2)),
    )


def run_report(cfg: Config, rd: RunDir) -> dict[str, Any]:
    started = utcnow()
    html = render(cfg, rd)
    rd.report.write_text(html, encoding="utf-8")
    rd.write_stage(
        "report",
        outputs=[rd.rel(rd.report)],
        message=f"report.html rendered ({len(html)//1024} KB, self-contained)",
        data={"bytes": len(html)},
        started_at=started,
    )
    return {"path": str(rd.report), "bytes": len(html)}


_TEMPLATE = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ADLC run {run_id}</title>
<style>
  :root {{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2128; --line:#30363d;
    --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff;
    --ok:#3fb950; --bad:#f85149; --warn:#d29922;
  }}
  [data-theme="light"] {{
    --bg:#ffffff; --panel:#f6f8fa; --panel2:#eaeef2; --line:#d0d7de;
    --fg:#1f2328; --muted:#59636e; --accent:#0969da;
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif }}
  .mono {{ font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; font-size:.86em }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:28px 22px 64px }}
  header.top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:20px; flex-wrap:wrap }}
  h1 {{ margin:0 0 6px; font-size:26px; letter-spacing:-.02em }}
  h2 {{ margin:36px 0 14px; font-size:18px; letter-spacing:-.01em }}
  h3 {{ margin:0; font-size:15px }}
  .sub {{ color:var(--muted); font-size:13px }}
  .banner {{ margin:22px 0; padding:16px 18px; border-radius:10px; border:1px solid var(--line);
    background:var(--panel); border-left:4px solid var(--muted) }}
  .banner.ok {{ border-left-color:var(--ok) }}
  .banner.bad {{ border-left-color:var(--bad) }}
  .banner strong {{ font-size:16px }}
  .banner ul {{ margin:10px 0 0 18px; color:var(--muted); font-size:13px }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:18px 0 6px }}
  .stat {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px }}
  .stat .k {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em }}
  .stat .v {{ font-size:22px; font-weight:600; margin-top:4px }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
    border:1px solid var(--line); border-radius:10px; overflow:hidden; font-size:14px }}
  th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top }}
  th {{ background:var(--panel2); font-size:12px; text-transform:uppercase;
    letter-spacing:.05em; color:var(--muted) }}
  tr:last-child td {{ border-bottom:none }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600 }}
  .pill.ok {{ background:rgba(63,185,80,.16); color:var(--ok) }}
  .pill.bad {{ background:rgba(248,81,73,.16); color:var(--bad) }}
  .pill.warn {{ background:rgba(210,153,34,.16); color:var(--warn) }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:5px; background:var(--panel2);
    border:1px solid var(--line); font-size:11.5px; color:var(--muted) }}
  .muted {{ color:var(--muted) }}
  .hash {{ cursor:copy }}
  details summary {{ cursor:pointer; color:var(--accent); font-size:13px }}
  pre {{ background:var(--bg); border:1px solid var(--line); border-radius:8px;
    padding:12px; overflow:auto; font-size:12px; max-height:340px }}
  ol.timeline {{ list-style:none; margin:0; padding:0 }}
  ol.timeline li {{ position:relative; padding:0 0 16px 22px; border-left:2px solid var(--line) }}
  ol.timeline li::before {{ content:""; position:absolute; left:-7px; top:4px; width:12px; height:12px;
    border-radius:50%; background:var(--muted); border:2px solid var(--bg) }}
  ol.timeline li.ok::before {{ background:var(--ok) }}
  ol.timeline li.bad::before {{ background:var(--bad) }}
  ol.timeline li.warn::before {{ background:var(--warn) }}
  .t-head {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap }}
  .t-head time {{ color:var(--muted); font-size:12px; margin-left:auto }}
  ol.timeline p {{ margin:4px 0 0; color:var(--muted); font-size:13px }}
  .meter {{ position:relative; height:10px; background:var(--panel2); border-radius:999px;
    overflow:hidden; margin:6px 0 4px }}
  .meter-fill {{ height:100%; border-radius:999px }}
  .meter-fill.ok {{ background:var(--ok) }} .meter-fill.bad {{ background:var(--bad) }}
  .meter-mark {{ position:absolute; top:-3px; width:2px; height:16px; background:var(--fg); opacity:.6 }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px }}
  .card header {{ display:flex; gap:10px; align-items:center; margin-bottom:8px; flex-wrap:wrap }}
  .actions {{ display:flex; gap:8px; margin-top:10px; flex-wrap:wrap }}
  .btn {{ display:inline-block; padding:5px 11px; border-radius:6px; font-size:13px;
    text-decoration:none; border:1px solid var(--line); background:var(--panel2); color:var(--fg) }}
  .btn.ok:hover {{ border-color:var(--ok); color:var(--ok) }}
  .btn.bad:hover {{ border-color:var(--bad); color:var(--bad) }}
  #theme {{ cursor:pointer; background:var(--panel2); color:var(--fg); border:1px solid var(--line);
    border-radius:6px; padding:6px 12px; font-size:13px }}
  .mermaid {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px }}
  .note {{ font-size:12.5px; color:var(--muted); margin-top:8px }}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>ADLC run <span class="mono">{run_id}</span></h1>
      <div class="sub">{repo} &middot; profile <strong>{profile}</strong> &middot;
        status <strong>{status}</strong> &middot;
        <span class="mono">{base_sha}</span> &rarr; <span class="mono">{head_sha}</span></div>
      <div class="sub">Generated {generated}</div>
    </div>
    <button id="theme">Toggle theme</button>
  </header>

  <div class="banner {banner_cls}">
    <strong>{banner_text}</strong>
    <div class="sub">{required_pass} of {required_total} required gates passing.
      A required gate that did not run counts as a failure &mdash; absence of evidence is not evidence of correctness.</div>
    {failure_list}
  </div>

  <div class="grid">
    <div class="stat"><div class="k">Qualification</div><div class="v" style="font-size:15px">{qualification}</div></div>
    <div class="stat"><div class="k">Stages</div><div class="v">{stage_count}</div></div>
    <div class="stat"><div class="k">Variants</div><div class="v">{variant_count}</div></div>
    <div class="stat"><div class="k">Artifacts</div><div class="v">{artifact_count}</div></div>
  </div>

  <h2>Gates</h2>
  <table><thead><tr><th>Status</th><th>Gate</th><th>Enforcement</th><th>Message</th><th></th></tr></thead>
  <tbody>{gate_rows}</tbody></table>

  <h2>Rubric</h2>
  {rubric}

  <h2>Task graph</h2>
  <div class="mermaid">{graph_mermaid}</div>
  <p class="note">Nodes on the same level ran concurrently in isolated worktrees; each level ends at a
    patch barrier where patches are applied in id order and tests run.</p>

  <h2>Evidence</h2>
  <table><thead><tr><th>Artifact</th><th>Kind</th><th>Size</th><th>SHA-256</th></tr></thead>
  <tbody>{artifact_rows}</tbody></table>
  <p class="note">Click a hash to copy it. Hashes are what the evidence gate verifies &mdash;
    the reviewing agent never sees raw traces, HAR or console text.</p>

  <h2>Decisions</h2>
  <div class="cards">{adr_cards}</div>
  <p class="note">Decisions are recorded through native GitHub pull request reviews:
    <em>Approve</em> accepts the ADR, <em>Request changes</em> rejects it and opens a successor run.
    History is never rewritten.</p>

  <h2>Timeline</h2>
  <ol class="timeline">{timeline}</ol>

  <h2>Raw run record</h2>
  <details><summary>run.json (adlc-run/v1)</summary><pre>{run_json}</pre></details>
</div>

<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
  (function () {{
    var root = document.documentElement;
    document.getElementById('theme').addEventListener('click', function () {{
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    }});
    document.querySelectorAll('.hash').forEach(function (el) {{
      el.addEventListener('click', function () {{
        navigator.clipboard && navigator.clipboard.writeText(el.title);
        var old = el.textContent; el.textContent = 'copied';
        setTimeout(function () {{ el.textContent = old; }}, 900);
      }});
    }});
    // Mermaid is progressive enhancement: offline the diagram source stays readable.
    if (window.mermaid) {{
      mermaid.initialize({{ startOnLoad: true, theme: 'dark', securityLevel: 'strict' }});
    }} else {{
      document.querySelectorAll('.mermaid').forEach(function (el) {{
        var pre = document.createElement('pre'); pre.textContent = el.textContent;
        el.replaceWith(pre);
      }});
    }}
  }})();
</script>
</body>
</html>
"""
