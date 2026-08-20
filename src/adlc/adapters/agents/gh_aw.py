"""``AgentRunner`` backed by a **GitHub Agentic Workflow** (``github/gh-aw``).

gh-aw is the event-driven half of the ADLC design (``docs/PLAN.md`` §1): agents
run inside GitHub Actions with least privilege, a network firewall and
``safe-outputs``. This adapter lets the *inner* loop borrow that execution
environment for one task node — it dispatches a workflow, waits for the run and
collects the patch the workflow uploads as an artifact.

The contract with the workflow is deliberately small:

* it is dispatched with inputs ``task_id``, ``base_sha``, ``write_set``
  (newline-separated) and ``prompt``;
* it must upload an artifact containing ``<task_id>.patch`` — a unified diff
  anchored to ``base_sha``.

Everything else (permissions, toolsets, firewall allowlist) is the workflow's
business, which is exactly the point of using gh-aw for it.

``detect()`` is filesystem-only: the golden rules forbid a subprocess in
``detect()``, so extension presence is established by looking for the installed
``gh-aw`` extension directory rather than by shelling out to
``gh extension list``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.adapters.agents.agent_task import resolve_repo_slug
from adlc.adapters.agents.copilot_sdk import (
    apply_patch,
    build_prompt,
    changed_paths,
    enumerate_patch_paths,
    fail,
    resolve_patch_path,
    run_git,
    sha_of,
    task_timeout,
    usable_write_set,
    violating_paths,
    write_patch_text,
)
from adlc.ports import TaskNode, TaskOutcome

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = ["GhAwRunner", "extension_dirs", "gh_config_dir", "select_patch_file"]

#: Name of the gh extension this adapter drives.
EXTENSION = "gh-aw"

#: Default workflow to dispatch. gh-aw compiles ``<name>.md`` to ``<name>.lock.yml``.
DEFAULT_WORKFLOW = "adlc-task.lock.yml"

DEFAULT_POLL_SECONDS = 10.0
GH_TIMEOUT = 120.0

#: `gh workflow run` inputs are size-limited; keep the prompt comfortably under it.
MAX_PROMPT_CHARS = 60_000

TOKEN_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")


# ---------------------------------------------------------------------------
# Cheap, offline probes for `gh` state
# ---------------------------------------------------------------------------


def gh_config_dir() -> Path:
    """Where ``gh`` keeps ``hosts.yml``. Mirrors gh's own resolution order."""
    if override := (os.environ.get("GH_CONFIG_DIR") or "").strip():
        return Path(override)
    if os.name == "nt" and (appdata := os.environ.get("AppData")):
        return Path(appdata) / "GitHub CLI"
    if xdg := (os.environ.get("XDG_CONFIG_HOME") or "").strip():
        return Path(xdg) / "gh"
    return Path.home() / ".config" / "gh"


def extension_dirs() -> list[Path]:
    """Every directory ``gh`` may have installed extensions into."""
    home = Path.home()
    candidates: list[Path] = []
    if override := (os.environ.get("GH_CONFIG_DIR") or "").strip():
        candidates.append(Path(override) / "extensions")
    if local := (os.environ.get("LOCALAPPDATA") or "").strip():
        candidates.append(Path(local) / "GitHub CLI" / "extensions")
    if xdg := (os.environ.get("XDG_DATA_HOME") or "").strip():
        candidates.append(Path(xdg) / "gh" / "extensions")
    candidates += [
        home / ".local" / "share" / "gh" / "extensions",
        home / "AppData" / "Local" / "GitHub CLI" / "extensions",
    ]
    return candidates


def _extension_installed(name: str = EXTENSION) -> Path | None:
    for directory in extension_dirs():
        with suppress(OSError):
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _authenticated() -> tuple[bool, str]:
    for var in TOKEN_ENV_VARS:
        if (os.environ.get(var) or "").strip():
            return True, f"${var}"
    hosts = gh_config_dir() / "hosts.yml"
    with suppress(OSError):
        if hosts.is_file() and hosts.stat().st_size > 0:
            return True, str(hosts)
    return False, ""


# ---------------------------------------------------------------------------
# `gh` invocation
# ---------------------------------------------------------------------------


class GhResult:
    __slots__ = ("code", "err", "out")

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def ok(self) -> bool:
        return self.code == 0

    def json(self) -> Any:
        with suppress(json.JSONDecodeError):
            return json.loads(self.out or "null")
        return None


async def run_gh(*args: str, stdin: str | None = None, timeout: float = GH_TIMEOUT) -> GhResult:
    """Run one ``gh`` command. Never raises; always times out."""
    env = dict(os.environ)
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("NO_COLOR", "1")
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (OSError, ValueError) as exc:
        return GhResult(127, "", f"gh could not be started: {exc}")
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode("utf-8") if stdin is not None else None),
            timeout=timeout,
        )
    except TimeoutError:
        with suppress(Exception):
            proc.kill()
        return GhResult(124, "", f"gh {' '.join(args[:3])} timed out after {timeout}s")
    return GhResult(
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace").strip(),
    )


# ---------------------------------------------------------------------------
# Artifact selection
# ---------------------------------------------------------------------------


def select_patch_file(root: Path, task_id: str) -> Path | None:
    """Pick the patch a gh-aw run produced. It must be named for the task.

    There is deliberately no "any ``*.patch`` will do" fallback: several nodes
    at the same level dispatch the *same* workflow concurrently, so accepting an
    arbitrary patch risks attributing another task's work to this node.
    """
    if not task_id:
        return None
    with suppress(OSError):
        candidates = sorted(p for p in root.rglob("*.patch") if p.is_file())
        exact = [p for p in candidates if p.name == f"{task_id}.patch"]
        named = [p for p in candidates if task_id in p.name]
        for group in (exact, named):
            if group:
                return group[0]
    return None


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class GhAwRunner:
    """Dispatch one task node to a GitHub Agentic Workflow and collect its patch.

    Requires the ``gh`` CLI, the ``gh-aw`` extension, an authenticated host and
    a compiled agentic workflow in the target repository. Cost is whatever the
    workflow's engine consumes plus GitHub Actions minutes.
    """

    name = "gh-aw"
    kind = "agents"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        try:
            if shutil.which("gh") is None:
                return False, "gh CLI not on PATH (https://cli.github.com)"
            installed = _extension_installed()
            if installed is None:
                looked = ", ".join(str(d) for d in extension_dirs()[:3])
                return False, (
                    f"gh extension '{EXTENSION}' is not installed "
                    f"(gh extension install githubnext/gh-aw); looked in {looked}"
                )
            ok, where = _authenticated()
            if not ok:
                return False, (
                    "gh is installed with gh-aw but not authenticated: no "
                    f"{' or '.join('$' + v for v in TOKEN_ENV_VARS)} and no "
                    f"{gh_config_dir() / 'hosts.yml'} (run `gh auth login`)"
                )
            slug, source = resolve_repo_slug(cfg)
            if not slug:
                return False, f"gh-aw needs owner/repo: {source}"
            return True, f"gh CLI + {EXTENSION} extension for {slug}; auth from {where}"
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"gh-aw detection failed: {exc.__class__.__name__}: {exc}"

    def __init__(self, workflow: str | None = None, ref: str | None = None) -> None:
        self.workflow = workflow or os.environ.get("ADLC_GHAW_WORKFLOW") or DEFAULT_WORKFLOW
        self.ref = ref or os.environ.get("ADLC_GHAW_REF") or ""
        #: ``workflow`` uses ``gh workflow run`` (GA); ``aw`` uses ``gh aw run``.
        self.mode = (os.environ.get("ADLC_GHAW_MODE") or "workflow").strip().lower()

    # -- gh wrappers -------------------------------------------------------
    async def list_runs(self, slug: str, limit: int = 30) -> list[dict[str, Any]] | None:
        """Recent runs of this workflow, or ``None`` when the query failed.

        ``None`` must never be treated as "no runs": a transient failure of the
        pre-dispatch snapshot would make every pre-existing run look new.
        """
        result = await run_gh(
            "run", "list", "--repo", slug, "--workflow", self.workflow,
            "--limit", str(limit), "--json", "databaseId,displayTitle,status,conclusion",
        )
        if not result.ok:
            return None
        rows = result.json()
        if not isinstance(rows, list):
            return None
        return [row for row in rows if isinstance(row, dict) and "databaseId" in row]

    async def list_run_ids(self, slug: str, limit: int = 30) -> set[int] | None:
        rows = await self.list_runs(slug, limit)
        return None if rows is None else {row["databaseId"] for row in rows}

    async def dispatch(self, slug: str, inputs: dict[str, str]) -> GhResult:
        if self.mode == "aw":
            args = ["aw", "run", self.workflow, "--repo", slug]
            for key, value in inputs.items():
                args += ["-f", f"{key}={value}"]
            return await run_gh(*args)
        args = ["workflow", "run", self.workflow, "--repo", slug, "--json"]
        if self.ref:
            args += ["--ref", self.ref]
        return await run_gh(*args, stdin=json.dumps(inputs))

    async def view_run(self, slug: str, run_id: int) -> dict[str, Any]:
        result = await run_gh(
            "run", "view", str(run_id), "--repo", slug, "--json", "status,conclusion,url"
        )
        data = result.json()
        return data if isinstance(data, dict) else {}

    # -- Protocol ----------------------------------------------------------
    async def run_task(self, node: TaskNode, worktree: Path, cfg: Config) -> TaskOutcome:
        available, reason = self.detect(cfg)
        if not available:
            return fail(f"gh-aw unavailable: {reason}")
        slug, _ = resolve_repo_slug(cfg)
        if not slug:  # pragma: no cover - detect() already guaranteed it
            return fail("gh-aw unavailable: repository disappeared after detect()")

        write_set = usable_write_set(node)
        if not write_set:
            return fail(f"task {node.get('id', '?')} declares an empty writeSet")

        base_sha = await sha_of(worktree)
        if not base_sha:
            return fail(f"{worktree} is not a git worktree with a resolvable HEAD")

        task_id = str(node.get("id") or "task")
        inputs = {
            "task_id": task_id,
            "base_sha": base_sha,
            "write_set": "\n".join(write_set),
            "prompt": build_prompt(node, worktree)[:MAX_PROMPT_CHARS],
        }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + task_timeout(cfg)

        before = await self.list_run_ids(slug)
        if before is None:
            return fail(
                f"could not list existing runs of '{self.workflow}' in {slug}; refusing to "
                "dispatch, because without a baseline a pre-existing run would be mistaken "
                "for this task's"
            )
        dispatched = await self.dispatch(slug, inputs)
        if not dispatched.ok:
            return fail(
                f"could not dispatch workflow '{self.workflow}' in {slug}: "
                f"{dispatched.err or dispatched.out}".strip()
            )

        run_id, why = await self._await_new_run(slug, before, task_id, deadline)
        if run_id is None:
            return fail(why)

        run = await self._await_completion(slug, run_id, deadline, self._interval(cfg))
        status, conclusion = run.get("status"), run.get("conclusion")
        log = f"gh-aw run {run_id} ({run.get('url', '')}) status={status} conclusion={conclusion}"
        if status != "completed" or conclusion != "success":
            return fail(f"{log} — workflow did not succeed")

        return await self._collect(node, worktree, cfg, slug, run_id, task_id, write_set, log)

    # -- internals ---------------------------------------------------------
    async def _await_new_run(
        self, slug: str, before: set[int], task_id: str, deadline: float
    ) -> tuple[int | None, str]:
        """Correlate the dispatch with its run id.

        ``workflow_dispatch`` does not return a run id and GitHub does not
        expose dispatch inputs on the run, so correlation is done in two steps:

        1. diff the run list against a snapshot taken *before* dispatching --
           ids rather than timestamps, so no clock agreement is required;
        2. among the new runs, prefer the one whose ``displayTitle`` is this
           task's id, which the workflow sets with
           ``run-name: ${{ inputs.task_id }}``.

        ``docs/PLAN.md`` §4.4 runs every node at a level concurrently against
        the *same* workflow, so step 2 is what stops one node adopting a
        sibling's run. If several new runs appear and none is identifiable,
        this fails closed rather than guessing.
        """
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            rows = await self.list_runs(slug)
            if rows is not None:
                new = [row for row in rows if row["databaseId"] not in before]
                titled = [row for row in new if str(row.get("displayTitle") or "") == task_id]
                if titled:
                    return max(row["databaseId"] for row in titled), ""
                if len(new) == 1:
                    return new[0]["databaseId"], ""
                if len(new) > 1:
                    return None, (
                        f"{len(new)} new runs of '{self.workflow}' appeared and none is "
                        f"identifiable as task {task_id}; add "
                        "`run-name: ${{ inputs.task_id }}` to the workflow so concurrent "
                        "nodes can be told apart"
                    )
            await asyncio.sleep(min(5.0, max(0.0, deadline - loop.time())))
        return None, (
            f"workflow '{self.workflow}' was dispatched in {slug} but no new run "
            "appeared before the task budget expired"
        )

    async def _await_completion(
        self, slug: str, run_id: int, deadline: float, interval: float
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        run: dict[str, Any] = {}
        while True:
            run = await self.view_run(slug, run_id)
            if run.get("status") == "completed":
                return run
            if loop.time() >= deadline:
                return {**run, "status": run.get("status") or "unknown", "conclusion": "timed_out"}
            await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))

    async def _collect(
        self,
        node: TaskNode,
        worktree: Path,
        cfg: Config,
        slug: str,
        run_id: int,
        task_id: str,
        write_set: list[str],
        log: str,
    ) -> TaskOutcome:
        """Download the run's artifacts and promote the patch, write-set checked.

        The artifact bytes are produced by a remote workflow, so nothing here
        trusts a hand-written diff parse: git enumerates the paths
        (:func:`enumerate_patch_paths`), git decides whether the patch is
        anchored to the base SHA (``git apply``), and git reports what actually
        landed (:func:`changed_paths`). Any violation is rolled back.
        """
        tmp = Path(tempfile.mkdtemp(prefix="adlc-ghaw-"))
        try:
            downloaded = await run_gh("run", "download", str(run_id), "--repo", slug, "--dir",
                                      str(tmp))
            if not downloaded.ok:
                return fail(
                    f"could not download artifacts for run {run_id}: "
                    f"{downloaded.err or downloaded.out}".strip(),
                    log,
                )
            source = select_patch_file(tmp, task_id)
            if source is None:
                return fail(
                    f"run {run_id} uploaded no '{task_id}.patch' artifact — the agentic "
                    f"workflow '{self.workflow}' must upload one named for the task",
                    log,
                )

            destination = resolve_patch_path(node, worktree, cfg)
            size = write_patch_text(destination, source.read_bytes())

            declared = await enumerate_patch_paths(worktree, destination)
            if declared is None:
                destination.unlink(missing_ok=True)
                base = await sha_of(worktree)
                return fail(
                    f"git could not parse {source.name} from run {run_id}, or it is not "
                    f"anchored to base {(base or '?')[:12]}",
                    log,
                )
            violations = violating_paths(declared, write_set)
            if violations:
                destination.unlink(missing_ok=True)
                listed = ", ".join(violations[:10]) + (" …" if len(violations) > 10 else "")
                return fail(
                    f"refused: patch from run {run_id} touches {len(violations)} path(s) "
                    f"outside writeSet: {listed}",
                    log,
                )

            base = await sha_of(worktree)
            applied = await apply_patch(worktree, destination)
            if not applied.ok:
                destination.unlink(missing_ok=True)
                return fail(
                    f"patch from run {run_id} did not apply at base {(base or '?')[:12]} — "
                    f"the workflow must anchor it to base_sha: {applied.err}",
                    log,
                )

            # Re-check against what git says actually landed. `git apply` honours
            # quoted headers and rename records that a pre-scan can misread, so
            # this is the authoritative check.
            landed = await changed_paths(worktree)
            if landed is None or violating_paths(landed, write_set):
                await run_git(worktree, "apply", "--reverse", "--index", str(destination))
                destination.unlink(missing_ok=True)
                outside = "unknown" if landed is None else ", ".join(
                    violating_paths(landed, write_set)[:10]
                )
                return fail(
                    f"refused: after applying, patch from run {run_id} had touched paths "
                    f"outside writeSet ({outside}); the change has been rolled back",
                    log,
                )
            return {
                "status": "ok",
                "patchPath": str(destination),
                "log": (
                    f"{log}\napplied {source.name} ({size} bytes) at verified base "
                    f"{(base or '?')[:12]}, covering {len(landed)} path(s)"
                ),
            }
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _interval(cfg: Config) -> float:
        with suppress(Exception):
            return max(1.0, float((cfg.limits or {}).get("pollSeconds") or DEFAULT_POLL_SECONDS))
        return DEFAULT_POLL_SECONDS
