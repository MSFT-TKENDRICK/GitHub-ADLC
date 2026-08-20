"""``AgentRunner`` backed by the **GitHub Copilot SDK** (``github-copilot-sdk``).

This module also hosts the shared execution core used by all three L1 runners
(:mod:`adlc.adapters.agents.copilot_sdk`, :mod:`adlc.adapters.agents.agent_task`
and :mod:`adlc.adapters.agents.gh_aw`).

Why the shared core lives here rather than in a ``_common.py``: workstream L1
owns exactly three modules (``docs/PLAN.md`` §6) and may not add files to the
package, so one of the three has to carry the shared code. Nothing in this
module imports the Copilot SDK at import time -- the SDK is imported lazily
inside :meth:`CopilotSdkRunner.run_task` -- so importing these helpers from the
sibling runners is free and cannot fail because of a missing optional
dependency.

Contract (``docs/PLAN.md`` §4.4):

* a task node executes in an **isolated git worktree** checked out at ``baseSha``;
* the output is a patch at ``patches/<task-id>.patch`` **anchored to that exact
  SHA**;
* a runner MUST NOT write outside ``node['writeSet']`` -- and never inside
  :data:`adlc.ports.PROTECTED_PATHS`;
* the runner reports token/cost accounting only when the backend actually
  reports it. Inventing numbers would be failing silently.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.ports import PROTECTED_PATHS, TaskNode, TaskOutcome
from adlc.runs import RunDir

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = [
    "CopilotSdkRunner",
    "GitResult",
    "PatchResult",
    "apply_patch",
    "build_prompt",
    "changed_paths",
    "collect_usage",
    "enumerate_patch_paths",
    "fail",
    "finalize_patch",
    "find_token",
    "patch_from_range",
    "path_allowed",
    "paths_in_patch",
    "resolve_patch_path",
    "revert_paths",
    "run_git",
    "sha_of",
    "task_timeout",
    "usable_write_set",
    "violating_paths",
    "write_patch_text",
]

#: Environment variables that can carry a GitHub / Copilot credential.
TOKEN_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_CLI_TOKEN")

#: Default wall-clock budget for one task, in seconds. Override with
#: ``limits.taskTimeoutSeconds`` in ``.adlc/config.yaml``.
DEFAULT_TASK_TIMEOUT = 1800

#: Default budget for a single git invocation, in seconds.
GIT_TIMEOUT = 120

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Write-set enforcement
# ---------------------------------------------------------------------------


def _normalize(rel: str) -> str:
    """Repo-relative, forward-slashed. Keeps leading dots (``.github/**``)."""
    rel = rel.replace("\\", "/").strip().strip('"')
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a git-style path glob into an anchored regex.

    ``**`` crosses directory separators, ``*`` and ``?`` do not.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches(rel: str, pattern: str) -> bool:
    pattern = _normalize(pattern)
    if not pattern:
        return False
    if any(c in pattern for c in "*?"):
        return _glob_to_regex(pattern).match(rel) is not None
    # A bare path matches itself, and a bare directory matches everything under it.
    return rel == pattern or rel.startswith(pattern.rstrip("/") + "/")


def _denied(rel: str, deny: tuple[str, ...]) -> bool:
    """Deny matching is case-insensitive.

    ``.GITHUB/workflows/x.yml`` and ``.github/workflows/x.yml`` are the same
    file on Windows and on a default macOS volume, so a case variation must not
    slip past :data:`adlc.ports.PROTECTED_PATHS`. Over-denying is the safe
    direction; the allow list stays case-sensitive so a mis-cased path is still
    refused rather than silently accepted.
    """
    lowered = rel.lower()
    return any(_matches(lowered, pattern.lower()) for pattern in deny)


def usable_write_set(node: TaskNode) -> list[str]:
    """Return the node's declared write set, normalized. Never ``None``."""
    return [_normalize(p) for p in (node.get("writeSet") or []) if str(p).strip()]


def path_allowed(rel: str, write_set: list[str], deny: tuple[str, ...] = PROTECTED_PATHS) -> bool:
    """Is ``rel`` writable by a task declaring ``write_set``?

    ``deny`` always wins: :data:`adlc.ports.PROTECTED_PATHS` may never be
    written by an agent, even if a graph mistakenly declares one (§4.8).
    An **empty** write set allows nothing -- fail closed.
    """
    rel = _normalize(rel)
    if not rel or ".." in Path(rel).parts:
        return False
    if _denied(rel, deny):
        return False
    return any(_matches(rel, pattern) for pattern in write_set)


def violating_paths(paths: list[str], write_set: list[str]) -> list[str]:
    """Paths that a task was not permitted to touch, sorted and de-duplicated."""
    return sorted({_normalize(p) for p in paths if not path_allowed(p, write_set)})


_DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")
_DIFF_SIDE = re.compile(r"^(?:\+\+\+|---) [ab]/(?P<p>.+?)(?:\t.*)?$")


def paths_in_patch(text: str) -> list[str]:
    """Best-effort scan of a unified diff, for **logging only**.

    .. warning::
       This must never be the write-set boundary. A hand-written diff parser
       cannot see C-quoted headers (``diff --git "a/…" "b/…"``, emitted for any
       path with a non-ASCII byte, quote or backslash) or ``rename from`` /
       ``rename to`` records, both of which ``git apply`` honours. A patch from
       an untrusted source can therefore touch paths this function does not
       report. Enforcement uses :func:`enumerate_patch_paths` and
       :func:`changed_paths`, which let git do the parsing.
    """
    found: set[str] = set()
    for line in text.splitlines():
        if match := _DIFF_GIT.match(line):
            found.add(_normalize(match.group("a")))
            found.add(_normalize(match.group("b")))
        elif line.startswith(("+++ ", "--- ")) and (match := _DIFF_SIDE.match(line)):
            found.add(_normalize(match.group("p")))
    found.discard("dev/null")
    return sorted(found)


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    code: int
    out: bytes
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", errors="replace")


async def run_git(
    worktree: Path,
    *args: str,
    timeout: float = GIT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> GitResult:
    """Run one git command inside ``worktree``. Never raises; times out."""
    proc_env = dict(os.environ)
    # Never let git block on an interactive credential or GPG prompt.
    proc_env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    if env:
        proc_env.update(env)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.quotepath=false",
            "-C",
            str(worktree),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
    except (OSError, ValueError) as exc:
        return GitResult(127, b"", f"git could not be started: {exc}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with suppress(Exception):
            proc.kill()
        return GitResult(124, b"", f"git {' '.join(args)} timed out after {timeout}s")
    return GitResult(proc.returncode or 0, out, err.decode("utf-8", errors="replace").strip())


async def sha_of(worktree: Path, rev: str = "HEAD") -> str | None:
    """Resolve ``rev`` inside ``worktree``. Returns ``None`` when unresolvable."""
    result = await run_git(worktree, "rev-parse", "--verify", f"{rev}^{{commit}}")
    return result.text.strip() if result.ok else None


async def changed_paths(worktree: Path) -> list[str] | None:
    """Every path git reports as changed, including untracked files.

    Returns ``None`` when the probe itself failed (git missing, timed out,
    not a repository). Callers **must** treat ``None`` as "unknown" and fail
    closed: conflating it with "nothing changed" would silently disable
    write-set enforcement.

    ``.gitignore``d files are deliberately not reported: build output and
    dependency trees are not agent authorship and must not trip enforcement.
    Both sides of a rename or copy are reported.
    """
    result = await run_git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not result.ok:
        return None
    fields = result.text.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        paths.append(_normalize(path))
        # In -z mode a rename/copy emits the source path as the next field.
        if ("R" in xy or "C" in xy) and i < len(fields):
            paths.append(_normalize(fields[i]))
            i += 1
    return paths


async def enumerate_patch_paths(worktree: Path, patch: Path) -> list[str] | None:
    """Ask **git** which paths a patch touches, without applying it for real.

    A hand-written diff parser is not safe here: ``git apply`` honours C-quoted
    headers and ``rename from`` / ``rename to`` records, so a patch from an
    untrusted producer (a gh-aw artifact) can touch paths that regex scanning
    never sees.

    Nor is git's *human-readable* output safe. ``git apply --summary`` prints
    renames in the brace-compressed form ``docs/{decisions/0001-adr.md =>
    guide.md}``, which splits on ``" => "`` into two paths that do not exist --
    and the mangled source ``docs/{decisions/0001-adr.md`` matches ``docs/**``
    while evading ``docs/decisions/**``, laundering a protected path into an
    allowed one.

    So nothing is parsed from a rendered string. The patch is applied to a
    **scratch index** (``GIT_INDEX_FILE``, never the worktree or the real
    index) and ``git diff-index --no-renames`` reports the result structurally:
    NUL-separated, unquoted, both sides of every rename. As a bonus this also
    proves the patch applies at ``HEAD``.

    Returns ``None`` when git cannot parse or apply the patch -- fail closed.
    """
    scratch = Path(tempfile.mkdtemp(prefix="adlc-index-"))
    env = {"GIT_INDEX_FILE": str(scratch / "index")}
    try:
        for args in (
            ("read-tree", "HEAD"),
            ("apply", "--cached", "--whitespace=nowarn", str(patch)),
        ):
            step = await run_git(worktree, *args, env=env)
            if not step.ok:
                return None
        listed = await run_git(
            worktree, "diff-index", "--cached", "--name-only", "-z", "--no-renames", "HEAD",
            env=env,
        )
        if not listed.ok:
            return None
        return sorted({_normalize(p) for p in listed.text.split("\0") if p.strip()})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def revert_paths(worktree: Path, paths: list[str], base_sha: str) -> None:
    """Undo out-of-write-set edits so the refusal is actually enforced."""
    for path in paths:
        restored = await run_git(
            worktree, "restore", "--source", base_sha, "--staged", "--worktree", "--", path
        )
        if restored.ok:
            continue
        # The path does not exist at base_sha: it is a file the agent created.
        await run_git(worktree, "rm", "--cached", "--force", "--quiet", "--", path)
        target = worktree / path
        with suppress(OSError):
            if target.is_file() or target.is_symlink():
                target.unlink()


# ---------------------------------------------------------------------------
# Patch production
# ---------------------------------------------------------------------------


def _safe_id(node: TaskNode) -> str:
    return _ID_SAFE.sub("_", str(node.get("id") or "task")).strip("._-") or "task"


def resolve_patch_path(node: TaskNode, worktree: Path, cfg: Config) -> Path:
    """Where ``patches/<task-id>.patch`` belongs for this worktree.

    ``run_task`` is handed a node and a worktree but not a run id, and the
    spine's executor puts worktrees in the system temp directory, so the run
    directory has to be discovered. Resolution order:

    1. ``$ADLC_PATCH_DIR`` (explicit override, used by tests);
    2. ``$ADLC_RUN_ID`` resolved through :class:`adlc.runs.RunDir`;
    3. the nearest ancestor that is a run directory -- either a direct child of
       ``cfg.runs_dir`` or a directory holding ``run.json`` / ``taskgraph.json``;
    4. a sibling of the worktree named after it.

    Step 4 is deliberately *not* ``<worktree>/../patches``: under the executor
    that is the shared system temp root, where two concurrent runs of the same
    graph would collide on ``<task-id>.patch``. Naming it after the worktree
    makes it unique. The patch directory is never placed inside the worktree,
    which would make the patch part of its own diff.
    """
    name = f"{_safe_id(node)}.patch"
    if override := os.environ.get("ADLC_PATCH_DIR"):
        return Path(override).expanduser() / name
    if run_id := (os.environ.get("ADLC_RUN_ID") or "").strip():
        with suppress(Exception):
            return RunDir(cfg, run_id).patches_dir / name

    worktree = worktree.resolve()
    runs_dir: Path | None = None
    with suppress(Exception):
        runs_dir = cfg.runs_dir.resolve()

    chosen: Path | None = None
    for ancestor in (worktree, *worktree.parents):
        if runs_dir is not None and ancestor.parent == runs_dir:
            chosen = ancestor / "patches"
            break
        if (ancestor / "run.json").is_file() or (ancestor / "taskgraph.json").is_file():
            chosen = ancestor / "patches"
            break
    if chosen is None or _is_within(chosen, worktree):
        chosen = worktree.parent / f"{worktree.name}.patches"
    return chosen / name


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def write_patch_text(path: Path, data: bytes) -> int:
    """Write patch bytes, creating the parent directory. Returns byte count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


@dataclass
class PatchResult:
    ok: bool
    reason: str
    patch_path: Path | None = None
    changed: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    size: int = 0


async def finalize_patch(
    node: TaskNode, worktree: Path, cfg: Config, base_sha: str | None = None
) -> PatchResult:
    """Turn the agent's edits in ``worktree`` into ``patches/<task-id>.patch``.

    Fails closed: an edit outside ``node['writeSet']`` is reverted and the task
    is failed rather than quietly trimmed from the diff, and a run that produced
    no changes at all is a failure, not a silent pass.
    """
    base_sha = base_sha or await sha_of(worktree)
    if not base_sha:
        return PatchResult(False, f"{worktree} is not a git worktree with a resolvable HEAD")

    write_set = usable_write_set(node)
    if not write_set:
        return PatchResult(False, f"task {_safe_id(node)} declares an empty writeSet")

    touched = await changed_paths(worktree)
    if touched is None:
        return PatchResult(
            False,
            "could not determine what the agent changed (git status failed) — refusing "
            "to emit a patch that was never write-set checked",
        )
    violations = violating_paths(touched, write_set)
    if violations:
        await revert_paths(worktree, violations, base_sha)
        listed = ", ".join(violations[:10]) + (" …" if len(violations) > 10 else "")
        return PatchResult(
            False,
            f"refused: {len(violations)} path(s) outside writeSet were written and "
            f"have been reverted: {listed}",
            changed=touched,
            violations=violations,
        )

    staged = await run_git(worktree, "add", "--all", "--", ".")
    if not staged.ok:
        return PatchResult(False, f"git add failed: {staged.err}", changed=touched)

    # Scoped to the write set as defence in depth: every changed path has
    # already been checked, so this can only ever narrow the diff.
    diff = await run_git(
        worktree, "diff", "--binary", "--no-color", "--no-renames", "--cached", base_sha,
        "--", *write_set,
    )
    if not diff.ok:
        return PatchResult(False, f"git diff failed: {diff.err}", changed=touched)
    if not diff.out.strip():
        return PatchResult(
            False, "agent produced no file changes in the worktree", changed=touched
        )

    patch_path = resolve_patch_path(node, worktree, cfg)
    size = write_patch_text(patch_path, diff.out)
    return PatchResult(
        True, f"patch anchored to {base_sha[:12]} ({size} bytes)", patch_path, touched, [], size
    )


async def apply_patch(worktree: Path, patch: Path) -> GitResult:
    """Apply a patch into ``worktree``, staging the result.

    Required for the runners whose agent worked **somewhere else**
    (:mod:`~adlc.adapters.agents.agent_task`, :mod:`~adlc.adapters.agents.gh_aw`).
    The spine's executor extracts the canonical ``patches/<task-id>.patch`` by
    diffing the worktree after ``run_task`` returns
    (:meth:`adlc.executor.Worktree.diff`), so a runner that only wrote a patch
    file and left the worktree untouched would be recorded as "no changes
    produced" and its work silently dropped. Applying it here makes all three
    runners look identical to the executor -- and, because the patch is anchored
    to the worktree's base SHA, a successful apply is also proof of that
    anchoring.
    """
    return await run_git(worktree, "apply", "--index", "--whitespace=nowarn", str(patch))


async def patch_from_range(
    node: TaskNode,
    worktree: Path,
    cfg: Config,
    base_sha: str,
    head_rev: str,
) -> PatchResult:
    """Produce a patch for ``base_sha..head_rev`` -- for remotely produced work.

    The Agent Tasks cloud agent and gh-aw both do their work somewhere else and
    hand back a ref. The resulting patch is still anchored to the worktree's
    ``base_sha``, the write set is enforced against the full name-only diff
    before anything is written, and the patch is then applied into the worktree
    so the executor sees the same changes a local agent would have made.
    """
    write_set = usable_write_set(node)
    if not write_set:
        return PatchResult(False, f"task {_safe_id(node)} declares an empty writeSet")

    names = await run_git(
        worktree, "diff", "--name-only", "-z", "--no-renames", base_sha, head_rev
    )
    if not names.ok:
        return PatchResult(False, f"git diff --name-only failed: {names.err}")
    touched = [_normalize(p) for p in names.text.split("\0") if p.strip()]
    if not touched:
        return PatchResult(False, f"{head_rev} is identical to base {base_sha[:12]}")

    violations = violating_paths(touched, write_set)
    if violations:
        listed = ", ".join(violations[:10]) + (" …" if len(violations) > 10 else "")
        return PatchResult(
            False,
            f"refused: result touches {len(violations)} path(s) outside writeSet: {listed}",
            changed=touched,
            violations=violations,
        )

    # ``--no-renames`` on both the check and the diff: a rename record names only
    # its destination in ``--name-only``, so a rename *out of* a protected path
    # would otherwise be invisible to the check while still deleting the source.
    diff = await run_git(
        worktree, "diff", "--binary", "--no-color", "--no-renames",
        base_sha, head_rev, "--", *write_set,
    )
    if not diff.ok:
        return PatchResult(False, f"git diff failed: {diff.err}", changed=touched)
    if not diff.out.strip():
        return PatchResult(
            False, "resulting diff was empty after write-set scoping", changed=touched
        )

    patch_path = resolve_patch_path(node, worktree, cfg)
    size = write_patch_text(patch_path, diff.out)

    applied = await apply_patch(worktree, patch_path)
    if not applied.ok:
        return PatchResult(
            False,
            f"patch did not apply at base {base_sha[:12]}: {applied.err}",
            patch_path,
            touched,
        )
    return PatchResult(
        True, f"patch anchored to {base_sha[:12]} ({size} bytes)", patch_path, touched, [], size
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def find_token(names: tuple[str, ...] = TOKEN_ENV_VARS) -> tuple[str | None, str | None]:
    """First non-empty credential from ``names``. Returns ``(value, var_name)``."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return None, None


def fail(reason: str, log: str = "") -> TaskOutcome:
    """A ``TaskOutcome`` whose first log line is the specific reason."""
    return {"status": "fail", "log": f"{reason}\n{log}".strip()}


def task_timeout(cfg: Config) -> float:
    with suppress(Exception):
        return float((cfg.limits or {}).get("taskTimeoutSeconds") or DEFAULT_TASK_TIMEOUT)
    return float(DEFAULT_TASK_TIMEOUT)


_USAGE_IN_KEYS = ("input_tokens", "prompt_tokens", "tokens_in", "inputTokens", "promptTokens")
_USAGE_OUT_KEYS = ("output_tokens", "completion_tokens", "tokens_out", "outputTokens")
_USAGE_COST_KEYS = ("cost", "total_cost", "premium_requests", "credits", "totalCost")


def collect_usage(source: Any) -> dict[str, Any]:
    """Best-effort token/cost extraction from a backend response.

    Returns only the keys the backend actually reported. ``TaskOutcome`` is
    ``total=False``, so an unreported figure is simply absent -- never a
    fabricated zero.
    """
    usage = source
    for attr in ("usage", "token_usage", "tokenUsage"):
        candidate = _get(source, attr)
        if candidate is not None:
            usage = candidate
            break
    out: dict[str, Any] = {}
    for keys, field_name, cast in (
        (_USAGE_IN_KEYS, "tokensIn", int),
        (_USAGE_OUT_KEYS, "tokensOut", int),
        (_USAGE_COST_KEYS, "cost", float),
    ):
        for key in keys:
            value = _get(usage, key)
            if isinstance(value, (int, float)):
                out[field_name] = cast(value)
                break
    return out


def _get(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_PROMPT_MAX_EXCERPT = 8_192


def build_prompt(node: TaskNode, worktree: Path | None = None) -> str:
    """Render one task node into an instruction for a coding agent.

    The context capsule is a *cache*, not the source of truth (§4.3): the agent
    is told it may read more, read-only, but may only write the declared set.
    """
    capsule = node.get("context") or {}
    write_set = usable_write_set(node)
    lines: list[str] = [
        "You are implementing exactly one task in an isolated git worktree.",
        "",
        f"## Task {node.get('id', '?')} — {node.get('title', '(untitled)')}",
        f"Kind: {node.get('kind', 'implement')}",
    ]
    if worktree is not None:
        lines.append(f"Worktree root: {worktree}")

    if acceptance := node.get("acceptance"):
        lines += ["", "## Acceptance criteria", *[f"- {item}" for item in acceptance]]

    allowed = [f"   - {path}" for path in write_set] or ["   - (none declared — do nothing)"]
    lines += [
        "",
        "## Hard rules — violating any of these fails the task",
        "1. Create or modify ONLY these paths (relative to the worktree root):",
        *allowed,
        "2. Never touch: " + ", ".join(PROTECTED_PATHS),
        (
            "3. Do NOT run `git commit`, `git push`, `git checkout` or create branches. "
            "Leave your work as uncommitted changes in the worktree; the framework "
            "extracts the patch itself."
        ),
        "4. You may read any file in the repository, but write only the paths above.",
    ]
    if do_not_touch := capsule.get("doNotTouch"):
        lines.append("5. Additionally do not touch: " + ", ".join(do_not_touch))

    if interfaces := capsule.get("interfaces"):
        lines += ["", "## Interfaces you must code against", interfaces.strip()]
    if conventions := capsule.get("conventions"):
        lines += ["", "## Conventions", conventions.strip()]
    if commands := capsule.get("commands"):
        lines += ["", "## Repository commands"]
        lines += [f"- {name}: `{cmd}`" for name, cmd in commands.items() if cmd]

    if refs := capsule.get("refs"):
        lines += ["", "## Context (a cache — re-read from disk if you need more)"]
        for ref in refs:
            path = ref.get("path", "?")
            detail = []
            if symbols := ref.get("symbols"):
                detail.append("symbols: " + ", ".join(symbols))
            if ranges := ref.get("lines"):
                detail.append("lines: " + ", ".join(f"{a}-{b}" for a, b in ranges))
            lines.append(f"### {path}" + (f" ({'; '.join(detail)})" if detail else ""))
            if excerpt := ref.get("excerpt"):
                lines.append("```")
                lines.append(excerpt[:_PROMPT_MAX_EXCERPT])
                lines.append("```")

    lines += [
        "",
        "## Definition of done",
        (
            "Every acceptance criterion is satisfied by code you wrote inside the "
            "allowed paths, and the repository's test command still passes."
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class CopilotSdkRunner:
    """Run a task with the **GitHub Copilot SDK** in-process.

    The SDK (`github-copilot-sdk`, imported as ``copilot``) drives the Copilot
    CLI runtime over JSON-RPC. It requires a Copilot entitlement, so this
    adapter is never on the credential-free path -- when it is unavailable the
    framework falls back to the spine's ``fake`` runner.
    """

    name = "copilot-sdk"
    kind = "agents"

    #: Import name of the SDK distribution ``github-copilot-sdk``.
    module = "copilot"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        try:
            try:
                spec = find_spec(CopilotSdkRunner.module)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                return False, (
                    "Copilot SDK not installed: no module 'copilot' "
                    "(pip install 'adlc[copilot]' / github-copilot-sdk)"
                )
            locations = list(getattr(spec, "submodule_search_locations", None) or [])
            if locations and not any((Path(loc) / "session.py").exists() for loc in locations):
                return False, (
                    "a module named 'copilot' is installed but it is not the GitHub "
                    "Copilot SDK (copilot.session is missing)"
                )
            token, var = find_token()
            if not token:
                return False, (
                    "Copilot SDK installed but no credential found in "
                    f"{'/'.join(TOKEN_ENV_VARS)}"
                )
            return True, f"Copilot SDK importable; credential from ${var}"
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"copilot-sdk detection failed: {exc.__class__.__name__}: {exc}"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("ADLC_COPILOT_MODEL") or "auto"

    async def run_task(self, node: TaskNode, worktree: Path, cfg: Config) -> TaskOutcome:
        available, reason = self.detect(cfg)
        if not available:
            return fail(f"copilot-sdk unavailable: {reason}")

        base_sha = await sha_of(worktree)
        if not base_sha:
            return fail(f"{worktree} is not a git worktree with a resolvable HEAD")

        prompt = build_prompt(node, worktree)
        try:
            transcript, usage = await asyncio.wait_for(
                self._converse(prompt, worktree), timeout=task_timeout(cfg)
            )
        except TimeoutError:
            return fail(
                f"copilot-sdk timed out after {task_timeout(cfg):.0f}s "
                f"on task {node.get('id', '?')}"
            )
        except Exception as exc:  # noqa: BLE001 - a leaf must never crash the spine
            return fail(f"copilot-sdk session failed: {exc.__class__.__name__}: {exc}")

        result = await finalize_patch(node, worktree, cfg, base_sha)
        log = f"{result.reason}\n\n--- copilot transcript ---\n{transcript}".strip()
        if not result.ok:
            return {"status": "fail", "log": log, **usage}
        return {
            "status": "ok",
            "patchPath": str(result.patch_path),
            "log": log,
            **usage,
        }

    async def _converse(self, prompt: str, worktree: Path) -> tuple[str, dict[str, Any]]:
        """Drive one Copilot SDK session rooted at ``worktree``.

        Isolated here so tests can substitute it without needing the SDK, a
        credential or the bundled CLI runtime.
        """
        # Lazy: `github-copilot-sdk` is an optional dependency, so importing at
        # module scope would make this whole module (and its shared helpers)
        # undiscoverable when the SDK is not installed.
        from copilot import CopilotClient
        from copilot.session import PermissionHandler

        token, _ = find_token()
        client = CopilotClient(working_directory=str(worktree), github_token=token)
        await client.start()
        session = None
        try:
            session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                model=self.model,
                working_directory=str(worktree),
            )
            response = await self._send(session, prompt)
        finally:
            if session is not None:
                with suppress(Exception):
                    await session.disconnect()
            with suppress(Exception):
                await client.stop()
        return _transcript(response), collect_usage(response)

    @staticmethod
    async def _send(session: Any, prompt: str) -> Any:
        """Send the prompt and wait for the turn to finish.

        Prefers ``send_and_wait`` when the installed SDK exposes it and falls
        back to ``send`` plus the ``session.idle`` event, which is the surface
        documented for ``github-copilot-sdk`` 1.x.
        """
        if callable(getattr(session, "send_and_wait", None)):
            return await session.send_and_wait(prompt)

        done = asyncio.Event()
        messages: list[str] = []
        last: list[Any] = []

        def on_event(event: Any) -> None:
            data = _get(event, "data")
            kind = str(_get(event, "type") or type(data).__name__)
            last.append(data if data is not None else event)
            if "message" in kind.lower() and (content := _get(data, "content")):
                messages.append(str(content))
            if "idle" in kind.lower():
                done.set()

        session.on(on_event)
        await session.send(prompt)
        await done.wait()
        return {"content": "\n".join(messages), "events": last}


def _transcript(response: Any, limit: int = 20_000) -> str:
    for attr in ("content", "text", "message"):
        value = _get(response, attr)
        if isinstance(value, str) and value.strip():
            return value[:limit]
    return str(response)[:limit]
