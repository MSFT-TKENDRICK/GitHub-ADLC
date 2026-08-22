"""A workflow that GitHub refuses to compile fails in the worst possible way.

`adlc-feedback.yml` shipped with ``PACK_FILE: ${{ runner.temp }}/...`` in a
job-level ``env:`` block. That is valid YAML, so every parser in this repo -- and
every test in `test_ci_authority.py`, which reads the file with ``yaml.safe_load``
-- accepted it. GitHub did not. ``jobs.<job_id>.env`` may only reference the
github, needs, strategy, matrix, vars, secrets and inputs contexts; ``runner`` is
resolved per-step and is not among them.

The failure mode is what makes this worth a dedicated guard. GitHub does not fail
the offending job, because it never creates one: it creates a workflow *run* with
zero jobs, reports "Workflow did not create any check runs", and titles the run
with the file path instead of the workflow's ``name:``. There is no log to
download and ``gh run view --log-failed`` returns "log not found", so the
observable evidence points at everything except the actual cause. A reviewer
reading only the red X will conclude the environment is misconfigured.

So: parse every workflow, walk every expression, and assert that each one only
names a context that is legal where it appears. This is a compile check we can
run before pushing, which is the only place it is cheap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# https://docs.github.com/actions/writing-workflows/choosing-what-your-workflow-does/accessing-contextual-information-about-workflow-runs
WORKFLOW_ENV_CONTEXTS = frozenset({"github", "vars", "inputs", "secrets"})
JOB_ENV_CONTEXTS = frozenset(
    {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs"}
)

_EXPRESSION_RE = re.compile(r"\$\{\{(.+?)\}\}", re.DOTALL)
# GitHub expression string literals are single-quoted ('' escapes a quote).
# Their contents are data, not references, so they are removed before scanning.
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
# The head of a dotted reference: an identifier not preceded by a name character,
# a dot or a hyphen (so `run-id` in `steps.x.outputs.run-id` is not a head), and
# not immediately followed by `(` (so `always()` and `format(...)` are excluded).
_CONTEXT_RE = re.compile(r"(?<![\w.\-])([a-zA-Z_][a-zA-Z0-9_-]*)(?!\s*\()")

# Operators and literals that appear bare inside expressions and are not contexts.
_NOT_CONTEXTS = frozenset({"true", "false", "null", "and", "or", "not"})

# Every context GitHub defines. A leading name outside this set is an "undefined
# variable" and, like an illegal context, kills the whole file at compile time.
ALL_CONTEXTS = frozenset(
    {
        "env",
        "github",
        "inputs",
        "job",
        "matrix",
        "needs",
        "runner",
        "secrets",
        "steps",
        "strategy",
        "vars",
        # Only legal in `on.workflow_call.outputs.<name>.value`, but this check is
        # about undefined names rather than placement, so it is allowed globally.
        "jobs",
    }
)


def _workflows() -> list[Path]:
    return sorted(p for p in WORKFLOW_DIR.glob("*.yml") if p.is_file())


def _handwritten_workflows() -> list[Path]:
    """Workflows authored in this repo, excluding gh-aw `.lock.yml` output.

    Lock files are machine-generated from the sibling `.md` recipes and are
    regenerated wholesale, so a finding in one is not actionable here.
    """
    return [p for p in _workflows() if not p.name.endswith(".lock.yml")]


def _contexts_in(value: object) -> set[str]:
    """Every context name referenced by any ``${{ }}`` expression in ``value``."""
    if not isinstance(value, str):
        return set()
    names: set[str] = set()
    for expression in _EXPRESSION_RE.findall(value):
        expression = _STRING_LITERAL_RE.sub("''", expression)
        for name in _CONTEXT_RE.findall(expression):
            if name not in _NOT_CONTEXTS:
                names.add(name)
    return names


def _env_block(owner: object) -> dict[str, object]:
    if not isinstance(owner, dict):
        return {}
    env = owner.get("env")
    return env if isinstance(env, dict) else {}


def _walk_strings(node: object, path: str = "") -> list[tuple[str, str]]:
    """Every string leaf in the document, with a dotted path for the message."""
    if isinstance(node, str):
        return [(path, node)]
    if isinstance(node, dict):
        found: list[tuple[str, str]] = []
        for key, value in node.items():
            found.extend(_walk_strings(value, f"{path}.{key}" if path else str(key)))
        return found
    if isinstance(node, list):
        found = []
        for index, value in enumerate(node):
            found.extend(_walk_strings(value, f"{path}[{index}]"))
        return found
    return []


@pytest.mark.parametrize("path", _handwritten_workflows(), ids=lambda p: p.name)
def test_every_expression_names_a_real_context(path: Path) -> None:
    """A `${{ }}` inside a `script:` block is expanded, comment syntax or not.

    `adlc-feedback.yml` carried an *illustrative* `${{ A && B || C }}` inside a
    JavaScript `//` comment in an `actions/github-script` block, explaining why
    that idiom was being avoided. GitHub expands expressions across the whole
    scalar before Node ever parses it, so `A`, `B` and `C` were undefined
    variables and the file did not compile. A comment is not a hiding place.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    violations = []
    for where, text in _walk_strings(document):
        illegal = _contexts_in(text) - ALL_CONTEXTS
        if illegal:
            violations.append(f"{where} references undefined {sorted(illegal)}")

    assert not violations, (
        f"{path.name} would be rejected by GitHub at compile time:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_workflow_is_parseable(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    assert isinstance(document.get("jobs"), dict), f"{path.name} declares no jobs"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_env_blocks_only_reference_legal_contexts(path: Path) -> None:
    """`runner` in a job-level `env:` makes GitHub reject the entire file."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    violations: list[str] = []

    for key, value in _env_block(document).items():
        illegal = _contexts_in(value) - WORKFLOW_ENV_CONTEXTS
        if illegal:
            violations.append(
                f"workflow-level env.{key} references {sorted(illegal)}; "
                f"only {sorted(WORKFLOW_ENV_CONTEXTS)} are available there"
            )

    for job_id, job in (document.get("jobs") or {}).items():
        for key, value in _env_block(job).items():
            illegal = _contexts_in(value) - JOB_ENV_CONTEXTS
            if illegal:
                violations.append(
                    f"jobs.{job_id}.env.{key} references {sorted(illegal)}; "
                    f"only {sorted(JOB_ENV_CONTEXTS)} are available there. "
                    "Move it to the step that needs it, or export it via $GITHUB_ENV."
                )

    assert not violations, (
        f"{path.name} would be rejected by GitHub at compile time, producing a "
        "run with zero jobs and no readable log:\n  " + "\n  ".join(violations)
    )


def test_the_feedback_workflow_binds_the_pack_path_at_step_scope() -> None:
    """The specific regression: PACK_FILE must not live in the job `env:`."""
    document = yaml.safe_load(
        (WORKFLOW_DIR / "adlc-feedback.yml").read_text(encoding="utf-8")
    )
    apply_job = document["jobs"]["apply"]
    assert "PACK_FILE" not in _env_block(apply_job)

    extract = next(
        step for step in apply_job["steps"] if step.get("id") == "extract"
    )
    assert "runner.temp" in str(_env_block(extract).get("PACK_FILE", "")), (
        "the extract step must bind PACK_FILE itself, where `runner` is in scope"
    )
    # Downstream steps consume $PACK_FILE from the shell, so it has to be exported.
    assert "PACK_FILE=" in extract["run"], (
        "extract must export PACK_FILE through $GITHUB_ENV for later steps"
    )
