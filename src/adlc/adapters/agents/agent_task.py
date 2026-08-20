"""``AgentRunner`` backed by the GitHub Copilot **Agent Tasks REST API**.

> **Public preview.** The Agent Tasks API (``/agents/repos/{owner}/{repo}/tasks``)
> is a GitHub public-preview surface. Its request/response shape may change
> without notice, so every field this adapter reads is looked up defensively and
> a shape it does not recognise degrades to a specific ``fail`` reason rather
> than an exception.

Unlike :class:`~adlc.adapters.agents.copilot_sdk.CopilotSdkRunner`, the cloud
agent does its work **remotely** and pushes a branch (optionally opening a pull
request). This adapter therefore:

1. creates a task with the node's prompt;
2. polls until the task reaches a terminal status;
3. fetches the resulting ref into the local worktree;
4. produces ``patches/<task-id>.patch`` as a diff from the worktree's **base
   SHA** to that ref, refusing anything outside ``node['writeSet']``.

HTTP is done with the standard library (:mod:`urllib.request`) on purpose: the
spine must stay installable with no extra dependencies, so this adapter adds
none. Blocking calls are pushed onto a worker thread so ``run_task`` stays
properly async.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.adapters.agents.copilot_sdk import (
    build_prompt,
    fail,
    patch_from_range,
    run_git,
    sha_of,
    task_timeout,
    usable_write_set,
)
from adlc.ports import TaskNode, TaskOutcome

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = ["AgentTaskRunner", "extract_result_ref", "resolve_repo_slug"]

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

#: Credentials accepted for the REST API, in priority order.
TOKEN_ENV_VARS: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")

#: Statuses the API may report (public preview).
STATUSES: tuple[str, ...] = (
    "queued", "in_progress", "completed", "failed",
    "idle", "waiting_for_user", "timed_out", "cancelled",
)
#: Statuses that mean "keep polling".
PENDING_STATUSES: frozenset[str] = frozenset({"queued", "in_progress"})
#: The only status that means the agent finished its work successfully.
SUCCESS_STATUS = "completed"

DEFAULT_POLL_SECONDS = 15.0
HTTP_TIMEOUT = 30.0

_SLUG = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_REMOTE_URL = re.compile(r"^\s*url\s*=\s*(?P<url>\S+)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Repo identity (cheap, offline)
# ---------------------------------------------------------------------------


def _git_dir(root: Path) -> Path | None:
    """Locate the real git directory, following a worktree's ``.git`` file."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    with suppress(OSError, ValueError):
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            git_dir = Path(text.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
            common = git_dir / "commondir"
            if common.is_file():
                rel = common.read_text(encoding="utf-8").strip()
                return (git_dir / rel).resolve()
            return git_dir
    return None


def _slug_from_url(url: str) -> str | None:
    url = url.strip().rstrip("/").removesuffix(".git")
    # git@github.com:owner/repo · ssh://git@github.com/owner/repo · https://github.com/owner/repo
    for marker in ("github.com:", "github.com/"):
        if marker in url:
            candidate = url.split(marker, 1)[1].strip("/")
            return candidate if _SLUG.match(candidate) else None
    return None


def resolve_repo_slug(cfg: Config, remote: str = "origin") -> tuple[str | None, str]:
    """Resolve ``owner/repo`` without any network call or subprocess.

    Order: ``$ADLC_REPO`` → ``$GITHUB_REPOSITORY`` → ``repo:`` in
    ``.adlc/config.yaml`` → the ``origin`` remote in the git config file.
    """
    for var in ("ADLC_REPO", "GITHUB_REPOSITORY"):
        value = (os.environ.get(var) or "").strip()
        if value:
            if _SLUG.match(value):
                return value, f"${var}"
            return None, f"${var}='{value}' is not in owner/repo form"

    with suppress(Exception):
        configured = str((cfg.raw or {}).get("repo") or "").strip()
        if configured and _SLUG.match(configured):
            return configured, ".adlc/config.yaml repo:"

    with suppress(Exception):
        git_dir = _git_dir(cfg.root)
        if git_dir is not None and (config := git_dir / "config").is_file():
            text = config.read_text(encoding="utf-8", errors="replace")
            section = f'[remote "{remote}"]'
            if section in text:
                tail = text.split(section, 1)[1]
                match = _REMOTE_URL.search(tail)
                if match and (slug := _slug_from_url(match.group("url"))):
                    return slug, f"git remote '{remote}'"
            return None, f"git remote '{remote}' is missing or is not a github.com URL"
    return None, "repository could not be determined (no .git/config found)"


def _find_token() -> tuple[str | None, str | None]:
    for name in TOKEN_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return None, None


# ---------------------------------------------------------------------------
# Minimal REST client (stdlib only)
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _request(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    """One blocking REST call. Raises :class:`ApiError` on any non-2xx."""
    if not url.startswith("https://"):  # never let a config value downgrade the transport
        raise ApiError(0, f"refusing non-https request to {url!r}")
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", API_VERSION)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "adlc-agent-task/0.1")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800] if exc.fp else exc.reason
        raise ApiError(exc.code, str(detail)) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, f"could not reach {url}: {exc.reason}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(200, f"response was not JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Response shape helpers (preview API — read defensively)
# ---------------------------------------------------------------------------


def _dig(source: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(source, dict):
            return None
        source = source.get(key)
    return source


def extract_result_ref(task: dict[str, Any]) -> str | None:
    """Find the ref the cloud agent pushed its work to.

    The preview API has surfaced this under several names, so every plausible
    location is tried before giving up. A pull-request number is usable too:
    ``refs/pull/<n>/head`` is always fetchable.
    """
    candidates = (
        _dig(task, "pull_request", "head_ref"),
        _dig(task, "pull_request", "head", "ref"),
        _dig(task, "pull_request", "headRef"),
        task.get("head_ref"),
        task.get("headRef"),
        task.get("branch"),
        task.get("ref"),
        _dig(task, "result", "branch"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for number in (_dig(task, "pull_request", "number"), task.get("pull_request_number")):
        if isinstance(number, int) and number > 0:
            return f"refs/pull/{number}/head"
    return None


def _task_id(created: dict[str, Any]) -> str | None:
    for key in ("id", "task_id", "taskId", "number"):
        value = created.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


def _status_of(task: dict[str, Any]) -> str:
    for key in ("status", "state"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class AgentTaskRunner:
    """Delegate a task node to the Copilot cloud agent via the Agent Tasks API.

    **Preview.** The endpoint is public preview; treat availability and schema
    as unstable. Requires a token whose account has Copilot coding-agent access
    and write permission on the target repository.
    """

    name = "agent-task"
    kind = "agents"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        try:
            token, var = _find_token()
            if not token:
                return False, (
                    "Agent Tasks API needs a GitHub token in "
                    f"{' or '.join('$' + v for v in TOKEN_ENV_VARS)}"
                )
            slug, source = resolve_repo_slug(cfg)
            if not slug:
                return False, f"Agent Tasks API needs owner/repo: {source}"
            return True, (
                f"Agent Tasks API (public preview) for {slug} via {source}; "
                f"credential from ${var}"
            )
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"agent-task detection failed: {exc.__class__.__name__}: {exc}"

    def __init__(self, model: str | None = None, remote: str | None = None) -> None:
        self.model = model or os.environ.get("ADLC_AGENT_TASK_MODEL") or "auto"
        self.remote = remote or os.environ.get("ADLC_GIT_REMOTE") or "origin"

    # -- REST wrappers (async) --------------------------------------------
    async def create_task(
        self, slug: str, token: str, prompt: str, base_ref: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            _request,
            "POST",
            f"{API_ROOT}/agents/repos/{slug}/tasks",
            token,
            {
                "prompt": prompt,
                "base_ref": base_ref,
                "create_pull_request": True,
                "model": self.model,
            },
        )

    async def get_task(self, slug: str, token: str, task_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            _request, "GET", f"{API_ROOT}/agents/repos/{slug}/tasks/{task_id}", token
        )

    async def poll(
        self, slug: str, token: str, task_id: str, deadline: float, interval: float
    ) -> dict[str, Any]:
        """Poll until the task leaves :data:`PENDING_STATUSES` or time runs out."""
        loop = asyncio.get_running_loop()
        task: dict[str, Any] = {}
        while True:
            task = await self.get_task(slug, token, task_id)
            if _status_of(task) not in PENDING_STATUSES:
                return task
            if loop.time() >= deadline:
                task = dict(task)
                task["status"] = "timed_out"
                task["_adlc_local_timeout"] = True
                return task
            await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))

    # -- Protocol ----------------------------------------------------------
    async def run_task(self, node: TaskNode, worktree: Path, cfg: Config) -> TaskOutcome:
        available, reason = self.detect(cfg)
        if not available:
            return fail(f"agent-task unavailable: {reason}")
        token, _ = _find_token()
        slug, _ = resolve_repo_slug(cfg, self.remote)
        if not token or not slug:  # pragma: no cover - detect() already guaranteed both
            return fail("agent-task unavailable: token or repository disappeared after detect()")

        if not usable_write_set(node):
            return fail(f"task {node.get('id', '?')} declares an empty writeSet")

        base_sha = await sha_of(worktree)
        if not base_sha:
            return fail(f"{worktree} is not a git worktree with a resolvable HEAD")
        base_ref = await self._base_ref(worktree, base_sha)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + task_timeout(cfg)
        prompt = build_prompt(node, worktree)

        try:
            created = await self.create_task(slug, token, prompt, base_ref)
        except ApiError as exc:
            return fail(f"agent-task create failed for {slug}: {exc}")

        task_id = _task_id(created)
        if not task_id:
            return fail(
                "agent-task create returned no task id — the public-preview response "
                f"shape is not recognised: {sorted(created)[:12]}"
            )

        try:
            task = await self.poll(slug, token, task_id, deadline, self._interval(cfg))
        except ApiError as exc:
            return fail(f"agent-task poll failed for task {task_id}: {exc}")

        status = _status_of(task)
        log = f"agent-task {task_id} on {slug} finished with status '{status}'"
        if status != SUCCESS_STATUS:
            hint = {
                "waiting_for_user": " — the cloud agent asked a question; ADLC runs unattended",
                "timed_out": f" — budget was {task_timeout(cfg):.0f}s",
                "idle": " — the agent stopped without completing",
            }.get(status, "")
            return fail(f"{log}{hint}", _summary(task))

        ref = extract_result_ref(task)
        if not ref:
            return fail(f"{log} but reported no branch or pull request to fetch", _summary(task))

        fetched = await run_git(
            worktree, "fetch", "--no-tags", self.remote, f"{ref}:refs/adlc/agent-task/{task_id}"
        )
        if not fetched.ok:
            fetched = await run_git(worktree, "fetch", "--no-tags", self.remote, ref)
            head_rev = "FETCH_HEAD"
            if not fetched.ok:
                return fail(f"could not fetch '{ref}' from '{self.remote}': {fetched.err}", log)
        else:
            head_rev = f"refs/adlc/agent-task/{task_id}"

        result = await patch_from_range(node, worktree, cfg, base_sha, head_rev)
        full_log = f"{log}\nref: {ref}\n{result.reason}"
        if not result.ok:
            return {"status": "fail", "log": full_log}
        return {"status": "ok", "patchPath": str(result.patch_path), "log": full_log}

    # -- internals ---------------------------------------------------------
    async def _base_ref(self, worktree: Path, base_sha: str) -> str:
        """What to hand the API as ``base_ref``.

        The patch is always anchored to the local ``base_sha`` regardless, so an
        imprecise base ref costs correctness nothing — the write-set check and
        the ``base_sha..head`` range keep the result bounded.
        """
        if override := (os.environ.get("ADLC_BASE_REF") or "").strip():
            return override
        branch = await run_git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        name = branch.text.strip()
        if branch.ok and name and name != "HEAD":
            return name
        return base_sha

    @staticmethod
    def _interval(cfg: Config) -> float:
        with suppress(Exception):
            return max(1.0, float((cfg.limits or {}).get("pollSeconds") or DEFAULT_POLL_SECONDS))
        return DEFAULT_POLL_SECONDS


def _summary(task: dict[str, Any], limit: int = 2_000) -> str:
    """A compact, non-secret dump of the task record for the outcome log."""
    keep = ("id", "status", "state", "error", "message", "conclusion", "html_url", "updated_at")
    trimmed = {key: task[key] for key in keep if key in task}
    if pull_request := task.get("pull_request"):
        trimmed["pull_request"] = {
            key: pull_request.get(key)
            for key in ("number", "html_url", "head_ref", "state")
            if isinstance(pull_request, dict) and key in pull_request
        }
    return json.dumps(trimmed, indent=2, default=str)[:limit]
