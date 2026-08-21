"""Inline CSS and JavaScript for the report.

Kept as plain strings in their own module for one reason: they are the two
largest blobs in the report, and interpolating them through ``str.format`` would
mean brace-doubling every CSS rule and every JS object literal. That is a
maintenance trap -- one missed brace produces a runtime error a long way from its
cause. :func:`adlc.report.html.fill` substitutes ``{{TOKEN}}`` markers instead,
so this file stays valid CSS and valid JS that can be linted and pasted as-is.
"""

from __future__ import annotations

__all__ = ["CSS", "JS"]

CSS = """
:root {
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2128; --panel3:#21262d; --line:#30363d;
  --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent-dim:rgba(88,166,255,.14);
  --ok:#3fb950; --bad:#f85149; --warn:#d29922; --info:#a371f7;
  --add-bg:rgba(63,185,80,.13); --add-word:rgba(63,185,80,.35);
  --del-bg:rgba(248,81,73,.13); --del-word:rgba(248,81,73,.35);
  --gut:#6e7681; --radius:10px;
  --shadow:0 8px 28px rgba(1,4,9,.55);
}
[data-theme="light"] {
  --bg:#ffffff; --panel:#f6f8fa; --panel2:#eaeef2; --panel3:#dde3ea; --line:#d0d7de;
  --fg:#1f2328; --muted:#59636e; --accent:#0969da; --accent-dim:rgba(9,105,218,.1);
  --ok:#1a7f37; --bad:#cf222e; --warn:#9a6700; --info:#8250df;
  --add-bg:rgba(26,127,55,.1); --add-word:rgba(26,127,55,.28);
  --del-bg:rgba(207,34,46,.1); --del-word:rgba(207,34,46,.28);
  --gut:#8c959f; --shadow:0 8px 28px rgba(31,35,40,.12);
}
* { box-sizing:border-box }
html { scroll-behavior:smooth }
body {
  margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
}
.mono { font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; font-size:.86em }
.muted { color:var(--muted) }
.wrap { max-width:1280px; margin:0 auto; padding:0 22px 80px }
a { color:var(--accent) }

/* ---- skip link + focus: the report is read by keyboard more than it looks ---- */
.skip {
  position:absolute; left:-9999px; top:0; z-index:99;
  background:var(--accent); color:#fff; padding:10px 16px; border-radius:0 0 8px 0;
}
.skip:focus { left:0 }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:4px }

/* ---- top bar ---- */
.topbar {
  position:sticky; top:0; z-index:30; background:var(--bg);
  border-bottom:1px solid var(--line); backdrop-filter:blur(8px);
}
.topbar .inner {
  max-width:1280px; margin:0 auto; padding:11px 22px;
  display:flex; gap:16px; align-items:center; flex-wrap:wrap;
}
.brand { font-weight:650; letter-spacing:-.01em; white-space:nowrap }
.brand .mono { color:var(--muted); font-weight:400 }
nav.tabs { display:flex; gap:2px; flex-wrap:wrap; margin-left:auto }
nav.tabs button {
  background:transparent; border:1px solid transparent; color:var(--muted);
  padding:6px 12px; border-radius:7px; font:inherit; font-size:13.5px; cursor:pointer;
}
nav.tabs button:hover { color:var(--fg); background:var(--panel) }
nav.tabs button[aria-selected="true"] {
  color:var(--fg); background:var(--panel); border-color:var(--line);
}
.iconbtn {
  background:var(--panel); color:var(--fg); border:1px solid var(--line);
  border-radius:7px; padding:6px 11px; font:inherit; font-size:13px; cursor:pointer;
}
.iconbtn:hover { border-color:var(--accent); color:var(--accent) }

/* ---- headline ---- */
h1 { margin:26px 0 6px; font-size:25px; letter-spacing:-.02em }
h2 { margin:34px 0 12px; font-size:18px; letter-spacing:-.01em }
h3 { margin:0 0 6px; font-size:15px }
h4 { margin:16px 0 6px; font-size:13px; text-transform:uppercase;
     letter-spacing:.06em; color:var(--muted) }
.sub { color:var(--muted); font-size:13px }
.note { font-size:12.5px; color:var(--muted); margin-top:8px; max-width:82ch }

.banner {
  margin:18px 0; padding:15px 18px; border-radius:var(--radius);
  border:1px solid var(--line); background:var(--panel); border-left:4px solid var(--muted);
}
.banner.ok { border-left-color:var(--ok) }
.banner.bad { border-left-color:var(--bad) }
.banner strong { font-size:16px }
.banner ul { margin:9px 0 0 18px; color:var(--muted); font-size:13px }

.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); gap:11px; margin:16px 0 }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); padding:13px 15px }
.stat .k { color:var(--muted); font-size:11.5px; text-transform:uppercase; letter-spacing:.05em }
.stat .v { font-size:21px; font-weight:600; margin-top:3px }
.stat .v.sm { font-size:14px; font-weight:500; line-height:1.4 }

/* ---- generic chrome ---- */
table { width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:var(--radius); overflow:hidden; font-size:14px }
th,td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top }
th { background:var(--panel2); font-size:11.5px; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted) }
tr:last-child td { border-bottom:none }
td.num { text-align:right; font-variant-numeric:tabular-nums }
.pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600 }
.pill.ok { background:rgba(63,185,80,.16); color:var(--ok) }
.pill.bad { background:rgba(248,81,73,.16); color:var(--bad) }
.pill.warn { background:rgba(210,153,34,.16); color:var(--warn) }
.pill.info { background:rgba(163,113,247,.16); color:var(--info) }
.tag { display:inline-block; padding:1px 7px; border-radius:5px; background:var(--panel2);
  border:1px solid var(--line); font-size:11.5px; color:var(--muted) }
.hash { cursor:copy }
details summary { cursor:pointer; color:var(--accent); font-size:13px }
pre { background:var(--bg); border:1px solid var(--line); border-radius:8px;
  padding:12px; overflow:auto; font-size:12px; max-height:360px }
.card { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); padding:14px 16px }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px }
.btn { display:inline-block; padding:5px 11px; border-radius:6px; font-size:13px;
  text-decoration:none; border:1px solid var(--line); background:var(--panel2); color:var(--fg); cursor:pointer }
.btn:hover { border-color:var(--accent); color:var(--accent) }
.btn.ok:hover { border-color:var(--ok); color:var(--ok) }
.btn.bad:hover { border-color:var(--bad); color:var(--bad) }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px }
.tldr { color:var(--muted); font-size:13px; margin:2px 0 0 }
.empty { color:var(--muted); font-size:13.5px; padding:20px; text-align:center;
  border:1px dashed var(--line); border-radius:var(--radius) }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap }
.spacer { flex:1 }

.tabpanel { display:none }
.tabpanel.active { display:block }

/* ---- hero recording ---- */
.hero { display:grid; gap:16px; grid-template-columns:minmax(0,2.1fr) minmax(240px,1fr);
  align-items:start; margin:18px 0 6px }
@media (max-width:900px) { .hero { grid-template-columns:1fr } }
.hero video, .hero .noplay {
  width:100%; border-radius:var(--radius); border:1px solid var(--line);
  background:#000; display:block; box-shadow:var(--shadow);
}
.hero .noplay { padding:36px 20px; text-align:center; background:var(--panel); color:var(--muted) }
.hero-meta { display:flex; flex-direction:column; gap:9px }

/* ---- before/after slideshow ---- */
.slideshow { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:14px 16px }
.slide-stage { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:10px 0 }
.slide-stage.one { grid-template-columns:1fr }
.slide-stage figure { margin:0 }
.slide-stage figcaption { font-size:12px; color:var(--muted); margin-bottom:5px;
  display:flex; gap:8px; align-items:center }
.shot { position:relative; border:1px solid var(--line); border-radius:8px;
  overflow:hidden; background:var(--panel2); min-height:80px }
.shot img { width:100%; display:block }
.shot.overlay img { position:absolute; inset:0; width:100%; height:100% ; object-fit:contain }
.shot.overlay img.b { position:static; mix-blend-mode:normal }
.shot.overlay img.a { mix-blend-mode:difference }
.slide-nav { display:flex; gap:8px; align-items:center; flex-wrap:wrap }
.slide-dots { display:flex; gap:5px; flex-wrap:wrap; margin-left:auto }
.slide-dots button { width:9px; height:9px; padding:0; border-radius:50%;
  border:1px solid var(--line); background:var(--panel2); cursor:pointer }
.slide-dots button[aria-current="true"] { background:var(--accent); border-color:var(--accent) }
.modes { display:flex; gap:2px; border:1px solid var(--line); border-radius:7px; overflow:hidden }
.modes button { background:var(--panel2); border:0; color:var(--muted);
  padding:4px 10px; font:inherit; font-size:12.5px; cursor:pointer }
.modes button[aria-pressed="true"] { background:var(--accent-dim); color:var(--accent) }

/* ---- task gitgraph ---- */
.graphwrap { display:grid; grid-template-columns:minmax(0,1.55fr) minmax(300px,1fr);
  gap:14px; align-items:start }
@media (max-width:1000px) { .graphwrap { grid-template-columns:1fr } }
.gitgraph { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); overflow:auto; max-height:600px; padding:4px }
.gitgraph svg { display:block }
.gg-lane { fill:var(--panel2) }
.gg-lane-label { fill:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:.08em }
.gg-edge { stroke:var(--line); stroke-width:2; fill:none }
.gg-edge.hot { stroke:var(--accent); stroke-width:2.5 }
.gg-node { cursor:pointer }
.gg-node circle { stroke:var(--bg); stroke-width:3; transition:r .12s ease }
.gg-node:hover circle, .gg-node:focus circle { r:12 }
.gg-node[aria-current="true"] circle { stroke:var(--accent); stroke-width:3.5 }
.gg-node text.id { fill:var(--fg); font-size:11.5px; font-weight:600 }
.gg-node text.ttl { fill:var(--muted); font-size:10.5px }
.gg-k-implement circle { fill:var(--accent) }
.gg-k-test circle { fill:var(--ok) }
.gg-k-doc circle { fill:var(--info) }
.gg-k-infra circle { fill:var(--warn) }
.legend { display:flex; gap:12px; flex-wrap:wrap; font-size:12px; color:var(--muted); margin-top:8px }
.legend i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px }

.detail { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:15px 17px; position:sticky; top:64px; max-height:600px; overflow:auto }
.detail dl { display:grid; grid-template-columns:auto 1fr; gap:5px 12px; margin:10px 0 0; font-size:13px }
.detail dt { color:var(--muted) }
.detail dd { margin:0 }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px }
.chip { font-size:11.5px; padding:2px 8px; border-radius:999px;
  background:var(--panel2); border:1px solid var(--line); cursor:pointer; color:var(--fg) }
.chip:hover { border-color:var(--accent); color:var(--accent) }

/* ---- diff viewer ---- */
.difffile { border:1px solid var(--line); border-radius:var(--radius);
  background:var(--panel); margin-bottom:10px; overflow:hidden }
.difffile > summary { list-style:none; padding:9px 13px; display:flex; gap:10px;
  align-items:center; flex-wrap:wrap; background:var(--panel2); color:var(--fg) }
.difffile > summary::-webkit-details-marker { display:none }
.difffile > summary::before { content:"\\25B8"; color:var(--muted); font-size:11px }
.difffile[open] > summary::before { content:"\\25BE" }
.difffile .fname { font-weight:600 }
.plus { color:var(--ok); font-variant-numeric:tabular-nums }
.minus { color:var(--bad); font-variant-numeric:tabular-nums }
.dtable { width:100%; border-collapse:collapse; border-radius:0; border:0;
  font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; font-size:12.2px }
.dtable td { padding:0 8px; border:0; white-space:pre-wrap; word-break:break-word; vertical-align:top }
.dtable td.g { width:1%; min-width:44px; text-align:right; color:var(--gut);
  background:var(--panel2); user-select:none; font-variant-numeric:tabular-nums;
  border-right:1px solid var(--line) }
.dtable td.m { width:1%; padding:0 4px 0 8px; color:var(--gut); user-select:none }
.dtable tr.add { background:var(--add-bg) } .dtable tr.add td.m { color:var(--ok) }
.dtable tr.del { background:var(--del-bg) } .dtable tr.del td.m { color:var(--bad) }
.dtable tr.hunk td { background:var(--panel3); color:var(--muted); padding:4px 8px; font-size:11.5px }
.dtable tr.add mark { background:var(--add-word); color:inherit; border-radius:2px; padding:0 1px }
.dtable tr.del mark { background:var(--del-word); color:inherit; border-radius:2px; padding:0 1px }
.dtable.split td.c { width:50% }
.dtable.split tr.pad td { background:var(--panel2); opacity:.45 }

/* ---- personas ---- */
.trace { list-style:none; margin:10px 0 0; padding:0 }
.trace li { position:relative; padding:0 0 14px 20px; border-left:2px solid var(--line) }
.trace li::before { content:""; position:absolute; left:-6px; top:5px; width:10px; height:10px;
  border-radius:50%; background:var(--info); border:2px solid var(--panel) }
.trace .obs { font-size:13px }
.trace .thought { font-size:13px; color:var(--fg); background:var(--accent-dim);
  border-left:3px solid var(--accent); padding:7px 10px; border-radius:0 6px 6px 0; margin:5px 0 }
.trace .act, .trace .out { font-size:12.5px; color:var(--muted) }
.friction { border-left:3px solid var(--warn); background:rgba(210,153,34,.08);
  padding:8px 11px; border-radius:0 6px 6px 0; margin-top:8px; font-size:13px }

/* ---- adr detail + citations ---- */
.adrwrap { display:grid; grid-template-columns:minmax(0,1fr) minmax(260px,.55fr); gap:14px; align-items:start }
@media (max-width:1000px) { .adrwrap { grid-template-columns:1fr } }
.citations { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:14px 16px; position:sticky; top:64px }
.citations ul { list-style:none; margin:6px 0 14px; padding:0 }
.citations li { padding:5px 0; border-bottom:1px solid var(--line); font-size:13px;
  display:flex; gap:8px; align-items:baseline }
.citations li:last-child { border-bottom:0 }
.citations .k { font-size:10.5px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); min-width:74px }

/* ---- timeline ---- */
ol.timeline { list-style:none; margin:0; padding:0 }
ol.timeline li { position:relative; padding:0 0 16px 22px; border-left:2px solid var(--line) }
ol.timeline li::before { content:""; position:absolute; left:-7px; top:4px; width:12px; height:12px;
  border-radius:50%; background:var(--muted); border:2px solid var(--bg) }
ol.timeline li.ok::before { background:var(--ok) }
ol.timeline li.bad::before { background:var(--bad) }
ol.timeline li.warn::before { background:var(--warn) }
.t-head { display:flex; gap:10px; align-items:center; flex-wrap:wrap }
.t-head time { color:var(--muted); font-size:12px; margin-left:auto }
ol.timeline p { margin:4px 0 0; color:var(--muted); font-size:13px }

.meter { position:relative; height:10px; background:var(--panel2); border-radius:999px;
  overflow:hidden; margin:6px 0 4px }
.meter-fill { height:100% } .meter-fill.ok { background:var(--ok) } .meter-fill.bad { background:var(--bad) }
.meter-mark { position:absolute; top:-3px; width:2px; height:16px; background:var(--fg); opacity:.6 }
.mermaid { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); padding:14px;
  white-space:pre; overflow-x:auto; font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  font-size:.86em; color:var(--muted) }

@media (prefers-reduced-motion:reduce) {
  html { scroll-behavior:auto }
  * { transition:none !important; animation:none !important }
}
@media print {
  .topbar, nav.tabs, .slide-nav, .iconbtn { display:none }
  .tabpanel { display:block !important }
  body { background:#fff; color:#000 }
}
"""


JS = r"""
(function () {
  'use strict';
  var M = JSON.parse(document.getElementById('adlc-model').textContent);
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function esc(s) { return String(s == null ? '' : s); }

  /* ------------------------------------------------------------------ tabs */
  function showTab(name, push) {
    $$('nav.tabs button').forEach(function (b) {
      b.setAttribute('aria-selected', String(b.dataset.tab === name));
    });
    $$('.tabpanel').forEach(function (p) { p.classList.toggle('active', p.id === 'tab-' + name); });
    if (push !== false && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
    var panel = $('#tab-' + name);
    if (panel) panel.focus({ preventScroll: true });
  }
  $$('nav.tabs button').forEach(function (b) {
    b.addEventListener('click', function () { showTab(b.dataset.tab); });
  });

  /* ------------------------------------------------------------- gitgraph */
  var NS = 'http://www.w3.org/2000/svg';
  function svgEl(tag, attrs) {
    var node = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  var byId = {};
  M.graph.nodes.forEach(function (n) { byId[n.id] = n; });

  function drawGraph() {
    var host = $('#gitgraph');
    if (!host) return;
    if (!M.graph.nodes.length) {
      host.appendChild(el('p', 'empty', 'No task graph was compiled for this run.'));
      return;
    }
    var svg = svgEl('svg', {
      viewBox: '0 0 ' + M.graph.width + ' ' + M.graph.height,
      width: M.graph.width, height: M.graph.height,
      role: 'group', 'aria-label': 'Task graph. Each column is a parallel wave.'
    });

    M.graph.levels.forEach(function (lvl) {
      var xs = M.graph.nodes.filter(function (n) { return n.level === lvl; });
      if (!xs.length) return;
      svg.appendChild(svgEl('text', {
        x: xs[0].x - 14, y: 16, class: 'gg-lane-label'
      })).textContent = 'wave ' + lvl;
    });

    M.graph.edges.forEach(function (e) {
      var d = e.straight
        ? 'M' + e.x1 + ',' + e.y1 + ' L' + e.x2 + ',' + e.y2
        : 'M' + e.x1 + ',' + e.y1 +
          ' C' + (e.x1 + 70) + ',' + e.y1 + ' ' + (e.x2 - 70) + ',' + e.y2 +
          ' ' + e.x2 + ',' + e.y2;
      svg.appendChild(svgEl('path', {
        d: d, class: 'gg-edge', 'data-from': e.from, 'data-to': e.to
      }));
    });

    M.graph.nodes.forEach(function (n) {
      var g = svgEl('g', {
        class: 'gg-node gg-k-' + n.kind, tabindex: '0', role: 'button',
        'data-id': n.id,
        'aria-label': n.id + '. ' + n.title + '. ' + (n.tldr || '')
      });
      g.appendChild(svgEl('circle', { cx: n.x, cy: n.y, r: 9 }));
      var id = svgEl('text', { x: n.x + 16, y: n.y - 1, class: 'id' });
      id.textContent = n.id;
      g.appendChild(id);
      var ttl = svgEl('text', { x: n.x + 16, y: n.y + 13, class: 'ttl' });
      ttl.textContent = n.title.length > 22 ? n.title.slice(0, 21) + '\u2026' : n.title;
      g.appendChild(ttl);
      var tip = svgEl('title', {});
      tip.textContent = n.tldr || n.title;
      g.appendChild(tip);
      g.addEventListener('click', function () { selectNode(n.id); });
      g.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectNode(n.id); }
      });
      svg.appendChild(g);
    });
    host.appendChild(svg);
  }

  function selectNode(id) {
    var n = byId[id];
    if (!n) return;
    $$('.gg-node').forEach(function (g) {
      g.setAttribute('aria-current', String(g.dataset.id === id));
    });
    var lit = {};
    lit[id] = true;
    (n.dependsOn || []).forEach(function (d) { lit[d] = true; });
    $$('.gg-edge').forEach(function (p) {
      p.classList.toggle('hot', p.dataset.to === id || p.dataset.from === id);
    });

    var box = $('#node-detail');
    box.innerHTML = '';
    box.appendChild(el('h3', null, n.id + ' \u2014 ' + n.title));
    box.appendChild(el('p', 'tldr', n.tldr || ''));

    var dl = el('dl');
    function pair(k, v) {
      if (v == null || v === '' || (v.length === 0)) return;
      dl.appendChild(el('dt', null, k));
      dl.appendChild(el('dd', null, v));
    }
    pair('Kind', n.kind);
    pair('Wave', 'level ' + n.level + (n.dependsOn.length ? '' : ' (no prerequisites)'));
    pair('Depends on', n.dependsOn.join(', ') || 'nothing');
    pair('Writes', n.writeSet.join(', '));
    pair('Proves', (n.acceptance || []).join(', '));
    if (n.stats && n.stats.files) {
      pair('Change', n.stats.files + ' file(s), +' + n.stats.additions + ' / \u2212' + n.stats.deletions);
    }
    box.appendChild(dl);

    if ((n.adrRefs || []).length) {
      box.appendChild(el('h4', null, 'Decisions made here'));
      var chips = el('div', 'chips');
      n.adrRefs.forEach(function (num) {
        var b = el('button', 'chip', 'ADR ' + num);
        b.addEventListener('click', function () { showTab('decisions'); selectAdr(num); });
        chips.appendChild(b);
      });
      box.appendChild(chips);
    }

    var diff = (M.diffs || []).filter(function (d) { return d.taskId === n.id; })[0];
    box.appendChild(el('h4', null, 'Change'));
    if (diff && diff.files && diff.files.length) {
      var jump = el('button', 'btn', 'Open the diff for ' + n.id);
      jump.addEventListener('click', function () { showTab('diff'); renderDiff(n.id); });
      box.appendChild(jump);
    } else {
      box.appendChild(el('p', 'muted', 'No patch was recorded for this node.'));
    }

    if ((n.artifactSha256 || []).length) {
      box.appendChild(el('h4', null, 'Evidence naming this node'));
      var ul = el('ul');
      n.artifactSha256.slice(0, 8).forEach(function (h) {
        var a = (M.artifacts || []).filter(function (x) { return x.sha256 === h; })[0];
        ul.appendChild(el('li', 'mono', (a ? a.path : h.slice(0, 16) + '\u2026')));
      });
      box.appendChild(ul);
    }
  }

  /* ----------------------------------------------------------- diff view */
  var diffMode = 'unified';

  function lineText(line) {
    var frag = document.createDocumentFragment();
    var segs = line.segs || [];
    if (!segs.length) { frag.appendChild(document.createTextNode(line.text)); return frag; }
    var at = 0;
    segs.forEach(function (s) {
      if (s[0] > at) frag.appendChild(document.createTextNode(line.text.slice(at, s[0])));
      var m = el('mark', null, line.text.slice(s[0], s[1]));
      frag.appendChild(m);
      at = s[1];
    });
    if (at < line.text.length) frag.appendChild(document.createTextNode(line.text.slice(at)));
    return frag;
  }

  function unifiedTable(file) {
    var t = el('table', 'dtable');
    var tb = el('tbody');
    file.hunks.forEach(function (h) {
      var hr = el('tr', 'hunk');
      var hc = el('td', null, h.header);
      hc.colSpan = 4;
      hr.appendChild(hc);
      tb.appendChild(hr);
      h.lines.forEach(function (l) {
        var tr = el('tr', l.type === 'add' ? 'add' : (l.type === 'del' ? 'del' : ''));
        tr.appendChild(el('td', 'g', l.oldNo == null ? '' : String(l.oldNo)));
        tr.appendChild(el('td', 'g', l.newNo == null ? '' : String(l.newNo)));
        tr.appendChild(el('td', 'm', l.type === 'add' ? '+' : (l.type === 'del' ? '-' : ' ')));
        var c = el('td', 'c');
        c.appendChild(lineText(l));
        tr.appendChild(c);
        tb.appendChild(tr);
      });
    });
    t.appendChild(tb);
    return t;
  }

  function splitTable(file) {
    var t = el('table', 'dtable split');
    var tb = el('tbody');
    file.hunks.forEach(function (h) {
      var hr = el('tr', 'hunk');
      var hc = el('td', null, h.header);
      hc.colSpan = 6;
      hr.appendChild(hc);
      tb.appendChild(hr);

      // Group each run of removals/additions so the two sides line up.
      var i = 0;
      while (i < h.lines.length) {
        var l = h.lines[i];
        if (l.type === 'ctx') {
          tb.appendChild(sideRow(l, l, '', ''));
          i++;
          continue;
        }
        var dels = [], adds = [];
        while (i < h.lines.length && h.lines[i].type === 'del') { dels.push(h.lines[i]); i++; }
        while (i < h.lines.length && h.lines[i].type === 'add') { adds.push(h.lines[i]); i++; }
        var n = Math.max(dels.length, adds.length);
        for (var k = 0; k < n; k++) {
          tb.appendChild(sideRow(dels[k] || null, adds[k] || null, 'del', 'add'));
        }
      }
    });
    t.appendChild(tb);
    return t;
  }

  function sideRow(left, right, lc, rc) {
    var tr = el('tr', (!left && rc) ? 'pad-l' : '');
    function half(line, cls) {
      var g = el('td', 'g', line ? String(line.oldNo != null ? line.oldNo : line.newNo) : '');
      var m = el('td', 'm', line ? (cls === 'add' ? '+' : (cls === 'del' ? '-' : ' ')) : '');
      var c = el('td', 'c');
      if (line) c.appendChild(lineText(line));
      if (cls) { g.classList.add(cls); m.classList.add(cls); c.classList.add(cls); }
      if (!line) { g.classList.add('pad'); c.classList.add('pad'); }
      return [g, m, c];
    }
    half(left, left ? lc : '').forEach(function (td) { tr.appendChild(td); });
    half(right, right ? rc : '').forEach(function (td) { tr.appendChild(td); });
    if (left && !right && lc) tr.classList.add('del');
    if (right && !left && rc) tr.classList.add('add');
    return tr;
  }

  function renderDiff(taskId) {
    var host = $('#diff-host');
    if (!host) return;
    host.innerHTML = '';
    var sets = (M.diffs || []).filter(function (d) { return !taskId || d.taskId === taskId; });
    var sel = $('#diff-task');
    if (sel && taskId && sel.value !== taskId) sel.value = taskId;
    if (!sets.length) {
      host.appendChild(el('p', 'empty', 'No patches were produced for this run.'));
      return;
    }
    sets.forEach(function (set) {
      if (set.error) {
        host.appendChild(el('p', 'empty', set.source + ': ' + set.error));
        return;
      }
      if (!set.files.length) {
        host.appendChild(el('p', 'empty', set.source + ' contains no file changes.'));
        return;
      }
      set.files.forEach(function (f) {
        var d = el('details', 'difffile');
        if (set.files.length <= 4) d.open = true;
        var s = el('summary');
        s.appendChild(el('span', 'fname mono', f.path));
        if (f.status !== 'modified') s.appendChild(el('span', 'tag', f.status));
        s.appendChild(el('span', 'plus', '+' + f.additions));
        s.appendChild(el('span', 'minus', '\u2212' + f.deletions));
        if (f.truncated) s.appendChild(el('span', 'tag', 'truncated'));
        d.appendChild(s);
        if (f.binary) {
          d.appendChild(el('p', 'muted', 'Binary file \u2014 not shown as text.'));
        } else {
          d.appendChild(diffMode === 'split' ? splitTable(f) : unifiedTable(f));
        }
        if (f.truncated) {
          d.appendChild(el('p', 'note',
            'Only the first lines are shown. The complete patch is in ' + set.source + '.'));
        }
        host.appendChild(d);
      });
    });
  }

  /* ------------------------------------------------------------ slideshow */
  var slide = 0, shotMode = 'side';
  function renderSlide() {
    var pairs = (M.media && M.media.pairs) || [];
    var stage = $('#slide-stage');
    if (!stage) return;
    stage.innerHTML = '';
    if (!pairs.length) {
      stage.appendChild(el('p', 'empty', 'No screenshots were captured for this run.'));
      return;
    }
    slide = Math.max(0, Math.min(slide, pairs.length - 1));
    var p = pairs[slide];
    var label = $('#slide-label');
    if (label) {
      label.textContent = (slide + 1) + ' / ' + pairs.length + ' \u00b7 ' + p.label;
    }
    var why = $('#slide-rule');
    if (why) {
      why.textContent = 'Paired because: ' + p.rule + ' (confidence: ' + p.confidence + ')';
    }

    function figure(item, caption) {
      var fig = el('figure');
      var cap = el('figcaption');
      cap.appendChild(el('span', null, caption));
      if (item && item.human) cap.appendChild(el('span', 'tag', item.human));
      fig.appendChild(cap);
      var box = el('div', 'shot');
      if (item && item.src) {
        var img = el('img');
        img.src = item.src;
        img.alt = caption + ': ' + item.caption;
        img.loading = 'lazy';
        box.appendChild(img);
      } else if (item) {
        box.appendChild(el('p', 'muted', item.reason || ('Not embedded: ' + item.path)));
      } else {
        box.appendChild(el('p', 'muted', 'Nothing captured for this side.'));
      }
      fig.appendChild(box);
      return fig;
    }

    if (shotMode === 'diff' && p.before && p.after && p.before.src && p.after.src) {
      stage.className = 'slide-stage one';
      var fig = el('figure');
      fig.appendChild(el('figcaption', null, 'Difference blend \u2014 lit pixels changed'));
      var box = el('div', 'shot overlay');
      // The blend only carries meaning as a composite, so expose it as one
      // labelled image. Announcing two separate images would describe a thing
      // the reader cannot perceive; the honest move is to say the view is
      // visual-only and name the routes that are not.
      box.setAttribute('role', 'img');
      box.setAttribute('aria-label',
        'Difference blend of ' + p.label + ': the before and after captures ' +
        'composited so the pixels that changed appear lit. This view is visual ' +
        'only.');
      var b = el('img'); b.className = 'b'; b.src = p.before.src; b.alt = '';
      var a = el('img'); a.className = 'a'; a.src = p.after.src; a.alt = '';
      b.setAttribute('aria-hidden', 'true');
      a.setAttribute('aria-hidden', 'true');
      box.appendChild(b); box.appendChild(a);
      fig.appendChild(box);
      fig.appendChild(el('p', 'muted',
        'Difference blend is a visual-only comparison. Use Side by side to read ' +
        'each capture on its own, or the Diff tab for the textual change.'));
      stage.appendChild(fig);
    } else if (shotMode === 'after' || !p.before) {
      stage.className = 'slide-stage one';
      stage.appendChild(figure(p.after, 'After'));
    } else if (shotMode === 'before') {
      stage.className = 'slide-stage one';
      stage.appendChild(figure(p.before, 'Before'));
    } else {
      stage.className = 'slide-stage';
      stage.appendChild(figure(p.before, 'Before'));
      stage.appendChild(figure(p.after, 'After'));
    }

    var dots = $('#slide-dots');
    if (dots) {
      // Build once, then only update state. Rebuilding the strip on every render
      // destroyed the very button the user had just activated, so keyboard focus
      // fell back to <body> and their place in the strip was lost on every step.
      if (dots.childElementCount !== pairs.length) {
        dots.innerHTML = '';
        pairs.forEach(function (_, i) {
          var b = el('button');
          b.setAttribute('aria-label', 'Go to comparison ' + (i + 1));
          b.addEventListener('click', function () { slide = i; renderSlide(); });
          dots.appendChild(b);
        });
      }
      for (var d = 0; d < dots.children.length; d++) {
        dots.children[d].setAttribute('aria-current', String(d === slide));
      }
    }
  }

  /* -------------------------------------------------------------- personas */
  function renderPersonas() {
    var host = $('#persona-host');
    if (!host) return;
    var records = M.personas || [];
    if (!records.length) {
      host.appendChild(el('p', 'empty',
        'No persona walkthroughs were recorded. Run `adlc personas` after evidence capture.'));
      return;
    }
    records.forEach(function (r) {
      var card = el('article', 'card');
      var head = el('div', 'row');
      var vcls = { satisfied: 'ok', partial: 'warn', confused: 'warn', blocked: 'bad' }[r.verdict] || 'warn';
      head.appendChild(el('span', 'pill ' + vcls, r.verdict || 'unknown'));
      head.appendChild(el('h3', null, r.name + ' \u2014 ' + (r.role || 'user')));
      head.appendChild(el('span', 'spacer'));
      head.appendChild(el('span', 'tag', r.simulated === false ? 'real session' : 'simulated'));
      head.appendChild(el('span', 'tag', r.scenarioId || ''));
      card.appendChild(head);
      card.appendChild(el('p', 'tldr', r.tldr || ''));
      if (r.scenarioText) card.appendChild(el('p', 'muted', r.scenarioText));
      if (r._invalid) {
        card.appendChild(el('p', 'friction',
          'This record does not match the schema: ' + r._invalid.join('; ')));
      }

      var det = el('details');
      det.appendChild(el('summary', null, 'What they were thinking, step by step'));
      var ol = el('ol', 'trace');
      (r.steps || []).forEach(function (s) {
        var li = el('li');
        if (s.observation) li.appendChild(el('div', 'obs', s.observation));
        if (s.thought) li.appendChild(el('div', 'thought', '\u201c' + s.thought + '\u201d'));
        if (s.action) li.appendChild(el('div', 'act', 'Did: ' + s.action));
        if (s.outcome) li.appendChild(el('div', 'out', 'Result: ' + s.outcome));
        ol.appendChild(li);
      });
      det.appendChild(ol);
      card.appendChild(det);

      (r.friction || []).forEach(function (f) {
        var d = el('div', 'friction');
        d.appendChild(el('strong', null, (f.severity || 'medium') + ' friction: '));
        d.appendChild(document.createTextNode(f.summary));
        card.appendChild(d);
      });
      host.appendChild(card);
    });
  }

  /* ------------------------------------------------------------------ ADRs */
  function selectAdr(number) {
    var adr = (M.adrs || []).filter(function (a) { return a.number === number; })[0];
    if (!adr) return;
    $$('#adr-list button').forEach(function (b) {
      b.setAttribute('aria-current', String(b.dataset.num === number));
    });

    var host = $('#adr-detail');
    host.innerHTML = '';
    var cls = { accepted: 'ok', rejected: 'bad', proposed: 'warn', superseded: 'info' }[adr.status] || 'warn';
    var head = el('div', 'row');
    head.appendChild(el('span', 'pill ' + cls, adr.status));
    head.appendChild(el('h3', null, adr.number + ' \u2014 ' + adr.title));
    host.appendChild(head);
    host.appendChild(el('p', 'tldr', adr.tldr || ''));
    host.appendChild(el('p', 'mono muted', adr.path));

    function section(title, body) {
      if (!body) return;
      host.appendChild(el('h4', null, title));
      host.appendChild(el('p', null, body));
    }
    function list(title, items) {
      if (!items || !items.length) return;
      host.appendChild(el('h4', null, title));
      var ul = el('ul');
      items.forEach(function (i) { ul.appendChild(el('li', null, i)); });
      host.appendChild(ul);
    }
    section('Context', adr.context);
    list('Decision drivers', adr.drivers);
    list('Options considered', adr.options);
    if (adr.chosen) {
      host.appendChild(el('h4', null, 'Chosen'));
      var p = el('p');
      p.appendChild(el('strong', null, adr.chosen));
      if (adr.justification) p.appendChild(document.createTextNode(' \u2014 because ' + adr.justification));
      host.appendChild(p);
    }
    list('Consequences', adr.consequences);
    section('Confirmation', adr.confirmation);
    (adr.sections || []).forEach(function (s) {
      if (!s.body && !(s.bullets || []).length) return;
      host.appendChild(el('h4', null, s.title));
      if (s.body) host.appendChild(el('p', null, s.body));
      if ((s.bullets || []).length) {
        var ul = el('ul');
        s.bullets.forEach(function (i) { ul.appendChild(el('li', null, i)); });
        host.appendChild(ul);
      }
    });

    if ((adr.nodes || []).length) {
      host.appendChild(el('h4', null, 'Decided while doing'));
      var chips = el('div', 'chips');
      adr.nodes.forEach(function (n) {
        var b = el('button', 'chip', n.id + ' ' + n.title);
        b.addEventListener('click', function () { showTab('graph'); selectNode(n.id); });
        chips.appendChild(b);
      });
      host.appendChild(chips);
    }

    var pane = $('#adr-citations');
    pane.innerHTML = '';
    pane.appendChild(el('h3', null, 'What informed this'));
    var cites = adr.citations || [];
    if (!cites.length) {
      pane.appendChild(el('p', 'empty',
        'This decision cites nothing. An unsourced decision cannot be audited.'));
      return;
    }
    var groups = {};
    cites.forEach(function (c) { (groups[c.kind] = groups[c.kind] || []).push(c); });
    var names = {
      requirement: 'Requirements', artifact: 'Evidence artifacts', adr: 'Other decisions',
      file: 'Files', web: 'External sources', run: 'Runs', anchor: 'Within this document'
    };
    Object.keys(groups).forEach(function (kind) {
      pane.appendChild(el('h4', null, names[kind] || kind));
      var ul = el('ul');
      groups[kind].forEach(function (c) {
        var li = el('li');
        li.appendChild(el('span', 'k', kind));
        if (kind === 'web') {
          var a = el('a', null, c.label);
          a.href = c.ref; a.target = '_blank'; a.rel = 'noopener noreferrer';
          li.appendChild(a);
        } else if (kind === 'adr') {
          var b = el('button', 'chip', c.label);
          b.addEventListener('click', function () { selectAdr(c.ref); });
          li.appendChild(b);
        } else if (kind === 'artifact') {
          var art = (M.artifacts || []).filter(function (x) { return x.sha256 === c.ref; })[0];
          var t = el('button', 'chip', art ? art.path : c.label);
          t.addEventListener('click', function () { showTab('evidence'); highlight(c.ref); });
          li.appendChild(t);
        } else {
          li.appendChild(el('span', 'mono', c.label));
        }
        ul.appendChild(li);
      });
      pane.appendChild(ul);
    });
  }

  function highlight(sha) {
    var row = $$('#tab-evidence tr[data-sha="' + sha + '"]')[0];
    if (!row) return;
    row.scrollIntoView({ block: 'center' });
    row.style.outline = '2px solid var(--accent)';
    setTimeout(function () { row.style.outline = ''; }, 2200);
  }

  /* --------------------------------------------------------------- wiring */
  $$('.hash').forEach(function (node) {
    node.addEventListener('click', function () {
      if (navigator.clipboard) navigator.clipboard.writeText(node.title || node.textContent);
      var old = node.textContent;
      node.textContent = 'copied';
      setTimeout(function () { node.textContent = old; }, 900);
    });
  });

  var themeBtn = $('#theme');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var root = document.documentElement;
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    });
  }

  $$('#slide-prev,#slide-next').forEach(function (b) {
    b.addEventListener('click', function () {
      slide += (b.id === 'slide-next' ? 1 : -1);
      var n = ((M.media && M.media.pairs) || []).length || 1;
      slide = (slide + n) % n;
      renderSlide();
    });
  });
  $$('#shot-modes button').forEach(function (b) {
    b.addEventListener('click', function () {
      shotMode = b.dataset.mode;
      $$('#shot-modes button').forEach(function (x) {
        x.setAttribute('aria-pressed', String(x === b));
      });
      renderSlide();
    });
  });
  $$('#diff-modes button').forEach(function (b) {
    b.addEventListener('click', function () {
      diffMode = b.dataset.mode;
      $$('#diff-modes button').forEach(function (x) {
        x.setAttribute('aria-pressed', String(x === b));
      });
      renderDiff($('#diff-task') ? $('#diff-task').value : '');
    });
  });
  var taskSel = $('#diff-task');
  if (taskSel) taskSel.addEventListener('change', function () { renderDiff(taskSel.value); });

  $$('#adr-list button').forEach(function (b) {
    b.addEventListener('click', function () { selectAdr(b.dataset.num); });
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
    if (ev.key === 'ArrowRight' && $('#tab-visuals').classList.contains('active')) {
      $('#slide-next').click();
    }
    if (ev.key === 'ArrowLeft' && $('#tab-visuals').classList.contains('active')) {
      $('#slide-prev').click();
    }
  });

  drawGraph();
  if (M.graph.nodes.length) selectNode(M.graph.nodes[0].id);
  renderSlide();
  renderPersonas();
  renderDiff('');
  if ((M.adrs || []).length) selectAdr(M.adrs[0].number);
  showTab((location.hash || '#overview').slice(1) || 'overview', false);
})();
"""
