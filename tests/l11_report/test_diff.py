"""L11 — unified-diff parsing for the report's diff viewer.

The design commitment these tests protect is that **the browser never diffs
anything**. Everything a diff viewer normally computes on load -- line numbers,
add/delete classification, intra-line word highlights -- is computed here, once,
at render time, and shipped as a flat pre-computed array.

That makes the parser load-bearing in two directions. It has to be *correct*,
because nothing downstream can repair a mis-parsed hunk. And it has to be
*tolerant*, because the patches it reads are produced by agents: they may lack a
``diff --git`` header, carry commit prose, or stop mid-hunk. A report that
refuses to render because one patch was malformed has failed at its only job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adlc.report.diff import (
    MAX_FILES,
    MAX_LINE_CHARS,
    MAX_LINES_PER_FILE,
    collect_diffs,
    diff_stats,
    parse_unified,
)

SIMPLE = """diff --git a/src/theme.py b/src/theme.py
--- a/src/theme.py
+++ b/src/theme.py
@@ -1,4 +1,4 @@ def theme():
 header
-value = compute(a, b)
+value = compute(a, b, c)
 tail
"""


def lines(files: list, index: int = 0, hunk: int = 0) -> list[dict]:
    return files[index].to_json()["hunks"][hunk]["lines"]


class TestLineClassification:
    def test_a_simple_change_is_parsed_into_one_file_and_one_hunk(self) -> None:
        files = parse_unified(SIMPLE)
        assert len(files) == 1
        assert files[0].path == "src/theme.py"
        assert len(files[0].hunks) == 1

    def test_each_line_is_classified(self) -> None:
        assert [ln["type"] for ln in lines(parse_unified(SIMPLE))] == ["ctx", "del", "add", "ctx"]

    def test_the_marker_is_stripped_from_the_rendered_text(self) -> None:
        texts = [ln["text"] for ln in lines(parse_unified(SIMPLE))]
        assert texts[1] == "value = compute(a, b)"
        assert not any(t.startswith(("+", "-")) for t in texts)

    def test_both_gutters_are_numbered_from_the_hunk_header(self) -> None:
        got = [(ln["oldNo"], ln["newNo"]) for ln in lines(parse_unified(SIMPLE))]
        assert got == [(1, 1), (2, None), (None, 2), (3, 3)]

    def test_counts_are_tallied(self) -> None:
        assert (parse_unified(SIMPLE)[0].additions, parse_unified(SIMPLE)[0].deletions) == (1, 1)

    def test_the_hunk_section_heading_is_kept(self) -> None:
        assert parse_unified(SIMPLE)[0].hunks[0]["section"] == "def theme():"

    def test_a_no_newline_marker_is_not_rendered_as_a_line(self) -> None:
        patch = SIMPLE + "\\ No newline at end of file\n"
        assert len(lines(parse_unified(patch))) == 4


class TestWordLevelHighlights:
    def test_only_the_changed_substring_is_marked(self) -> None:
        added = lines(parse_unified(SIMPLE))[2]
        start, end = added["segs"][0]
        assert added["text"][start:end] == ", c"

    def test_a_pure_insertion_leaves_the_removed_line_unmarked(self) -> None:
        removed = lines(parse_unified(SIMPLE))[1]
        assert "segs" not in removed, "nothing was removed from that line"

    def test_an_indentation_only_change_is_still_highlighted(self) -> None:
        """The change a reviewer most often misses must not be the one we hide."""
        patch = (
            "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n"
            "-    value = 1\n"
            "+        value = 1\n"
        )
        assert "segs" in lines(parse_unified(patch))[1]

    def test_unpaired_lines_get_no_spans(self) -> None:
        patch = "--- a/x.py\n+++ b/x.py\n@@ -1,0 +1,2 @@\n+one\n+two\n"
        assert all("segs" not in ln for ln in lines(parse_unified(patch)))


class TestFileStatus:
    def test_an_added_file_is_recognised(self) -> None:
        patch = (
            "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+hello\n"
        )
        assert parse_unified(patch)[0].status == "added"

    def test_a_deleted_file_is_recognised(self) -> None:
        patch = (
            "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n"
            "--- a/old.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-bye\n"
        )
        assert parse_unified(patch)[0].status == "deleted"

    def test_a_rename_keeps_both_paths(self) -> None:
        patch = (
            "diff --git a/a.txt b/b.txt\nrename from a.txt\nrename to b.txt\n"
            "--- a/a.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        parsed = parse_unified(patch)[0]
        assert (parsed.status, parsed.old_path, parsed.path) == ("renamed", "a.txt", "b.txt")

    def test_a_binary_file_is_flagged_and_kept(self) -> None:
        patch = (
            "diff --git a/img.png b/img.png\n"
            "Binary files a/img.png and b/img.png differ\n"
        )
        parsed = parse_unified(patch)
        assert len(parsed) == 1, "a binary change is still a change worth listing"
        assert parsed[0].binary is True


class TestTolerance:
    """Agent-produced patches are messy; none of this is worth failing a report over."""

    def test_a_patch_without_a_diff_git_header_still_parses(self) -> None:
        patch = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        assert parse_unified(patch)[0].path == "x.py"

    def test_commit_prose_above_the_first_file_is_ignored(self) -> None:
        assert len(parse_unified("Subject: fix the thing\n\nSome prose.\n\n" + SIMPLE)) == 1

    def test_a_patch_truncated_mid_hunk_keeps_what_it_had(self) -> None:
        patch = "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,9 @@\n ctx\n-gone\n"
        assert [ln["type"] for ln in lines(parse_unified(patch))] == ["ctx", "del"]

    @pytest.mark.parametrize("text", ["", "   ", "not a diff at all"])
    def test_junk_yields_no_files_rather_than_an_error(self, text: str) -> None:
        assert parse_unified(text) == []

    def test_a_header_with_no_hunks_is_not_reported_as_a_change(self) -> None:
        assert parse_unified("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n") == []


class TestBounds:
    """A vendored bundle must degrade the viewer, never the report."""

    def test_a_file_past_the_line_budget_is_marked_truncated(self) -> None:
        body = "".join(f"+line {i}\n" for i in range(MAX_LINES_PER_FILE + 50))
        parsed = parse_unified(f"--- a/big.js\n+++ b/big.js\n@@ -0,0 +1,{MAX_LINES_PER_FILE + 50} @@\n{body}")
        assert parsed[0].truncated is True
        assert sum(len(h["lines"]) for h in parsed[0].hunks) == MAX_LINES_PER_FILE

    def test_a_patch_past_the_file_budget_stops_collecting(self) -> None:
        patch = "".join(
            f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            for i in range(MAX_FILES + 10)
        )
        assert len(parse_unified(patch)) <= MAX_FILES

    def test_a_minified_line_is_clipped_with_an_explicit_marker(self) -> None:
        long_line = "x" * (MAX_LINE_CHARS + 500)
        parsed = parse_unified(f"--- a/b.js\n+++ b/b.js\n@@ -0,0 +1,1 @@\n+{long_line}\n")
        text = lines(parsed)[0]["text"]
        assert len(text) < len(long_line)
        assert "line truncated by adlc report" in text


class TestStats:
    def test_stats_sum_across_files(self) -> None:
        assert diff_stats(parse_unified(SIMPLE)) == {"files": 1, "additions": 1, "deletions": 1}

    def test_stats_of_nothing_are_zero_not_an_error(self) -> None:
        assert diff_stats([]) == {"files": 0, "additions": 0, "deletions": 0}


class TestCollectDiffs:
    def test_a_missing_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert collect_diffs(tmp_path / "nope") == []

    def test_patches_are_keyed_by_task_id(self, tmp_path: Path) -> None:
        (tmp_path / "T002.patch").write_text(SIMPLE, encoding="utf-8")
        (tmp_path / "T001.patch").write_text(SIMPLE, encoding="utf-8")
        collected = collect_diffs(tmp_path)
        assert [d["taskId"] for d in collected] == ["T001", "T002"], "sorted for a stable report"
        assert collected[0]["stats"]["additions"] == 1

    def test_a_non_patch_file_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
        assert collect_diffs(tmp_path) == []

    def test_undecodable_bytes_do_not_break_the_report(self, tmp_path: Path) -> None:
        (tmp_path / "T001.patch").write_bytes(b"--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-\xff\xfe\n+ok\n")
        collected = collect_diffs(tmp_path)
        assert len(collected) == 1
        assert collected[0]["stats"]["additions"] == 1
