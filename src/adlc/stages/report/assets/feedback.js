/* Layer 7-UI: turn the three annotation surfaces into one submitted act.
 *
 * This asset is injected as a str.format *substitution value* and is never
 * rescanned, so it uses ordinary single braces throughout.
 *
 * packDigest DECISION: INCLUDED.
 * The Python ingest (adlc.stages.feedback.pack_digest) hashes
 *   json.dumps(pack_without_packDigest, sort_keys=True,
 *              separators=(",", ":"), ensure_ascii=False).encode("utf-8")
 * with SHA-256. canonicalize() below reproduces those exact bytes:
 *   - it sorts object keys recursively at every depth (schema keys are ASCII,
 *     so String#sort's code-unit order equals Python's code-point order);
 *   - it renders numbers with numToken()/pyFloatRepr(), which replicate
 *     CPython's float repr byte-for-byte (verified against a 1,000,000-sample
 *     fuzz plus every fixed/scientific boundary: 1e-4, 1e-5, 1e15, 1e16, ...);
 *   - it escapes strings with JSON.stringify, which is identical to
 *     json.dumps(ensure_ascii=False) for all text that is not a lone
 *     surrogate; and
 *   - it UTF-8 encodes before hashing.
 * The single input where JS and Python diverge is a lone surrogate: JS escapes
 * it, Python raises UnicodeEncodeError. apply_feedback catches exactly that and
 * refuses the pack, so a lone surrogate can never become a *false* digest
 * mismatch on an honest pack. If crypto.subtle is unavailable we omit
 * packDigest rather than attach a wrong one -- it is optional on ingest
 * ("if declared:"), and a digest that fails for honest packs is worse than
 * none.
 *
 * reportDigest is computed from the report's own served bytes (fetched once at
 * load), never guessed, and omitted when the page is not served (a file://
 * download cannot read its own bytes). Emitting it from Python is impossible:
 * the report embeds the digest, so a build-time digest of report.html has no
 * fixed point.
 */
(function () {
  "use strict";

  // -- canonicalisation: byte-identical to feedback.canonical_bytes ---------
  // Defined first and with no DOM or window access, so a Node-based test can
  // load this shipped file and prove the digest matches the Python ingest.

  function pyFloatRepr(n) {
    if (n === 0) return (1 / n === -Infinity) ? "-0.0" : "0.0";
    const neg = n < 0;
    const exp = Math.abs(n).toExponential();
    const m = exp.match(/^(\d)(?:\.(\d+))?e([+-]\d+)$/);
    const digits = m[1] + (m[2] || "");
    const e = parseInt(m[3], 10);
    const nd = digits.length;
    const decpt = e + 1;
    let out;
    if (decpt > -4 && decpt <= 16) {
      if (decpt <= 0) out = "0." + "0".repeat(-decpt) + digits;
      else if (decpt >= nd) out = digits + "0".repeat(decpt - nd) + ".0";
      else out = digits.slice(0, decpt) + "." + digits.slice(decpt);
    } else {
      const mant = digits.length === 1 ? digits : digits[0] + "." + digits.slice(1);
      const es = e < 0 ? "-" : "+";
      let ea = Math.abs(e).toString();
      if (ea.length < 2) ea = "0" + ea;
      out = mant + "e" + es + ea;
    }
    return neg ? "-" + out : out;
  }

  function numToken(n) {
    if (Number.isInteger(n) && Math.abs(n) < 1e21) return String(n);
    return pyFloatRepr(n);
  }

  function canonicalize(value) {
    if (value === null) return "null";
    const t = typeof value;
    if (t === "boolean") return value ? "true" : "false";
    if (t === "number") {
      if (!isFinite(value)) throw new Error("a non-finite number cannot be canonicalised");
      return numToken(value);
    }
    if (t === "string") return JSON.stringify(value);
    if (Array.isArray(value)) {
      const parts = [];
      for (let i = 0; i < value.length; i++) parts.push(canonicalize(value[i]));
      return "[" + parts.join(",") + "]";
    }
    if (t === "object") {
      const keys = Object.keys(value).filter(function (k) { return value[k] !== undefined; }).sort();
      const parts = [];
      for (let i = 0; i < keys.length; i++) {
        parts.push(JSON.stringify(keys[i]) + ":" + canonicalize(value[keys[i]]));
      }
      return "{" + parts.join(",") + "}";
    }
    throw new Error("value of type " + t + " cannot be canonicalised");
  }

  // Test seam: under Node there is no document. Export the pure canonicaliser
  // for the equivalence proof, then stop -- there is nothing to wire without a
  // DOM. In a browser `document` exists, so execution continues below.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined" && module.exports) {
      module.exports = { pyFloatRepr: pyFloatRepr, numToken: numToken, canonicalize: canonicalize };
    }
    return;
  }

  // Shared cross-layer registry. Exact idempotent lazy initialiser so load
  // order relative to the evidence/reasoning/diff sections does not matter.
  const store = (window.adlcFeedback = window.adlcFeedback || {
    annotations: [], critiques: [], diffDecisions: [], listeners: [],
    notify() { this.listeners.forEach(function (fn) { try { fn(); } catch (e) {} }); },
    subscribe(fn) { this.listeners.push(fn); },
  });

  const cfgEl = document.getElementById("adlc-feedback-config");
  if (!cfgEl) return; // section omitted (nothing to submit against)
  let cfg;
  try { cfg = JSON.parse(cfgEl.textContent); } catch (e) { return; }

  const byId = function (id) { return document.getElementById(id); };
  const verdictEl = byId("adlc-verdict");
  const routeEl = byId("adlc-route");
  const summaryEl = byId("adlc-summary");
  const byEl = byId("adlc-submitted-by");
  const dlBtn = byId("adlc-download");
  const copyBtn = byId("adlc-copy");
  const postBtn = byId("adlc-submit");
  const statusEl = byId("adlc-status");
  const errorEl = byId("adlc-error");
  const conflictEl = byId("adlc-conflict");
  const countsEl = byId("adlc-counts");
  const guidanceEl = byId("adlc-guidance");
  const fallbackEl = byId("adlc-copy-fallback");
  const postNoteEl = byId("adlc-submit-note");
  if (!verdictEl || !routeEl || !summaryEl || !postBtn) return;

  const params = new URLSearchParams(location.search);
  const nonce = params.get("nonce") || "";
  // Only the adlc loopback server is a legitimate submission target: it binds
  // 127.0.0.1 and its origin check accepts exactly 127.0.0.1/localhost. Gating
  // the client to those hosts stops any other http(s) page that happens to carry
  // a ?nonce= from POSTing feedback and reporting a forged "accepted".
  const loopback = location.hostname === "127.0.0.1" || location.hostname === "localhost";
  const served = /^https?:$/.test(location.protocol) && loopback && nonce.length > 0;

  function subtleOk() {
    return typeof window.crypto !== "undefined" && window.crypto && window.crypto.subtle;
  }

  async function sha256Hex(bytes) {
    const digest = await window.crypto.subtle.digest("SHA-256", bytes);
    const arr = new Uint8Array(digest);
    let hex = "";
    for (let i = 0; i < arr.length; i++) hex += arr[i].toString(16).padStart(2, "0");
    return hex;
  }

  async function packDigest(pack) {
    if (!subtleOk()) return null;
    try {
      const bytes = new TextEncoder().encode(canonicalize(pack));
      return "sha256:" + (await sha256Hex(bytes));
    } catch (e) {
      return null; // never attach a digest we are not certain of
    }
  }

  async function computeReportDigest() {
    if (!served || !subtleOk()) return null;
    try {
      const resp = await fetch(location.href, { cache: "no-store" });
      if (!resp.ok) return null;
      const buf = await resp.arrayBuffer();
      return "sha256:" + (await sha256Hex(buf));
    } catch (e) {
      return null;
    }
  }

  // Captured once, at load, so it names the rendering the reviewer authored
  // against -- not whatever the server happens to hold at submit time.
  const reportDigestPromise = computeReportDigest();

  // -- pack assembly --------------------------------------------------------

  function collect(list) {
    return Array.isArray(list) ? list.slice() : [];
  }

  function scrubText(s) {
    // Replace UNPAIRED UTF-16 surrogates with U+FFFD so the value is always
    // encodable as UTF-8. The ingest hashes json.loads(wire); a lone surrogate
    // survives JSON.stringify as \\udXXX but makes Python's UTF-8 encode raise,
    // which would refuse an otherwise-honest pack. Scrubbing keeps download,
    // copy and POST consistent and keeps the packDigest identical on both sides.
    // Valid surrogate pairs are left untouched. Applied only to the fields this
    // section owns (summary, submittedBy); the sibling arrays are passed through
    // unreshaped by contract.
    return s.replace(/[\ud800-\udfff]/g, function (ch, i, str) {
      var code = ch.charCodeAt(0);
      if (code <= 0xdbff) {
        var next = str.charCodeAt(i + 1);
        if (next >= 0xdc00 && next <= 0xdfff) return ch;
      } else {
        var prev = str.charCodeAt(i - 1);
        if (prev >= 0xd800 && prev <= 0xdbff) return ch;
      }
      return "\ufffd";
    });
  }

  async function assemblePack() {
    const pack = {
      schemaVersion: cfg.schemaVersion,
      runId: cfg.runId,
      candidateSha: cfg.candidateSha || "",
      submittedAt: new Date().toISOString(),
      verdict: verdictEl.value,
      route: routeEl.value,
    };
    const by = scrubText(byEl && byEl.value ? byEl.value : "").trim();
    if (by) pack.submittedBy = by;
    const summary = scrubText((summaryEl.value || "").replace(/\r\n/g, "\n")).trim();
    if (summary) pack.summary = summary;

    const annotations = collect(store.annotations);
    const critiques = collect(store.critiques);
    const diffDecisions = collect(store.diffDecisions);
    if (annotations.length) pack.annotations = annotations;
    if (critiques.length) pack.critiques = critiques;
    if (diffDecisions.length) pack.diffDecisions = diffDecisions;

    const rd = await reportDigestPromise;
    if (rd) pack.reportDigest = rd;

    const pd = await packDigest(pack);
    if (pd) pack.packDigest = pd;
    return pack;
  }

  // -- pre-submit conflict detection (mirror of blocking_conflicts) ---------

  function idOf(item) {
    return String(item && item.id != null ? item.id : "?");
  }

  function blockingConflicts() {
    if (verdictEl.value !== "accept") return [];
    const ids = [];
    collect(store.annotations).forEach(function (it) {
      if (it && it.severity === "blocker") ids.push(idOf(it));
    });
    collect(store.critiques).forEach(function (it) {
      if (it && it.severity === "blocker") ids.push(idOf(it));
    });
    collect(store.diffDecisions).forEach(function (it) {
      if (it && it.decision === "reject") ids.push(idOf(it));
    });
    return ids.sort();
  }

  // -- announcements --------------------------------------------------------

  function clearOutcome() {
    statusEl.textContent = "";
    errorEl.textContent = "";
  }

  function announceStatus(msg) {
    errorEl.textContent = "";
    statusEl.textContent = msg;
  }

  function announceError(msg) {
    statusEl.textContent = "";
    errorEl.textContent = msg;
    try { errorEl.focus(); } catch (e) {}
  }

  // -- egress ---------------------------------------------------------------

  function hasSubmittableContent() {
    const summaryFilled = (summaryEl.value || "").trim().length > 0;
    return summaryFilled
      || collect(store.annotations).length > 0
      || collect(store.critiques).length > 0
      || collect(store.diffDecisions).length > 0;
  }

  function setEgress(enabled) {
    dlBtn.disabled = !enabled;
    copyBtn.disabled = !enabled;
    postBtn.disabled = !enabled || !served;
  }

  async function onDownload() {
    clearOutcome();
    try {
      const pack = await assemblePack();
      const text = JSON.stringify(pack, null, 2);
      const blob = new Blob([text], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const name = "adlc-feedback-" + (cfg.runId || "run") + ".json";
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 0);
      announceStatus("Pack downloaded as " + name + ". Apply it with:  adlc feedback apply " + name);
    } catch (e) {
      announceError("Could not build the feedback pack: " + (e && e.message ? e.message : String(e)) + ". Nothing was downloaded.");
    }
  }

  async function onCopy() {
    clearOutcome();
    let text;
    try {
      const pack = await assemblePack();
      text = JSON.stringify(pack, null, 2);
    } catch (e) {
      announceError("Could not build the feedback pack: " + (e && e.message ? e.message : String(e)) + ". Nothing was copied.");
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        fallbackEl.hidden = true;
        announceStatus("Pack copied to the clipboard.");
        return;
      }
      throw new Error("clipboard API unavailable");
    } catch (e) {
      // file:// is not a secure context in every browser, so the async
      // clipboard may be missing. Expose the text for a manual copy rather
      // than fail silently.
      fallbackEl.value = text;
      fallbackEl.hidden = false;
      fallbackEl.focus();
      fallbackEl.select();
      announceStatus("Clipboard is unavailable here. The pack is selected in the box below \u2014 press Ctrl+C or Cmd+C to copy it.");
    }
  }

  async function onSubmit() {
    clearOutcome();
    if (!served) {
      announceError("Direct submission needs the report's loopback server, which is not present. Download or copy the pack and run:  adlc feedback apply <file>");
      return;
    }
    let body, byteLength;
    try {
      const pack = await assemblePack();
      body = JSON.stringify(pack);
      byteLength = new TextEncoder().encode(body).length;
    } catch (e) {
      announceError("Could not build the feedback pack: " + (e && e.message ? e.message : String(e)) + ". Nothing was sent.");
      return;
    }
    if (byteLength > cfg.maxBodyBytes) {
      announceError("This pack is " + byteLength + " bytes, over the server's " + cfg.maxBodyBytes
        + "-byte limit. Remove some annotations, or download the pack and apply it from the command line. Nothing was sent.");
      return;
    }

    postBtn.disabled = true;
    announceStatus("Submitting feedback\u2026");

    let resp, rawText;
    try {
      // Same-origin POST: a relative path resolves to the page's own origin, so
      // the custom nonce header triggers no CORS preflight (the server answers
      // OPTIONS with 405 by design). text/plain is a CORS-safelisted
      // Content-Type; the server does json.loads regardless.
      const headers = { "Content-Type": "text/plain;charset=UTF-8" };
      headers[cfg.nonceHeader] = nonce;
      resp = await fetch(cfg.submitPath, {
        method: "POST",
        headers: headers,
        body: body,
        cache: "no-store",
      });
      rawText = await resp.text();
    } catch (e) {
      postBtn.disabled = false;
      announceError("The server did not return a response, so the outcome is unknown: "
        + (e && e.message ? e.message : String(e))
        + ". The feedback may or may not have been applied \u2014 resubmitting is safe"
        + " (identical packs are de-duplicated on the server), or download the pack and"
        + " apply it from the command line.");
      return;
    }

    let result = null;
    try { result = JSON.parse(rawText); } catch (e) { result = null; }

    if (resp.ok && result && result.applied === true) {
      let extra = "";
      if (result.successorRun) {
        extra = " Successor run " + result.successorRun + " created (" + (result.outcome || "iterate") + ").";
      } else if (result.outcome) {
        extra = " Outcome: " + result.outcome + ".";
      }
      if (result.reportDrift) {
        extra += " Note: the server reports this pack was authored against a different rendering of the report.";
      }
      announceStatus("Feedback accepted." + extra);
    } else {
      const reason = (result && result.reason) ? result.reason : (rawText || ("HTTP " + resp.status));
      announceError("The server REFUSED this feedback (HTTP " + resp.status + "): " + reason
        + "  \u2014 nothing was applied. Fix the issue and resubmit, or download the pack.");
    }
    postBtn.disabled = false;
    refresh();
  }

  // -- live state -----------------------------------------------------------

  function refresh() {
    const a = collect(store.annotations).length;
    const c = collect(store.critiques).length;
    const d = collect(store.diffDecisions).length;
    countsEl.textContent = "Collected " + a + " annotation(s), " + c + " reasoning critique(s) and "
      + d + " diff decision(s) from the surfaces above.";

    const conflicts = blockingConflicts();
    if (conflicts.length) {
      conflictEl.textContent = "\u26a0 Blocking conflict: verdict \u201caccept\u201d cannot ship with "
        + conflicts.length + " unresolved blocker/reject item(s) \u2014 " + conflicts.join(", ")
        + ". Change the verdict to \u201crevise\u201d or resolve those items before submitting.";
    } else {
      conflictEl.textContent = "";
    }

    const content = hasSubmittableContent();
    if (!conflicts.length && !content) {
      guidanceEl.textContent = "Add a summary, or at least one annotation, critique or diff decision, before submitting \u2014 an empty pack is refused.";
    } else {
      guidanceEl.textContent = "";
    }

    setEgress(conflicts.length === 0 && content);
  }

  function init() {
    if (served) {
      postBtn.disabled = false;
      if (postNoteEl) { postNoteEl.textContent = ""; postNoteEl.hidden = true; }
    } else {
      postBtn.disabled = true;
      if (postNoteEl) {
        postNoteEl.hidden = false;
        postNoteEl.textContent = location.protocol === "file:"
          ? "Opened from a file, so direct submission is off. Download or copy the pack and run:  adlc feedback apply <file>"
          : "No submit nonce is present, so the loopback server cannot be reached. Download or copy the pack instead.";
      }
    }
    dlBtn.addEventListener("click", onDownload);
    copyBtn.addEventListener("click", onCopy);
    postBtn.addEventListener("click", onSubmit);
    verdictEl.addEventListener("change", refresh);
    summaryEl.addEventListener("input", refresh);
    store.subscribe(refresh);
    refresh();
  }

  init();
})();
