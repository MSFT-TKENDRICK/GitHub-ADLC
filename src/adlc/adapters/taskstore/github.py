"""GitHub Issues task store (leaf L5).

Projects a ``taskgraph.json`` DAG onto GitHub's native work-tracking primitives:
one **parent issue** per run and one **sub-issue per task node**, linked with the
GA sub-issues REST API.

Design notes that matter (see ``docs/taskstore.md`` for the full write-up):

* ``POST /repos/{owner}/{repo}/issues/{n}/sub_issues`` takes ``sub_issue_id`` --
  the issue's numeric **database id**, *not* its number. Mixing them up silently
  links the wrong issue, so :class:`GitHubTaskStore` never passes a number where
  an id is wanted.
* GitHub documents a limit of **100 sub-issues per parent** and **8 levels** of
  nesting. Larger graphs are chunked across several parent issues which are
  themselves nested under a root issue (3 levels), so a run is always reachable
  from a single entry point.
* Issue dependencies **do** have a documented creation endpoint:
  ``POST /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`` with body
  ``{"issue_id": <id>}``. It is used here, but only as a *projection*:
  ``taskgraph.json`` remains authoritative for dependencies, and every edge is
  also rendered into the issue body as text plus a mermaid snippet so the graph
  survives even when the API is unavailable.
* ``sync()`` is called repeatedly for the same run, so every write is guarded by
  a stable marker: ``<!-- adlc:run=<runId> node=<nodeId> -->``.

This adapter is **optional**. Without a token :meth:`GitHubTaskStore.detect`
returns ``(False, reason)`` and the spine's credential-free SQLite store is used
instead.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from adlc.adapters._transport import require_https
from adlc.ports import TaskGraph, TaskNode

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

__all__ = [
    "SUB_ISSUE_LIMIT",
    "GitHubTaskStore",
    "GitHubTaskStoreError",
    "RestTransport",
    "node_marker",
    "parent_marker",
    "render_node_body",
    "root_marker",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Documented GitHub limit on sub-issues attached to a single parent issue.
#: Verified 2026-08-19 against
#: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues
#: ("You can add up to 100 sub-issues per parent issue and create up to eight
#: levels of nested sub-issues.") Lower it via ``taskstore.github.maxSubIssues``.
SUB_ISSUE_LIMIT = 100

#: Documented maximum nesting depth. This adapter uses at most 3 (root/part/task).
NESTING_DEPTH_LIMIT = 8

#: GitHub rejects issue bodies over 65_536 characters; leave room for markers.
BODY_BUDGET = 60_000

#: Labels are capped at 50 characters by the API.
LABEL_MAX = 50

RUN_LABEL_PREFIX = "adlc-run:"
STATUS_LABEL_PREFIX = "adlc-status:"

#: Label namespaces this adapter owns and therefore *replaces* rather than
#: accumulates. Anything else on an issue (including human-added labels and
#: ``adlc-status:*``, which ``update()`` manages) is left alone.
OWNED_LABEL_PREFIXES = ("adlc-run:", "adlc-kind:", "adlc-level:")

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_SERVER_URL = "https://github.com"

#: Additive endpoints (sub-issues, dependencies) are available in every supported
#: REST version, so the long-lived stable pin is used. Override if you must.
API_VERSION = os.environ.get("GITHUB_API_VERSION") or "2022-11-28"

#: Marker embedded in every issue body this adapter owns. Identity, not decoration.
MARKER_RE = re.compile(
    r"<!--\s*adlc:run=(?P<run>\S+)\s+(?P<key>node|parent|root)=(?P<value>\S+)\s*-->"
)
UPDATE_MARKER_RE = re.compile(
    r"<!--\s*adlc:update\s+run=(?P<run>\S+)\s+node=(?P<node>\S+)\s+digest=(?P<digest>\S+)\s*-->"
)

#: ``status`` strings accepted by :meth:`GitHubTaskStore.update`, normalised.
_TERMINAL_OK = {"ok", "done", "complete", "completed", "pass", "passed", "merged"}
_TERMINAL_SKIPPED = {"skipped", "skip", "wont_do", "not_planned", "cancelled", "canceled"}
_OPEN_STATES = {
    "pending", "queued", "ready", "in_progress", "running", "started",
    "blocked", "fail", "failed", "error", "retry", "replan",
}

_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")

_REMOTE_URL_RE = re.compile(r"^\s*url\s*=\s*(?P<url>\S+)\s*$", re.MULTILINE)


class GitHubTaskStoreError(RuntimeError):
    """Raised when the store cannot honour a request. Never swallowed silently."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@runtime_checkable
class GitHubTransport(Protocol):
    """The seam that lets the projection logic be unit-tested without network."""

    def request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Return ``(status_code, decoded_json_or_None)``. Must not raise on 404."""
        ...

    def paginate(self, path: str) -> list[Any]:
        """Return every item of a paginated collection endpoint."""
        ...

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        """Execute a GraphQL document and return the ``data`` payload."""
        ...


class RestTransport:
    """A dependency-free GitHub client built on :mod:`urllib`.

    Deliberately stdlib-only: this leaf must not add a required dependency to
    ``pyproject.toml``.
    """

    #: Statuses that are worth retrying (secondary rate limits, transient 5xx).
    _RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

    def __init__(
        self,
        token: str,
        api_url: str = DEFAULT_API_URL,
        graphql_url: str | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        opener: Any | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url or f"{self.api_url}/graphql"
        self.timeout = timeout
        self.retries = max(1, retries)
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep

    # -- low level --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "adlc-taskstore-github",
            "Content-Type": "application/json",
        }

    def _absolute(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.api_url}/{path.lstrip('/')}"

    def _open(
        self, method: str, url: str, body: Mapping[str, Any] | None
    ) -> tuple[int, Any, dict[str, str]]:
        require_https(url)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method=method.upper())  # noqa: S310 - scheme checked above
        for key, value in self._headers().items():
            req.add_header(key, value)

        last: tuple[int, Any, dict[str, str]] | None = None
        for attempt in range(self.retries):
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    return resp.status, _decode(raw), headers
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                last = (exc.code, _decode(raw), headers)
                if exc.code not in self._RETRY_STATUSES or attempt == self.retries - 1:
                    return last
                self._sleep(_retry_delay(headers, attempt))
            except urllib.error.URLError as exc:
                raise GitHubTaskStoreError(f"{method} {url} failed: {exc.reason}") from exc
            except (OSError, http.client.HTTPException) as exc:
                # urllib does not wrap everything: a read timeout, a reset
                # connection, or a truncated response arrives raw. Callers rely
                # on this transport only ever emitting its own error type.
                raise GitHubTaskStoreError(f"{method} {url} failed: {exc!r}") from exc
        if last is None:
            # Unreachable while retries >= 1: every loop iteration either
            # returns or assigns `last`. Raising rather than asserting means the
            # invariant still holds under `python -O`, where asserts vanish.
            raise GitHubTaskStoreError(
                f"{method} {url} produced no response after {self.retries} attempt(s)"
            )
        return last

    # -- transport protocol ----------------------------------------------
    def request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> tuple[int, Any]:
        status, data, _ = self._open(method, self._absolute(path), body)
        return status, data

    def paginate(self, path: str) -> list[Any]:
        url: str | None = self._absolute(path)
        items: list[Any] = []
        while url:
            status, data, headers = self._open("GET", url, None)
            if status == 404:
                return items
            if status >= 400:
                raise GitHubTaskStoreError(f"GET {url} -> {status}: {_message(data)}")
            if isinstance(data, list):
                items.extend(data)
            elif data is not None:
                items.append(data)
            url = _next_link(headers.get("link", ""))
        return items

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        status, data, _ = self._open(
            "POST", self.graphql_url, {"query": query, "variables": dict(variables)}
        )
        if status >= 400:
            raise GitHubTaskStoreError(f"GraphQL request failed ({status}): {_message(data)}")
        if isinstance(data, Mapping) and data.get("errors"):
            raise GitHubTaskStoreError(f"GraphQL errors: {json.dumps(data['errors'])}")
        return (data or {}).get("data")


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _message(data: Any) -> str:
    if isinstance(data, Mapping):
        parts = [str(data.get("message", ""))]
        for err in data.get("errors", []) or []:
            if isinstance(err, Mapping):
                parts.append(str(err.get("message") or err.get("code") or err))
            else:
                parts.append(str(err))
        return " | ".join(p for p in parts if p)
    return str(data)


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    for key in ("retry-after", "x-ratelimit-reset"):
        raw = headers.get(key)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if key == "x-ratelimit-reset":
            value = max(0.0, value - time.time())
        return min(60.0, max(1.0, value))
    return min(60.0, 2.0**attempt)


def _next_link(link_header: str) -> str | None:
    for chunk in link_header.split(","):
        parts = chunk.split(";")
        if len(parts) < 2:
            continue
        url = parts[0].strip().strip("<>")
        if any('rel="next"' in p.replace("'", '"').strip() for p in parts[1:]):
            return url
    return None


# ---------------------------------------------------------------------------
# Markers & rendering  (pure functions -- unit-testable without any client)
# ---------------------------------------------------------------------------


def node_marker(run_id: str, node_id: str) -> str:
    return f"<!-- adlc:run={run_id} node={node_id} -->"


def parent_marker(run_id: str, part: int) -> str:
    return f"<!-- adlc:run={run_id} parent={part} -->"


def root_marker(run_id: str) -> str:
    return f"<!-- adlc:run={run_id} root=1 -->"


def run_label(run_id: str) -> str:
    """A per-run label used to *narrow* the candidate set before marker matching."""
    label = f"{RUN_LABEL_PREFIX}{run_id}"
    if len(label) <= LABEL_MAX:
        return label
    keep = LABEL_MAX - len(RUN_LABEL_PREFIX)
    return f"{RUN_LABEL_PREFIX}{run_id[-keep:]}"


def parse_marker(body: str | None) -> tuple[str, str, str] | None:
    """Return ``(run_id, kind, value)`` for an adlc-owned issue body."""
    if not body:
        return None
    match = MARKER_RE.search(body)
    if match is None:
        return None
    return match.group("run"), match.group("key"), match.group("value")


def _fence(lines: Sequence[str], lang: str = "") -> str:
    return "\n".join([f"```{lang}", *lines, "```"])


def _bullets(values: Iterable[str], empty: str) -> str:
    items = [f"- `{v}`" for v in values]
    return "\n".join(items) if items else empty


def _neighbourhood_mermaid(
    node: TaskNode, nodes_by_id: Mapping[str, TaskNode], numbers: Mapping[str, int]
) -> str:
    node_id = node["id"]
    dependants = [n["id"] for n in nodes_by_id.values() if node_id in (n.get("dependsOn") or [])]
    here = _mnode(node_id, nodes_by_id, numbers)
    edges: list[str] = []
    for dep in node.get("dependsOn") or []:
        edges.append(f"    {_mnode(dep, nodes_by_id, numbers)} --> {here}")
    for dep in dependants:
        edges.append(f"    {here} --> {_mnode(dep, nodes_by_id, numbers)}")
    if not edges:
        edges.append(f"    {here}")
    return _fence(["flowchart LR", *edges], "mermaid")


def _mnode(node_id: str, nodes_by_id: Mapping[str, TaskNode], numbers: Mapping[str, int]) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", node_id)
    label = node_id
    if node_id in numbers:
        label = f"{node_id} #{numbers[node_id]}"
    elif node_id not in nodes_by_id:
        label = f"{node_id} (not in graph)"
    return f'{ident}["{label}"]'


def _refs(node_ids: Sequence[str], numbers: Mapping[str, int]) -> str:
    if not node_ids:
        return "_none_"
    out = []
    for nid in node_ids:
        out.append(f"#{numbers[nid]} (`{nid}`)" if nid in numbers else f"`{nid}` (not yet synced)")
    return ", ".join(out)


def render_node_body(
    graph: TaskGraph,
    node: TaskNode,
    numbers: Mapping[str, int],
    *,
    run_dir: str | None = None,
) -> str:
    """Render the sub-issue body for one task node.

    Deliberately does **not** inline the context capsule: capsules are bounded to
    64 KiB on purpose and issue bodies have their own limit. The run directory is
    referenced instead.
    """
    run_id = graph.get("runId", "")
    nodes_by_id = {n["id"]: n for n in graph.get("nodes", [])}
    node_id = node["id"]
    depends_on = list(node.get("dependsOn") or [])
    blocks = sorted(n["id"] for n in nodes_by_id.values() if node_id in (n.get("dependsOn") or []))
    directory = run_dir or f".adlc/runs/{run_id}"

    sections = [
        node_marker(run_id, node_id),
        (
            f"**Task `{node_id}`** -- kind `{node.get('kind', 'implement')}` · "
            f"level `{node.get('level', 0)}` · run `{run_id}`"
        ),
        "",
        f"> {node.get('title', node_id)}",
        "",
        "### Write set",
        "",
        _bullets(node.get("writeSet") or [], "_none declared_"),
        "",
        "### Acceptance criteria",
        "",
        _bullets(node.get("acceptance") or [], "_none declared_"),
        "",
        "### Dependencies",
        "",
        f"Blocked by: {_refs(depends_on, numbers)}",
        "",
        f"Blocks: {_refs(blocks, numbers)}",
        "",
        (
            "`taskgraph.json` is the authoritative source for dependencies. The lines "
            "above are a rendering of it; GitHub issue-dependency relationships are a "
            "projection of the same data and are never read back as truth."
        ),
        "",
        _neighbourhood_mermaid(node, nodes_by_id, numbers),
        "",
        "### Run",
        "",
        f"- Run id: `{run_id}`",
        f"- Base SHA: `{graph.get('baseSha', 'unknown')}`",
        f"- Run directory: `{directory}/`",
        f"- Task graph: `{directory}/taskgraph.json`",
        f"- Patch (when built): `{directory}/patches/{node_id}.patch`",
    ]
    optional = [
        ("Rubrics", node.get("rubricIds")),
        ("ADRs", node.get("adrRefs")),
    ]
    for title, values in optional:
        if values:
            sections.extend(["", f"### {title}", "", _bullets(values, "_none_")])
    if node.get("context"):
        sections.extend(
            [
                "",
                (
                    "_A bounded context capsule exists for this node. It is not inlined "
                    f"here (64 KiB budget); read it from `{directory}/taskgraph.json`._"
                ),
            ]
        )
    return _truncate("\n".join(sections))


def render_parent_body(
    graph: TaskGraph,
    part: int,
    total_parts: int,
    nodes: Sequence[TaskNode],
    numbers: Mapping[str, int],
    *,
    run_dir: str | None = None,
    limit: int = SUB_ISSUE_LIMIT,
) -> str:
    run_id = graph.get("runId", "")
    directory = run_dir or f".adlc/runs/{run_id}"
    rows = ["| Task | Kind | Level | Issue |", "| --- | --- | --- | --- |"]
    for node in nodes:
        ref = f"#{numbers[node['id']]}" if node["id"] in numbers else "_pending_"
        rows.append(
            f"| `{node['id']}` {node.get('title', '')} | {node.get('kind', '')} "
            f"| {node.get('level', 0)} | {ref} |"
        )
    heading = f"ADLC run `{run_id}`"
    if total_parts > 1:
        heading += f" -- part {part} of {total_parts}"
    sections = [
        parent_marker(run_id, part),
        f"# {heading}",
        "",
        f"- Base SHA: `{graph.get('baseSha', 'unknown')}`",
        f"- Spec digest: `{graph.get('specDigest', 'unknown')}`",
        f"- Run directory: `{directory}/`",
        (
            f"- Tasks in this part: {len(nodes)} (at most "
            f"{limit} sub-issues per parent)"
        ),
        "",
        (
            "Progress is tracked by GitHub's native `sub_issues_summary`. Dependencies "
            "between tasks live in `taskgraph.json`, which stays authoritative."
        ),
        "",
        *rows,
    ]
    return _truncate("\n".join(sections))


def render_root_body(
    graph: TaskGraph,
    parts: Sequence[int],
    numbers: Mapping[str, int],
    *,
    run_dir: str | None = None,
    limit: int = SUB_ISSUE_LIMIT,
) -> str:
    run_id = graph.get("runId", "")
    directory = run_dir or f".adlc/runs/{run_id}"
    listed = "\n".join(
        f"- Part {i}: " + (f"#{numbers[f'__part{i}']}" if f"__part{i}" in numbers else "_pending_")
        for i in parts
    )
    sections = [
        root_marker(run_id),
        f"# ADLC run `{run_id}`",
        "",
        (
            f"This graph has more than {limit} tasks, so it is split across "
            f"{len(parts)} parent issues nested under this one."
        ),
        "",
        listed,
        "",
        f"- Base SHA: `{graph.get('baseSha', 'unknown')}`",
        f"- Run directory: `{directory}/`",
    ]
    return _truncate("\n".join(sections))


def _truncate(body: str) -> str:
    if len(body) <= BODY_BUDGET:
        return body
    notice = "\n\n_…truncated to fit the GitHub issue body limit._"
    return body[: BODY_BUDGET - len(notice)].rstrip() + notice


def plan_chunks(nodes: Sequence[TaskNode], limit: int = SUB_ISSUE_LIMIT) -> list[list[TaskNode]]:
    """Split ordered nodes into parent-sized chunks, never exceeding ``limit``."""
    if limit < 1:
        raise GitHubTaskStoreError(f"sub-issue limit must be >= 1, got {limit}")
    return [list(nodes[i : i + limit]) for i in range(0, len(nodes), limit)] or [[]]


def order_nodes(nodes: Iterable[TaskNode]) -> list[TaskNode]:
    """Deterministic order: level first, then id. Keeps chunks level-coherent."""
    return sorted(nodes, key=lambda n: (int(n.get("level", 0)), str(n.get("id", ""))))


# ---------------------------------------------------------------------------
# Repository / credential resolution  (no network, never raises)
# ---------------------------------------------------------------------------


def _git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("gitdir:"):
                candidate = Path(line.split(":", 1)[1].strip())
                if not candidate.is_absolute():
                    candidate = (root / candidate).resolve()
                return candidate
    return None


def _git_config_path(root: Path) -> Path | None:
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    common = git_dir / "commondir"
    if common.is_file():
        try:
            rel = common.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            rel = ""
        if rel:
            candidate = Path(rel)
            if not candidate.is_absolute():
                candidate = (git_dir / candidate).resolve()
            if (candidate / "config").is_file():
                return candidate / "config"
    cfg = git_dir / "config"
    return cfg if cfg.is_file() else None


def _parse_remote(url: str, host: str) -> tuple[str, str] | None:
    cleaned = url.strip()
    cleaned = cleaned.removesuffix(".git")
    for prefix in (f"git@{host}:", f"ssh://git@{host}/", f"https://{host}/", f"http://{host}/"):
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix) :].strip("/")
            parts = rest.split("/")
            if len(parts) >= 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
    return None


def resolve_repo(
    cfg: Config | None, settings: Mapping[str, Any] | None = None
) -> tuple[str, str] | None:
    """Resolve ``(owner, repo)`` from config, env, or a github.com git remote."""
    settings = settings or {}
    candidates = [settings.get("repo"), os.environ.get("GITHUB_REPOSITORY")]
    for candidate in candidates:
        if candidate and "/" in str(candidate):
            owner, _, name = str(candidate).partition("/")
            if owner and name:
                return owner, name.split("/")[0]

    host = urllib.parse.urlparse(
        os.environ.get("GITHUB_SERVER_URL") or DEFAULT_SERVER_URL
    ).netloc or "github.com"
    root = getattr(cfg, "root", None)
    if root is None:
        return None
    config_path = _git_config_path(Path(root))
    if config_path is None:
        return None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in _REMOTE_URL_RE.finditer(text):
        parsed = _parse_remote(match.group("url"), host)
        if parsed:
            return parsed
    return None


def resolve_token() -> str | None:
    for var in _TOKEN_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class GitHubTaskStore:
    """``TaskStore`` backed by GitHub issues + sub-issues (+ optional Projects v2)."""

    name = "github"
    kind = "taskstore"

    def __init__(
        self,
        cfg: Config | None = None,
        transport: GitHubTransport | None = None,
        *,
        owner: str | None = None,
        repo: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self._transport = transport
        self._explicit_settings = settings is not None
        self._explicit_repo = owner is not None and repo is not None
        self._owner, self._repo = owner, repo
        self._apply_settings(
            dict(settings) if settings is not None else _settings_from_cfg(cfg)
        )

        #: Populated by :meth:`sync`. ``node id -> {"id", "number", "url"}``.
        self.node_records: dict[str, dict[str, Any]] = {}
        #: Parent issue records, in part order.
        self.parents: list[dict[str, Any]] = []
        #: Root issue record when the graph needed more than one parent.
        self.root: dict[str, Any] | None = None
        #: ``issue number -> sub_issues_summary`` as returned by the API.
        self.summaries: dict[int, dict[str, Any]] = {}
        #: Non-fatal problems (e.g. Projects v2 failures) surfaced to the caller.
        self.warnings: list[str] = []
        self._run_id: str | None = None

    def _apply_settings(self, settings: Mapping[str, Any]) -> None:
        self._settings = dict(settings)
        self.sub_issue_limit = int(self._settings.get("maxSubIssues", SUB_ISSUE_LIMIT))
        self.sync_dependencies = bool(self._settings.get("syncDependencies", True))
        self.enable_projects = bool(self._settings.get("enableProjects", False))
        self.project_id = self._settings.get("projectId") or os.environ.get("ADLC_GITHUB_PROJECT")
        self.project_fields: dict[str, Any] = dict(self._settings.get("projectFields") or {})
        self.extra_labels: list[str] = list(self._settings.get("labels") or [])
        if not self._explicit_repo:
            resolved = resolve_repo(self.cfg, self._settings)
            if resolved:
                self._owner, self._repo = resolved

    def bind(self, cfg: Config) -> None:
        """Attach this store to a config. Called by the graph stage before ``sync()``.

        ``select_adapter`` instantiates adapters with no arguments, so this is
        the only point at which the repository root -- and therefore the git
        remote and the ``taskstore.github`` config block -- becomes available.
        """
        self.cfg = cfg
        self._apply_settings(
            self._settings if self._explicit_settings else _settings_from_cfg(cfg)
        )

    # -- detection --------------------------------------------------------
    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, non-raising, no network. Env vars and one small file read only."""
        try:
            if resolve_token() is None:
                return False, "GITHUB_TOKEN not set — falling back to sqlite task store"
            settings = _settings_from_cfg(cfg)
            resolved = resolve_repo(cfg, settings)
            if resolved is None:
                return (
                    False,
                    (
                        "GITHUB_REPOSITORY not set and no github.com git remote found -- "
                        "falling back to sqlite task store"
                    ),
                )
            owner, repo = resolved
            return True, f"GitHub Issues task store available for {owner}/{repo}"
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"GitHub task store unavailable: {exc}"

    # -- plumbing ---------------------------------------------------------
    @property
    def slug(self) -> str:
        if not self._owner or not self._repo:
            raise GitHubTaskStoreError(
                "GitHub repository unresolved -- set GITHUB_REPOSITORY as 'owner/repo'"
            )
        return f"{self._owner}/{self._repo}"

    @property
    def transport(self) -> GitHubTransport:
        if self._transport is None:
            token = resolve_token()
            if token is None:
                raise GitHubTaskStoreError(
                    "GITHUB_TOKEN not set -- the GitHub task store cannot be used"
                )
            self._transport = RestTransport(
                token,
                api_url=os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL,
                graphql_url=os.environ.get("GITHUB_GRAPHQL_URL"),
            )
        return self._transport

    def _issues_path(self, suffix: str = "") -> str:
        return f"/repos/{self.slug}/issues{suffix}"

    def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        allow: Sequence[int] = (),
    ) -> tuple[int, Any]:
        status, data = self.transport.request(method, path, body)
        if status >= 400 and status not in allow:
            raise GitHubTaskStoreError(f"{method} {path} -> {status}: {_message(data)}")
        return status, data

    # -- TaskStore protocol ----------------------------------------------
    def sync(self, graph: TaskGraph) -> dict[str, str]:
        """Project ``graph`` onto issues. Idempotent: safe to call repeatedly.

        Returns ``{node_id: "<owner>/<repo>#<number>"}`` -- a self-describing
        external id, matching the spine SQLite store's ``sqlite:<run>/<node>``
        convention. Richer records (``id``, ``number``, ``node_id``, ``url``)
        stay available on :attr:`node_records`.
        """
        run_id = str(graph.get("runId") or "").strip()
        if not run_id:
            raise GitHubTaskStoreError("taskgraph is missing 'runId'; cannot sync to GitHub")
        # A runId that cannot survive a round trip through the body marker would
        # make every sync() re-create every issue, silently and forever. "-->"
        # round-trips through the regex but terminates the HTML comment early,
        # leaking the rest of the marker into the rendered issue.
        if "-->" in run_id or parse_marker(node_marker(run_id, "T000")) != (
            run_id,
            "node",
            "T000",
        ):
            raise GitHubTaskStoreError(
                f"runId {run_id!r} cannot be encoded in an issue marker (no whitespace "
                "or '-->' allowed); idempotency would be lost"
            )

        nodes = order_nodes(graph.get("nodes") or [])
        seen: set[str] = set()
        for node in nodes:
            node_id = str(node.get("id") or "")
            if not node_id:
                raise GitHubTaskStoreError("taskgraph contains a node without an 'id'")
            if node_id in seen:
                raise GitHubTaskStoreError(f"taskgraph contains duplicate node id '{node_id}'")
            seen.add(node_id)

        chunks = plan_chunks(nodes, self.sub_issue_limit)
        if len(chunks) > self.sub_issue_limit:
            raise GitHubTaskStoreError(
                f"{len(nodes)} tasks need {len(chunks)} parent issues, which exceeds the "
                f"{self.sub_issue_limit} sub-issues-per-parent limit for the root issue. "
                "Split the run into several task graphs."
            )

        self._run_id = run_id
        self.node_records = {}
        self.parents = []
        self.root = None
        self.warnings = []

        label = run_label(run_id)
        index = self._index_run_issues(run_id, label)
        run_dir = self._run_dir(run_id)

        # Pass 1 -- ensure every issue exists so cross-references can resolve.
        numbers: dict[str, int] = {}
        for part, chunk in enumerate(chunks, start=1):
            record = self._ensure_issue(
                key=("parent", str(part)),
                index=index,
                title=_parent_title(run_id, part, len(chunks)),
                body=render_parent_body(
                    graph, part, len(chunks), chunk, {}, run_dir=run_dir,
                    limit=self.sub_issue_limit,
                ),
                labels=[label, "adlc", "adlc-run-parent", *self.extra_labels],
            )
            self.parents.append(record)
            numbers[f"__part{part}"] = record["number"]

        parts = list(range(1, len(chunks) + 1))
        if len(chunks) > 1:
            self.root = self._ensure_issue(
                key=("root", "1"),
                index=index,
                title=f"ADLC run {run_id}",
                body=render_root_body(
                    graph, parts, numbers, run_dir=run_dir, limit=self.sub_issue_limit
                ),
                labels=[label, "adlc", "adlc-run-root", *self.extra_labels],
            )

        for node in nodes:
            record = self._ensure_issue(
                key=("node", node["id"]),
                index=index,
                title=_node_title(node),
                body=render_node_body(graph, node, {}, run_dir=run_dir),
                labels=self._node_labels(label, node),
            )
            self.node_records[node["id"]] = record
            numbers[node["id"]] = record["number"]

        # Pass 2 -- rewrite bodies now that every issue number is known.
        for node in nodes:
            self._sync_body(
                self.node_records[node["id"]],
                render_node_body(graph, node, numbers, run_dir=run_dir),
            )
        for part, chunk in enumerate(chunks, start=1):
            self._sync_body(
                self.parents[part - 1],
                render_parent_body(
                    graph, part, len(chunks), chunk, numbers, run_dir=run_dir,
                    limit=self.sub_issue_limit,
                ),
            )
        if self.root is not None:
            self._sync_body(
                self.root,
                render_root_body(
                    graph, parts, numbers, run_dir=run_dir, limit=self.sub_issue_limit
                ),
            )

        # Pass 2b -- retire nodes a replan removed, so the rollup stays truthful.
        self._retire_orphans(index)

        # Pass 3 -- attach sub-issues (by id, never by number).
        for part, chunk in enumerate(chunks, start=1):
            parent = self.parents[part - 1]
            self._attach_sub_issues(parent, [self.node_records[n["id"]] for n in chunk])
        if self.root is not None:
            self._attach_sub_issues(self.root, list(self.parents))

        # Pass 4 -- project dependency edges onto GitHub's issue-dependency API.
        # taskgraph.json stays authoritative; this is a best-effort mirror.
        if self.sync_dependencies:
            self._sync_dependencies(nodes)

        # Pass 5 -- optional Projects v2 wiring. Must never fail the sync.
        if self.enable_projects:
            self._sync_project(nodes)

        return {
            node_id: f"{self.slug}#{rec['number']}"
            for node_id, rec in self.node_records.items()
        }

    def update(self, node_id: str, status: str, note: str = "") -> None:
        """Record ``status`` on the issue for ``node_id``: comment, label, open/close."""
        record = self.node_records.get(node_id)
        if record is None:
            record = self._relocate(node_id)
        normalised = (status or "").strip().lower()
        if not normalised:
            raise GitHubTaskStoreError(f"update({node_id!r}) requires a non-empty status")

        number = record["number"]
        digest = _digest(f"{normalised}\x1f{note}")
        if self._last_update_digest(number, node_id) != digest:
            marker = f"<!-- adlc:update run={self._run_id} node={node_id} digest={digest} -->"
            lines = [marker, f"**Status → `{normalised}`**"]
            if note:
                lines.extend(["", note])
            self._call("POST", self._issues_path(f"/{number}/comments"), {"body": "\n".join(lines)})

        patch: dict[str, Any] = {"labels": self._status_labels(record, normalised)}
        if normalised in _TERMINAL_OK:
            patch.update(state="closed", state_reason="completed")
        elif normalised in _TERMINAL_SKIPPED:
            patch.update(state="closed", state_reason="not_planned")
        elif normalised in _OPEN_STATES:
            patch.update(state="open")
        _, data = self._call("PATCH", self._issues_path(f"/{number}"), patch)
        if isinstance(data, Mapping):
            record.update(_record(data))
        self._refresh_summaries()

    # -- progress ---------------------------------------------------------
    def progress(self) -> dict[str, Any]:
        """Surface GitHub's native ``sub_issues_summary`` for the synced run.

        Only the task-holding parents are aggregated. The root issue's own
        summary counts *parents*, not tasks, so including it would double count.
        """
        totals = {"total": 0, "completed": 0}
        for parent in self.parents:
            summary = self.summaries.get(parent["number"], {})
            totals["total"] += int(summary.get("total") or 0)
            totals["completed"] += int(summary.get("completed") or 0)
        percent = round(100 * totals["completed"] / totals["total"]) if totals["total"] else 0
        return {
            "runId": self._run_id,
            "repo": f"{self._owner}/{self._repo}" if self._owner and self._repo else None,
            "root": self.root["number"] if self.root else None,
            "parents": [p["number"] for p in self.parents],
            "subIssuesSummary": {**totals, "percent_completed": percent},
            "perParent": dict(self.summaries),
            "warnings": list(self.warnings),
        }

    # -- internals --------------------------------------------------------
    def _run_dir(self, run_id: str) -> str:
        """Repo-relative run directory, derived from config when it is bound."""
        if self.cfg is not None:
            try:
                run_dir = self.cfg.run_dir(run_id).resolve()
                return run_dir.relative_to(Path(self.cfg.root).resolve()).as_posix()
            except (AttributeError, OSError, ValueError):
                pass
        return f".adlc/runs/{run_id}"

    def _node_labels(self, label: str, node: TaskNode) -> list[str]:
        labels = [label, "adlc", f"adlc-kind:{node.get('kind', 'implement')}",
                  f"adlc-level:{node.get('level', 0)}"]
        labels.extend(self.extra_labels)
        return [lbl for lbl in dict.fromkeys(labels) if len(lbl) <= LABEL_MAX]

    def _status_labels(self, record: Mapping[str, Any], status: str) -> list[str]:
        existing = [
            lbl for lbl in record.get("labels", [])
            if not lbl.startswith(STATUS_LABEL_PREFIX)
        ]
        new = f"{STATUS_LABEL_PREFIX}{re.sub(r'[^a-z0-9_.-]+', '-', status)}"[:LABEL_MAX]
        return list(dict.fromkeys([*existing, new]))

    def _index_run_issues(self, run_id: str, label: str) -> dict[tuple[str, str], dict[str, Any]]:
        """Build ``(kind, value) -> record`` for issues this run already owns.

        The per-run label narrows the candidate set; the body marker is what
        actually establishes identity. Listing by label is used rather than the
        search API because search is eventually consistent, which would let a
        second ``sync()`` create duplicates.
        """
        query = urllib.parse.urlencode(
            {"labels": label, "state": "all", "per_page": "100"}
        )
        issues = self.transport.paginate(self._issues_path(f"?{query}"))
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for issue in issues:
            if not isinstance(issue, Mapping) or issue.get("pull_request"):
                continue
            parsed = parse_marker(issue.get("body"))
            if parsed is None or parsed[0] != run_id:
                continue
            index[(parsed[1], parsed[2])] = _record(issue)
        return index

    def _ensure_issue(
        self,
        *,
        key: tuple[str, str],
        index: Mapping[tuple[str, str], dict[str, Any]],
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> dict[str, Any]:
        existing = index.get(key)
        if existing is not None:
            # Owned label namespaces are *replaced*, not accumulated: a replan
            # that changes a node's kind or level must not leave it claiming
            # both. Human labels and adlc-status:* are preserved.
            desired = list(dict.fromkeys(labels))
            current = list(existing.get("labels", []))
            kept = [
                lbl for lbl in current
                if not lbl.startswith(OWNED_LABEL_PREFIXES) and lbl not in desired
            ]
            final = [*desired, *kept]
            if sorted(final) != sorted(current):
                self._call(
                    "PATCH", self._issues_path(f"/{existing['number']}"), {"labels": final}
                )
                existing["labels"] = final
            if existing.get("title") != title:
                self._call("PATCH", self._issues_path(f"/{existing['number']}"), {"title": title})
                existing["title"] = title
            return existing
        _, data = self._call(
            "POST", self._issues_path(), {"title": title, "body": body, "labels": list(labels)}
        )
        if not isinstance(data, Mapping):
            raise GitHubTaskStoreError(f"unexpected response creating issue {title!r}: {data!r}")
        return _record(data)

    def _sync_body(self, record: dict[str, Any], body: str) -> None:
        if record.get("body") == body:
            return
        _, data = self._call("PATCH", self._issues_path(f"/{record['number']}"), {"body": body})
        record["body"] = body
        if isinstance(data, Mapping):
            record.update(_record(data))

    def _attach_sub_issues(
        self, parent: Mapping[str, Any], children: Sequence[Mapping[str, Any]]
    ) -> None:
        if len(children) > self.sub_issue_limit:
            raise GitHubTaskStoreError(
                f"issue #{parent['number']} would need {len(children)} sub-issues, over "
                f"GitHub's documented limit of {self.sub_issue_limit}"
            )
        path = self._issues_path(f"/{parent['number']}/sub_issues")
        existing_ids = {
            item.get("id")
            for item in self.transport.paginate(f"{path}?per_page=100")
            if isinstance(item, Mapping)
        }
        for child in children:
            if child["id"] in existing_ids:
                continue
            # NOTE: this endpoint takes the issue *id*, not its number.
            status, data = self._call("POST", path, {"sub_issue_id": child["id"]}, allow=(409, 422))
            if status >= 400:
                # The sub-issue may already hang off a different parent from an
                # earlier, differently-chunked sync. `replace_parent` re-homes it.
                status, data = self._call(
                    "POST",
                    path,
                    {"sub_issue_id": child["id"], "replace_parent": True},
                    allow=(409, 422),
                )
            if status >= 400:
                self.warnings.append(
                    f"could not attach #{child['number']} (id {child['id']}) to "
                    f"#{parent['number']}: {_message(data)}"
                )
                continue
            if isinstance(data, Mapping) and data.get("sub_issues_summary"):
                self.summaries[parent["number"]] = dict(data["sub_issues_summary"])
        self._refresh_summary(parent["number"])

    def _sync_dependencies(self, nodes: Sequence[TaskNode]) -> None:
        """Mirror ``dependsOn`` edges onto GitHub issue dependencies.

        Uses the documented endpoint
        ``POST /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`` with a
        body of ``{"issue_id": <id>}`` -- the blocker's numeric **id**, not its
        number. The relationship is only creatable from the *blocked* issue's
        side; there is no ``POST .../dependencies/blocking`` endpoint.

        Every failure is downgraded to a warning: an older GHES, a token without
        ``issues:write``, or a repository with dependencies disabled must not
        fail the sync, because ``taskgraph.json`` already holds the truth.
        """
        for node in nodes:
            depends_on = [d for d in (node.get("dependsOn") or []) if d in self.node_records]
            if not depends_on:
                continue
            blocked = self.node_records[node["id"]]
            path = self._issues_path(f"/{blocked['number']}/dependencies/blocked_by")
            try:
                existing = {
                    item.get("id")
                    for item in self.transport.paginate(f"{path}?per_page=100")
                    if isinstance(item, Mapping)
                }
            except Exception as exc:  # noqa: BLE001 - an optional mirror, never fatal
                self.warnings.append(f"issue dependencies unavailable for {node['id']}: {exc}")
                continue
            for dep_id in depends_on:
                blocker = self.node_records[dep_id]
                if blocker["id"] in existing:
                    continue
                try:
                    status, data = self._call(
                        "POST", path, {"issue_id": blocker["id"]}, allow=(403, 404, 410, 422)
                    )
                except Exception as exc:  # noqa: BLE001 - an optional mirror, never fatal
                    status, data = 599, {"message": str(exc)}
                if status >= 400:
                    self.warnings.append(
                        f"could not record '{node['id']} blocked by {dep_id}' "
                        f"(#{blocked['number']} <- #{blocker['number']}): {_message(data)}"
                    )
                elif isinstance(data, Mapping) and data.get("issue_dependencies_summary"):
                    blocked["dependencies"] = dict(data["issue_dependencies_summary"])

    def _retire_orphans(self, index: Mapping[tuple[str, str], dict[str, Any]]) -> None:
        """Detach and close issues for nodes a replan removed from the graph.

        ``sync()`` is otherwise add-only, which would leave a removed task
        counted in the parent's ``sub_issues_summary`` forever -- a run that can
        never reach 100%. Detaching fixes the rollup; closing as ``not_planned``
        says why. Both are no-ops on the next sync, so idempotency holds.
        """
        for (kind, node_id), record in sorted(index.items()):
            if kind != "node" or node_id in self.node_records:
                continue
            detached = self._detach_from_parent(record)
            if record.get("state") == "closed":
                continue
            self._call(
                "PATCH",
                self._issues_path(f"/{record['number']}"),
                {"state": "closed", "state_reason": "not_planned"},
            )
            record["state"] = "closed"
            self.warnings.append(
                f"node '{node_id}' is no longer in the task graph; issue "
                f"#{record['number']} was "
                + ("detached and closed" if detached else "closed")
                + " as not planned"
            )

    def _detach_from_parent(self, record: Mapping[str, Any]) -> bool:
        """Remove ``record`` from whatever parent holds it. Returns whether it did."""
        status, data = self._call(
            "GET", self._issues_path(f"/{record['number']}/parent"), allow=(404, 410)
        )
        if status >= 400 or not isinstance(data, Mapping) or data.get("number") is None:
            return False
        # NOTE: singular `sub_issue`, and the body takes the *id*.
        status, _ = self._call(
            "DELETE",
            self._issues_path(f"/{data['number']}/sub_issue"),
            {"sub_issue_id": record["id"]},
            allow=(400, 404, 422),
        )
        return status < 400

    def _refresh_summary(self, number: int) -> None:
        status, data = self._call("GET", self._issues_path(f"/{number}"), allow=(404,))
        if status == 404 or not isinstance(data, Mapping):
            return
        summary = data.get("sub_issues_summary")
        if isinstance(summary, Mapping):
            self.summaries[number] = dict(summary)

    def _refresh_summaries(self) -> None:
        for parent in self.parents:
            self._refresh_summary(parent["number"])
        if self.root is not None:
            self._refresh_summary(self.root["number"])

    def _last_update_digest(self, number: int, node_id: str) -> str | None:
        """Digest of the most recent ``adlc:update`` comment for ``node_id``.

        Comparing against the *latest* comment rather than any comment keeps a
        replayed update idempotent while still recording a genuine repeat
        transition -- ``running -> fail -> running`` is real history in an
        evidence-producing framework, not noise.
        """
        latest: str | None = None
        for comment in self.transport.paginate(
            self._issues_path(f"/{number}/comments?per_page=100")
        ):
            if not isinstance(comment, Mapping):
                continue
            match = UPDATE_MARKER_RE.search(comment.get("body") or "")
            if match and match.group("node") == node_id:
                latest = match.group("digest")
        return latest

    def _relocate(self, node_id: str) -> dict[str, Any]:
        run_id = self._run_id or os.environ.get("ADLC_RUN_ID")
        if not run_id:
            raise GitHubTaskStoreError(
                f"update({node_id!r}) called before sync(); set ADLC_RUN_ID so the issue "
                "for this node can be located"
            )
        self._run_id = run_id
        index = self._index_run_issues(run_id, run_label(run_id))
        for (kind, value), rec in index.items():
            if kind == "node":
                self.node_records.setdefault(value, rec)
            elif kind == "parent" and rec not in self.parents:
                self.parents.append(rec)
            elif kind == "root":
                self.root = rec
        record = self.node_records.get(node_id)
        if record is None:
            raise GitHubTaskStoreError(
                f"no GitHub issue found for node '{node_id}' in run '{run_id}'; run sync() first"
            )
        return record

    # -- Projects v2 (optional, best-effort) ------------------------------
    _ADD_ITEM = (
        "mutation($project:ID!,$content:ID!){"
        "addProjectV2ItemById(input:{projectId:$project,contentId:$content}){item{id}}}"
    )
    _SET_FIELD = (
        "mutation($project:ID!,$item:ID!,$field:ID!,$value:ProjectV2FieldValue!){"
        "updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,"
        "fieldId:$field,value:$value}){projectV2Item{id}}}"
    )

    def _sync_project(self, nodes: Sequence[TaskNode]) -> None:
        if not self.project_id:
            self.warnings.append(
                "taskstore.github.enableProjects is on but no projectId/ADLC_GITHUB_PROJECT "
                "was provided -- skipping Projects v2 sync"
            )
            return
        for node in nodes:
            record = self.node_records.get(node["id"])
            if record is None or not record.get("node_id"):
                continue
            try:
                data = self.transport.graphql(
                    self._ADD_ITEM, {"project": self.project_id, "content": record["node_id"]}
                )
                added = (data or {}).get("addProjectV2ItemById") or {}
                item_id = (added.get("item") or {}).get("id")
                if not item_id:
                    continue
                self._set_project_fields(item_id, node)
            except Exception as exc:  # noqa: BLE001 - Projects must never fail sync()
                self.warnings.append(f"Projects v2 sync failed for {node['id']}: {exc}")

    def _set_project_fields(self, item_id: str, node: TaskNode) -> None:
        level_field = self.project_fields.get("level")
        if level_field:
            self.transport.graphql(
                self._SET_FIELD,
                {
                    "project": self.project_id,
                    "item": item_id,
                    "field": level_field,
                    "value": {"number": float(node.get("level", 0))},
                },
            )
        kind_field = self.project_fields.get("kind")
        kind_options = self.project_fields.get("kindOptions") or {}
        option_id = kind_options.get(node.get("kind"))
        if kind_field and option_id:
            self.transport.graphql(
                self._SET_FIELD,
                {
                    "project": self.project_id,
                    "item": item_id,
                    "field": kind_field,
                    "value": {"singleSelectOptionId": option_id},
                },
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_from_cfg(cfg: Config | None) -> dict[str, Any]:
    raw = getattr(cfg, "raw", None)
    if not isinstance(raw, Mapping):
        return {}
    section = raw.get("taskstore")
    if not isinstance(section, Mapping):
        return {}
    github = section.get("github")
    return dict(github) if isinstance(github, Mapping) else {}


def _record(issue: Mapping[str, Any]) -> dict[str, Any]:
    labels = []
    for label in issue.get("labels") or []:
        labels.append(label.get("name", "") if isinstance(label, Mapping) else str(label))
    return {
        "id": issue.get("id"),
        "number": issue.get("number"),
        "node_id": issue.get("node_id"),
        "title": issue.get("title"),
        "body": issue.get("body"),
        "state": issue.get("state"),
        "url": issue.get("html_url"),
        "labels": [lbl for lbl in labels if lbl],
    }


def _parent_title(run_id: str, part: int, total: int) -> str:
    if total <= 1:
        return f"ADLC run {run_id}"
    return f"ADLC run {run_id} -- tasks part {part}/{total}"


def _node_title(node: TaskNode) -> str:
    return f"[{node['id']}] {node.get('title', node['id'])}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
