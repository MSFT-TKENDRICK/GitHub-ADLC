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
          /* The manifest emits requirement prose under `text`; there is no
           * `title` (the schema is additionalProperties:false), so reading one
           * showed the bare id and silently dropped the words that make it
           * legible -- "US1-AC1" instead of "US1-AC1 -- A theme toggle exists." */
          " " + req.id + (req.text ? " \u2014 " + req.text : "")
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

  /* Geometry as words. An annotation's position is meaningful data -- it is the
   * difference between "the logo is wrong" and "the logo is wrong HERE -- and a
   * reviewer who cannot see the SVG still has to be able to read it back, check
   * it, and correct it. Percentages, because that is what the form takes, so
   * what you hear is what you can retype. */
  function describeGeometry(annotation) {
    var geometry = annotation.geometry;
    if (!geometry || !geometry.points || !geometry.points.length) {
      return "whole artifact";
    }
    var pct = function (point) {
      return Math.round(point[0] * 100) + "%, " + Math.round(point[1] * 100) + "%";
    };
    var points = geometry.points;
    if (points.length === 1) return "point at " + pct(points[0]);
    if (points.length === 2) return "region from " + pct(points[0]) + " to " + pct(points[1]);
    /* Never read out hundreds of coordinate pairs; summarise the extent. */
    var xs = points.map(function (p) { return p[0]; });
    var ys = points.map(function (p) { return p[1]; });
    return (
      geometry.shape +
      ", " +
      points.length +
      " points, spanning " +
      pct([Math.min.apply(null, xs), Math.min.apply(null, ys)]) +
      " to " +
      pct([Math.max.apply(null, xs), Math.max.apply(null, ys)])
    );
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

    /* The annotation list. Without it this console can only CREATE annotations:
     * the marks exist solely as SVG inside a role="presentation" overlay, so a
     * reviewer who is not looking at the picture cannot list what they have
     * annotated, read back where it landed, correct a mistake, or delete one.
     * A mis-placed mark would be permanent in the pack, with reloading and
     * losing everything as the only escape. */
    var listHeadingId = slug + "-list-h";
    var list = el("ul", { class: "annotations", "aria-labelledby": listHeadingId });
    var listHeading = el("h4", { id: listHeadingId, text: "Annotations on this artifact" });
    var empty = el("p", { class: "muted", text: "No annotations on this artifact yet." });
    card.appendChild(listHeading);
    card.appendChild(empty);
    card.appendChild(list);

    function loadForEditing(annotation) {
      shape.value = annotation.shape;
      syncGeometry();
      sev.value = annotation.severity;
      comment.value = annotation.comment || "";
      var points = (annotation.geometry || {}).points;
      if (points && points.length) {
        var xs = points.map(function (p) { return p[0]; });
        var ys = points.map(function (p) { return p[1]; });
        var left = Math.min.apply(null, xs);
        var top = Math.min.apply(null, ys);
        form.elements.left.value = (left * 100).toFixed(1);
        form.elements.top.value = (top * 100).toFixed(1);
        form.elements.width.value = ((Math.max.apply(null, xs) - left) * 100).toFixed(1);
        form.elements.height.value = ((Math.max.apply(null, ys) - top) * 100).toFixed(1);
      }
      var picked = annotation.requirementIds || [];
      Array.prototype.slice.call(form.querySelectorAll(".reqs input")).forEach(function (box) {
        box.checked = picked.indexOf(box.value) !== -1;
      });
    }

    function renderList(state) {
      while (list.firstChild) list.removeChild(list.firstChild);
      var mine = state.annotations.filter(function (a) {
        return a.artifactSha256 === artifact.sha256;
      });
      empty.hidden = mine.length > 0;
      list.hidden = mine.length === 0;

      mine.forEach(function (annotation, position) {
        var where = describeGeometry(annotation);
        /* Ordinal first: "annotation 2 of 5" is how you keep your place in a
         * list you are hearing rather than seeing. */
        var label =
          "Annotation " +
          (position + 1) +
          " of " +
          mine.length +
          " on " +
          artifact.path +
          ": " +
          annotation.severity +
          ", " +
          where;

        var item = el("li", { class: "annotation sev-" + annotation.severity });
        item.appendChild(el("p", { class: "what", text: label }));
        if (annotation.comment) {
          item.appendChild(el("p", { class: "said", text: annotation.comment }));
        }
        if ((annotation.requirementIds || []).length) {
          item.appendChild(
            el("p", {
              class: "muted",
              text: "Requirements: " + annotation.requirementIds.join(", ")
            })
          );
        }

        var editBtn = el("button", {
          type: "button",
          text: "Edit",
          "aria-label": "Edit " + label
        });
        editBtn.addEventListener("click", function () {
          loadForEditing(annotation);
          /* Editing pulls the annotation back into the form it came from, so
           * there is one authoring surface rather than two that can disagree.
           * Say plainly that it has left the pack -- a silent removal here
           * would be a data-loss trap. */
          session.removeAnnotation(annotation.id);
          announce(
            "Editing annotation " +
              (position + 1) +
              " on " +
              artifact.path +
              ". It has been removed from the pack; choose Add annotation to put it back."
          );
          comment.focus();
        });

        var delBtn = el("button", {
          type: "button",
          text: "Delete",
          "aria-label": "Delete " + label
        });
        delBtn.addEventListener("click", function () {
          session.removeAnnotation(annotation.id);
          announce("Deleted annotation " + (position + 1) + " on " + artifact.path + ".");
          /* Focus would otherwise fall to <body> when this button is removed.
           * Send it somewhere deliberate and near. */
          (list.querySelector("button") || shape).focus();
        });

        item.appendChild(editBtn);
        item.appendChild(delBtn);
        list.appendChild(item);
      });
    }

    session.subscribe(renderList);
    renderList(session.state());

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
    /* No requirement picker here, deliberately. The pack's `critique` object is
     * additionalProperties:false with no requirementIds field, and the SDK's
     * addCritique builds each record from a fixed allowlist -- so a requirementIds
     * value passed here would be dropped in silence, never reaching the pack. A
     * picker would be a data-loss trap: the reviewer ticks boxes that vanish with
     * no error. Annotations legitimately carry requirementIds; critiques do not. */
    form.appendChild(el("button", { type: "submit", text: "Add critique" }));

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var chosen = form.querySelector("input[type=radio]:checked");
      try {
        var record = session.critiqueFor(target.id, chosen.value, comment.value);
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

  /* Diff rows own their accept/reject buttons in closures, so a state change
   * that did not come from a click -- restoring a draft -- has no way to reach
   * them. Each row registers a resync here; `syncDiffRows` replays the current
   * decisions onto every row's buttons and status text. */
  var diffRowSyncers = [];

  function syncDiffRows(state) {
    diffRowSyncers.forEach(function (fn) {
      fn(state);
    });
  }

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
    if (row.value !== undefined && row.value !== null) {
      facts.push("now " + row.value);
    }
    if (row.budgetCrossed) facts.push(row.budgetCrossed);
    /* Spelled out rather than shown only as a colour. */
    if (row.regression) facts.push("REGRESSION");
    card.appendChild(el("p", { class: "muted", text: facts.join(" \u00b7 ") }));

    /* The candidate image is inlined exactly once, in `targets.artifacts`;
     * `row.inline` is null by design (see feedback_targets.py: re-encoding it
     * into the diff row would double the document). Recover it by content hash
     * -- the row's `sha256` IS the candidate's -- via the SDK's own index. Do
     * NOT match on `row.targetId`: a screenshot's targetId is variant-relative
     * (e.g. "home.png") while an artifact's path is evidence-relative (e.g.
     * "evidence/candidate-a/home.png"), so a path match silently finds nothing. */
    var candidateArtifact = row.sha256 ? session.artifactBySha(row.sha256) : null;
    var candidateInline = candidateArtifact ? candidateArtifact.inline : null;
    if (candidateInline || row.baselineInline) {
      var pair = el("div", { class: "pair" });
      if (row.baselineInline) {
        pair.appendChild(
          el("figure", {}, [
            el("img", { src: row.baselineInline, alt: "Baseline rendering of " + row.targetId }),
            el("figcaption", { text: "baseline" })
          ])
        );
      }
      if (candidateInline) {
        pair.appendChild(
          el("figure", {}, [
            el("img", { src: candidateInline, alt: "Candidate rendering of " + row.targetId }),
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
    var buttons = [];
    var buttonByDecision = {};
    (session.enums.diffDecision || []).forEach(function (decision) {
      /* Without these two attributes the buttons list reads "accept, reject,
       * accept, reject..." for every row in the diff, with nothing to say which
       * row you are on or what it is currently set to. `aria-pressed` is the
       * right role here because these are toggles over one mutually exclusive
       * choice, which is exactly how report.html models the same control. */
      var button = el("button", {
        type: "button",
        text: decision,
        "aria-pressed": "false",
        "aria-label": decision + " change to " + row.targetKind + " " + row.targetId
      });
      buttons.push(button);
      buttonByDecision[decision] = button;
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
          buttons.forEach(function (other) {
            other.setAttribute("aria-pressed", other === button ? "true" : "false");
          });
          announce(row.targetId + " marked " + decision + ".");
        } catch (err) {
          say(err);
        }
      });
      form.appendChild(button);
    });
    /* Replay an already-recorded decision onto this row -- used on restore,
     * where the decision arrives in session state with no click to fire the
     * handler above. Kept identical to that handler in what it shows: same
     * status text, same aria-pressed toggling, so a restored row is
     * indistinguishable from a freshly clicked one to a screen reader. */
    diffRowSyncers.push(function (currentState) {
      var decided = (currentState.diffDecisions || []).filter(function (d) {
        return d.targetKind === row.targetKind && d.targetId === row.targetId;
      })[0];
      var decision = decided ? decided.decision : null;
      state.textContent = decision || "undecided";
      if (decision) card.setAttribute("data-decision", decision);
      else card.removeAttribute("data-decision");
      buttons.forEach(function (other) {
        other.setAttribute("aria-pressed", other === buttonByDecision[decision] ? "true" : "false");
      });
    });
    form.appendChild(state);
    card.appendChild(form);
    return card;
  }

  // -- submission -----------------------------------------------------------

  var lastConflictText = null;

  function refreshCounts() {
    var state = session.state();
    $("#count-annotations").textContent = state.annotations.length;
    $("#count-critiques").textContent = state.critiques.length;
    $("#count-decisions").textContent = state.diffDecisions.length;
    $("#count-undecided").textContent = session.undecidedRows().length;

    var conflicts = session.blockingConflicts();
    var warn = $("#conflicts");
    var text = conflicts.length
      ? "Verdict '" +
        state.verdict +
        "' contradicts " +
        conflicts.length +
        " blocking item(s): " +
        conflicts.join(", ")
      : "";
    /* `#conflicts` is role="alert", and `refreshCounts` runs on every session
     * change -- which includes every keystroke in the Summary field, because
     * setSummary notifies. Assigning textContent unconditionally would fire an
     * assertive live-region mutation per character, interrupting the user's own
     * typing echo and making the summary impossible to compose. Only speak when
     * the message actually changes. */
    if (text !== lastConflictText) {
      warn.textContent = text;
      warn.hidden = conflicts.length === 0;
      lastConflictText = text;
    }
  }

  function mount() {
    live = $("#status");

    $("#run-line").textContent =
      targets.run.runId +
      " \u00b7 " +
      String(targets.run.candidateSha).slice(0, 12) +
      (targets.run.baselineRunId ? " \u00b7 vs " + targets.run.baselineRunId : " \u00b7 no baseline");

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
          text: targets.run.baselineRunId
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
      /* aria-disabled, not disabled. A disabled button is unfocusable, so the
       * reason it is unavailable -- which is the one thing the reviewer needs,
       * because it tells them to use the download path instead -- can never be
       * reached or announced. */
      submitButton.setAttribute("aria-disabled", "true");
      $("#submit-note").textContent =
        "This manifest carries no endpoint, which is the normal case for a file:// " +
        "page. Download or copy the pack and run `adlc feedback apply --pack <file>`.";
      submitButton.addEventListener("click", function () {
        announce($("#submit-note").textContent);
      });
    } else {
      $("#submit-note").textContent = "Submitting posts to " + targets.submission.endpoint + ".";
      var submitting = false;
      submitButton.addEventListener("click", function () {
        /* Never disable the focused element. Disabling blurs it to <body>, and
         * this page is long, so the reviewer is thrown to the top of the
         * document at the exact moment they complete their task -- with no way
         * back but Tab. `aria-busy` plus a guard flag says "working" without
         * destroying the focus point. */
        if (submitting) return;
        submitting = true;
        submitButton.setAttribute("aria-busy", "true");
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
          .catch(say)
          .then(function () {
            submitting = false;
            submitButton.removeAttribute("aria-busy");
          });
      });
    }

    $("#save").addEventListener("click", function () {
      announce(session.save() ? "Draft saved in this browser." : "This browser refused storage.");
    });
    $("#restore").addEventListener("click", function () {
      if (!session.restore()) {
        announce("No draft found for this run and this report.");
        return;
      }
      /* restore() rewrote session state and notified, so the annotation lists
       * and overlays (which subscribe) have already redrawn and the counts have
       * refreshed. What restore() cannot reach is the DOM that is seeded from
       * state only once, at mount: the verdict and route selects, the summary
       * and submitted-by fields, and each diff row's pressed state. Push state
       * into them now, BEFORE announcing. Skip this and the select still shows
       * `revise` while the pack carries `accept` -- the reviewer submits the
       * opposite of what they see -- and the summary box stays empty, so the
       * next keystroke overwrites the restored prose with the stale DOM value. */
      var restored = session.state();
      verdict.value = restored.verdict;
      route.value = restored.route;
      $("#summary").value = restored.summary || "";
      $("#submitted-by").value = restored.submittedBy || "";
      syncDiffRows(restored);
      announce(
        "Draft restored. Verdict is now " +
          restored.verdict +
          ", route " +
          restored.route +
          ". The form, annotations and diff decisions on this page now match the saved draft."
      );
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
