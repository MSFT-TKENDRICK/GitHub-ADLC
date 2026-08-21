"""Feedback targets -- the GUI-agnostic half of the human feedback loop.

``report.html`` is one review GUI. It will not be the last. This stage exists so
the *next* one does not have to be rewritten against ADLC internals: it reduces a
run to a single self-describing document, ``feedback-targets.json``, listing
everything a human could give feedback on and exactly how to submit it.

The contract has two halves:

* **in** -- ``adlc-feedback-targets/v1`` (this module), and
* **out** -- ``adlc-human-feedback/v1`` (:mod:`adlc.stages.feedback`).

A GUI that reads the first and emits the second is a first-class ADLC review
surface. It needs no Python, no access to ``.adlc/runs``, and no knowledge of how
evidence is laid out on disk.

Two decisions are load-bearing:

**Enums and limits are derived, never hand-copied.** ``submission.enums`` and
``submission.limits`` are read out of ``human-feedback-pack.schema.json`` when
this document is built. A hand-copied enum is a lie waiting to happen: the schema
gains a severity, the GUI keeps offering four, and a reviewer's work is refused
at ingestion for a reason nobody can see. Derivation *fails loudly* if the schema
moves -- silently emitting an empty list would be the same rot in new clothing.

**Every row arrives decision-ready.** A diff row carries the ``targetKind`` and
``targetId`` a ``diffDecision`` must name, and an artifact carries the
``artifactSha256`` an annotation must cite. A GUI never derives an identifier, so
it can never derive one subtly wrong and have the citation-or-discard rule throw
the reviewer's comment away.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.reduce import load_run
from adlc.runs import RunDir, read_json, sha256_bytes, utcnow, write_json
from adlc.schemas import is_valid, load_schema
from adlc.stages.evidence import extract_requirements
from adlc.stages.evidence_diff import diff_path
from adlc.stages.feedback import PACK_SCHEMA_VERSION, REVIEW_FENCE

__all__ = [
    "DEFAULT_PER_ARTIFACT_BYTES",
    "DEFAULT_TOTAL_BYTES",
    "SCHEMA_VERSION",
    "compute_targets",
    "run_feedback_targets",
    "submission_contract",
    "targets_path",
]

SCHEMA_VERSION = "adlc-feedback-targets/v1"

#: Inlining is the document's main growth vector, so it is bounded rather than
#: hoped about. Budgets apply to *raw* file bytes; base64 inflates by ~4/3 on top.
DEFAULT_PER_ARTIFACT_BYTES = 2 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 12 * 1024 * 1024

#: Kinds a GUI can meaningfully draw on. Everything else is still listed -- a
#: reviewer must see the whole evidence set, not a silently filtered subset --
#: but is flagged ``annotatable: false`` so no GUI has to guess.
_ANNOTATABLE_MEDIA = ("image/", "video/")

_REASONING_TEXT_CAP = 20_000
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

#: Change kinds whose baseline image is worth inlining. ``unchanged`` is excluded
#: because its baseline is byte-identical to the candidate, which is already
#: inlined in ``artifacts``; ``added`` has no baseline at all. Spending the
#: shared budget on either starves the changed pairs the manifest exists to show.
_BASELINE_INLINE_CHANGES = frozenset({"changed", "removed"})


def targets_path(rd: RunDir) -> Path:
    return rd.path / "feedback-targets.json"


# ---------------------------------------------------------------------------
# Submission contract -- derived from the pack schema, never hand-copied
# ---------------------------------------------------------------------------


class SchemaDerivationError(RuntimeError):
    """The pack schema no longer has the shape the manifest derives from.

    Raised rather than degraded. A GUI handed an empty enum shows an empty
    dropdown and the reviewer's work dies at ingestion with no explanation; a
    build that fails here is noticed by the person who moved the schema.
    """


def _walk(schema: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = schema
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise SchemaDerivationError(
                "human-feedback-pack schema has no " + "/".join(path) + "; the manifest "
                "derives its enums and limits from that schema, so this must be fixed "
                "rather than defaulted"
            )
        node = node[key]
    return node


def _enum(schema: dict[str, Any], *path: str) -> list[str]:
    values = _walk(schema, (*path, "enum"))
    if not isinstance(values, list) or not values:
        raise SchemaDerivationError("/".join(path) + " is not a non-empty enum")
    return [str(v) for v in values]


def _int(schema: dict[str, Any], *path: str) -> int:
    value = _walk(schema, path)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaDerivationError("/".join(path) + " is not an integer limit")
    return value


def submission_contract(
    *,
    endpoint: str | None = None,
    nonce_header: str | None = None,
    nonce: str | None = None,
    max_body_bytes: int | None = None,
) -> dict[str, Any]:
    """Everything a GUI needs to build an acceptable pack, read out of the schema.

    ``endpoint`` is ``None`` when the report was opened as a plain file. That is
    the contract path, not a degraded one: download-or-copy is how a pack is
    meant to travel, and the loopback server is a convenience wrapper over the
    same CLI.
    """
    pack = load_schema("human-feedback-pack")
    defs = ("$defs",)
    ann = (*defs, "annotation", "properties")
    crit = (*defs, "critique", "properties")
    dec = (*defs, "diffDecision", "properties")

    enums = {
        "verdict": _enum(pack, "properties", "verdict"),
        "route": _enum(pack, "properties", "route"),
        "severity": _enum(pack, *defs, "severity"),
        "shape": _enum(pack, *ann, "shape"),
        "critiqueStance": _enum(pack, *crit, "stance"),
        "critiqueTargetKind": _enum(pack, *crit, "targetKind"),
        "diffDecision": _enum(pack, *dec, "decision"),
        "diffTargetKind": _enum(pack, *dec, "targetKind"),
    }
    limits = {
        "annotations": _int(pack, "properties", "annotations", "maxItems"),
        "critiques": _int(pack, "properties", "critiques", "maxItems"),
        "diffDecisions": _int(pack, "properties", "diffDecisions", "maxItems"),
        "summaryChars": _int(pack, "properties", "summary", "maxLength"),
        "commentChars": _int(pack, *ann, "comment", "maxLength"),
        "critiqueCommentChars": _int(pack, *crit, "comment", "maxLength"),
        "diffCommentChars": _int(pack, *dec, "comment", "maxLength"),
        "geometryPoints": _int(pack, *ann, "geometry", "properties", "points", "maxItems"),
        "requirementIdsPerAnnotation": _int(pack, *defs, "requirementIds", "maxItems"),
        "annotationIdsPerDecision": _int(pack, *dec, "annotationIds", "maxItems"),
        "idChars": _int(pack, *defs, "id", "maxLength"),
        "targetRefChars": _int(pack, *crit, "targetRef", "maxLength"),
        "submittedByChars": _int(pack, "properties", "submittedBy", "maxLength"),
    }
    return {
        "packSchemaVersion": PACK_SCHEMA_VERSION,
        "endpoint": endpoint,
        "nonceHeader": nonce_header,
        "nonce": nonce,
        "maxBodyBytes": max_body_bytes,
        "idPattern": str(_walk(pack, (*defs, "id", "pattern"))),
        "reviewFence": REVIEW_FENCE,
        "enums": enums,
        "limits": limits,
    }


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class _Budget:
    """Tracks inlining spend so an over-budget document is visible, not silent."""

    def __init__(self, per_artifact: int, total: int) -> None:
        self.per_artifact = per_artifact
        self.total = total
        self.spent = 0
        self.inlined = 0
        self.omitted = 0

    def take(self, path: Path, size: int, media_type: str | None) -> tuple[str | None, str | None]:
        """Return ``(data_uri, omitted_reason)`` -- exactly one is non-None."""
        if not media_type:
            self.omitted += 1
            return None, "not inlined: unknown media type"
        if size > self.per_artifact:
            self.omitted += 1
            return None, (
                f"not inlined: {size} bytes exceeds the {self.per_artifact}-byte "
                "per-artifact budget; hash and size above still identify it"
            )
        if self.spent + size > self.total:
            self.omitted += 1
            return None, (
                f"not inlined: the {self.total}-byte document budget is exhausted; "
                "hash and size above still identify it"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            self.omitted += 1
            return None, f"not inlined: unreadable ({exc.__class__.__name__})"
        self.spent += size
        self.inlined += 1
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{media_type};base64,{encoded}", None


def _annotatable(media_type: str | None) -> bool:
    return bool(media_type) and any(media_type.startswith(p) for p in _ANNOTATABLE_MEDIA)


def _artifacts(rd: RunDir, run: dict[str, Any], budget: _Budget) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, ref in enumerate(run.get("artifacts") or [], start=1):
        if not isinstance(ref, dict):
            continue
        digest = str(ref.get("sha256") or "")
        rel = str(ref.get("path") or "")
        media_type = ref.get("mediaType") or ref.get("mimeType")
        media_type = str(media_type) if media_type else None
        size = int(ref.get("bytes") or 0)
        annotatable = _annotatable(media_type)

        inline: str | None = None
        reason: str | None = None
        if annotatable:
            path = rd.path / rel
            if path.is_file():
                inline, reason = budget.take(path, size or path.stat().st_size, media_type)
            else:
                budget.omitted += 1
                reason = "not inlined: file is not present in the run directory"

        entry: dict[str, Any] = {
            "id": f"art-{index}",
            "path": rel,
            "sha256": digest,
            "kind": str(ref.get("kind") or "file"),
            "mediaType": media_type,
            "bytes": size,
            "width": None,
            "height": None,
            "annotatable": annotatable,
            "inline": inline,
            "inlineOmittedReason": reason,
        }
        if inline is not None and media_type == "image/png":
            entry["width"], entry["height"] = _png_size(rd.path / rel)
        out.append(entry)
    return out


def _png_size(path: Path) -> tuple[int | None, int | None]:
    """Natural size from the PNG IHDR chunk, so geometry can be normalised.

    Read from the header rather than an image library: annotation coordinates are
    normalised to the artifact's natural size, and that number has to come from
    somewhere that is not the reviewer's viewport.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None, None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None, None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return (width or None), (height or None)


# ---------------------------------------------------------------------------
# Reasoning -- everything an agent asserted that a human may push back on
# ---------------------------------------------------------------------------


def _digest(text: str) -> str:
    return "sha256:" + sha256_bytes(text.encode("utf-8"))


def _reason_entry(
    seq: list[dict[str, Any]],
    *,
    kind: str,
    ref: str,
    title: str,
    text: str,
    author: str = "",
    severity: str | None = None,
    confidence: str | None = None,
    citations: list[str] | None = None,
) -> None:
    body = text.strip()
    if not body:
        return
    body = body[:_REASONING_TEXT_CAP]
    seq.append({
        "id": f"rsn-{len(seq) + 1}",
        "targetKind": kind,
        "targetRef": ref[:512],
        "targetTitle": title[:512],
        "sourceDigest": _digest(body),
        "author": author[:128],
        "text": body,
        "severity": severity,
        "confidence": confidence,
        "citations": [c[:512] for c in (citations or [])[:40]],
    })


def _reasoning(cfg: Config, rd: RunDir) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    _squad_findings(rd, out)
    _personas(rd, out)
    _rubric_rationales(rd, out)
    _adr_justifications(cfg, out)
    return out


def _squad_findings(rd: RunDir, out: list[dict[str, Any]]) -> None:
    if not rd.reviews_dir.is_dir():
        return
    # Imported lazily: the review parser pulls in yaml and the gate machinery,
    # and a manifest for a run with no reviews should not pay for either.
    from adlc.adapters.gate.adversarial_review import parse_review

    for path in sorted(rd.reviews_dir.glob("*.md")):
        try:
            review = parse_review(path)
        except Exception:  # noqa: BLE001 - a malformed review must not sink the manifest
            continue
        rel = rd.rel(path)
        for index, finding in enumerate(review.findings, start=1):
            _reason_entry(
                out,
                kind="squad_finding",
                ref=f"{rel}#finding-{index}",
                title=finding.title,
                text=finding.body,
                author=review.member or review.squad,
                severity=finding.severity or None,
                confidence=getattr(finding, "confidence", "") or None,
                citations=list(finding.citations),
            )


def _personas(rd: RunDir, out: list[dict[str, Any]]) -> None:
    path = rd.enrichment_dir / "personas.md"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    rel = rd.rel(path)
    for slug, title, body in _split_sections(text):
        _reason_entry(
            out,
            kind="persona",
            ref=f"{rel}#{slug}",
            title=title,
            text=body,
            author="enrich_personas",
        )


def _split_sections(text: str) -> list[tuple[str, str, str]]:
    """Split markdown on its deepest-but-one heading level into (slug, title, body).

    Sectioning is what makes a critique locatable: ``targetRef`` has to name a
    span of reasoning, not a whole file, or a reviewer's disagreement lands on
    1,000 lines at once.
    """
    lines = text.splitlines()
    heads = [(i, m) for i, line in enumerate(lines) if (m := _HEADING_RE.match(line))]
    if not heads:
        return []
    level = min(len(m.group(1)) for _, m in heads)
    picked = [(i, m) for i, m in heads if len(m.group(1)) == level]
    out: list[tuple[str, str, str]] = []
    for pos, (start, match) in enumerate(picked):
        end = picked[pos + 1][0] if pos + 1 < len(picked) else len(lines)
        title = match.group(2).strip()
        body = "\n".join(lines[start + 1 : end]).strip()
        out.append((_slug(title) or f"section-{pos + 1}", title, body))
    return out


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]


def _rubric_rationales(rd: RunDir, out: list[dict[str, Any]]) -> None:
    path = rd.evals_dir / "rubric-score.json"
    if not path.is_file():
        return
    try:
        score = read_json(path)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(score, dict):
        return
    for crit in score.get("criteria") or []:
        if not isinstance(crit, dict):
            continue
        crit_id = str(crit.get("id") or "")
        _reason_entry(
            out,
            kind="rubric_criterion",
            ref=f"evals/rubric-score.json#{crit_id}",
            title=str(crit.get("statement") or crit_id),
            text=str(crit.get("rationale") or ""),
            author=str(score.get("backend") or "eval"),
            confidence=None if crit.get("passed") is None else str(crit.get("passed")),
        )


def _adr_justifications(cfg: Config, out: list[dict[str, Any]]) -> None:
    from adlc.stages.adr import list_adrs

    try:
        adrs = list_adrs(cfg)
    except Exception:  # noqa: BLE001
        return
    for adr in adrs:
        try:
            text = adr.path.read_text(encoding="utf-8")
        except OSError:
            continue
        _reason_entry(
            out,
            kind="adr",
            ref=f"{adr.number}",
            title=adr.title,
            text=_adr_justification(text),
            author="adr",
            confidence=adr.status or None,
        )


def _adr_justification(text: str) -> str:
    """The justification section if the ADR has one, else the whole document.

    Falling back to the whole document is deliberate: an ADR whose headings do
    not match our expectation is still a decision a human may want to argue
    with, and dropping it would silently narrow what can be critiqued.
    """
    for slug, _title, body in _split_sections(text):
        if body and ("justification" in slug or "rationale" in slug or "decision" in slug):
            return body
    return text


# ---------------------------------------------------------------------------
# Diff -- normalised into decision-ready rows
# ---------------------------------------------------------------------------


def _diff(rd: RunDir, budget: _Budget) -> dict[str, Any] | None:
    path = diff_path(rd)
    if not path.is_file():
        return None
    try:
        raw = read_json(path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    baseline_run_id = raw.get("baselineRunId")
    # Enumerate the baseline's variant dirs once for the whole manifest. Doing it
    # inside the row loop re-read the directory once per screenshot, which is
    # S x (1 opendir + 2V stats) -- the same quadratic the report's screenshot
    # section already avoids with a one-shot index.
    variants: list[Path] | None = None
    if isinstance(baseline_run_id, str) and baseline_run_id:
        runs_root = rd.path.parent.resolve()
        base_dir = (runs_root / baseline_run_id / "evidence").resolve()
        if base_dir.is_relative_to(runs_root) and base_dir.is_dir():
            variants = _variant_dirs(base_dir)
    return {
        "baselineRunId": baseline_run_id,
        "measurements": [_measurement_row(m) for m in _rows(raw, "measurements")],
        "coverage": [_coverage_row(c) for c in _rows(raw, "coverage")],
        "screenshots": [
            _screenshot_row(rd, s, budget, baseline_run_id, variants)
            for s in _rows(raw, "screenshots")
        ],
    }


def _rows(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [r for r in (raw.get(key) or []) if isinstance(r, dict)]


def _measurement_row(m: dict[str, Any]) -> dict[str, Any]:
    passed = m.get("passed")
    baseline_passed = m.get("baselinePassed")
    return {
        "targetKind": "measurement",
        "targetId": str(m.get("metricId") or ""),
        "label": str(m.get("metricId") or ""),
        "change": str(m.get("change") or "unchanged"),
        "value": m.get("value"),
        "baselineValue": m.get("baselineValue"),
        "delta": m.get("delta"),
        "budget": m.get("budget"),
        "passed": passed,
        "baselinePassed": baseline_passed,
        "budgetCrossed": m.get("budgetCrossed"),
        "collector": m.get("collector"),
        # Computed once, here, so every GUI agrees on what a regression is
        # instead of each inventing a rule and disagreeing with ingestion.
        "regression": m.get("budgetCrossed") == "entered_breach"
        or (baseline_passed is True and passed is False)
        or str(m.get("change")) == "removed",
    }


def _coverage_row(c: dict[str, Any]) -> dict[str, Any]:
    change = str(c.get("change") or "unchanged")
    return {
        "targetKind": "coverage",
        "targetId": str(c.get("requirementId") or ""),
        "label": str(c.get("requirementId") or ""),
        "change": change,
        "present": c.get("present"),
        "baselinePresent": c.get("baselinePresent"),
        "evidenceKinds": [str(k) for k in (c.get("evidenceKinds") or [])],
        "baselineEvidenceKinds": [str(k) for k in (c.get("baselineEvidenceKinds") or [])],
        "regression": change in ("lost", "removed"),
    }


def _screenshot_row(
    rd: RunDir,
    s: dict[str, Any],
    budget: _Budget,
    baseline_run_id: Any,
    variants: list[Path] | None = None,
) -> dict[str, Any]:
    rel = str(s.get("path") or "")
    change = str(s.get("change") or "unchanged")
    row: dict[str, Any] = {
        "targetKind": "screenshot",
        "targetId": rel,
        "label": rel,
        "change": change,
        "sha256": s.get("sha256"),
        "baselineSha256": s.get("baselineSha256"),
        "bytes": s.get("bytes"),
        "baselineBytes": s.get("baselineBytes"),
        "inline": None,
        "baselineInline": None,
        "inlineOmittedReason": None,
        # "removed" is the regression: evidence that existed and no longer does.
        # "changed" is a question for the human, which is the whole point of the
        # accept/reject row, so it is not pre-judged here.
        "regression": change == "removed",
    }
    # The candidate image is already inlined once in ``artifacts``; re-encoding it
    # here would double the document for no gain. Only the baseline -- which is
    # in another run dir and therefore absent from ``artifacts`` -- is inlined.
    #
    # And only for rows where the baseline is a *different* image. Diff rows
    # arrive in path order, uncorrelated with change status, so inlining every
    # baseline spends the whole budget on whatever sorts first -- overwhelmingly
    # unchanged images, whose baseline is byte-identical to the candidate that is
    # already inlined. The changed pairs, the only reason this manifest exists,
    # would then be the ones reported as over budget.
    if change not in _BASELINE_INLINE_CHANGES:
        row["inlineOmittedReason"] = (
            f"baseline not inlined: '{change}' has no distinct baseline image to compare"
        )
    elif baseline_run_id and s.get("baselineSha256"):
        candidate = _baseline_screenshot(rd, str(baseline_run_id), rel, variants)
        if candidate is not None and candidate.is_file():
            size = candidate.stat().st_size
            row["baselineInline"], row["inlineOmittedReason"] = budget.take(
                candidate, size, "image/png"
            )
    return row


def _baseline_screenshot(
    rd: RunDir, baseline_run_id: str, rel: str, variants: list[Path] | None = None
) -> Path | None:
    """Locate ``rel`` inside the baseline run's evidence, under any variant dir.

    ``rel`` is variant-relative by design -- that is what makes the diff stable
    across a variant rename -- so it has to be re-anchored rather than joined.
    Because it is variant-relative *by construction*, the direct join below
    essentially never hits; the variant scan is the real path, which is why the
    caller passes a ``variants`` listing enumerated once for the whole manifest
    rather than re-reading the directory once per screenshot.

    Both ``baseline_run_id`` and ``rel`` are read out of a diff document, and the
    result is base64-inlined into the manifest. Neither is attacker-controlled
    today (``.adlc/**`` is protected and the document is produced by the
    ``evidence_diff`` stage), but a join that escapes its root would turn any
    future write path into an arbitrary-file read, so both are confined here.
    """
    runs_root = rd.path.parent.resolve()
    base_dir = (runs_root / baseline_run_id / "evidence").resolve()
    if not base_dir.is_relative_to(runs_root) or not base_dir.is_dir():
        return None

    def _confined(candidate: Path) -> Path | None:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base_dir) or not resolved.is_file():
            return None
        return resolved

    if found := _confined(base_dir / rel):
        return found
    for variant in _variant_dirs(base_dir) if variants is None else variants:
        if found := _confined(variant / rel):
            return found
    return None


def _variant_dirs(base_dir: Path) -> list[Path]:
    """Immediate subdirectories of an evidence tree, one enumeration."""
    try:
        return sorted(p for p in base_dir.iterdir() if p.is_dir())
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def compute_targets(
    cfg: Config,
    rd: RunDir,
    *,
    endpoint: str | None = None,
    nonce_header: str | None = None,
    nonce: str | None = None,
    max_body_bytes: int | None = None,
    per_artifact_bytes: int | None = None,
    total_bytes: int | None = None,
) -> dict[str, Any]:
    """Reduce a run to everything a review GUI needs. Pure: writes nothing."""
    opts = cfg.raw.get("feedback") or {} if isinstance(cfg.raw, dict) else {}
    budget = _Budget(
        int(per_artifact_bytes or opts.get("perArtifactBytes") or DEFAULT_PER_ARTIFACT_BYTES),
        int(total_bytes or opts.get("totalBytes") or DEFAULT_TOTAL_BYTES),
    )
    run = load_run(rd)

    report_digest = None
    if rd.report.is_file():
        try:
            report_digest = "sha256:" + sha256_bytes(rd.report.read_bytes())
        except OSError:
            report_digest = None

    artifacts = _artifacts(rd, run, budget)
    diff = _diff(rd, budget)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": utcnow(),
        "run": {
            "runId": str(run.get("runId") or rd.run_id),
            "candidateSha": str(run.get("headSha") or ""),
            "baselineRunId": run.get("referencesRun"),
            "reportDigest": report_digest,
            "profile": run.get("profile"),
            "title": run.get("title"),
            "passed": run.get("passed"),
        },
        "requirements": [
            {
                "id": str(r.get("id") or ""),
                "text": str(r.get("text") or "")[:4000],
                "source": str(r.get("source") or ""),
            }
            for r in extract_requirements(rd)
        ],
        "artifacts": artifacts,
        "reasoning": _reasoning(cfg, rd),
        "diff": diff,
        "submission": submission_contract(
            endpoint=endpoint,
            nonce_header=nonce_header,
            nonce=nonce,
            max_body_bytes=max_body_bytes,
        ),
        "budgets": {
            "perArtifactBytes": budget.per_artifact,
            "totalBytes": budget.total,
            "inlinedBytes": budget.spent,
            "inlinedCount": budget.inlined,
            "omittedCount": budget.omitted,
        },
    }


def run_feedback_targets(cfg: Config, rd: RunDir, **kwargs: Any) -> dict[str, Any]:
    """Write ``<run>/feedback-targets.json`` and record a stage result."""
    targets = compute_targets(cfg, rd, **kwargs)
    valid, errors = is_valid("feedback-targets", targets)
    if not valid:
        rd.write_stage(
            "feedback-targets",
            status="fail",
            started_at=targets["generatedAt"],
            outputs=[],
            message="feedback targets failed schema validation: " + "; ".join(errors[:5]),
            data={"errors": errors[:5]},
        )
        raise ValueError("invalid feedback targets: " + "; ".join(errors[:5]))

    path = targets_path(rd)
    write_json(path, targets)
    diff = targets.get("diff") or {}
    rd.write_stage(
        "feedback-targets",
        status="ok",
        started_at=targets["generatedAt"],
        outputs=[rd.rel(path)],
        message=(
            f"{len(targets['artifacts'])} artifact(s), "
            f"{len(targets['reasoning'])} reasoning target(s), "
            f"{sum(len(diff.get(k) or []) for k in ('measurements', 'coverage', 'screenshots'))} "
            "diff row(s) offered for feedback"
        ),
        data={
            "artifacts": len(targets["artifacts"]),
            "reasoning": len(targets["reasoning"]),
            "inlinedBytes": targets["budgets"]["inlinedBytes"],
            "omittedCount": targets["budgets"]["omittedCount"],
        },
    )
    return targets
