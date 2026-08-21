"""Unified-diff parsing for the evidence report.

The report renders textual diffs the way every developer already reads them --
GitHub / ``diff2html`` conventions: per-file cards, hunk headers, line numbers
in two gutters, unified or side-by-side, with intra-line word highlights.

The speed decision is that **the browser never diffs anything**. Diffing is an
O(n*m) problem and doing it in JavaScript on load is what makes web diff viewers
feel slow. Here the patches already exist on disk in unified form, so this
module parses them once, at report-render time, and computes the word-level
highlights in Python with :mod:`difflib`. What ships to the page is a flat,
pre-computed line array: rendering is a linear walk that builds DOM nodes and
nothing else. Files stay collapsed until opened, so a 40-file change costs one
row per file until the reader asks for one.

Everything here is bounded. A generated lockfile or a vendored bundle can be
megabytes of diff, and embedding that would defeat the single-file design. Files
past :data:`MAX_LINES_PER_FILE` are truncated with an explicit marker, and the
untruncated patch is always still linked as a hash-verified artifact.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_FILES",
    "MAX_LINES_PER_FILE",
    "FileDiff",
    "collect_diffs",
    "diff_stats",
    "parse_unified",
]

#: Per-file line budget. Beyond this the file is marked truncated.
MAX_LINES_PER_FILE = 900

#: Per-patch file budget.
MAX_FILES = 60

#: Longest single line kept verbatim. Minified bundles have 200 KB lines.
MAX_LINE_CHARS = 2000

_DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)\s*$")
_OLD_FILE = re.compile(r"^--- (?:a/)?(?P<path>.+?)\s*$")
_NEW_FILE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+?)\s*$")
_HUNK = re.compile(
    r"^@@ -(?P<oldStart>\d+)(?:,(?P<oldLines>\d+))? "
    r"\+(?P<newStart>\d+)(?:,(?P<newLines>\d+))? @@(?P<section>.*)$"
)
_WORD = re.compile(r"\w+|\s+|[^\w\s]")


@dataclass
class FileDiff:
    """One file's worth of unified diff, ready to render."""

    path: str
    old_path: str = ""
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    truncated: bool = False
    hunks: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "oldPath": self.old_path or self.path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "truncated": self.truncated,
            "hunks": self.hunks,
        }


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text)


def _offsets(tokens: list[str]) -> list[int]:
    out = [0]
    for token in tokens:
        out.append(out[-1] + len(token))
    return out


def _merge(spans: list[list[int]]) -> list[list[int]]:
    if not spans:
        return []
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _word_spans(old: str, new: str) -> tuple[list[list[int]], list[list[int]]]:
    """Character ranges that differ between a paired removed/added line.

    Returned as ``[[start, end], ...]`` offsets into each line, so the renderer
    only has to wrap substrings and never has to know how they were derived.
    Whitespace-only differences are reported too, because a change that is only
    indentation is exactly the change a reviewer most often misses.
    """
    if not old or not new:
        return [], []

    old_tokens, new_tokens = _tokens(old), _tokens(new)
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    old_offsets, new_offsets = _offsets(old_tokens), _offsets(new_tokens)
    old_spans: list[list[int]] = []
    new_spans: list[list[int]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            old_spans.append([old_offsets[i1], old_offsets[i2]])
        if j2 > j1:
            new_spans.append([new_offsets[j1], new_offsets[j2]])

    # A line rewritten end to end yields one span covering everything, which
    # highlights the whole row and tells the reader nothing. Drop it.
    if len(old_spans) == 1 and old_spans[0] == [0, len(old)]:
        old_spans = []
    if len(new_spans) == 1 and new_spans[0] == [0, len(new)]:
        new_spans = []
    return _merge(old_spans), _merge(new_spans)


def _pair_block(removed: list[dict[str, Any]], added: list[dict[str, Any]]) -> None:
    """Attach word-level spans to a removed/added run, pairing by position.

    Pairing positionally rather than by similarity is deliberate: it is what
    every mainstream diff viewer does, it is linear, and it matches how the
    lines were written. A cleverer pairing would sometimes be prettier and would
    always be harder to predict.
    """
    for old_line, new_line in zip(removed, added, strict=False):
        old_spans, new_spans = _word_spans(old_line["text"], new_line["text"])
        if old_spans:
            old_line["segs"] = old_spans
        if new_spans:
            new_line["segs"] = new_spans


def _clip(text: str) -> str:
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS] + " ...[line truncated by adlc report]"


def parse_unified(text: str) -> list[FileDiff]:
    """Parse a unified diff (``git diff`` or a ``format-patch`` body).

    Tolerant by design: a patch produced by an agent may omit the ``diff --git``
    header, may carry commit-message prose above the first file, and may end
    mid-hunk. None of that is worth failing a whole report over, so anything
    unrecognised is skipped rather than raised.
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: dict[str, Any] | None = None
    removed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    old_no = new_no = 0
    lines_kept = 0

    def close_block() -> None:
        nonlocal removed, added
        if removed and added:
            _pair_block(removed, added)
        removed, added = [], []

    def close_hunk() -> None:
        nonlocal hunk
        close_block()
        if current is not None and hunk is not None and hunk["lines"]:
            current.hunks.append(hunk)
        hunk = None

    def close_file() -> None:
        nonlocal current, lines_kept
        close_hunk()
        if current is not None and (current.hunks or current.binary):
            files.append(current)
        current = None
        lines_kept = 0

    for raw in (text or "").splitlines():
        header = _DIFF_GIT.match(raw)
        if header:
            close_file()
            if len(files) >= MAX_FILES:
                break
            current = FileDiff(path=header.group("b"), old_path=header.group("a"))
            continue

        old_match = _OLD_FILE.match(raw) if raw.startswith("--- ") else None
        new_match = _NEW_FILE.match(raw) if raw.startswith("+++ ") else None

        if current is None:
            # A bare `--- a/x` with no `diff --git` above it still starts a file.
            if old_match:
                if len(files) >= MAX_FILES:
                    break
                path = old_match.group("path")
                current = FileDiff(path="", old_path="" if path == "/dev/null" else path)
                if path == "/dev/null":
                    current.status = "added"
            continue

        if raw.startswith("new file mode"):
            current.status = "added"
            continue
        if raw.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if raw.startswith("rename from "):
            current.old_path = raw[len("rename from "):].strip()
            current.status = "renamed"
            continue
        if raw.startswith("rename to "):
            current.path = raw[len("rename to "):].strip()
            current.status = "renamed"
            continue
        if raw.startswith(("Binary files", "GIT binary patch")):
            current.binary = True
            continue

        if old_match:
            path = old_match.group("path")
            if path == "/dev/null":
                current.status = "added"
            elif not current.old_path:
                current.old_path = path
            continue
        if new_match:
            path = new_match.group("path")
            if path == "/dev/null":
                current.status = "deleted"
            elif not current.path:
                current.path = path
            continue

        hunk_match = _HUNK.match(raw)
        if hunk_match:
            close_hunk()
            old_no = int(hunk_match.group("oldStart"))
            new_no = int(hunk_match.group("newStart"))
            hunk = {
                "header": raw.rstrip(),
                "section": hunk_match.group("section").strip(),
                "oldStart": old_no,
                "newStart": new_no,
                "lines": [],
            }
            continue

        if hunk is None:
            continue
        if lines_kept >= MAX_LINES_PER_FILE:
            current.truncated = True
            continue

        marker, body = (raw[:1], raw[1:]) if raw else (" ", "")
        if marker == "\\":  # "\ No newline at end of file"
            continue
        if marker == "-":
            entry = {"type": "del", "oldNo": old_no, "newNo": None, "text": _clip(body)}
            hunk["lines"].append(entry)
            removed.append(entry)
            current.deletions += 1
            old_no += 1
        elif marker == "+":
            entry = {"type": "add", "oldNo": None, "newNo": new_no, "text": _clip(body)}
            hunk["lines"].append(entry)
            added.append(entry)
            current.additions += 1
            new_no += 1
        else:
            close_block()
            hunk["lines"].append(
                {"type": "ctx", "oldNo": old_no, "newNo": new_no, "text": _clip(body)}
            )
            old_no += 1
            new_no += 1
        lines_kept += 1

    close_file()
    for item in files:
        if not item.path:
            item.path = item.old_path or "(unknown)"
    return files


def diff_stats(files: list[FileDiff]) -> dict[str, int]:
    return {
        "files": len(files),
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
    }


def collect_diffs(patches_dir: Path) -> list[dict[str, Any]]:
    """Parse every ``patches/<task-id>.patch`` into a renderable diff set.

    Keyed by task id so the task-graph detail pane can show exactly the change
    one node produced -- which is the question a reader of a graph actually has.
    An unreadable patch is reported as an empty diff *with a reason* rather than
    silently omitted: a missing diff and a broken diff must not look alike.
    """
    out: list[dict[str, Any]] = []
    if not patches_dir.is_dir():
        return out
    for path in sorted(patches_dir.glob("*.patch")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append({
                "taskId": path.stem,
                "source": path.name,
                "bytes": 0,
                "error": f"unreadable: {exc}",
                "files": [],
                "stats": {"files": 0, "additions": 0, "deletions": 0},
            })
            continue
        files = parse_unified(text)
        out.append({
            "taskId": path.stem,
            "source": path.name,
            "bytes": len(text.encode("utf-8", errors="replace")),
            "error": "",
            "files": [f.to_json() for f in files],
            "stats": diff_stats(files),
        })
    return out
