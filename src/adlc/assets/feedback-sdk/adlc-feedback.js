/*
 * adlc-feedback -- the portable client half of the ADLC human feedback loop.
 *
 * This file is deliberately DOM-free and framework-free. It knows nothing about
 * report.html, canvases, React, or any other rendering choice, because the GUI
 * is the part most likely to be replaced. What it knows is the contract:
 *
 *     in   adlc-feedback-targets/v1   (feedback-targets.json)
 *     out  adlc-human-feedback/v1     (the pack `adlc feedback apply` ingests)
 *
 * A GUI supplies pixels and events; this supplies identity, normalisation,
 * validation, the canonical digest, and egress. Anything a second GUI would
 * otherwise have to reimplement -- and reimplement subtly differently -- lives
 * here on purpose.
 *
 * Loads as a classic script (sets globalThis.AdlcFeedbackSDK), as CommonJS, and
 * as an ES module via the generated .mjs wrapper.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.AdlcFeedbackSDK = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var PACK_VERSION = "adlc-human-feedback/v1";
  var TARGETS_VERSION = "adlc-feedback-targets/v1";

  /*
   * Geometry is quantised to 4 decimals. That is not cosmetic: it is what makes
   * the digest reproducible across languages. Python renders floats below 1e-4
   * in exponent form (1e-05) where JavaScript renders 0.00001, so two honest
   * implementations would disagree on the canonical bytes for the same pack.
   * Four decimals over a 0..1 range is sub-pixel on any display that exists,
   * and every 4-decimal value has the same shortest round-trip form in both
   * languages. Precision we cannot verify is worse than precision we cap.
   */
  var GEOMETRY_DECIMALS = 4;

  // ---------------------------------------------------------------------
  // Canonical JSON -- must reproduce adlc.stages.feedback.canonical_bytes
  // ---------------------------------------------------------------------

  function canonicalize(value) {
    if (value === null) return "null";
    var type = typeof value;
    if (type === "boolean") return value ? "true" : "false";
    if (type === "number") return canonicalNumber(value);
    if (type === "string") return JSON.stringify(value);
    if (Array.isArray(value)) {
      var items = [];
      for (var i = 0; i < value.length; i += 1) items.push(canonicalize(value[i]));
      return "[" + items.join(",") + "]";
    }
    if (type === "object") {
      /*
       * Sorted recursively. JSON.stringify preserves insertion order, so a GUI
       * that builds an object in a different field order than another GUI would
       * otherwise produce a different digest for identical feedback. Keys in
       * this contract are ASCII and schema-fixed (additionalProperties:false),
       * so JavaScript's UTF-16 ordering and Python's code-point ordering agree.
       */
      var keys = Object.keys(value).filter(function (k) {
        return value[k] !== undefined;
      });
      keys.sort();
      var parts = [];
      for (var j = 0; j < keys.length; j += 1) {
        parts.push(JSON.stringify(keys[j]) + ":" + canonicalize(value[keys[j]]));
      }
      return "{" + parts.join(",") + "}";
    }
    throw new TypeError("cannot canonicalize a value of type " + type);
  }

  function canonicalNumber(n) {
    if (!isFinite(n)) {
      throw new RangeError("NaN and Infinity have no JSON representation");
    }
    if (Number.isInteger(n)) return String(n);
    /*
     * String(n) is used rather than a hand-rolled decimal because it is exactly
     * what JSON.stringify puts on the wire, and the digest has to describe the
     * bytes that were actually sent. The quantisation check is what makes that
     * safe across languages: for a value on the 4-decimal grid, JavaScript's
     * shortest round-trip form and Python's repr are the same string. Off that
     * grid they are not (Python renders 1e-05, JavaScript renders 0.00001), so
     * an unquantised number is refused rather than silently digested into a
     * value ingestion will compute differently.
     */
    if (Number(n.toFixed(GEOMETRY_DECIMALS)) !== n) {
      throw new RangeError(
        "number " + n + " carries more than " + GEOMETRY_DECIMALS +
          " decimals; its canonical form is not reproducible in Python, so the " +
          "digest would not match. Round it with AdlcFeedbackSDK.quantizeNumber first."
      );
    }
    return String(n);
  }

  function utf8Bytes(text) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(text);
    var buf = [];
    for (var i = 0; i < text.length; i += 1) {
      var c = text.charCodeAt(i);
      if (c < 0x80) buf.push(c);
      else if (c < 0x800) buf.push(0xc0 | (c >> 6), 0x80 | (c & 63));
      else buf.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    }
    return new Uint8Array(buf);
  }

  /*
   * Digesting needs SubtleCrypto, which browsers only expose on a secure origin.
   * A file:// page therefore cannot compute it -- so packDigest is optional in
   * the schema and ingestion verifies it only `if declared`. Claiming a digest
   * we could not compute would be worse than omitting one.
   */
  function packDigest(pack) {
    var body = {};
    Object.keys(pack).forEach(function (k) {
      if (k !== "packDigest") body[k] = pack[k];
    });
    var bytes = utf8Bytes(canonicalize(body));
    var subtle =
      typeof crypto !== "undefined" && crypto.subtle ? crypto.subtle : null;
    if (!subtle) return Promise.resolve(null);
    return subtle.digest("SHA-256", bytes).then(function (buf) {
      var out = "";
      new Uint8Array(buf).forEach(function (b) {
        out += b.toString(16).padStart(2, "0");
      });
      return "sha256:" + out;
    });
  }

  // ---------------------------------------------------------------------
  // Identity and text hygiene
  // ---------------------------------------------------------------------

  var idCounter = 0;

  function newId(prefix) {
    idCounter += 1;
    var rand;
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      var a = new Uint32Array(1);
      crypto.getRandomValues(a);
      rand = a[0].toString(36);
    } else {
      rand = Math.floor(Math.random() * 0xffffffff).toString(36);
    }
    // Matches the pack schema's id pattern ^[A-Za-z0-9._-]+$ by construction.
    return (prefix || "f") + "-" + idCounter.toString(36) + "-" + rand;
  }

  /*
   * Lone surrogates survive JSON.stringify as \udXXX escapes but make Python's
   * canonical encoder raise on .encode("utf-8"). They cannot be typed, only
   * pasted from broken data, so they are dropped rather than allowed to turn a
   * reviewer's submission into a server-side traceback.
   */
  function cleanText(value, limit) {
    var text = String(value === null || value === undefined ? "" : value);
    text = text.replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/g, "");
    text = text.replace(/(^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/g, "$1");
    // eslint-disable-next-line no-control-regex
    text = text.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
    text = text.replace(/[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/g, "");
    text = text.replace(/\r\n?/g, "\n").trim();
    if (limit && text.length > limit) text = text.slice(0, limit);
    return text;
  }

  // ---------------------------------------------------------------------
  // Geometry -- normalised to the artifact's natural size, never the viewport
  // ---------------------------------------------------------------------

  function quantize(n) {
    var v = Number(n);
    if (!isFinite(v)) return 0;
    if (v < 0) v = 0;
    if (v > 1) v = 1;
    return Number(v.toFixed(GEOMETRY_DECIMALS));
  }

  /*
   * Unclamped sibling of quantize, for numbers outside the 0..1 range such as a
   * video playback offset. Same grid, same reason: a number off the grid has no
   * reproducible canonical form, and an unreproducible digest is worse than none.
   */
  function quantizeNumber(n) {
    var v = Number(n);
    if (!isFinite(v)) return 0;
    return Number(v.toFixed(GEOMETRY_DECIMALS));
  }

  /*
   * The single most important helper here. An annotation stored in rendered
   * pixels means something different at another window width, on another
   * display, or after the GUI is replaced -- which is exactly what is about to
   * happen. Callers pass the natural size they were given in the manifest.
   */
  function normalizePoint(x, y, naturalWidth, naturalHeight) {
    var w = Number(naturalWidth) || 0;
    var h = Number(naturalHeight) || 0;
    if (w <= 0 || h <= 0) {
      throw new RangeError(
        "natural width and height are required to normalise a point; " +
          "without them the annotation would mean whatever the viewport happened to be"
      );
    }
    return [quantize(Number(x) / w), quantize(Number(y) / h)];
  }

  function normalizeRect(rect, naturalWidth, naturalHeight) {
    var a = normalizePoint(rect.x, rect.y, naturalWidth, naturalHeight);
    var b = normalizePoint(
      rect.x + rect.width,
      rect.y + rect.height,
      naturalWidth,
      naturalHeight
    );
    return [a, b];
  }

  // ---------------------------------------------------------------------
  // Session
  // ---------------------------------------------------------------------

  function createSession(targets, options) {
    if (!targets || targets.schemaVersion !== TARGETS_VERSION) {
      throw new TypeError(
        "expected a " + TARGETS_VERSION + " document; got " +
          (targets && targets.schemaVersion ? targets.schemaVersion : typeof targets)
      );
    }
    var opts = options || {};
    var submission = targets.submission || {};
    var limits = submission.limits || {};
    var enums = submission.enums || {};

    var artifactsBySha = {};
    (targets.artifacts || []).forEach(function (a) {
      if (a && a.sha256) artifactsBySha[a.sha256] = a;
    });

    var state = {
      verdict: (enums.verdict || ["revise"]).indexOf("revise") >= 0 ? "revise" : (enums.verdict || [])[0],
      route: (enums.route || ["outer"]).indexOf("outer") >= 0 ? "outer" : (enums.route || [])[0],
      summary: "",
      submittedBy: "",
      annotations: [],
      critiques: [],
      diffDecisions: []
    };

    var listeners = [];

    function notify(change) {
      listeners.slice().forEach(function (fn) {
        try {
          fn(snapshot(), change);
        } catch (err) {
          /* A broken listener must not stop the others or lose the edit. */
          if (typeof console !== "undefined") console.error(err);
        }
      });
    }

    function snapshot() {
      return JSON.parse(JSON.stringify(state));
    }

    function cap(collection, limitKey) {
      var max = limits[limitKey];
      if (max && state[collection].length >= max) {
        throw new RangeError(
          "at the " + max + "-item limit for " + collection +
            "; ingestion would refuse the whole pack, so the item is refused here " +
            "where you can still see why"
        );
      }
    }

    function requireEnum(name, value) {
      var allowed = enums[name];
      if (allowed && allowed.indexOf(value) < 0) {
        throw new RangeError(
          value + " is not a valid " + name + "; expected one of " + allowed.join(", ")
        );
      }
      return value;
    }

    // -- annotations ----------------------------------------------------

    function addAnnotation(input) {
      cap("annotations", "annotations");
      var art = input.artifactSha256 ? artifactsBySha[input.artifactSha256] : null;
      if (!art) {
        /*
         * Citation-or-discard, enforced at the point of authorship. Ingestion
         * drops an annotation naming an unknown hash; discovering that after
         * submission means the reviewer's comment is gone with no recourse.
         */
        throw new RangeError(
          "artifactSha256 " + input.artifactSha256 + " is not in this run's evidence; " +
            "ingestion would discard this annotation"
        );
      }
      var comment = cleanText(input.comment, limits.commentChars || 4000);
      if (!comment) throw new RangeError("an annotation without a comment says nothing");

      var shape = requireEnum("shape", input.shape || "whole");
      var record = {
        id: input.id || newId("ann"),
        artifactSha256: art.sha256,
        artifactPath: art.path,
        artifactKind: art.kind,
        shape: shape,
        comment: comment
      };
      if (input.severity) record.severity = requireEnum("severity", input.severity);
      if (input.timestampMs !== undefined && input.timestampMs !== null) {
        record.timestampMs = quantizeNumber(input.timestampMs);
      }
      if (input.requirementIds && input.requirementIds.length) {
        record.requirementIds = input.requirementIds
          .slice(0, limits.requirementIdsPerAnnotation || 40)
          .map(String);
      }
      if (shape !== "whole") {
        var points = (input.points || []).map(function (p) {
          return [quantize(p[0]), quantize(p[1])];
        });
        if (!points.length) {
          throw new RangeError(
            "shape '" + shape + "' needs at least one normalised point; use shape " +
              "'whole' to comment on the artifact as a whole"
          );
        }
        var maxPoints = limits.geometryPoints || 400;
        if (points.length > maxPoints) {
          // Decimate rather than truncate: a freehand stroke cut off halfway is
          // a different mark, while a thinned one is still the mark drawn.
          points = decimate(points, maxPoints);
        }
        record.geometry = { points: points };
      }
      state.annotations.push(record);
      notify({ type: "annotation:add", id: record.id });
      return record;
    }

    function decimate(points, max) {
      var out = [];
      var step = (points.length - 1) / (max - 1);
      for (var i = 0; i < max; i += 1) out.push(points[Math.round(i * step)]);
      return out;
    }

    // -- critiques ------------------------------------------------------

    function addCritique(input) {
      cap("critiques", "critiques");
      var comment = cleanText(input.comment, limits.critiqueCommentChars || 4000);
      if (!comment) throw new RangeError("a critique without a comment says nothing");
      var record = {
        id: input.id || newId("crit"),
        targetKind: requireEnum("critiqueTargetKind", input.targetKind),
        targetRef: String(input.targetRef || "").slice(0, limits.targetRefChars || 512),
        stance: requireEnum("critiqueStance", input.stance),
        comment: comment
      };
      if (!record.targetRef) {
        throw new RangeError("a critique needs a targetRef or nobody can find what it is about");
      }
      if (input.targetTitle) record.targetTitle = cleanText(input.targetTitle, 512);
      if (input.severity) record.severity = requireEnum("severity", input.severity);
      /*
       * Carried from the manifest, never recomputed by the GUI: the point of the
       * digest is to pin the exact text the human read, and a GUI that hashes
       * its own re-rendered copy would pin its own formatting instead.
       */
      if (input.sourceDigest) record.sourceDigest = String(input.sourceDigest);
      state.critiques.push(record);
      notify({ type: "critique:add", id: record.id });
      return record;
    }

    function critiqueFor(reasoningId, stance, comment, extra) {
      var target = (targets.reasoning || []).filter(function (r) {
        return r.id === reasoningId;
      })[0];
      if (!target) throw new RangeError("no reasoning target with id " + reasoningId);
      var input = {
        targetKind: target.targetKind,
        targetRef: target.targetRef,
        targetTitle: target.targetTitle,
        sourceDigest: target.sourceDigest,
        stance: stance,
        comment: comment
      };
      Object.keys(extra || {}).forEach(function (k) {
        input[k] = extra[k];
      });
      return addCritique(input);
    }

    // -- diff decisions --------------------------------------------------

    function decide(input) {
      var existing = state.diffDecisions.filter(function (d) {
        return d.targetKind === input.targetKind && d.targetId === input.targetId;
      })[0];
      if (!existing) cap("diffDecisions", "diffDecisions");

      var record = existing || {
        id: input.id || newId("dec"),
        targetKind: requireEnum("diffTargetKind", input.targetKind),
        targetId: String(input.targetId || "")
      };
      if (!record.targetId) throw new RangeError("a diff decision needs a targetId");
      record.decision = requireEnum("diffDecision", input.decision);
      var comment = cleanText(input.comment, limits.diffCommentChars || 4000);
      if (comment) record.comment = comment;
      else delete record.comment;
      if (input.annotationIds && input.annotationIds.length) {
        record.annotationIds = input.annotationIds
          .slice(0, limits.annotationIdsPerDecision || 40)
          .map(String);
      }
      if (!existing) state.diffDecisions.push(record);
      notify({ type: "diff:decide", id: record.id });
      return record;
    }

    function diffRows() {
      var diff = targets.diff;
      if (!diff) return [];
      return []
        .concat(diff.measurements || [])
        .concat(diff.coverage || [])
        .concat(diff.screenshots || []);
    }

    function undecidedRows() {
      var decided = {};
      state.diffDecisions.forEach(function (d) {
        decided[d.targetKind + "\u0000" + d.targetId] = true;
      });
      return diffRows().filter(function (row) {
        // "unchanged" is not a decision anyone owes; only real deltas are.
        return (
          row.change !== "unchanged" &&
          !decided[row.targetKind + "\u0000" + row.targetId]
        );
      });
    }

    function remove(collection, id) {
      var before = state[collection].length;
      state[collection] = state[collection].filter(function (item) {
        return item.id !== id;
      });
      if (state[collection].length !== before) notify({ type: collection + ":remove", id: id });
      return before !== state[collection].length;
    }

    function setVerdict(value) {
      state.verdict = requireEnum("verdict", value);
      notify({ type: "verdict", value: value });
    }

    function setRoute(value) {
      state.route = requireEnum("route", value);
      notify({ type: "route", value: value });
    }

    function setSummary(value) {
      state.summary = cleanText(value, limits.summaryChars || 4000);
      notify({ type: "summary" });
    }

    function setSubmittedBy(value) {
      state.submittedBy = cleanText(value, limits.submittedByChars || 128);
      notify({ type: "submittedBy" });
    }

    // -- verdict consistency, mirroring adlc.stages.feedback -------------

    function blockingConflicts() {
      if (state.verdict !== "accept") return [];
      var ids = [];
      ["annotations", "critiques"].forEach(function (c) {
        state[c].forEach(function (item) {
          if (item.severity === "blocker") ids.push(String(item.id));
        });
      });
      state.diffDecisions.forEach(function (d) {
        if (d.decision === "reject") ids.push(String(d.id));
      });
      return ids.sort();
    }

    function isEmpty() {
      return (
        !state.annotations.length &&
        !state.critiques.length &&
        !state.diffDecisions.length &&
        !state.summary
      );
    }

    // -- pack ------------------------------------------------------------

    function buildPack(extra) {
      var conflicts = blockingConflicts();
      if (conflicts.length) {
        throw new RangeError(
          "verdict 'accept' contradicts " + conflicts.length +
            " blocking item(s): " + conflicts.join(", ") +
            "; ingestion refuses this rather than quietly downgrading your verdict"
        );
      }
      var pack = {
        schemaVersion: PACK_VERSION,
        runId: targets.run.runId,
        candidateSha: targets.run.candidateSha,
        submittedAt: (extra && extra.submittedAt) || new Date().toISOString(),
        verdict: state.verdict,
        route: state.route
      };
      if (targets.run.reportDigest) pack.reportDigest = targets.run.reportDigest;
      if (state.submittedBy) pack.submittedBy = state.submittedBy;
      if (state.summary) pack.summary = state.summary;
      if (state.annotations.length) pack.annotations = state.annotations.map(clone);
      if (state.critiques.length) pack.critiques = state.critiques.map(clone);
      if (state.diffDecisions.length) pack.diffDecisions = state.diffDecisions.map(clone);
      return pack;
    }

    function clone(o) {
      return JSON.parse(JSON.stringify(o));
    }

    function buildSignedPack(extra) {
      var pack = buildPack(extra);
      return Promise.resolve(packDigest(pack)).then(function (digest) {
        // Omitted rather than faked when SubtleCrypto is unavailable; the field
        // is optional precisely so a file:// page can still submit honestly.
        if (digest) pack.packDigest = digest;
        return pack;
      });
    }

    function toText(extra) {
      return buildSignedPack(extra).then(function (pack) {
        return JSON.stringify(pack, null, 2) + "\n";
      });
    }

    function toBlob(extra) {
      return toText(extra).then(function (text) {
        if (typeof Blob === "undefined") return text;
        return new Blob([text], { type: "application/json" });
      });
    }

    function suggestedFilename() {
      return "feedback-" + targets.run.runId + ".json";
    }

    /*
     * Only usable when the GUI is served from the loopback server itself. A
     * custom nonce header from any other origin triggers a CORS preflight, and
     * the server answers OPTIONS with 405 on purpose -- if a page you did not
     * open could POST here, the nonce would be the only thing between your run
     * directory and the whole internet. Download/copy is the universal path.
     */
    function submit(extra) {
      if (!submission.endpoint) {
        return Promise.reject(
          new Error(
            "no submission endpoint in this manifest; export the pack and run " +
              "`adlc feedback apply --pack <file>` instead"
          )
        );
      }
      return toText(extra).then(function (body) {
        var max = submission.maxBodyBytes;
        if (max && utf8Bytes(body).length > max) {
          throw new RangeError(
            "pack is larger than the server's " + max + "-byte limit; " +
              "export it to a file and apply it from the CLI"
          );
        }
        var headers = { "Content-Type": "application/json" };
        if (submission.nonceHeader && submission.nonce) {
          headers[submission.nonceHeader] = submission.nonce;
        }
        return fetch(submission.endpoint, {
          method: "POST",
          headers: headers,
          body: body
        }).then(function (res) {
          return res.text().then(function (text) {
            var parsed = null;
            try {
              parsed = JSON.parse(text);
            } catch (err) {
              parsed = { message: text };
            }
            if (!res.ok) {
              var err2 = new Error(
                (parsed && parsed.message) || "submission failed with " + res.status
              );
              err2.status = res.status;
              err2.body = parsed;
              throw err2;
            }
            return parsed;
          });
        });
      });
    }

    // -- persistence ------------------------------------------------------

    /*
     * Keyed by run *and* report digest. Restoring notes taken against a
     * different rendering of a different run is worse than losing them: the
     * reviewer cannot tell that is what happened.
     */
    function storageKey() {
      return "adlc-feedback:" + targets.run.runId + ":" + (targets.run.reportDigest || "");
    }

    function save(storage) {
      var store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
      if (!store) return false;
      try {
        store.setItem(storageKey(), JSON.stringify(state));
        return true;
      } catch (err) {
        return false;
      }
    }

    function restore(storage) {
      var store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
      if (!store) return false;
      var raw = null;
      try {
        raw = store.getItem(storageKey());
      } catch (err) {
        return false;
      }
      if (!raw) return false;
      try {
        var loaded = JSON.parse(raw);
        Object.keys(state).forEach(function (k) {
          if (loaded[k] !== undefined) state[k] = loaded[k];
        });
        notify({ type: "restore" });
        return true;
      } catch (err) {
        return false;
      }
    }

    var session = {
      targets: targets,
      limits: limits,
      enums: enums,
      artifacts: targets.artifacts || [],
      reasoning: targets.reasoning || [],
      requirements: targets.requirements || [],
      diff: targets.diff || null,
      diffRows: diffRows,
      undecidedRows: undecidedRows,
      artifactBySha: function (sha) {
        return artifactsBySha[sha] || null;
      },
      state: snapshot,
      subscribe: function (fn) {
        listeners.push(fn);
        return function () {
          listeners = listeners.filter(function (x) {
            return x !== fn;
          });
        };
      },
      addAnnotation: addAnnotation,
      addCritique: addCritique,
      critiqueFor: critiqueFor,
      decide: decide,
      removeAnnotation: function (id) {
        return remove("annotations", id);
      },
      removeCritique: function (id) {
        return remove("critiques", id);
      },
      removeDecision: function (id) {
        return remove("diffDecisions", id);
      },
      setVerdict: setVerdict,
      setRoute: setRoute,
      setSummary: setSummary,
      setSubmittedBy: setSubmittedBy,
      blockingConflicts: blockingConflicts,
      isEmpty: isEmpty,
      buildPack: buildPack,
      buildSignedPack: buildSignedPack,
      toText: toText,
      toBlob: toBlob,
      suggestedFilename: suggestedFilename,
      submit: submit,
      canSubmit: function () {
        return Boolean(submission.endpoint);
      },
      save: save,
      restore: restore,
      storageKey: storageKey
    };

    if (opts.autoRestore) session.restore();
    return session;
  }

  return {
    version: "1.0.0",
    PACK_VERSION: PACK_VERSION,
    TARGETS_VERSION: TARGETS_VERSION,
    GEOMETRY_DECIMALS: GEOMETRY_DECIMALS,
    createSession: createSession,
    canonicalize: canonicalize,
    packDigest: packDigest,
    normalizePoint: normalizePoint,
    normalizeRect: normalizeRect,
    quantize: quantize,
    quantizeNumber: quantizeNumber,
    cleanText: cleanText,
    newId: newId
  };
});
