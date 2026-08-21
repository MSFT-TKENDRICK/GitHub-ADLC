// Layer 5 -- critique UI behaviour for agent-authored reasoning cards.
//
// Loaded verbatim inside the reasoning section's own <script> via read_asset().
// It reads its data from a <script type="application/json"> block (never from JS
// source), persists recorded critiques to localStorage keyed by run id, and
// mirrors them into the shared window.adlcFeedback client registry so the other
// feedback layers observe the same store. No framework, no network.

(function () {
  // Lazy, idempotent shared registry. Whichever layer loads first creates it;
  // load order across layers does not matter and no layer owns the global.
  const store = (window.adlcFeedback = window.adlcFeedback || {
    annotations: [], critiques: [], diffDecisions: [], listeners: [],
    notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },
    subscribe(fn) { this.listeners.push(fn); },
  });

  function start() {
    var dataEl = document.getElementById('adlc-critique-data');
    if (!dataEl) { return; }

    var payload;
    try {
      payload = JSON.parse(dataEl.textContent);
    } catch (e) {
      return;
    }
    var runId = (payload && payload.runId) || 'unknown';
    var targets = (payload && payload.targets) || [];
    var byId = {};
    targets.forEach(function (t) { byId[t.id] = t; });

    var KEY = 'adlc.critiques.' + runId;

    // Schema vocabularies, mirrored from $defs.critique so anything read back from
    // storage is normalised to a valid critique before it enters the shared store.
    var STANCES = { agree: 1, disagree: 1, needs_evidence: 1, out_of_scope: 1 };
    var KINDS = { squad_finding: 1, persona: 1, rubric_criterion: 1, adr: 1 };
    var ID_RE = /^[A-Za-z0-9._-]{1,64}$/;
    var DIGEST_RE = /^sha256:[a-f0-9]{64}$/;

    function sanitize(c) {
      if (!c || typeof c !== 'object') { return null; }
      if (typeof c.id !== 'string' || !ID_RE.test(c.id)) { return null; }
      if (!STANCES[c.stance] || !KINDS[c.targetKind]) { return null; }
      if (typeof c.targetRef !== 'string' || c.targetRef.length < 1 || c.targetRef.length > 512) {
        return null;
      }
      if (typeof c.comment !== 'string' || c.comment.length < 1 || c.comment.length > 4000) {
        return null;
      }
      // Rebuild from scratch so unknown keys can never violate additionalProperties:false.
      var clean = {
        id: c.id,
        targetKind: c.targetKind,
        targetRef: c.targetRef,
        stance: c.stance,
        comment: c.comment,
      };
      if (typeof c.targetTitle === 'string' && c.targetTitle.length <= 512) {
        clean.targetTitle = c.targetTitle;
      }
      if (typeof c.sourceDigest === 'string' && DIGEST_RE.test(c.sourceDigest)) {
        clean.sourceDigest = c.sourceDigest;
      }
      return clean;
    }

    function load() {
      try {
        var raw = window.localStorage.getItem(KEY);
        if (!raw) { return []; }
        var parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) { return []; }
        var out = [];
        parsed.forEach(function (c) {
          var clean = sanitize(c);
          if (clean) { out.push(clean); }
        });
        return out;
      } catch (e) {
        // Private mode, disabled storage or quota: degrade to in-memory only.
        return [];
      }
    }

    function persist() {
      try {
        window.localStorage.setItem(KEY, JSON.stringify(store.critiques));
        return true;
      } catch (e) {
        return false;
      }
    }

    function indexOfId(id) {
      for (var i = 0; i < store.critiques.length; i++) {
        if (store.critiques[i].id === id) { return i; }
      }
      return -1;
    }

    function upsert(critique) {
      var at = indexOfId(critique.id);
      if (at === -1) { store.critiques.push(critique); } else { store.critiques[at] = critique; }
    }

    function removeId(id) {
      var at = indexOfId(id);
      if (at !== -1) { store.critiques.splice(at, 1); }
    }

    function announce(card, message, recorded) {
      var status = card.querySelector('.critique-status');
      if (!status) { return; }
      status.textContent = message;
      if (recorded) { status.classList.add('recorded'); } else { status.classList.remove('recorded'); }
    }

    function selectedStance(card, id) {
      var checked = card.querySelector('input[name="' + id + '-stance"]:checked');
      return checked ? checked.value : '';
    }

    function record(card) {
      var id = card.getAttribute('data-critique-id');
      var desc = byId[id];
      if (!desc) { return; }
      var stance = selectedStance(card, id);
      if (!stance) {
        announce(card, 'Choose a stance before recording this critique.', false);
        return;
      }
      var field = card.querySelector('textarea');
      var comment = field ? field.value.trim() : '';
      if (!comment) {
        announce(card, 'Add a comment before recording this critique.', false);
        return;
      }
      var critique = {
        id: id,
        targetKind: desc.targetKind,
        targetRef: desc.targetRef,
        stance: stance,
        comment: comment,
      };
      if (desc.targetTitle) { critique.targetTitle = desc.targetTitle; }
      if (desc.sourceDigest) { critique.sourceDigest = desc.sourceDigest; }
      upsert(critique);
      var saved = persist();
      card.classList.add('has-critique');
      announce(card, saved
        ? 'Critique recorded (' + stance + ').'
        : 'Critique recorded (' + stance + '); could not save to this browser.', true);
      store.notify();
    }

    function clear(card) {
      var id = card.getAttribute('data-critique-id');
      removeId(id);
      persist();
      var checked = card.querySelector('input[name="' + id + '-stance"]:checked');
      if (checked) { checked.checked = false; }
      var field = card.querySelector('textarea');
      if (field) { field.value = ''; }
      card.classList.remove('has-critique');
      announce(card, 'Critique cleared.', false);
      store.notify();
    }

    function hydrate(card) {
      var id = card.getAttribute('data-critique-id');
      var existing = store.critiques[indexOfId(id)];
      if (!existing) { return; }
      var radio = card.querySelector(
        'input[name="' + id + '-stance"][value="' + existing.stance + '"]'
      );
      if (radio) { radio.checked = true; }
      var field = card.querySelector('textarea');
      if (field) { field.value = existing.comment; }
      // Reflect the saved state visually only. The checked radio and filled
      // textarea already convey it to assistive tech; the aria-live status must
      // NOT be written on load or every saved card would announce on first paint.
      card.classList.add('has-critique');
    }

    // Hydrate the shared array in place so the reference other layers hold stays valid.
    var loaded = load();
    store.critiques.length = 0;
    loaded.forEach(function (c) { store.critiques.push(c); });

    var cards = document.querySelectorAll('.rcard');
    Array.prototype.forEach.call(cards, function (card) {
      hydrate(card);
      var buttons = card.querySelectorAll('button[data-action]');
      Array.prototype.forEach.call(buttons, function (btn) {
        btn.addEventListener('click', function () {
          if (btn.getAttribute('data-action') === 'record') { record(card); } else { clear(card); }
        });
      });
    });

    store.notify();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
