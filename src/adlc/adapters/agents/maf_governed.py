"""``AgentRunner`` that executes a task node through a governed MAF agent.

The contract is identical to every other runner (``adlc.ports.AgentRunner``):
one task node, one isolated worktree, one patch anchored to that worktree's base
SHA, and **no writes outside ``node['writeSet']``**. The only thing that is
different is *how* the agent is allowed to act: every tool call it makes passes
through Agent Governance Toolkit policy enforcement first, via Microsoft Agent
Framework's function middleware (``adlc.maf.middleware``).

MAF is not scheduling anything here. The spine's topological executor already
decided this node should run; this module just runs it under policy.

Both MAF and AGT are public preview, so every import of them is deferred into
the function that needs it. Importing this module on a machine with neither
installed is fine and is exercised by ``tests/l2_governance``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from adlc.maf.middleware import (
    AGT_INSTALL_HINT,
    GovernanceUnavailable,
    PolicyEngine,
    detect_agt,
    detect_maf,
    resolve_policy_path,
)
from adlc.ports import PROTECTED_PATHS

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config
    from adlc.ports import TaskNode, TaskOutcome

__all__ = ["MafGovernedRunner"]

#: Environment variables that can supply a MAF chat client, checked in order.
#: The value is ``(env var, module, class name, kwargs-from-env)``.
_CHAT_CLIENT_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("AZURE_AI_PROJECT_ENDPOINT", "agent_framework.foundry", "FoundryChatClient"),
    ("AZURE_OPENAI_ENDPOINT", "agent_framework.azure", "AzureOpenAIChatClient"),
    ("OPENAI_API_KEY", "agent_framework.openai", "OpenAIChatClient"),
)

_MAX_TOOL_BYTES = 64 * 1024
_DEFAULT_GIT_TIMEOUT = 120

INSTRUCTIONS = """\
You are an ADLC implementation agent working inside an isolated git worktree.

Rules you cannot negotiate:
* You may only create or modify the files listed in ALLOWED FILES below.
* Never touch .github/**, .adlc/**, schemas/**, docs/decisions/** or pyproject.toml.
* Every tool call is checked against an Agent Governance Toolkit policy before it
  runs. A blocked call is not an error to work around — stop and explain instead.
* Make the smallest change that satisfies the acceptance criteria. Do not
  refactor unrelated code.

Finish by summarizing what you changed and why.
"""


class MafGovernedRunner:
    """Run one task node through a governance-wrapped MAF agent."""

    name = "maf"
    kind = "agents"

    # -- detection --------------------------------------------------------
    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, non-raising: module-finder probes plus env-var lookups."""
        available, reason = detect_maf(cfg)
        if not available:
            return False, reason
        available, reason = detect_agt(cfg)
        if not available:
            return False, reason
        if resolve_policy_path(cfg) is None:
            return False, "no AGT policy found — expected .adlc/policy.yaml"
        if not _chat_client_env():
            expected = ", ".join(env for env, _, _ in _CHAT_CLIENT_CANDIDATES)
            return False, f"no MAF chat client configured — set one of {expected}"
        return True, "MAF + AGT available with a configured chat client"

    # -- execution --------------------------------------------------------
    async def run_task(
        self, node: TaskNode, worktree: Path, cfg: Config
    ) -> TaskOutcome:
        node_id = str(node.get("id") or "task")
        worktree = Path(worktree)
        log: list[str] = []

        available, reason = self.detect(cfg)
        if not available:
            # Never silently run ungoverned. An unavailable governance stack is
            # a failed task, not a free pass.
            return _fail(f"governance unavailable: {reason}", log)

        base_sha = _git_output(worktree, "rev-parse", "HEAD", strip=True)
        if base_sha is None:
            return _fail(f"{worktree} is not a git worktree", log)
        log.append(f"base sha {base_sha}")

        write_set = [str(p) for p in (node.get("writeSet") or [])]
        if not write_set:
            return _fail(f"node {node_id} declares an empty writeSet", log)

        try:
            engine = PolicyEngine.load(
                cfg, agent_id=f"adlc:{node_id}", session_id=base_sha[:12], strict=True
            )
        except GovernanceUnavailable as exc:
            return _fail(str(exc), log)
        if engine is None:  # pragma: no cover - strict=True raises
            return _fail(AGT_INSTALL_HINT, log)

        run_dir = _resolve_run_dir(cfg)
        try:
            agent_log, usage = await self._invoke(node, worktree, cfg, engine, write_set)
            log.append(agent_log)
        except GovernanceUnavailable as exc:
            _emit_decisions(run_dir, engine)
            return _fail(str(exc), log)
        except Exception as exc:  # noqa: BLE001 - a leaf must never break the spine
            _emit_decisions(run_dir, engine)
            return _fail(f"{type(exc).__name__}: {exc}", log)
        finally:
            engine.close()

        _emit_decisions(run_dir, engine)

        denied = [record for record in engine.records if not record.permits]
        if denied:
            summary = "; ".join(f"{r.tool}: {r.decision}" for r in denied[:5])
            return _fail(f"{len(denied)} tool call(s) blocked by policy — {summary}", log)

        violations = _write_set_violations(worktree, write_set)
        if violations:
            _git(worktree, "reset", "--hard", base_sha)
            _git(worktree, "clean", "-fd")
            return _fail(
                f"agent wrote outside writeSet: {', '.join(sorted(violations)[:10])}", log
            )

        patch_path = run_dir / "patches" / f"{node_id}.patch"
        written = _write_patch(worktree, base_sha, patch_path)
        if written is None:
            return _fail("could not produce a patch anchored to the base SHA", log)
        if not written:
            log.append("agent produced no changes")

        return {
            "status": "ok",
            "patchPath": str(patch_path),
            "log": "\n".join(log),
            "tokensIn": usage.get("tokensIn", 0),
            "tokensOut": usage.get("tokensOut", 0),
            "cost": usage.get("cost", 0.0),
        }

    # -- the governed agent ----------------------------------------------
    async def _invoke(
        self,
        node: TaskNode,
        worktree: Path,
        cfg: Config,
        engine: PolicyEngine,
        write_set: Sequence[str],
    ) -> tuple[str, dict[str, Any]]:
        from adlc.maf.agents import build_governed_agent

        chat_client = _build_chat_client()
        tools = build_worktree_tools(worktree, write_set)
        governed = build_governed_agent(
            chat_client=chat_client,
            instructions=INSTRUCTIONS,
            tools=tools,
            name=f"adlc-{node.get('id') or 'task'}",
            cfg=cfg,
            engine=engine,
        )
        async with governed:
            result = await governed.run(build_prompt(node, write_set))
        return _response_text(result), _usage(result)


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def build_prompt(node: TaskNode, write_set: Sequence[str]) -> str:
    """Render the task node (and its bounded context capsule) as a prompt."""
    lines = [
        f"# Task {node.get('id', '')}: {node.get('title', '')}".rstrip(),
        f"Kind: {node.get('kind', 'implement')}",
        "",
        "## ALLOWED FILES (your writeSet — nothing else)",
        *(f"- {path}" for path in write_set),
    ]

    acceptance = node.get("acceptance") or []
    if acceptance:
        lines += ["", "## Acceptance criteria", *(f"- {item}" for item in acceptance)]

    capsule = node.get("context") or {}
    if interfaces := capsule.get("interfaces"):
        lines += ["", "## Interfaces", str(interfaces)]
    if conventions := capsule.get("conventions"):
        lines += ["", "## Conventions", str(conventions)]
    if commands := capsule.get("commands"):
        lines += ["", "## Commands"]
        lines += [f"- {key}: {value}" for key, value in dict(commands).items()]
    if refs := capsule.get("refs"):
        lines += ["", "## Context references"]
        for ref in refs:
            path = ref.get("path", "")
            symbols = ", ".join(ref.get("symbols") or [])
            lines.append(f"- {path}" + (f" ({symbols})" if symbols else ""))
            if excerpt := ref.get("excerpt"):
                lines += ["```", str(excerpt), "```"]

    do_not_touch = capsule.get("doNotTouch") or list(PROTECTED_PATHS)
    lines += ["", "## Never touch", *(f"- {path}" for path in do_not_touch)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worktree-scoped tools
# ---------------------------------------------------------------------------


def build_worktree_tools(
    worktree: Path, write_set: Sequence[str]
) -> list[Callable[..., Any]]:
    """Tools confined to ``worktree``, and to ``write_set`` for mutations.

    The AGT policy is the authoritative control; these checks are defence in
    depth so a policy misconfiguration still cannot escape the worktree.
    """
    root = Path(worktree).resolve()
    allowed = [_compile_glob(pattern) for pattern in write_set]
    protected = [_compile_glob(pattern) for pattern in PROTECTED_PATHS]

    def _resolve(path: str) -> tuple[Path | None, str]:
        try:
            target = (root / path).resolve()
        except (OSError, ValueError):
            return None, f"invalid path: {path!r}"
        try:
            rel = target.relative_to(root)
        except ValueError:
            return None, f"path escapes the worktree: {path!r}"
        return target, rel.as_posix()

    def read_file(path: str) -> str:
        """Read a UTF-8 text file from the worktree."""
        target, rel = _resolve(path)
        if target is None:
            return f"ERROR: {rel}"
        if any(pattern.match(rel) for pattern in protected):
            return f"ERROR: {rel} is a framework-protected path"
        if not target.is_file():
            return f"ERROR: {rel} does not exist"
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"ERROR: cannot read {rel}: {exc}"
        return text[:_MAX_TOOL_BYTES]

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file. Only paths in the task's writeSet."""
        target, rel = _resolve(path)
        if target is None:
            return f"ERROR: {rel}"
        if any(pattern.match(rel) for pattern in protected):
            return f"ERROR: {rel} is a framework-protected path"
        if not any(pattern.match(rel) for pattern in allowed):
            return f"ERROR: {rel} is not in this task's writeSet"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: cannot write {rel}: {exc}"
        return f"wrote {rel} ({len(content)} chars)"

    def list_files(pattern: str = "*") -> str:
        """List worktree files matching a glob, excluding .git."""
        try:
            matches = sorted(
                p.relative_to(root).as_posix()
                for p in root.glob(pattern)
                if p.is_file() and ".git" not in p.parts
            )
        except (OSError, ValueError) as exc:
            return f"ERROR: {exc}"
        return "\n".join(matches[:500]) or "(no matches)"

    return [read_file, write_file, list_files]


# ---------------------------------------------------------------------------
# Write-set enforcement
# ---------------------------------------------------------------------------


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a git-style glob into a precise regex.

    ``fnmatch`` alone is wrong here: its ``*`` crosses ``/``, which would let
    ``src/*`` authorize ``src/a/b/c.ts``. ``**`` crosses separators, ``*`` and
    ``?`` do not.
    """
    normalized = PurePosixPath(pattern.replace("\\", "/")).as_posix()
    # A trailing `/**` is covered by the "everything under this path" suffix
    # appended below, which also makes the directory entry itself match --
    # what we want when guarding `.github/**`.
    normalized = normalized.removesuffix("/**")
    out: list[str] = []
    i = 0
    while i < len(normalized):
        char = normalized[i]
        if normalized.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if normalized.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = normalized.find("]", i)
            if end == -1:
                out.append(re.escape(char))
            else:
                out.append(fnmatch.translate(normalized[i : end + 1])[4:-3])
                i = end + 1
                continue
        else:
            out.append(re.escape(char))
        i += 1
    # A bare directory pattern (`src/foo`) also authorizes everything under it.
    return re.compile(rf"(?s:{''.join(out)})(?:/.*)?\Z")


def _write_set_violations(worktree: Path, write_set: Sequence[str]) -> set[str]:
    """Paths the agent changed that its writeSet does not authorize."""
    allowed = [_compile_glob(pattern) for pattern in write_set]
    protected = [_compile_glob(pattern) for pattern in PROTECTED_PATHS]
    violations: set[str] = set()
    for path in changed_paths(worktree):
        if any(pattern.match(path) for pattern in protected) or not any(pattern.match(path) for pattern in allowed):
            violations.add(path)
    return violations


def changed_paths(worktree: Path) -> set[str]:
    """Every path git reports as added, modified, deleted, renamed or untracked."""
    raw = _git_output(worktree, "status", "--porcelain=1", "-z", "--untracked-files=all")
    if not raw:
        return set()
    fields = [field for field in raw.split("\0") if field]
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.add(PurePosixPath(path.replace("\\", "/")).as_posix())
        # A rename/copy entry is followed by its source path as a separate field.
        if ("R" in status or "C" in status) and index < len(fields):
            paths.add(PurePosixPath(fields[index].replace("\\", "/")).as_posix())
            index += 1
    return paths


# ---------------------------------------------------------------------------
# Patch production
# ---------------------------------------------------------------------------


def _write_patch(worktree: Path, base_sha: str, out: Path) -> bool | None:
    """Stage everything and write a patch anchored to ``base_sha``.

    Returns ``True`` when a non-empty patch was written, ``False`` when the
    agent changed nothing, and ``None`` on failure.
    """
    if _git(worktree, "add", "--all") is None:
        return None
    diff = _git_output(worktree, "diff", "--cached", "--binary", base_sha)
    if diff is None:
        return None
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(diff + ("\n" if diff and not diff.endswith("\n") else ""), encoding="utf-8")
    except OSError:
        return None
    return bool(diff.strip())


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=_DEFAULT_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return completed if completed.returncode == 0 else None


def _git_output(worktree: Path, *args: str, strip: bool = False) -> str | None:
    """Stdout of a git command, or ``None`` if it failed.

    ``strip`` is opt-in because ``status --porcelain -z`` and ``diff --binary``
    are both whitespace-significant.
    """
    completed = _git(worktree, *args)
    if completed is None:
        return None
    return completed.stdout.strip() if strip else completed.stdout


# ---------------------------------------------------------------------------
# Chat client resolution
# ---------------------------------------------------------------------------


def _chat_client_env() -> str | None:
    if os.environ.get("ADLC_MAF_CHAT_CLIENT"):
        return "ADLC_MAF_CHAT_CLIENT"
    for env, _module, _cls in _CHAT_CLIENT_CANDIDATES:
        if os.environ.get(env):
            return env
    return None


def _build_chat_client() -> Any:
    """Construct a MAF chat client from the environment.

    ``ADLC_MAF_CHAT_CLIENT`` may name a ``module:attr`` factory, which is the
    escape hatch for hosts that already own their client construction.
    """
    import importlib

    if spec := os.environ.get("ADLC_MAF_CHAT_CLIENT"):
        module_name, _, attr = spec.partition(":")
        if not attr:
            module_name, _, attr = spec.rpartition(".")
        factory = getattr(importlib.import_module(module_name), attr)
        return factory()

    errors: list[str] = []
    for env, module_name, class_name in _CHAT_CLIENT_CANDIDATES:
        if not os.environ.get(env):
            continue
        try:
            client_cls = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            errors.append(f"{class_name}: {exc}")
            continue
        return client_cls()

    detail = f" ({'; '.join(errors)})" if errors else ""
    raise GovernanceUnavailable(f"no MAF chat client could be constructed{detail}")


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------


def _response_text(result: Any) -> str:
    for attr in ("text", "content", "value"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    return str(result)


def _usage(result: Any) -> dict[str, Any]:
    usage = getattr(result, "usage_details", None) or getattr(result, "usage", None)
    if usage is None:
        return {}
    def _int(*names: str) -> int:
        for name in names:
            value = getattr(usage, name, None)
            if isinstance(value, int):
                return value
        return 0

    return {
        "tokensIn": _int("input_token_count", "prompt_tokens", "input_tokens"),
        "tokensOut": _int("output_token_count", "completion_tokens", "output_tokens"),
        "cost": 0.0,
    }


def _resolve_run_dir(cfg: Config) -> Path:
    """Where patches and governance evidence for this run belong.

    The ``AgentRunner`` signature does not carry a run id, so the spine passes
    it through the environment. ``.adlc/runs/current`` is the honest fallback.
    """
    if raw := os.environ.get("ADLC_RUN_DIR"):
        return Path(raw)
    if run_id := os.environ.get("ADLC_RUN_ID"):
        return cfg.run_dir(run_id)
    return cfg.runs_dir / "current"


def _emit_decisions(run_dir: Path, engine: PolicyEngine) -> None:
    """Write the middleware decision log where the ``governance`` gate reads it."""
    target = run_dir / "gates" / "governance-decisions.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(engine.evidence(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _fail(message: str, log: Iterable[str]) -> TaskOutcome:
    return {
        "status": "fail",
        "patchPath": "",
        "log": "\n".join([*log, message]),
        "tokensIn": 0,
        "tokensOut": 0,
        "cost": 0.0,
    }
