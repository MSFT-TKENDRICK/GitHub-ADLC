"""Media embedding for the report: the recording and the visual diffs.

The report is one file that must open from ``file://``, so every image and video
it shows has to travel inside it as a ``data:`` URI. That is a hard constraint
and also a hard budget: base64 costs 33% overhead, and a report nobody can open
because it is 400 MB has failed at its only job.

So embedding is bounded and *explicit about what it dropped*. A file that
exceeds its budget is not silently omitted -- it renders as a card carrying the
path, the size and the SHA-256, so the reader knows the evidence exists, knows
it was captured, and can go and get it. Silence here would be indistinguishable
from "we never recorded that", which is the failure mode this whole framework is
built to prevent.

Before/after pairing is heuristic, and the heuristics are ordered from most to
least trustworthy: an explicit ``before``/``after`` marker in the filename beats
the same filename appearing in two variant directories, which beats "these two
screenshots were taken one after another". Every pair records *which* rule
matched, so a reader can discount a weak pairing rather than being told a
confident-looking lie.
"""

from __future__ import annotations

import base64
import re
from collections import deque
from pathlib import Path
from typing import Any

from adlc.runs import RunDir
from adlc.summarize import humanise_bytes

__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_TOTAL_BYTES",
    "MAX_VIDEO_BYTES",
    "build_media",
]

#: A single hero recording is worth real weight -- it is the artifact the whole
#: page is built around. Beyond this, link it instead.
MAX_VIDEO_BYTES = 6 * 1024 * 1024

#: Screenshots are small. One that isn't is usually a full-page capture of a
#: page that should have been captured per-viewport.
MAX_IMAGE_BYTES = 1_500 * 1024

#: Whole-document ceiling. Browsers and mail gateways both start to struggle
#: well before this; it exists so a run with 200 screenshots degrades to links
#: rather than to an unopenable file.
MAX_TOTAL_BYTES = 24 * 1024 * 1024

_MIME = {
    ".webm": "video/webm", ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}

_BEFORE = re.compile(r"(?:^|[-_.])(before|baseline|prev|previous|old|control)(?:$|[-_.])", re.IGNORECASE)
_AFTER = re.compile(r"(?:^|[-_.])(after|candidate|current|new|treatment)(?:$|[-_.])", re.IGNORECASE)
_STEP = re.compile(r"(\d{1,4})")


def _mime_for(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


def _caption(path: Path) -> str:
    stem = re.sub(r"^\d+[-_]", "", path.stem)
    return re.sub(r"[-_]+", " ", stem).strip().capitalize() or path.name


def _normalise(path: Path) -> str:
    """A pairing key: the filename with before/after words and digits removed."""
    stem = path.stem.lower()
    stem = _BEFORE.sub("-", stem)
    stem = _AFTER.sub("-", stem)
    stem = re.sub(r"\d+", "", stem)
    return re.sub(r"[-_.]+", "-", stem).strip("-")


class _Budget:
    """Tracks the remaining embedding allowance across the whole document."""

    def __init__(self, total: int = MAX_TOTAL_BYTES) -> None:
        self.remaining = total
        self.embedded = 0
        self.linked = 0

    def take(self, size: int, cap: int) -> bool:
        if size > cap or size > self.remaining:
            self.linked += 1
            return False
        self.remaining -= size
        self.embedded += 1
        return True


def _entry(rd: RunDir, path: Path, budget: _Budget, cap: int) -> dict[str, Any]:
    """One media item, embedded if it fits and honestly described if it does not."""
    try:
        size = path.stat().st_size
        data = path.read_bytes() if budget.take(size, cap) else b""
    except OSError as exc:
        return {
            "path": rd.rel(path), "name": path.name, "caption": _caption(path),
            "mime": _mime_for(path), "bytes": 0, "human": "unreadable",
            "src": "", "embedded": False, "reason": f"unreadable: {exc}", "sha256": "",
        }

    embedded = bool(data)
    return {
        "path": rd.rel(path),
        "name": path.name,
        "caption": _caption(path),
        "mime": _mime_for(path),
        "bytes": size,
        "human": humanise_bytes(size),
        "src": (
            f"data:{_mime_for(path)};base64,{base64.b64encode(data).decode('ascii')}"
            if embedded else ""
        ),
        "embedded": embedded,
        "reason": "" if embedded else (
            f"{humanise_bytes(size)} exceeds the {humanise_bytes(cap)} embed budget; "
            f"open it from the run directory"
        ),
        "sha256": "",
    }


def _pair_label(*names: str) -> str:
    """A label for a pair: the shared subject, with the before/after word gone.

    ``settings-before.png`` paired with ``settings-after.png`` is a slide about
    the settings page, not a slide about "before".
    """
    for name in names:
        key = _normalise(Path(name))
        if key:
            return re.sub(r"[-_]+", " ", key).strip().capitalize()
    return _caption(Path(names[0])) if names else ""


def _pair_shots(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair screenshots into before/after slides, best rule first."""
    remaining = list(shots)
    pairs: list[dict[str, Any]] = []
    used: set[str] = set()

    # Rule 1 -- explicit before/after markers in the filename. Someone named
    # these on purpose; trust that over anything we could infer.
    #
    # The "after" candidates are bucketed by pairing key up front so this stays
    # O(befores + afters). Rescanning the after list for every before made it
    # quadratic *and* re-ran the normalising regexes on names whose key never
    # changes -- and a 200-screenshot run is exactly the case this module
    # promises to degrade gracefully on.
    afters_by_key: dict[str, deque[dict[str, Any]]] = {}
    for shot in remaining:
        if _AFTER.search(Path(shot["name"]).stem):
            afters_by_key.setdefault(_normalise(Path(shot["name"])), deque()).append(shot)

    for before in remaining:
        if not _BEFORE.search(Path(before["name"]).stem):
            continue
        bucket = afters_by_key.get(_normalise(Path(before["name"])), deque())
        match = None
        while bucket:
            # A consumed candidate can never match again, so drop it rather
            # than re-skipping it on every later pass. A name carrying *both*
            # markers must not pair with itself.
            if bucket[0]["path"] in used or bucket[0]["path"] == before["path"]:
                bucket.popleft()
                continue
            match = bucket.popleft()
            break
        if match:
            used.update({before["path"], match["path"]})
            pairs.append({
                "label": _pair_label(before["name"], match["name"]),
                "before": before, "after": match,
                "rule": "filename declares before/after",
                "confidence": "high",
            })

    # Rule 2 -- the same filename captured under two different variant
    # directories. That is what an A/B evidence run produces.
    by_name: dict[str, list[dict[str, Any]]] = {}
    for shot in remaining:
        if shot["path"] in used:
            continue
        by_name.setdefault(shot["name"], []).append(shot)
    for name, group in by_name.items():
        variants = {Path(s["path"]).parent.as_posix() for s in group}
        if len(group) == 2 and len(variants) == 2:
            first, second = sorted(group, key=lambda s: s["path"])
            used.update({first["path"], second["path"]})
            pairs.append({
                "label": _caption(Path(name)), "before": first, "after": second,
                "rule": "same capture under two variants",
                "confidence": "high",
            })

    # Rule 3 -- consecutive numbered captures inside one variant. This is a
    # timeline, not a controlled comparison, so it is labelled as one.
    leftovers = [s for s in remaining if s["path"] not in used]
    leftovers.sort(key=lambda s: s["path"])
    for index in range(0, len(leftovers) - 1, 2):
        first, second = leftovers[index], leftovers[index + 1]
        if Path(first["path"]).parent != Path(second["path"]).parent:
            continue
        pairs.append({
            "label": f"{first['caption']} \u2192 {second['caption']}",
            "before": first, "after": second,
            "rule": "consecutive captures in the same run",
            "confidence": "low",
        })
        used.update({first["path"], second["path"]})

    unpaired = [s for s in shots if s["path"] not in used]
    for shot in unpaired:
        pairs.append({
            "label": shot["caption"], "before": None, "after": shot,
            "rule": "single capture, nothing to compare against",
            "confidence": "none",
        })
    return pairs


def build_media(rd: RunDir, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Collect the hero recording and the before/after slideshow.

    The longest recording wins the hero slot: an evidence run usually produces
    one full end-to-end capture plus incidental per-test clips, and the full one
    is what a reader should land on.
    """
    budget = _Budget()
    by_path = {a.get("path", ""): a for a in artifacts}

    videos_on_disk: list[Path] = []
    images_on_disk: list[Path] = []
    if rd.evidence_dir.is_dir():
        for path in sorted(rd.evidence_dir.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in {".webm", ".mp4", ".mov"}:
                videos_on_disk.append(path)
            elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                images_on_disk.append(path)

    # Hero first, so it gets the budget before 60 screenshots eat it.
    videos_on_disk.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    videos = [_entry(rd, path, budget, MAX_VIDEO_BYTES) for path in videos_on_disk]
    shots = [_entry(rd, path, budget, MAX_IMAGE_BYTES) for path in images_on_disk]

    for item in videos + shots:
        artifact = by_path.get(item["path"])
        if artifact:
            item["sha256"] = artifact.get("sha256", "")

    return {
        "hero": videos[0] if videos else None,
        "videos": videos[1:],
        "screenshots": shots,
        "pairs": _pair_shots(shots),
        "budget": {
            "embedded": budget.embedded,
            "linked": budget.linked,
            "remainingBytes": budget.remaining,
            "totalBytes": MAX_TOTAL_BYTES,
        },
    }
