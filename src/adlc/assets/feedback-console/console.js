/*
 * adlc feedback console -- reference consumer #2.
 *
 * This file shares ZERO code with report.html. It talks to exactly two things:
 * an adlc-feedback-targets/v1 document and the adlc-feedback SDK. If this
 * console works, the contract is GUI-agnostic. If it were quietly reaching into
 * the report package, it would not be evidence of anything.
 *
 * No framework, no build step, no network access at load time.
 */
(function () {
  "use strict";

  var SDK = globalThis.AdlcFeedbackSDK;
  var $ = function (sel, root) {
    return (root || document).querySelector(sel);
  };

  /* Nodes are built, never parsed. There is no innerHTML anywhere in this file,
   * so no value from the manifest can become markup. */
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (kid) {
      node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
    return node;
  }

  function svgEl(tag) {
    return document.createElementNS("http://www.w3.org/2000/svg", tag);
  }

  var targets = JSON.parse($("#adlc-targets").textContent);
  var session = SDK.createSession(targets);
  var live = null;
  var announced = "";

  function announce(message) {
    /* Re-announcing identical text needs a nudge, or a screen reader stays
     * silent on a repeated failure -- exactly when the user needs to hear it. */
    live.textContent = message === announced ? message + " " : message;
    announced = live.textContent;
  }

  function say(err) {
    announce(err && err.message ? err.message : String(err));
  }

  // -- shared controls ------------------------------------------------------

  function requirementPicker(slug) {
    var box = el("fieldset", { class: "reqs" }, [el("legend", { text: "Requirements" })]);
    (targets.requirements || []).forEach(function (req, i) {
      var id = slug + "-req-" + i;
      box.appendChild(
        el("label", { for: id, class: "chip" }, [
          el("input", { type: "checkbox", id: id, value: req.id }),
          " " + req.id + (req.title ? " \u2014 " + req.title : "")
        ])
      );
    });
    if (!(targets.requirements || []).length) {
      box.appendChild(el("p", { class: "muted", text: "This run declares no requirements." }));
    }
    return box;
  }

  function pickedRequirements(form) {
    return Array.prototype.slice
      .call(form.querySelectorAll(".reqs input:checked"))
      .map(function (i) {
        return i.value;
      });
  }

  function enumSelect(name, label) {
    var sel = el("select", { name: name, "aria-label": label });
    (session.enums[name] || []).forEach(function (value) {
      sel.appendChild(el("option", { value: value, text: value }));
    });
    return sel;
  }

  // -- artifacts + annotation ----------------------------------------------

  function renderArtifact(artifact, index) {
    var slug = "art-" + index;
    var card = el("section", { class: "card", "aria-labelledby": slug + "-h" });
    card.appendChild(el("h3", { id: slug + "-h", text: artifact.path }));
    card.appendChild(
      el("p", {
        class: "muted",
        text: artifact.kind + " \u00b7 " + artifact.bytes + " bytes \u00b7 " + artifact.sha256.slice(0, 12)
      })
    );

    var overlay = null;
    if (artifact.inline) {
      var frame = el("div", { class: "frame" });
      var img = el("img", { src: artifact.inline, alt: "Evidence artifact " + artifact.path });
      overlay = svgEl("svg");
      overlay.setAttribute("viewBox", "0 0 1000 1000");
      overlay.setAttribute("preserveAspectRatio", "none");
      overlay.setAttribute("class", "overlay");
      overlay.setAttribute("role", "presentation");
      frame.appendChild(img);
      frame.appendChild(overlay);
      card.appendChild(frame);
    } else {
      card.appendChild(
        el("p", {
          class: "notice",
          text:
            artifact.inlineOmittedReason ||
            "Not rendered inline. It can still be annotated as a whole."
        })
      );
    }

    var form = el("form", { class: "annotate", novalidate: "" });
    var shape = enumSelect("shape", "Shape");
    shape.value = artifact.inline ? "rect" : "whole";
    var sev = enumSelect("severity", "Severity");

    var geom = el("div", { class: "geom" });
    [["left", "Left %"], ["top", "Top %"], ["width", "Width %"], ["height", "Height %"]].forEach(
      function (pair) {
        geom.appendChild(
          el("label", {}, [
            pair[1],
            el("input", {
              type: "number",
              name: pair[0],
              min: "0",
              max: "100",
              step: "0.1",
              value: pair[0] === "width" || pair[0] === "height" ? "25" : "10"
            })
          ])
        );
      }
    );

    var comment = el("textarea", {
      name: "comment",
      rows: "3",
      maxlength: String(session.limits.commentChars || 4000),
      "aria-label": "Comment",
      placeholder: "What is wrong here?"
    });

    form.appendChild(el("label", {}, ["Shape ", shape]));
    form.appendChild(geom);
    form.appendChild(el("label", {}, ["Severity ", sev]));
    form.appendChild(el("label", {}, ["Comment ", comment]));
    form.appendChild(requirementPicker(slug));
    form.appendChild(el("button", { type: "submit", text: "Add annotation" }));

    function syncGeometry() {
      var whole = shape.value === "whole";
      geom.hidden = whole;
      Array.prototype.slice.call(geom.querySelectorAll("input")).forEach(function (i) {
        i.disabled = whole;
      });
    }
    shape.addEventListener("change", syncGeometry);
    syncGeometry();

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var frac = function (n) {
        return parseFloat(form.elements[n].value || "0") / 100;
      };
      var input = {
        artifactSha256: artifact.sha256,
        shape: shape.value,
        severity: sev.value,
        comment: comment.value,
        requirementIds: pickedRequirements(form)
      };
      if (shape.value !== "whole") {
        var l = frac("left");
        var t = frac("top");
        input.points =
          shape.value === "point"
            ? [[l, t]]
            : [[l, t], [l + frac("width"), t + frac("height")]];
      }
      try {
        var record = session.addAnnotation(input);
        comment.value = "";
        announce("Annotation " + record.id + " added on " + artifact.path + ".");
        shape.focus();
      } catch (err) {
        say(err);
      }
    });
    card.appendChild(form);

    if (overlay) wireOverlay(card, overlay, artifact);
    return card;
  }

  /*
   * Pointer drag fills the same form the keyboard uses; it never commits on its
   * own. That is deliberate: a pointer must not be able to author an annotation
   * the keyboard path could not have authored.
   */
  function wireOverlay(card, overlay, artifact) {
    var frame = $(".frame", card);
    var img = $("img", frame);
    var form = $("form.annotate", card);
    var start = null;

    function fractions(ev) {
      var rect = img.getBoundingClientRect();
      return SDK.normalizePoint(
        ev.clientX - rect.left,
        ev.clientY - rect.top,
        rect.width,
        rect.height
      );
    }

    frame.addEventListener("pointerdown", function (ev) {
      start = fractions(ev);
      try {
        frame.setPointerCapture(ev.pointerId);
      } catch (err) {
        /* capture is a nicety; the drag still works without it */
      }
    });
    frame.addEventListener("pointerup", function (ev) {
      if (!start) return;
      var end = fractions(ev);
      form.elements.shape.value = "rect";
      form.elements.shape.dispatchEvent(new Event("change"));
      form.elements.left.value = (Math.min(start[0], end[0]) * 100).toFixed(1);
      form.elements.top.value = (Math.min(start[1], end[1]) * 100).toFixed(1);
      form.elements.width.value = (Math.abs(end[0] - start[0]) * 100).toFixed(1);
      form.elements.height.value = (Math.abs(end[1] - start[1]) * 100).toFixed(1);
      start = null;
      announce("Region selected. Add a comment, then choose Add annotation.");
      form.elements.comment.focus();
    });

    session.subscribe(function (state) {
      while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
      state.annotations
        .filter(function (a) {
          return a.artifactSha256 === artifact.sha256 && a.geometry;
        })
        .forEach(function (a) {
          var pts = a.geometry.points;
          var mark;
          if (pts.length === 1) {
            mark = svgEl("circle");
            mark.setAttribute("cx", pts[0][0] * 1000);
            mark.setAttribute("cy", pts[0][1] * 1000);
            mark.setAttribute("r", "14");
          } else {
            mark = svgEl("rect");
            mark.setAttribute("x", Math.min(pts[0][0], pts[1][0]) * 1000);
            mark.setAttribute("y", Math.min(pts[0][1], pts[1][1]) * 1000);
            mark.setAttribute("width", Math.abs(pts[1][0] - pts[0][0]) * 1000);
            mark.setAttribute("height", Math.abs(pts[1][1] - pts[0][1]) * 1000);
          }
          /* Severity is carried by dash pattern as well as colour, so the marks
           * remain distinguishable without colour vision. */
          mark.setAttribute("class", "mark sev-" + a.severity);
          overlay.appendChild(mark);
        });
    });
  }

  // -- reasoning + critique -------------------------------------------------

  function renderReasoning(target, index) {
    var slug = "reason-" + index;
    var card = el("section", { class: "card", "aria-labelledby": slug + "-h" });
    card.appendChild(el("h3", { id: slug + "-h", text: target.targetTitle || target.targetRef }));
    card.appendChild(
      el("p", {
        class: "muted",
        text: [target.targetKind, target.author, target.severity]
          .filter(Boolean)
          .join(" \u00b7 ")
      })
    );
    card.appendChild(el("pre", { class: "reasoning", text: target.text }));

    var form = el("form", { class: "critique", novalidate: "" });
    var stances = el("fieldset", {}, [
      el("legend", { text: "Stance \u2014 " + (target.targetTitle || target.targetRef) })
    ]);
    (session.enums.critiqueStance || []).forEach(function (stance, i) {
      var id = slug + "-stance-" + i;
      var input = el("input", { type: "radio", name: slug + "-stance", id: id, value: stance });
      if (i === 0) input.checked = true;
      stances.appendChild(el("label", { for: id, class: "chip" }, [input, " " + stance]));
    });
    var comment = el("textarea", {
      name: "comment",
      rows: "3",
      maxlength: String(session.limits.commentChars || 4000),
      "aria-label": "Critique of " + (target.targetTitle || target.targetRef),
      placeholder: "Why is this reasoning wrong, unsupported, or out of scope?"
    });
    form.appendChild(stances);
    form.appendChild(el("label", {}, ["Critique ", comment]));
    form.appendChild(requirementPicker(slug));
    form.appendChild(el("button", { type: "submit", text: "Add critique" }));

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var chosen = form.querySelector("input[type=radio]:checked");
      try {
        var record = session.critiqueFor(target.id, chosen.value, comment.value, {
          requirementIds: pickedRequirements(form)
        });
        comment.value = "";
        announce("Critique " + record.id + " recorded against " + target.targetRef + ".");
      } catch (err) {
        say(err);
      }
    });
    card.appendChild(form);
    return card;
  }

  // -- evidence diff --------------------------------------------------------

  function renderDiffRow(row, index) {
    var slug = "diff-" + index;
    var card = el("section", {
      class: "card diff-row" + (row.regression ? " regression" : ""),
      "aria-labelledby": slug + "-h"
    });
    card.appendChild(el("h3", { id: slug + "-h", text: row.targetId }));

    var facts = [row.targetKind, row.change];
    if (row.baselineValue !== undefined && row.baselineValue !== null) {
      facts.push("was " + row.baselineValue);
    }
    if (row.candidateValue !== undefined && row.candidateValue !== null) {
      facts.push("now " + row.candidateValue);
    }
    if (row.budgetCrossed) facts.push(row.budgetCrossed);
    /* Spelled out rather than shown only as a colour. */
    if (row.regression) facts.push("REGRESSION");
    card.appendChild(el("p", { class: "muted", text: facts.join(" \u00b7 ") }));

    if (row.candidateInline || row.baselineInline) {
      var pair = el("div", { class: "pair" });
      if (row.baselineInline) {
        pair.appendChild(
          el("figure", {}, [
            el("img", { src: row.baselineInline, alt: "Baseline rendering of " + row.targetId }),
            el("figcaption", { text: "baseline" })
          ])
        );
      }
      if (row.candidateInline) {
        pair.appendChild(
          el("figure", {}, [
            el("img", { src: row.candidateInline, alt: "Candidate rendering of " + row.targetId }),
            el("figcaption", { text: "candidate" })
          ])
        );
      }
      card.appendChild(pair);
    }

    var form = el("form", { class: "decide", novalidate: "" });
    var note = el("input", {
      type: "text",
      name: "note",
      maxlength: String(session.limits.commentChars || 4000),
      "aria-label": "Note about " + row.targetId,
      placeholder: "Optional note"
    });
    var state = el("span", { class: "decision", "aria-live": "off", text: "undecided" });
    form.appendChild(note);
    (session.enums.diffDecision || []).forEach(function (decision) {
      var button = el("button", { type: "button", text: decision });
      button.addEventListener("click", function () {
        try {
          session.decide({
            targetKind: row.targetKind,
            targetId: row.targetId,
            decision: decision,
            comment: note.value
          });
          state.textContent = decision;
          card.setAttribute("data-decision", decision);
          announce(row.targetId + " marked " + decision + ".");
        } catch (err) {
          say(err);
        }
      });
      form.appendChild(button);
    });
    form.appendChild(state);
    card.appendChild(form);
    return card;
  }

  // -- submission -----------------------------------------------------------

  function refreshCounts() {
    var state = session.state();
    $("#count-annotations").textContent = state.annotations.length;
    $("#count-critiques").textContent = state.critiques.length;
    $("#count-decisions").textContent = state.diffDecisions.length;
    $("#count-undecided").textContent = session.undecidedRows().length;

    var conflicts = session.blockingConflicts();
    var warn = $("#conflicts");
    warn.hidden = conflicts.length === 0;
    warn.textContent = conflicts.length
      ? "Verdict '" +
        state.verdict +
        "' contradicts " +
        conflicts.length +
        " blocking item(s): " +
        conflicts.join(", ")
      : "";
  }

  function mount() {
    live = $("#status");

    $("#run-line").textContent =
      targets.run.runId +
      " \u00b7 " +
      String(targets.run.candidateSha).slice(0, 12) +
      (targets.run.referencesRun ? " \u00b7 vs " + targets.run.referencesRun : " \u00b7 no baseline");

    var artifacts = $("#artifacts");
    (targets.artifacts || []).forEach(function (a, i) {
      artifacts.appendChild(renderArtifact(a, i));
    });
    if (!(targets.artifacts || []).length) {
      artifacts.appendChild(el("p", { class: "muted", text: "This run produced no artifacts." }));
    }

    var reasoning = $("#reasoning");
    (targets.reasoning || []).forEach(function (r, i) {
      reasoning.appendChild(renderReasoning(r, i));
    });
    if (!(targets.reasoning || []).length) {
      reasoning.appendChild(
        el("p", { class: "muted", text: "No agent-authored reasoning in this run." })
      );
    }

    var diff = $("#diff");
    var rows = session.diffRows();
    rows.forEach(function (row, i) {
      diff.appendChild(renderDiffRow(row, i));
    });
    if (!rows.length) {
      diff.appendChild(
        el("p", {
          class: "muted",
          text: targets.run.referencesRun
            ? "Nothing changed against the baseline run."
            : "This run has no baseline, so there is nothing to diff."
        })
      );
    }

    var verdict = $("#verdict");
    (session.enums.verdict || []).forEach(function (v) {
      verdict.appendChild(el("option", { value: v, text: v }));
    });
    verdict.value = session.state().verdict;
    verdict.addEventListener("change", function () {
      session.setVerdict(verdict.value);
    });

    var route = $("#route");
    (session.enums.route || []).forEach(function (r) {
      route.appendChild(el("option", { value: r, text: r }));
    });
    route.value = session.state().route;
    route.addEventListener("change", function () {
      session.setRoute(route.value);
    });

    $("#summary").addEventListener("input", function (ev) {
      session.setSummary(ev.target.value);
    });
    $("#submitted-by").addEventListener("input", function (ev) {
      session.setSubmittedBy(ev.target.value);
    });

    $("#download").addEventListener("click", function () {
      session
        .toBlob()
        .then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = el("a", { href: url, download: session.suggestedFilename() });
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () {
            URL.revokeObjectURL(url);
          }, 0);
          announce("Pack downloaded. Apply it with: adlc feedback apply --pack <file>");
        })
        .catch(say);
    });

    /* One clipboard path for both copy buttons: a fallback that only some of
     * them honoured would be a fallback nobody could rely on. */
    function copyText(text, okMsg, fallbackMsg) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(function () {
          announce(okMsg);
        });
      }
      /* No silent failure: hand the reviewer the bytes to copy manually. */
      var out = $("#fallback");
      out.value = text;
      out.hidden = false;
      out.focus();
      out.select();
      announce(fallbackMsg);
      return Promise.resolve();
    }

    $("#copy").addEventListener("click", function () {
      session
        .toText()
        .then(function (text) {
          return copyText(
            text,
            "Pack copied to the clipboard.",
            "Clipboard unavailable. The pack is in the text box below; copy it manually."
          );
        })
        .catch(say);
    });

    /* The pack fenced for a native PR review. That transport is the only one
     * that carries authority in CI -- a downloaded file proves nothing about
     * who you are, but a review does. */
    $("#copy-review").addEventListener("click", function () {
      session
        .toReviewBody()
        .then(function (body) {
          return copyText(
            body,
            "Review body copied. Paste it into a PR review on the candidate commit.",
            "Clipboard unavailable. The review body is in the text box below; copy it manually."
          );
        })
        .catch(say);
    });

    var submitButton = $("#submit");
    if (!targets.submission.endpoint) {
      submitButton.disabled = true;
      $("#submit-note").textContent =
        "This manifest carries no endpoint, which is the normal case for a file:// " +
        "page. Download or copy the pack and run `adlc feedback apply --pack <file>`.";
    } else {
      $("#submit-note").textContent = "Submitting posts to " + targets.submission.endpoint + ".";
      submitButton.addEventListener("click", function () {
        submitButton.disabled = true;
        announce("Submitting\u2026");
        session
          .submit()
          .then(function (res) {
            announce(
              "Accepted. Successor run " +
                (res.successorRun || res.runId || "created") +
                ". The outer loop has been retriggered."
            );
          })
          .catch(function (err) {
            submitButton.disabled = false;
            say(err);
          });
      });
    }

    $("#save").addEventListener("click", function () {
      announce(session.save() ? "Draft saved in this browser." : "This browser refused storage.");
    });
    $("#restore").addEventListener("click", function () {
      if (session.restore()) {
        announce("Draft restored. Reload the page to redraw saved marks.");
      } else {
        announce("No draft found for this run and this report.");
      }
    });

    session.subscribe(refreshCounts);
    refreshCounts();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
