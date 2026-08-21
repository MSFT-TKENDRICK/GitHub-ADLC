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
# The leading identifier of each dotted reference inside an expression. Function
# calls (`always()`, `format(...)`) and literals are filtered out by requiring the
# name to not be immediately followed by `(`.
_CONTEXT_RE = re.compile(r"(?<![\w.'\"])([a-zA-Z_][a-zA-Z0-9_-]*)(?![\w(])")

# Operators and literals that appear bare inside expressions and are not contexts.
_NOT_CONTEXTS = frozenset({"true", "false", "null", "and", "or", "not"})


def _workflows() -> list[Path]:
    return sorted(p for p in WORKFLOW_DIR.glob("*.yml") if p.is_file())


def _contexts_in(value: object) -> set[str]:
    """Every context name referenced by any ``${{ }}`` expression in ``value``."""
    if not isinstance(value, str):
        return set()
    names: set[str] = set()
    for expression in _EXPRESSION_RE.findall(value):
        for name in _CONTEXT_RE.findall(expression):
            if name not in _NOT_CONTEXTS:
                names.add(name)
    return names


def _env_block(owner: object) -> dict[str, object]:
    if not isinstance(owner, dict):
        return {}
    env = owner.get("env")
    return env if isinstance(env, dict) else {}


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
