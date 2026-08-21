  (function () {
    'use strict';

    // -------------------------------------------------------------------------
    // Shared cross-layer client registry. Lazy + idempotent so no single layer
    // owns window.adlcFeedback and load order never matters: whichever section's
    // script runs first creates it, the rest reuse it. This block is the verbatim
    // contract the reasoning (5), diff (6) and feedback (7) layers rely on --
    // keep it byte-identical across layers.
    // -------------------------------------------------------------------------
    const store = (window.adlcFeedback = window.adlcFeedback || {
      annotations: [], critiques: [], diffDecisions: [], listeners: [],
      notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },
      subscribe(fn) { this.listeners.push(fn); },
    });

    // -- constants mirrored from schemas/human-feedback-pack.schema.json --------
    var STORAGE_PREFIX = 'adlc.annotations.';
    var SHAPES = ['rect', 'arrow', 'highlight', 'freehand', 'point', 'whole'];
    var SEVERITIES = ['info', 'minor', 'major', 'blocker'];
    var SEV_CODE = { info: 'info', minor: 'min', major: 'maj', blocker: 'blk' };
    var SEV_LABEL = { info: '(i) info', minor: '[!] minor', major: '[!!] major', blocker: '[x] blocker' };
    var SEV_STROKE = { info: 'var(--accent)', minor: 'var(--warn)', major: 'var(--warn)', blocker: 'var(--bad)' };
    var SEV_DASH = { info: '0', minor: '10 6', major: '4 4', blocker: '0' };
    var SHA_RE = /^[a-f0-9]{64}$/;
    var ID_RE = /^[A-Za-z0-9._-]+$/;
    var SVGNS = 'http://www.w3.org/2000/svg';
    var MAX_COMMENT = 4000, MAX_PATH = 512, MAX_KIND = 64, MAX_REQ = 64, MAX_REQS = 40, MAX_POINTS = 400;

    var data = null, runId = 'unknown', requirements = [];

    // -- geometry: everything is a fraction in [0,1] of the image's NATURAL size,
    //    never a rendered pixel, so a reviewer's viewport width cannot change what
    //    an annotation means. ---------------------------------------------------
    function clamp01(n) {
      n = Number(n);
      if (!isFinite(n)) return 0;
      return n < 0 ? 0 : n > 1 ? 1 : n;
    }

    // Map a pointer position to a [0,1] fraction of the image box. The fraction of
    // the rendered box equals the fraction of the natural image, so the stored
    // value is resolution independent.
    function normalizePoint(clientX, clientY, rect) {
      var w = rect.width || 1, h = rect.height || 1;
      return [clamp01((clientX - rect.left) / w), clamp01((clientY - rect.top) / h)];
    }

    // Build a normalised geometry object from the keyboard form's region fields
    // (all fractions in [0,1]). Returns null for 'whole', which omits geometry.
    function buildGeometry(shape, x, y, w, h) {
      if (shape === 'whole') return null;
      var x1 = clamp01(x), y1 = clamp01(y), x2 = clamp01(x + w), y2 = clamp01(y + h);
      var points = shape === 'point' ? [[x1, y1]] : [[x1, y1], [x2, y2]];
      return { points: points };
    }

    function makeId() {
      return 'an-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6);
    }

    // -- $defs.annotation shaping ---------------------------------------------
    function sanitizeReqs(list) {
      if (!Array.isArray(list)) return [];
      var out = [];
      for (var i = 0; i < list.length && out.length < MAX_REQS; i++) {
        var v = list[i];
        if (typeof v === 'string' && v.length) out.push(v.slice(0, MAX_REQ));
      }
      return out;
    }

    function sanitizeGeometry(g) {
      if (!g || !Array.isArray(g.points) || !g.points.length) return null;
      var pts = [];
      for (var i = 0; i < g.points.length && pts.length < MAX_POINTS; i++) {
        var p = g.points[i];
        if (Array.isArray(p) && p.length === 2) pts.push([clamp01(p[0]), clamp01(p[1])]);
      }
      return pts.length ? { points: pts } : null;
    }

    // Coerce any candidate into an object in EXACT $defs.annotation shape, or null
    // if it can never validate (bad hash, bad shape, empty comment). This is what
    // keeps store.annotations schema-valid at all times.
    function sanitize(a) {
      if (!a || typeof a !== 'object') return null;
      if (!SHA_RE.test(a.artifactSha256 || '')) return null;
      if (SHAPES.indexOf(a.shape) < 0) return null;
      var comment = typeof a.comment === 'string' ? a.comment.slice(0, MAX_COMMENT) : '';
      if (!comment.length) return null;
      var out = {
        id: (typeof a.id === 'string' && a.id.length <= 64 && ID_RE.test(a.id)) ? a.id : makeId(),
        artifactSha256: a.artifactSha256,
        artifactPath: typeof a.artifactPath === 'string' ? a.artifactPath.slice(0, MAX_PATH) : '',
        artifactKind: typeof a.artifactKind === 'string' ? a.artifactKind.slice(0, MAX_KIND) : '',
        shape: a.shape,
        severity: SEVERITIES.indexOf(a.severity) >= 0 ? a.severity : 'info',
        comment: comment,
        requirementIds: sanitizeReqs(a.requirementIds),
      };
      if (a.shape !== 'whole') {
        var geom = sanitizeGeometry(a.geometry);
        if (geom) out.geometry = geom;
      }
      return out;
    }

    // -- persistence: keyed by run id, degrades silently when localStorage is
    //    unavailable (private mode) or full (quota). ---------------------------
    function storageKey() { return STORAGE_PREFIX + runId; }

    function loadPersisted() {
      var raw = null;
      try { raw = window.localStorage.getItem(storageKey()); } catch (e) { return []; }
      if (!raw) return [];
      try {
        var parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed.map(sanitize).filter(Boolean) : [];
      } catch (e) { return []; }
    }

    function persist() {
      try {
        window.localStorage.setItem(storageKey(), JSON.stringify(store.annotations));
      } catch (e) { /* quota exceeded or storage disabled: stay in-memory, never throw */ }
    }

    // -- store mutation (preserve the array's identity so sibling layers may hold
    //    a live reference) --------------------------------------------------------
    function indexOf(id) {
      for (var i = 0; i < store.annotations.length; i++) {
        if (store.annotations[i].id === id) return i;
      }
      return -1;
    }
    function replaceAll(list) {
      store.annotations.length = 0;
      list.forEach(function (a) { store.annotations.push(a); });
    }
    function commit() { persist(); store.notify(); }
    function upsert(ann) {
      var i = indexOf(ann.id);
      if (i >= 0) store.annotations[i] = ann; else store.annotations.push(ann);
      commit();
    }
    function removeById(id) {
      var i = indexOf(id);
      if (i >= 0) { store.annotations.splice(i, 1); commit(); }
    }
    function forSha(sha) {
      return store.annotations.filter(function (a) { return a.artifactSha256 === sha; });
    }

    // -- tiny DOM helpers ------------------------------------------------------
    function el(tag, attrs, kids) {
      var e = document.createElement(tag);
      if (attrs) Object.keys(attrs).forEach(function (k) {
        if (k === 'text') e.textContent = attrs[k];
        else if (attrs[k] != null) e.setAttribute(k, attrs[k]);
      });
      (kids || []).forEach(function (c) {
        e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
      return e;
    }
    function svg(tag, attrs) {
      var e = document.createElementNS(SVGNS, tag);
      Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
      return e;
    }
    function assign(a, b) { Object.keys(b).forEach(function (k) { a[k] = b[k]; }); return a; }
    function labelWrap(text, control) {
      return el('label', { class: 'af-field' }, [el('span', { class: 'af-label', text: text }), control]);
    }
    function num(input) { var v = parseFloat(input.value); return isFinite(v) ? v : 0; }
    function pct(fraction) { return (Math.round(clamp01(fraction) * 1000) / 10).toString(); }
    function fmtPts(points) {
      return points.map(function (p) {
        return Math.round(p[0] * 100) + '%,' + Math.round(p[1] * 100) + '%';
      }).join(' to ');
    }
    function describe(ann) {
      var where = ann.geometry ? ann.shape + ' at ' + fmtPts(ann.geometry.points) : ann.shape + ' (whole image)';
      return where + ', ' + ann.severity + ' severity.';
    }
    // Re-set an aria-live region: blanking first makes screen readers re-announce
    // an identical message (e.g. two deletes in a row).
    function announce(status, msg) { status.textContent = ''; status.textContent = msg; }

    // -- the keyboard-first form (the ONLY path required to create/edit/delete) -
    function buildForm(fig, sha, path, kind, degraded, status) {
      var state = { editingId: null, pending: null };

      var shapeSel = el('select', { class: 'af-shape', 'aria-label': 'Annotation shape' },
        SHAPES.map(function (s) { return el('option', { value: s, text: s }); }));
      if (degraded) shapeSel.value = 'whole';

      function numInput(cls, label, value) {
        var inp = el('input', {
          type: 'number', min: '0', max: '100', step: '0.1', value: value, class: cls, 'aria-label': label,
        });
        // Typing into a region field is the keyboard taking over from any pointer
        // draw, so drop the pending pointer geometry.
        inp.addEventListener('input', function () { state.pending = null; });
        return inp;
      }
      var rx = numInput('af-rx', 'Region left edge, percent', '10');
      var ry = numInput('af-ry', 'Region top edge, percent', '10');
      var rw = numInput('af-rw', 'Region width, percent', '20');
      var rh = numInput('af-rh', 'Region height, percent', '20');

      var region = el('fieldset', { class: 'af-region' }, [
        el('legend', { text: 'Shape and region (percent of image)' }),
        labelWrap('Shape', shapeSel),
        labelWrap('Left %', rx), labelWrap('Top %', ry),
        labelWrap('Width %', rw), labelWrap('Height %', rh),
      ]);

      var sevSel = el('select', { class: 'af-sev', 'aria-label': 'Severity' },
        SEVERITIES.map(function (s) { return el('option', { value: s, text: s }); }));

      var comment = el('textarea', { class: 'af-comment', rows: '2', 'aria-label': 'Comment', required: 'required' });

      var reqBoxes = [];
      var reqNodes = requirements.map(function (r) {
        var cb = el('input', { type: 'checkbox', value: r.id });
        reqBoxes.push(cb);
        return el('label', { class: 'af-req' }, [cb, ' ' + r.id + (r.text ? ': ' + r.text : '')]);
      });
      var reqFree = el('input', {
        type: 'text', class: 'af-reqfree', 'aria-label': 'Other requirement ids, comma separated',
        placeholder: 'other-req-1, other-req-2',
      });
      var reqSet = el('fieldset', { class: 'af-reqs' },
        [el('legend', { text: 'Requirements' })].concat(reqNodes).concat([labelWrap('Other ids', reqFree)]));

      var addBtn = el('button', { type: 'submit', class: 'btn af-add', text: 'Add annotation' });
      var cancelBtn = el('button', { type: 'button', class: 'btn af-cancel', hidden: 'hidden', text: 'Cancel edit' });
      var node = el('form', { class: 'annot-form', 'aria-label': 'Add or edit an annotation for ' + path }, [
        region, labelWrap('Severity', sevSel), labelWrap('Comment', comment), reqSet,
        el('div', { class: 'annot-actions' }, [addBtn, cancelBtn]),
      ]);

      function collectReqs() {
        var ids = [];
        reqBoxes.forEach(function (cb) { if (cb.checked) ids.push(cb.value); });
        reqFree.value.split(',').forEach(function (t) { t = t.trim(); if (t) ids.push(t); });
        return sanitizeReqs(ids);
      }

      function readFields() {
        var shape = shapeSel.value, geom = null;
        if (shape !== 'whole') {
          if (state.pending && state.pending.length) geom = sanitizeGeometry({ points: state.pending });
          else geom = buildGeometry(shape, num(rx) / 100, num(ry) / 100, num(rw) / 100, num(rh) / 100);
        }
        return {
          id: state.editingId || makeId(),
          artifactSha256: sha, artifactPath: path, artifactKind: kind,
          shape: shape, severity: sevSel.value, comment: comment.value.trim(),
          requirementIds: collectReqs(), geometry: geom,
        };
      }

      function setRegionFromPoints(points) {
        var xs = points.map(function (p) { return p[0]; });
        var ys = points.map(function (p) { return p[1]; });
        var minx = Math.min.apply(null, xs), miny = Math.min.apply(null, ys);
        var maxx = Math.max.apply(null, xs), maxy = Math.max.apply(null, ys);
        rx.value = pct(minx); ry.value = pct(miny);
        rw.value = pct(maxx - minx); rh.value = pct(maxy - miny);
      }

      function setPending(points) { state.pending = points; setRegionFromPoints(points); }
      function focusStart() { shapeSel.focus(); }

      function reset() {
        state.editingId = null; state.pending = null;
        comment.value = '';
        reqBoxes.forEach(function (cb) { cb.checked = false; });
        reqFree.value = '';
        addBtn.textContent = 'Add annotation';
        cancelBtn.setAttribute('hidden', 'hidden');
      }

      function setEditing(ann) {
        state.editingId = ann.id;
        shapeSel.value = ann.shape;
        sevSel.value = ann.severity || 'info';
        comment.value = ann.comment || '';
        var known = {};
        reqBoxes.forEach(function (cb) {
          known[cb.value] = 1;
          cb.checked = (ann.requirementIds || []).indexOf(cb.value) >= 0;
        });
        reqFree.value = (ann.requirementIds || []).filter(function (id) { return !known[id]; }).join(', ');
        if (ann.geometry && ann.geometry.points) { state.pending = ann.geometry.points.slice(); setRegionFromPoints(ann.geometry.points); }
        else state.pending = null;
        addBtn.textContent = 'Save changes';
        cancelBtn.removeAttribute('hidden');
        announce(status, 'Editing ' + describe(ann) + ' Update the fields and choose Save changes.');
        comment.focus();
      }

      node.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var fields = readFields();
        if (!fields.comment) { announce(status, 'A comment is required before saving an annotation.'); comment.focus(); return; }
        var ann = sanitize(fields);
        if (!ann) { announce(status, 'That annotation is incomplete and was not saved.'); return; }
        var editing = !!state.editingId;
        upsert(ann);
        announce(status, (editing ? 'Annotation updated: ' : 'Annotation added: ') + describe(ann));
        reset();
        focusStart();
      });
      cancelBtn.addEventListener('click', function () { reset(); announce(status, 'Edit cancelled.'); focusStart(); });

      return {
        node: node, setEditing: setEditing, reset: reset, setPending: setPending,
        focusStart: focusStart, getShape: function () { return shapeSel.value; },
        editingId: function () { return state.editingId; },
      };
    }

    function doDelete(ann, status, form) {
      removeById(ann.id);
      if (form.editingId() === ann.id) form.reset();
      announce(status, 'Annotation deleted: ' + describe(ann));
      form.focusStart();
    }

    // -- the annotation list: keyboard navigable, each item focusable ----------
    function renderList(list, sha, form, status) {
      list.innerHTML = '';
      var items = forSha(sha);
      if (!items.length) {
        list.appendChild(el('li', {
          class: 'muted annot-empty',
          text: 'No annotations yet. Use the form above; every field works with the keyboard.',
        }));
        return;
      }
      items.forEach(function (ann, idx) {
        var badge = el('span', { class: 'annot-badge sev-' + ann.severity, text: SEV_LABEL[ann.severity] || ann.severity });
        var geomText = ann.geometry ? fmtPts(ann.geometry.points) : 'whole image';
        var meta = el('span', { class: 'annot-meta' }, [
          el('span', { class: 'annot-idx', text: '#' + (idx + 1) }), ' ',
          el('span', { class: 'annot-shape', text: ann.shape }), ' ',
          el('span', { class: 'annot-geom mono', text: geomText }),
        ]);
        var edit = el('button', { type: 'button', class: 'btn annot-edit', text: 'Edit' });
        var del = el('button', { type: 'button', class: 'btn annot-delete', text: 'Delete' });
        edit.addEventListener('click', function () { form.setEditing(ann); });
        del.addEventListener('click', function () { doDelete(ann, status, form); });
        var kids = [badge, ' ', meta, el('p', { class: 'annot-comment', text: ann.comment })];
        if (ann.requirementIds && ann.requirementIds.length) {
          kids.push(el('span', { class: 'annot-reqs', text: 'requirements: ' + ann.requirementIds.join(', ') }));
        }
        kids.push(el('div', { class: 'annot-item-actions' }, [edit, del]));
        var li = el('li', { class: 'annot-item', tabindex: '0', 'data-id': ann.id }, kids);
        li.addEventListener('keydown', function (ev) {
          if (ev.key === 'Delete') { ev.preventDefault(); doDelete(ann, status, form); }
          else if (ev.key === 'Enter') { ev.preventDefault(); form.setEditing(ann); }
        });
        list.appendChild(li);
      });
    }

    // -- the visual overlay (progressive enhancement over the form) ------------
    function overlayShape(shape, pts, common) {
      if (shape === 'point') return svg('circle', assign({ cx: pts[0][0], cy: pts[0][1], r: '9' }, common));
      if (shape === 'freehand') {
        return svg('polyline', assign({ points: pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' ') }, common));
      }
      if (shape === 'arrow') {
        return svg('line', assign({ x1: pts[0][0], y1: pts[0][1], x2: pts[1][0], y2: pts[1][1] }, common));
      }
      var x = Math.min(pts[0][0], pts[1][0]), y = Math.min(pts[0][1], pts[1][1]);
      var w = Math.abs(pts[1][0] - pts[0][0]), h = Math.abs(pts[1][1] - pts[0][1]);
      var attrs = assign({ x: x, y: y, width: w, height: h }, common);
      if (shape === 'highlight') { attrs.fill = common.stroke; attrs['fill-opacity'] = '0.18'; }
      return svg('rect', attrs);
    }

    function renderOverlay(overlay, labels, sha) {
      while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
      if (labels) labels.innerHTML = '';
      forSha(sha).forEach(function (ann, idx) {
        if (!ann.geometry || !ann.geometry.points || !ann.geometry.points.length) return;
        var pts = ann.geometry.points.map(function (p) { return [p[0] * 1000, p[1] * 1000]; });
        var stroke = SEV_STROKE[ann.severity] || 'var(--accent)';
        var common = {
          fill: 'none', stroke: stroke, 'stroke-width': ann.severity === 'blocker' ? '3' : '2',
          'stroke-dasharray': SEV_DASH[ann.severity] || '0', 'vector-effect': 'non-scaling-stroke',
        };
        overlay.appendChild(overlayShape(ann.shape, pts, common));
        if (labels) {
          var lab = el('span', { class: 'annot-label', text: '#' + (idx + 1) + ' ' + (SEV_CODE[ann.severity] || '') });
          lab.style.left = (pts[0][0] / 10) + '%';
          lab.style.top = (pts[0][1] / 10) + '%';
          labels.appendChild(lab);
        }
      });
    }

    function previewOverlay(overlay, shape, pts) {
      var old = overlay.querySelector('.af-preview');
      if (old) overlay.removeChild(old);
      if (shape === 'whole' || !pts.length) return;
      var scaled = pts.map(function (p) { return [p[0] * 1000, p[1] * 1000]; });
      var node = overlayShape(shape, scaled, {
        fill: 'none', stroke: 'var(--accent)', 'stroke-width': '2',
        'stroke-dasharray': '6 4', 'vector-effect': 'non-scaling-stroke', class: 'af-preview',
      });
      overlay.appendChild(node);
    }

    // Pointer drawing funnels through normalizePoint too, so both paths store the
    // same resolution-independent fractions. The overlay is not in the tab order
    // and never calls preventDefault on Tab, so it cannot trap keyboard focus.
    function wireOverlay(overlay, form) {
      var drawing = false, pts = [];
      function toPoint(ev) {
        var rect = overlay.getBoundingClientRect();
        var src = ev.touches && ev.touches[0] ? ev.touches[0] : ev;
        return normalizePoint(src.clientX, src.clientY, rect);
      }
      overlay.addEventListener('mousedown', function (ev) {
        var shape = form.getShape();
        if (shape === 'whole') return;
        ev.preventDefault();
        drawing = true; pts = [toPoint(ev)];
        if (shape === 'point') { drawing = false; form.setPending(pts); }
        previewOverlay(overlay, shape, pts);
      });
      overlay.addEventListener('mousemove', function (ev) {
        if (!drawing) return;
        var p = toPoint(ev), shape = form.getShape();
        if (shape === 'freehand') pts.push(p); else pts = [pts[0], p];
        form.setPending(pts);
        previewOverlay(overlay, shape, pts);
      });
      window.addEventListener('mouseup', function () {
        if (!drawing) return;
        drawing = false; form.setPending(pts);
      });
    }

    function initFigure(fig) {
      var sha = fig.getAttribute('data-sha') || '';
      var annotatable = fig.getAttribute('data-annotatable') !== '0' && SHA_RE.test(sha);
      var path = fig.getAttribute('data-path') || '';
      var kind = fig.getAttribute('data-kind') || '';
      var mount = fig.querySelector('.annot-mount');
      if (!mount) return;
      if (!annotatable) {
        mount.appendChild(el('p', {
          class: 'muted',
          text: 'This artifact cannot be annotated: an annotation must cite a 64-hex SHA-256 that appears in run.artifacts.',
        }));
        return;
      }
      var overlay = fig.querySelector('.annot-overlay');
      var labels = fig.querySelector('.annot-labels');
      var degraded = fig.hasAttribute('data-degraded');
      var status = el('div', { class: 'annot-status', role: 'status', 'aria-live': 'polite' });
      var list = el('ul', { class: 'annot-list' });
      var form = buildForm(fig, sha, path, kind, degraded, status);

      mount.appendChild(status);
      mount.appendChild(form.node);
      mount.appendChild(list);

      store.subscribe(function () {
        renderList(list, sha, form, status);
        if (overlay) renderOverlay(overlay, labels, sha);
      });
      renderList(list, sha, form, status);
      if (overlay) { renderOverlay(overlay, labels, sha); wireOverlay(overlay, form); }
    }

    function readData() {
      var node = document.getElementById('adlc-evidence-data');
      if (!node) return null;
      try { return JSON.parse(node.textContent); } catch (e) { return null; }
    }

    function boot() {
      data = readData();
      if (!data) return;
      runId = String(data.runId || 'unknown');
      requirements = Array.isArray(data.requirements) ? data.requirements : [];
      replaceAll(loadPersisted());
      Array.prototype.slice.call(document.querySelectorAll('.annot-artifact')).forEach(initFigure);
      store.notify();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
  })();
