(function () {
  'use strict';

  // Lazy, idempotent shared registry. Whichever interactive section loads first
  // creates it; every later section (annotations, critiques, feedback pack) finds
  // the same object, so load order does not matter and no layer owns the global.
  const store = (window.adlcFeedback = window.adlcFeedback || {
    annotations: [], critiques: [], diffDecisions: [], listeners: [],
    notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },
    subscribe(fn) { this.listeners.push(fn); },
  });

  const modelEl = document.getElementById('adlc-diff-model');
  if (!modelEl) return;

  let model;
  try {
    model = JSON.parse(modelEl.textContent || '{}');
  } catch (e) {
    return; // A corrupt data island disables the controls but never throws.
  }
  const runId = (model && model.runId) || 'unknown';
  const rows = (model && model.rows) || {};
  const storageKey = 'adlc.diffDecisions.' + runId;

  // $defs/id: what an annotation id must look like before we dare cite it in a
  // pack. Anything else is dropped so an emitted decision always validates.
  const ID_RE = /^[A-Za-z0-9._-]{1,64}$/;

  const live = document.querySelector('[data-diff-live]');
  const counter = document.querySelector('[data-diff-count]');

  // decisionId -> decision object, held in exact human-feedback-pack shape.
  const decisions = loadSaved();
  let refreshing = false;

  function loadSaved() {
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      const map = {};
      if (parsed && Array.isArray(parsed.decisions)) {
        parsed.decisions.forEach(function (d) {
          const rebuilt = sanitize(d);
          if (rebuilt) map[rebuilt.id] = rebuilt;
        });
      }
      return map;
    } catch (e) {
      return {}; // localStorage blocked or holding junk: start empty.
    }
  }

  // localStorage is untrusted: it can be stale, hand-edited, or written by another
  // tab. Rebuild every loaded entry into the exact schema shape from the trusted
  // model so a persisted decision can never smuggle an extra key, a bad enum, an
  // overlong comment, or a malformed target/annotation id into the exported pack.
  function sanitize(d) {
    if (!d || typeof d.id !== 'string' || !ID_RE.test(d.id)) return null;
    const meta = rows[d.id];
    if (!meta || !meta.targetId) return null; // decision for an unknown row: drop
    if (d.decision !== 'accept' && d.decision !== 'reject') return null;
    return {
      id: d.id,
      targetKind: meta.targetKind, // authoritative: from the model, not from storage
      targetId: meta.targetId,     // authoritative: from the model, not from storage
      decision: d.decision,
      comment: clampComment(d.comment),
      annotationIds: sanitizeAnnotationIds(d.annotationIds),
    };
  }

  function sanitizeAnnotationIds(v) {
    if (!Array.isArray(v)) return [];
    const out = [];
    v.forEach(function (x) {
      if (typeof x === 'string' && ID_RE.test(x) && out.indexOf(x) === -1) out.push(x);
    });
    return out.slice(0, 40);
  }

  function persist() {
    try {
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({ runId: runId, decisions: order() })
      );
    } catch (e) {
      // Unavailable or full (private mode, quota). Keep working in memory only.
    }
  }

  function clampComment(s) {
    s = (s == null) ? '' : String(s);
    return s.length > 4000 ? s.slice(0, 4000) : s;
  }

  function annotationIdsFor(meta) {
    const sha = meta.sha256 || meta.artifactSha256;
    if (!sha) return [];
    const ids = [];
    store.annotations.forEach(function (a) {
      if (a && a.artifactSha256 === sha && typeof a.id === 'string' && ID_RE.test(a.id)) {
        ids.push(a.id);
      }
    });
    return ids.slice(0, 40);
  }

  // Stable order == the model's row order, so a persisted pack is deterministic.
  function order() {
    return Object.keys(rows)
      .filter(function (id) { return decisions[id]; })
      .map(function (id) { return decisions[id]; });
  }

  // Mutate the shared array in place; never reassign, so a reference another
  // layer already captured stays live.
  function syncStore() {
    const arr = order();
    store.diffDecisions.length = 0;
    arr.forEach(function (d) { store.diffDecisions.push(d); });
  }

  function updateCounter() {
    if (!counter) return;
    const n = store.diffDecisions.length;
    if (n === 0) {
      counter.textContent = 'No decisions recorded yet.';
      return;
    }
    let a = 0;
    store.diffDecisions.forEach(function (d) { if (d.decision === 'accept') a += 1; });
    const r = n - a;
    counter.textContent =
      n + ' decision' + (n === 1 ? '' : 's') + ' recorded: ' + a + ' accepted, ' + r + ' rejected.';
  }

  function groupFor(id) {
    return document.querySelector('.dd[data-decision-id="' + cssEscape(id) + '"]');
  }

  function cssEscape(s) {
    return String(s).replace(/["\\]/g, '\\$&');
  }

  function reflect(id) {
    const group = groupFor(id);
    if (!group) return;
    const d = decisions[id];
    const acc = group.querySelector('.dd-accept');
    const rej = group.querySelector('.dd-reject');
    if (acc) acc.setAttribute('aria-pressed', d && d.decision === 'accept' ? 'true' : 'false');
    if (rej) rej.setAttribute('aria-pressed', d && d.decision === 'reject' ? 'true' : 'false');
    group.setAttribute('data-decided', d ? d.decision : 'none');
  }

  function setDecision(id, verdict) {
    const meta = rows[id];
    if (!meta) return;
    // minLength 1 on targetId: never emit an out-of-schema empty target.
    if (!meta.targetId) return;
    if (verdict === null) {
      delete decisions[id];
    } else {
      const group = groupFor(id);
      const commentEl = group ? group.querySelector('.dd-comment') : null;
      decisions[id] = {
        id: id,
        targetKind: meta.targetKind,
        targetId: meta.targetId,
        decision: verdict,
        comment: clampComment(commentEl ? commentEl.value : ''),
        annotationIds: annotationIdsFor(meta),
      };
    }
    reflect(id);
    syncStore();
    persist();
    store.notify();
    updateCounter();
    const label = meta.targetKind + ' ' + meta.targetId;
    if (live) {
      live.textContent =
        verdict === null
          ? 'Cleared decision for ' + label
          : (verdict === 'accept' ? 'Accepted change to ' : 'Rejected change to ') + label;
    }
  }

  Object.keys(rows).forEach(function (id) {
    const group = groupFor(id);
    if (!group) return;
    const acc = group.querySelector('.dd-accept');
    const rej = group.querySelector('.dd-reject');
    const commentEl = group.querySelector('.dd-comment');
    if (acc) acc.addEventListener('click', function () {
      const on = decisions[id] && decisions[id].decision === 'accept';
      setDecision(id, on ? null : 'accept');
    });
    if (rej) rej.addEventListener('click', function () {
      const on = decisions[id] && decisions[id].decision === 'reject';
      setDecision(id, on ? null : 'reject');
    });
    if (commentEl) commentEl.addEventListener('input', function () {
      if (decisions[id]) {
        decisions[id].comment = clampComment(commentEl.value);
        syncStore();
        persist();
        store.notify();
      }
    });
    reflect(id);
  });

  // Difference-blend toggle: add a class to the figure so CSS overlays the two
  // images already inlined side by side, using mix-blend-mode. The browser does the
  // pixel work, offline, with no image library and no second copy of the image.
  // Keyboard-operable (a real button) and its state is announced.
  document.querySelectorAll('[data-blend-toggle]').forEach(function (btn) {
    const targetId = btn.getAttribute('data-blend-toggle');
    const target = targetId ? document.getElementById(targetId) : null;
    const note = target ? target.querySelector('[data-blend-note]') : null;
    const name = btn.getAttribute('data-ss-name') || 'screenshot';
    btn.addEventListener('click', function () {
      const next = btn.getAttribute('aria-pressed') !== 'true';
      btn.setAttribute('aria-pressed', next ? 'true' : 'false');
      if (target) target.classList.toggle('blend-on', next);
      if (note) note.hidden = !next;
      if (live) live.textContent = 'Difference overlay ' + (next ? 'shown for ' : 'hidden for ') + name;
    });
  });

  // Keep annotation links fresh when the evidence layer adds markup, without
  // re-notifying (guarded, and never calls notify) so there is no feedback loop.
  store.subscribe(function () {
    if (refreshing) return;
    refreshing = true;
    try {
      let changed = false;
      Object.keys(decisions).forEach(function (id) {
        const meta = rows[id];
        if (!meta) return;
        const next = annotationIdsFor(meta);
        const cur = decisions[id].annotationIds || [];
        const differs =
          next.length !== cur.length ||
          next.some(function (v, i) { return v !== cur[i]; });
        if (differs) {
          decisions[id].annotationIds = next;
          changed = true;
        }
      });
      if (changed) { syncStore(); persist(); }
    } finally {
      refreshing = false;
    }
  });

  syncStore();
  Object.keys(rows).forEach(reflect);
  updateCounter();
})();
