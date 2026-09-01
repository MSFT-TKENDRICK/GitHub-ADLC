"""Evidence-diff stage -- the delta between a run and its baseline.

The evidence page (``report.html``) is otherwise a snapshot of one run. What a
reviewer actually decides on is *movement*: did a metric cross its budget, did a
requirement silently stop being evidenced, did a screenshot change. This module
computes that movement deterministically and offline against the run named in
``referencesRun``.

Three joins, no pixel work:

* **measurements** join on ``metricId`` -- equal value is ``unchanged``, a
  different value is ``changed`` with a signed ``delta``, and a metric that
  passed its budget in the baseline but fails now is an ``entered_breach`` (the
  reverse is ``left_breach``). Movement inside budget is noise; crossing a
  budget is a decision, so the two are reported separately.
* **coverage** joins on ``requirementId`` -- the load-bearing signal is
  ``lost``: a requirement that exists in both runs but silently stopped being
  evidenced. That is exactly what a human must be shown.
* **screenshots** are classified by SHA-256 at a path taken *relative to*
  ``evidence/<variant>/``. Keying on the full run-relative path would make a
  variant rename (``candidate-a`` -> ``candidate-b``) read as "every screenshot
  removed and every screenshot added", which is a useless diff. The report
  renders a changed pair with a CSS ``mix-blend-mode: difference`` overlay, so
  no image library is needed here or anywhere else.

Absence is always stated, never silent: with no ``referencesRun``, a missing
baseline directory, or an unreadable baseline review pack, the document is still
valid but carries ``baselineRunId: null`` and a ``reason`` saying which. A
silently empty diff is indistinguishable from "nothing changed".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import EvidenceDiff
from adlc.reduce import load_run
from adlc.runs import RunDir, read_json, sha256_file, utcnow, write_json
from adlc.schemas import ValidationError, is_valid

SCHEMA_NAME = "evidence-diff"
SCHEMA_VERSION = "adlc-evidence-diff/v1"

#: Written to the run root, alongside ``evidence-review-pack.json``.
ARTIFACT_NAME = "evidence-diff.json"

#: Suffixes we treat as screenshots. Mirrors the ``screenshot`` classification in
#: :meth:`RunDir.scan_artifacts` (``.png``) and broadens it to the other formats
#: a browser can capture. Only image artifacts are diffed by hash; everything
#: else in ``evidence/`` (traces, HAR, video, console logs) is out of scope here.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def diff_path(rd: RunDir) -> Path:
    """The well-known location of the emitted diff for ``rd``."""
    return rd.path / ARTIFACT_NAME


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_diff(cfg: Config, rd: RunDir, baseline: RunDir | None = None) -> EvidenceDiff:
    """Compute the evidence delta of ``rd`` against its baseline.

    Pure and deterministic: the same inputs always produce byte-identical output,
    so ``generatedAt`` is deliberately *not* stamped here -- :func:`run_evidence_diff`
    adds it at write time. ``baseline`` may be supplied directly; otherwise it is
    resolved from the run's ``referencesRun``.
    """
    if baseline is None:
        references = (load_run(rd).get("referencesRun") or "").strip()
        if not references:
            return _stated_absence(
                rd, f"run {rd.run_id} has no referencesRun, so there is no baseline to diff"
            )
        baseline = RunDir(cfg, references)

    ref = baseline.run_id
    if not baseline.exists():
        return _stated_absence(
            rd, f"baseline run '{ref}' referenced by {rd.run_id} has no run directory on disk"
        )
    if not baseline.review_pack.is_file():
        return _stated_absence(
            rd, f"baseline run '{ref}' has no evidence-review-pack.json to diff against"
        )
    try:
        base_pack = read_json(baseline.review_pack)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        # A baseline that exists but crashed mid-way leaves a truncated pack. A pack
        # sliced through a multi-byte UTF-8 char raises UnicodeDecodeError (a
        # ValueError, not an OSError), so it must be caught explicitly or the
        # "absence is stated" contract silently breaks with no artifact and no stage.
        return _stated_absence(
            rd, f"baseline run '{ref}' has an unreadable evidence-review-pack.json ({exc})"
        )
    if not isinstance(base_pack, dict):
        return _stated_absence(
            rd, f"baseline run '{ref}' evidence-review-pack.json is not a JSON object"
        )

    cand_pack = _read_pack(rd)

    measurements = _diff_measurements(cand_pack, base_pack)
    coverage = _diff_coverage(cand_pack, base_pack)
    screenshots = _diff_screenshots(rd, baseline)

    diff: EvidenceDiff = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": rd.run_id,
        "baselineRunId": ref,
        "measurements": measurements,
        "coverage": coverage,
        "screenshots": screenshots,
        "summary": _summary(measurements, coverage, screenshots),
    }
    return diff


def run_evidence_diff(cfg: Config, rd: RunDir) -> EvidenceDiff:
    """Compute the diff, validate it, persist the artifact, and record a stage.

    Fails loudly -- a diff that does not satisfy its schema is a framework bug,
    never something to write to disk. On failure a ``fail`` stage result is
    recorded (so the diagnostic survives) and a :class:`ValidationError` is raised.
    """
    started = utcnow()
    diff = compute_diff(cfg, rd)
    diff["generatedAt"] = utcnow()

    valid, errors = is_valid(SCHEMA_NAME, diff)
    if not valid:
        rd.write_stage(
            "evidence_diff",
            status="fail",
            message=f"evidence diff failed schema validation: {errors[:3]}",
            data={"baselineRunId": diff.get("baselineRunId"), "errors": errors[:5]},
            started_at=started,
        )
        raise ValidationError(SCHEMA_NAME, errors)

    out_path = diff_path(rd)
    write_json(out_path, diff)

    summary = diff.get("summary") or {}
    data: dict[str, Any] = {"baselineRunId": diff.get("baselineRunId"), **summary}
    if diff.get("reason"):
        data["reason"] = diff["reason"]
    rd.write_stage(
        "evidence_diff",
        status="ok",
        outputs=[rd.rel(out_path)],
        message=_message(diff),
        data=data,
        started_at=started,
    )
    return diff


# ---------------------------------------------------------------------------
# Measurements -- join on metricId
# ---------------------------------------------------------------------------


def _diff_measurements(cand_pack: dict[str, Any], base_pack: dict[str, Any]) -> list[dict[str, Any]]:
    cand = _by_key(cand_pack.get("measurements"), "metricId")
    base = _by_key(base_pack.get("measurements"), "metricId")

    out: list[dict[str, Any]] = []
    for metric_id in sorted(cand.keys() | base.keys()):
        c = cand.get(metric_id)
        b = base.get(metric_id)
        if c is not None and b is not None:
            out.append(_measurement_both(metric_id, c, b))
        elif c is not None:
            out.append(_measurement_one(metric_id, c, "added", is_candidate=True))
        else:
            out.append(_measurement_one(metric_id, b, "removed", is_candidate=False))
    return out


def _measurement_both(metric_id: str, c: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    value = _num(c.get("value"))
    baseline_value = _num(b.get("value"))
    # Values are recorded verbatim by collectors, so exact equality is both correct
    # and deterministic here; we never recompute them, so there is no float drift to
    # absorb. ``delta`` preserves the exact signed difference for the reader.
    equal = value is not None and baseline_value is not None and value == baseline_value
    entry: dict[str, Any] = {
        "metricId": metric_id,
        "change": "unchanged" if equal else "changed",
        "value": value,
        "baselineValue": baseline_value,
        "delta": _delta(value, baseline_value),
        "budget": _num(c.get("budget")),
        "passed": _bool_or_none(c.get("passed")),
        "baselinePassed": _bool_or_none(b.get("passed")),
        "budgetCrossed": _budget_crossed(b, c),
    }
    _stamp_provenance(entry, c)
    return entry


def _measurement_one(
    metric_id: str, src: dict[str, Any], change: str, *, is_candidate: bool
) -> dict[str, Any]:
    value = _num(src.get("value"))
    passed = _bool_or_none(src.get("passed"))
    entry: dict[str, Any] = {
        "metricId": metric_id,
        "change": change,
        "value": value if is_candidate else None,
        "baselineValue": None if is_candidate else value,
        "delta": None,
        "budget": _num(src.get("budget")),
        "passed": passed if is_candidate else None,
        "baselinePassed": None if is_candidate else passed,
        # A metric that exists on only one side has not crossed anything.
        "budgetCrossed": "none",
    }
    _stamp_provenance(entry, src)
    return entry


def _budget_crossed(b: dict[str, Any], c: dict[str, Any]) -> str:
    """Whether ``c`` crossed its budget relative to ``b``.

    Requires a budget *and* a pass/fail verdict on both sides; without either,
    "crossed" is unknowable, so the answer is ``none`` rather than a guess.
    """
    if _num(b.get("budget")) is None or _num(c.get("budget")) is None:
        return "none"
    base_passed = _bool_or_none(b.get("passed"))
    cand_passed = _bool_or_none(c.get("passed"))
    if base_passed is None or cand_passed is None:
        return "none"
    if base_passed and not cand_passed:
        return "entered_breach"
    if not base_passed and cand_passed:
        return "left_breach"
    return "none"


def _stamp_provenance(entry: dict[str, Any], src: dict[str, Any]) -> None:
    collector = src.get("collector")
    if isinstance(collector, str) and collector:
        entry["collector"] = collector
    sha = src.get("artifactSha256")
    if isinstance(sha, str) and sha:
        entry["artifactSha256"] = sha


# ---------------------------------------------------------------------------
# Coverage -- join on requirementId
# ---------------------------------------------------------------------------


def _diff_coverage(cand_pack: dict[str, Any], base_pack: dict[str, Any]) -> list[dict[str, Any]]:
    cand = _by_key(cand_pack.get("coverage"), "requirementId")
    base = _by_key(base_pack.get("coverage"), "requirementId")

    out: list[dict[str, Any]] = []
    for req_id in sorted(cand.keys() | base.keys()):
        c = cand.get(req_id)
        b = base.get(req_id)
        if c is not None and b is not None:
            c_present = _has_evidence(c)
            b_present = _has_evidence(b)
            if b_present and not c_present:
                change = "lost"
            elif not b_present and c_present:
                change = "gained"
            else:
                change = "unchanged"
            out.append({
                "requirementId": req_id,
                "change": change,
                "present": c_present,
                "baselinePresent": b_present,
                "evidenceKinds": _kinds(c),
                "baselineEvidenceKinds": _kinds(b),
            })
        elif c is not None:
            out.append({
                "requirementId": req_id,
                "change": "added",
                "present": _has_evidence(c),
                "baselinePresent": None,
                "evidenceKinds": _kinds(c),
                "baselineEvidenceKinds": [],
            })
        else:
            out.append({
                "requirementId": req_id,
                "change": "removed",
                "present": None,
                "baselinePresent": _has_evidence(b),
                "evidenceKinds": [],
                "baselineEvidenceKinds": _kinds(b),
            })
    return out


def _has_evidence(entry: dict[str, Any]) -> bool:
    """Does this coverage entry substantiate its requirement?

    ``present`` is the review pack's own has-evidence field (``build_review_pack``
    sets ``present = bool(hashes)``); we defer to it, falling back to the presence
    of evidence kinds or hashes only if an older pack omitted it.
    """
    if "present" in entry:
        return bool(entry.get("present"))
    return bool(entry.get("evidenceKinds") or entry.get("artifactSha256"))


def _kinds(entry: dict[str, Any]) -> list[str]:
    kinds = entry.get("evidenceKinds")
    if not isinstance(kinds, list):
        return []
    # Order is meaningful (evidence priority) and deterministic from the pack, so
    # it is preserved rather than sorted.
    return [str(k) for k in kinds]


# ---------------------------------------------------------------------------
# Screenshots -- classify by SHA-256 at a variant-relative path
# ---------------------------------------------------------------------------


def _diff_screenshots(rd: RunDir, baseline: RunDir) -> list[dict[str, Any]]:
    cand = _screenshots_by_relpath(rd)
    base = _screenshots_by_relpath(baseline)

    out: list[dict[str, Any]] = []
    for path in sorted(cand.keys() | base.keys()):
        c = cand.get(path)
        b = base.get(path)
        if c is not None and b is not None:
            change = "unchanged" if c["sha256"] == b["sha256"] else "changed"
            out.append({
                "path": path,
                "change": change,
                "sha256": c["sha256"],
                "baselineSha256": b["sha256"],
                "bytes": c["bytes"],
                "baselineBytes": b["bytes"],
            })
        elif c is not None:
            out.append({
                "path": path,
                "change": "added",
                "sha256": c["sha256"],
                "baselineSha256": None,
                "bytes": c["bytes"],
                "baselineBytes": None,
            })
        else:
            out.append({
                "path": path,
                "change": "removed",
                "sha256": None,
                "baselineSha256": b["sha256"],
                "bytes": None,
                "baselineBytes": b["bytes"],
            })
    return out


def _screenshots_by_relpath(rd: RunDir) -> dict[str, dict[str, Any]]:
    """Map each image under ``evidence/`` to its hash and size.

    The key is the path *within* the variant directory: for
    ``evidence/candidate-a/home.png`` the key is ``home.png``. That is the whole
    point -- it makes the diff stable across a variant rename. Each file is hashed
    exactly once (content is read only by :func:`sha256_file`); ``stat`` is metadata,
    not a second content read, so hundreds of screenshots cost one pass.
    """
    root = rd.evidence_dir
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    resolved_root = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        # Defence in depth: never hash a file that resolves outside the evidence tree
        # (e.g. a symlink planted in the bundle that points at a secret). rglob does not
        # recurse *into* symlinked directories, but it will still yield a symlinked
        # file, and only a hash -- not content -- would ever be emitted; even so,
        # hashing an unintended target is wrong, so skip anything that escapes.
        if not path.resolve().is_relative_to(resolved_root):
            continue
        parts = path.relative_to(root).parts
        key = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
        # If two variants carry the same intra-variant path, the last one in sorted
        # order wins deterministically; the diff identity is the variant-relative path
        # by design.
        out[key] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    return out


# ---------------------------------------------------------------------------
# Assembly helpers
# ---------------------------------------------------------------------------


def _summary(
    measurements: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    screenshots: list[dict[str, Any]],
) -> dict[str, int]:
    """Counters derived from the collections, so they can never disagree with them."""
    return {
        "measurementsChanged": sum(1 for m in measurements if m["change"] == "changed"),
        "budgetsEntered": sum(1 for m in measurements if m.get("budgetCrossed") == "entered_breach"),
        "budgetsLeft": sum(1 for m in measurements if m.get("budgetCrossed") == "left_breach"),
        "coverageLost": sum(1 for c in coverage if c["change"] == "lost"),
        "coverageGained": sum(1 for c in coverage if c["change"] == "gained"),
        "screenshotsChanged": sum(1 for s in screenshots if s["change"] == "changed"),
        "screenshotsAdded": sum(1 for s in screenshots if s["change"] == "added"),
        "screenshotsRemoved": sum(1 for s in screenshots if s["change"] == "removed"),
    }


def _stated_absence(rd: RunDir, reason: str) -> EvidenceDiff:
    """A valid, explicitly-empty diff. Absence of a baseline is never silent."""
    diff: EvidenceDiff = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": rd.run_id,
        "baselineRunId": None,
        "reason": reason,
        "measurements": [],
        "coverage": [],
        "screenshots": [],
        "summary": _summary([], [], []),
    }
    return diff


def _message(diff: EvidenceDiff) -> str:
    if diff.get("baselineRunId") is None:
        return f"no baseline diff: {diff.get('reason', '')}"
    s = diff.get("summary") or {}
    return (
        f"diff vs {diff['baselineRunId']}: {s.get('measurementsChanged', 0)} measurement(s) "
        f"changed ({s.get('budgetsEntered', 0)} entered budget, {s.get('budgetsLeft', 0)} left); "
        f"coverage {s.get('coverageLost', 0)} lost, {s.get('coverageGained', 0)} gained; "
        f"{s.get('screenshotsChanged', 0)} screenshot(s) changed"
    )


def _read_pack(rd: RunDir) -> dict[str, Any]:
    """Best-effort read of a run's review pack. A missing/broken candidate pack
    simply means no candidate-side measurements or coverage, which the joins render
    faithfully as removals."""
    if not rd.review_pack.is_file():
        return {}
    try:
        loaded = read_json(rd.review_pack)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # UnicodeDecodeError (a truncated multi-byte char) is a ValueError, not an
        # OSError; catch it so a half-written candidate pack degrades to "no candidate
        # evidence" instead of crashing the whole diff.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _by_key(items: Any, key: str) -> dict[str, dict[str, Any]]:
    """Index a list of dicts by a string key, skipping entries that lack it.

    A key appearing twice collapses to the last occurrence; the review pack emits
    entries in a stable order, so this is deterministic.
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if isinstance(value, str) and value:
            out[value] = item
    return out


def _num(value: Any) -> float | None:
    """Coerce a JSON number, treating booleans and non-numbers as absent."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _delta(value: float | None, baseline_value: float | None) -> float | None:
    if value is None or baseline_value is None:
        return None
    return value - baseline_value
