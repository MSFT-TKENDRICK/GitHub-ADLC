"""Property-based (fuzz) tests for the executor's patch-safety surfaces.

`normalise_patch` and `violated_write_set` are the two functions standing
between an agent-authored patch and `git apply` / the write-set conflict
guard -- exactly the surface prior fleet work flagged as a source of real
bugs (quoted-header and rename-metadata bypasses). Hypothesis is already a
project dependency but was not previously used anywhere; these are the
first fuzz tests in the suite.

Properties asserted, not examples:
* `normalise_patch` never raises on arbitrary bytes, is idempotent, never
  reintroduces a `\\r\\n`, and always ends with exactly one trailing `\\n`
  (when non-empty).
* `violated_write_set` never raises on arbitrary patch text and arbitrary
  write-set globs, always returns paths sorted and deduplicated, and a path
  explicitly present in the write-set (as a literal, not a glob) is never
  reported as a violation.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from adlc.executor import normalise_patch, violated_write_set

# ---------------------------------------------------------------------------
# normalise_patch
# ---------------------------------------------------------------------------


@given(st.binary(max_size=2000))
@settings(max_examples=300)
def test_normalise_patch_never_raises_on_arbitrary_bytes(data: bytes) -> None:
    normalise_patch(data)


@given(st.binary(max_size=2000))
@settings(max_examples=300)
def test_normalise_patch_is_idempotent(data: bytes) -> None:
    once = normalise_patch(data)
    twice = normalise_patch(once)
    assert once == twice


@given(st.binary(max_size=2000))
@settings(max_examples=300)
def test_normalise_patch_never_leaves_a_crlf(data: bytes) -> None:
    assert b"\r\n" not in normalise_patch(data)


@given(st.binary(min_size=1, max_size=2000))
@settings(max_examples=300)
def test_normalise_patch_always_ends_with_exactly_one_trailing_newline(data: bytes) -> None:
    result = normalise_patch(data)
    assert result.endswith(b"\n")
    assert not result.endswith(b"\n\n") or data.replace(b"\r\n", b"\n").endswith(b"\n\n")


def test_normalise_patch_empty_input_stays_empty() -> None:
    assert normalise_patch(b"") == b""


# ---------------------------------------------------------------------------
# violated_write_set
# ---------------------------------------------------------------------------

_patch_line = st.one_of(
    st.text(min_size=0, max_size=80),
    st.just(""),
    st.builds(lambda p: f"+++ b/{p}", st.text(min_size=0, max_size=60)),
    st.builds(lambda p: f"--- a/{p}", st.text(min_size=0, max_size=60)),
)


@given(st.lists(_patch_line, max_size=40), st.lists(st.text(min_size=1, max_size=30), max_size=10))
@settings(max_examples=300)
def test_violated_write_set_never_raises(lines: list[str], write_set: list[str]) -> None:
    violated_write_set("\n".join(lines), write_set)


@given(st.lists(_patch_line, max_size=40), st.lists(st.text(min_size=1, max_size=30), max_size=10))
@settings(max_examples=200)
def test_violated_write_set_result_is_sorted_and_deduplicated(
    lines: list[str], write_set: list[str]
) -> None:
    result = violated_write_set("\n".join(lines), write_set)
    assert result == sorted(set(result))


@given(st.text(alphabet=st.characters(blacklist_characters="\n\r"), min_size=1, max_size=40))
@settings(max_examples=200)
def test_a_path_declared_literally_in_the_write_set_is_never_a_violation(path: str) -> None:
    patch = f"+++ b/{path}\n"
    assert path not in violated_write_set(patch, [path])


def test_violated_write_set_accepts_bytes_input_too() -> None:
    patch = b"+++ b/src/outside.py\n"
    assert violated_write_set(patch, ["src/allowed.py"]) == ["src/outside.py"]


def test_violated_write_set_ignores_dev_null_source_lines() -> None:
    """`--- /dev/null` marks a newly-created file; it is not a 'touched path'."""
    patch = "--- /dev/null\n+++ b/src/new_file.py\n"
    assert violated_write_set(patch, []) == ["src/new_file.py"]


def test_violated_write_set_glob_pattern_in_write_set_matches() -> None:
    patch = "+++ b/src/pkg/module.py\n"
    assert violated_write_set(patch, ["src/pkg/*.py"]) == []
