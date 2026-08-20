"""Write-set enforcement and patch parsing — pure functions, no I/O."""

from __future__ import annotations

import pytest

from adlc.adapters.agents.copilot_sdk import (
    build_prompt,
    path_allowed,
    paths_in_patch,
    usable_write_set,
    violating_paths,
)
from adlc.ports import PROTECTED_PATHS

WRITE_SET = ["src/app.ts", "src/theme/**", "docs/*.md"]


@pytest.mark.parametrize(
    "candidate",
    ["src/app.ts", "src/theme/dark.ts", "src/theme/a/b/c.ts", "docs/guide.md"],
)
def test_declared_paths_are_allowed(candidate: str) -> None:
    assert path_allowed(candidate, WRITE_SET)


@pytest.mark.parametrize(
    "candidate",
    [
        "src/other.ts",          # sibling not declared
        "docs/nested/guide.md",  # `*` does not cross a separator
        "README.md",
        "../escape.ts",
        "src/../../escape.ts",
        "",
    ],
)
def test_undeclared_paths_are_refused(candidate: str) -> None:
    assert not path_allowed(candidate, WRITE_SET)


def test_windows_separators_are_normalized() -> None:
    assert path_allowed("src\\theme\\dark.ts", WRITE_SET)
    assert path_allowed("./src/app.ts", WRITE_SET)


def test_empty_write_set_allows_nothing() -> None:
    assert not path_allowed("src/app.ts", [])
    assert usable_write_set({"id": "T1"}) == []


@pytest.mark.parametrize(
    "candidate",
    [
        ".github/workflows/adlc.yml",
        ".adlc/config.yaml",
        "schemas/adlc-run.json",
        "docs/decisions/0004-choice.md",
        "pyproject.toml",
    ],
)
def test_protected_paths_win_over_a_permissive_write_set(candidate: str) -> None:
    """A graph that mistakenly declares a protected path still cannot write it (§4.8)."""
    assert not path_allowed(candidate, ["**"])
    assert not path_allowed(candidate, [candidate])


def test_protected_paths_constant_is_actually_used() -> None:
    assert ".github/**" in PROTECTED_PATHS
    assert violating_paths([".github/x.yml"], ["**"]) == [".github/x.yml"]


@pytest.mark.parametrize(
    "candidate",
    [".GitHub/workflows/x.yml", ".ADLC/config.yaml", "PyProject.TOML", "Schemas/run.json"],
)
def test_protected_paths_are_denied_case_insensitively(candidate: str) -> None:
    """`.GITHUB/` and `.github/` are the same directory on Windows and macOS."""
    assert not path_allowed(candidate, ["**"])


def test_violating_paths_is_sorted_and_deduplicated() -> None:
    violations = violating_paths(["z.txt", "a.txt", "z.txt", "src/app.ts"], WRITE_SET)
    assert violations == ["a.txt", "z.txt"]


PATCH = """diff --git a/src/app.ts b/src/app.ts
index 1111111..2222222 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1 +1,2 @@
 export const mount = () => {};
+export const theme = "dark";
diff --git a/src/theme/dark.ts b/src/theme/dark.ts
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/theme/dark.ts
@@ -0,0 +1 @@
+export default {};
"""


def test_paths_in_patch_reads_both_sides() -> None:
    assert paths_in_patch(PATCH) == ["src/app.ts", "src/theme/dark.ts"]


def test_paths_in_patch_ignores_dev_null() -> None:
    assert "dev/null" not in paths_in_patch(PATCH)


def test_paths_in_patch_on_non_diff_returns_nothing() -> None:
    assert paths_in_patch("this is not a patch at all") == []


def test_patch_touching_a_protected_path_is_a_violation() -> None:
    sneaky = PATCH + (
        "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n"
        "--- a/.github/workflows/x.yml\n"
        "+++ b/.github/workflows/x.yml\n"
    )
    assert violating_paths(paths_in_patch(sneaky), ["src/**", ".github/**"]) == [
        ".github/workflows/x.yml"
    ]


def test_prompt_states_the_write_set_and_the_protected_paths(node: dict) -> None:
    prompt = build_prompt(node)
    assert "src/app.ts" in prompt
    assert "src/theme.ts" in prompt
    for protected in PROTECTED_PATHS:
        assert protected in prompt
    assert "US1-AC1" in prompt
    assert "npm test" in prompt
    # The agent must not commit — the framework extracts the patch itself.
    assert "git commit" in prompt
