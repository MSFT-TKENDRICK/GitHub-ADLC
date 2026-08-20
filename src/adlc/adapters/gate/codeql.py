"""CodeQL code-scanning gate (``security``) — L4.

Why this module looks the way it does
-------------------------------------
The naive design — "run security scanning, then build" — does not work, because
CodeQL's own lifecycle forbids it:

    codeql init  →  build ONCE  →  analyze/upload  →  poll for the analysis of
                                                      the EXACT head SHA

``github/codeql-action/init`` must run *before* compilation so it can intercept
the compiler and construct a database; ``analyze`` must run *after*. There is
exactly one build, and the security gate consumes its output.

The second trap is asynchrony. ``analyze`` uploads a SARIF file and returns; the
alerts derived from it are **not** immediately queryable. A gate that calls
``GET /code-scanning/alerts`` right after analyze can be served *stale alerts
from a previous analysis of the default branch* and report a **false green**.

So this gate never trusts "the latest alerts for the repo". It polls
``GET /repos/{o}/{r}/code-scanning/analyses`` until it observes an analysis whose
``commit_sha`` equals the exact head SHA under test (optionally further pinned by
``ref``, ``category`` and ``analysis_key``), and only then reads alerts scoped to
that analysis's ``ref``.

If that analysis never appears within the timeout, the gate returns
``status: "not_run"`` with a reason. For a required gate the aggregator turns
``not_run`` into a build failure. **A timeout is never a pass.**

See ``docs/security-gates.md`` for the full rationale and the required GitHub
token permissions.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config
    from adlc.ports import GateResult, Run

__all__ = [
    "AnalysisPoll",
    "CodeQlGate",
    "GitHubApiError",
    "GitHubRestClient",
    "detect_github_credentials",
    "evaluate_threshold",
    "find_matching_analysis",
    "not_run_result",
    "poll_for_analysis",
    "resolve_repo",
    "resolve_token",
    "summarize_alerts",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Token env vars, in precedence order. ``GITHUB_TOKEN`` is what Actions injects.
TOKEN_ENV_VARS: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")
REPO_ENV_VAR = "GITHUB_REPOSITORY"
API_URL_ENV_VARS: tuple[str, ...] = ("GITHUB_API_URL",)

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "adlc-gate/0.1.0"

#: ``rule.security_severity_level`` — security-relevant CodeQL queries only.
SECURITY_SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
#: ``rule.severity`` — the SARIF level, present on every rule including
#: non-security (quality/maintainability) ones.
SARIF_SEVERITIES: tuple[str, ...] = ("error", "warning", "note", "none")

#: Default threshold: zero critical and zero high alerts. Medium/low are
#: reported in ``observed`` but do not block by default.
DEFAULT_MAX_BY_SEVERITY: dict[str, int] = {"critical": 0, "high": 0}

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PAGES = 20


class GitHubApiError(RuntimeError):
    """A GitHub REST call failed. Always surfaced, never swallowed into a pass."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Credential detection -- cheap, non-raising, NO NETWORK
# ---------------------------------------------------------------------------


def resolve_token(env: Mapping[str, str] | None = None) -> str | None:
    """First non-empty token env var, else ``None``. Never raises."""
    env = os.environ if env is None else env
    for name in TOKEN_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def resolve_repo(
    run: Run | Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve ``owner/repo`` from the run document, else the environment.

    The run document wins: a run is pinned to the repo it was created for, and
    trusting ambient env over an explicit run would let a mis-scoped workflow
    gate the wrong repository.
    """
    env = os.environ if env is None else env
    if run:
        candidate = str(run.get("repo") or "").strip()
        if candidate.count("/") == 1 and all(candidate.split("/")):
            return candidate
    candidate = (env.get(REPO_ENV_VAR) or "").strip()
    if candidate.count("/") == 1 and all(candidate.split("/")):
        return candidate
    return None


def detect_github_credentials(
    cfg: Config | None = None,
    env: Mapping[str, str] | None = None,
    *,
    feature: str = "code scanning",
) -> tuple[bool, str]:
    """Shared ``detect()`` body for every GitHub-API-backed gate.

    Checks only for the *presence* of credentials. It deliberately does not
    verify API reachability: ``detect()`` must be cheap, offline and incapable
    of hanging (see ``CONTRIBUTING.md`` rule 5).
    """
    env = os.environ if env is None else env
    token = resolve_token(env)
    repo = resolve_repo(None, env)
    missing: list[str] = []
    if not token:
        missing.append(f"${TOKEN_ENV_VARS[0]} (or ${TOKEN_ENV_VARS[1]})")
    if not repo:
        missing.append(f"${REPO_ENV_VAR} (owner/repo)")
    if missing:
        return False, (
            f"GitHub {feature} unavailable: missing {', '.join(missing)}. "
            "Local credential-free gates are used instead."
        )
    return True, f"GitHub {feature} available for {repo} via ${TOKEN_ENV_VARS[0]}"


# ---------------------------------------------------------------------------
# Minimal REST client -- stdlib only, so no new dependency in pyproject.toml
# ---------------------------------------------------------------------------


class GitHubRestClient:
    """A deliberately small GitHub REST client built on :mod:`urllib`.

    ADLC adapters must not add required dependencies, so this uses the stdlib
    rather than ``requests``/``httpx``.
    """

    def __init__(
        self,
        token: str,
        repo: str,
        *,
        api_url: str | None = None,
        timeout: float = 30.0,
        opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
    ) -> None:
        self.token = token
        self.repo = repo
        env_api = ""
        for name in API_URL_ENV_VARS:
            env_api = (os.environ.get(name) or "").strip()
            if env_api:
                break
        self.api_url = (api_url or env_api or DEFAULT_API_URL).rstrip("/")
        self.timeout = timeout
        self._opener = opener or _urlopen

    def _request(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.api_url}{path}"
        if params:
            query = {k: str(v) for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("User-Agent", USER_AGENT)
        try:
            status, body = self._opener(req, self.timeout)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = _safe_decode(exc.read())
            raise GitHubApiError(
                f"GET {path} failed with HTTP {exc.code}: {detail}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise GitHubApiError(f"GET {path} failed: {exc.reason}") from exc
        if status >= 400:
            raise GitHubApiError(
                f"GET {path} failed with HTTP {status}: {_safe_decode(body)}", status=status
            )
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError(f"GET {path} returned unparsable JSON: {exc}") from exc

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request(path, params)

    def get_list(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """Page through a list endpoint. Returns ``[]`` for an empty resource."""
        items, _ = self.get_list_paged(path, params, max_pages=max_pages, per_page=per_page)
        return items

    def get_list_paged(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Page through a list endpoint, reporting whether the page cap was hit.

        The second element is ``True`` when pagination stopped because
        ``max_pages`` was exhausted while pages were still coming back full —
        i.e. the caller is holding a **partial** result set. Callers that would
        otherwise report ``pass`` must treat that as unverified and fail closed.
        """
        out: list[dict[str, Any]] = []
        truncated = True
        for page in range(1, max_pages + 1):
            merged = dict(params or {})
            merged.update({"per_page": per_page, "page": page})
            payload = self._request(path, merged)
            if not payload:
                truncated = False
                break
            if not isinstance(payload, list):
                raise GitHubApiError(
                    f"GET {path} expected a JSON array, got {type(payload).__name__}"
                )
            out.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < per_page:
                truncated = False
                break
        return out, truncated

    # -- code scanning ----------------------------------------------------
    def list_analyses(
        self,
        *,
        ref: str | None = None,
        tool_name: str | None = None,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        """``GET /repos/{o}/{r}/code-scanning/analyses``.

        The endpoint has **no ``sha`` query parameter**, so the SHA match is done
        client-side against each analysis's ``commit_sha``. ``ref`` narrows the
        server-side result set, which keeps the client-side scan bounded.
        """
        return self.get_list(
            f"/repos/{self.repo}/code-scanning/analyses",
            {"ref": ref, "tool_name": tool_name, "sort": "created", "direction": "desc"},
            max_pages=max_pages,
        )

    def list_alerts(
        self,
        *,
        ref: str | None = None,
        state: str | None = "open",
        tool_name: str | None = None,
        severity: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """``GET /repos/{o}/{r}/code-scanning/alerts``."""
        return self.list_alerts_paged(
            ref=ref, state=state, tool_name=tool_name, severity=severity, max_pages=max_pages
        )[0]

    def list_alerts_paged(
        self,
        *,
        ref: str | None = None,
        state: str | None = "open",
        tool_name: str | None = None,
        severity: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """As :meth:`list_alerts`, but also reports result-set truncation."""
        return self.get_list_paged(
            f"/repos/{self.repo}/code-scanning/alerts",
            {"ref": ref, "state": state, "tool_name": tool_name, "severity": severity},
            max_pages=max_pages,
        )


def _urlopen(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:  # pragma: no cover
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read()


def _safe_decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")[:500]
    except Exception:  # noqa: BLE001 - error formatting must never mask the error
        return "<undecodable body>"


# ---------------------------------------------------------------------------
# Exact-SHA analysis matching -- the anti-stale-green core. Pure, no network.
# ---------------------------------------------------------------------------


def find_matching_analysis(
    analyses: Sequence[Mapping[str, Any]],
    *,
    sha: str,
    ref: str | None = None,
    category: str | None = None,
    analysis_key: str | None = None,
) -> dict[str, Any] | None:
    """Return the analysis produced for **exactly** ``sha``, else ``None``.

    Matching is exact (case-insensitive) full-string equality on ``commit_sha``.
    There is deliberately no prefix matching and no "fall back to the most
    recent analysis": both would reintroduce the stale-alert false green this
    whole module exists to prevent.

    ``ref``, ``category`` and ``analysis_key`` further pin the match so that a
    *different* workflow's analysis of the same commit cannot satisfy the gate.
    """
    wanted = (sha or "").strip().lower()
    if not wanted:
        return None
    for analysis in analyses:
        if not isinstance(analysis, Mapping):
            continue
        commit = str(analysis.get("commit_sha") or "").strip().lower()
        if not commit or commit != wanted:
            continue
        if ref and str(analysis.get("ref") or "") != ref:
            continue
        if category and str(analysis.get("category") or "") != category:
            continue
        if analysis_key and analysis_key not in str(analysis.get("analysis_key") or ""):
            continue
        return dict(analysis)
    return None


@dataclass
class AnalysisPoll:
    """Outcome of polling for the analysis of an exact SHA."""

    analysis: dict[str, Any] | None = None
    timed_out: bool = False
    attempts: int = 0
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.analysis is not None


def poll_for_analysis(
    fetch_analyses: Callable[[], Sequence[Mapping[str, Any]]],
    *,
    sha: str,
    ref: str | None = None,
    category: str | None = None,
    analysis_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AnalysisPoll:
    """Poll until the analysis for ``sha`` appears, or the timeout expires.

    ``sleep``/``monotonic`` are injectable so tests can drive this with a fake
    clock and assert the timeout branch deterministically, without sleeping.

    On timeout the result carries ``timed_out=True`` and ``analysis=None``. The
    caller MUST translate that into ``not_run`` — never ``pass``.

    Transport errors are recorded and retried rather than raised: a flaky API is
    indistinguishable from a slow one, and both must end in a timeout, not a
    silent success.

    ``interval`` is floored at one second so a misconfigured ``0`` cannot turn
    the wait into a tight loop that hammers the REST API.
    """
    interval = max(float(interval), 1.0)
    timeout = max(float(timeout), 0.0)
    started = monotonic()
    poll = AnalysisPoll()
    while True:
        poll.attempts += 1
        try:
            analyses = fetch_analyses() or []
        except GitHubApiError as exc:
            poll.errors.append(str(exc))
            analyses = []
        except Exception as exc:  # noqa: BLE001 - fail closed, never crash the run
            poll.errors.append(f"{type(exc).__name__}: {exc}")
            analyses = []

        match = find_matching_analysis(
            analyses, sha=sha, ref=ref, category=category, analysis_key=analysis_key
        )
        if match is not None:
            poll.analysis = match
            poll.elapsed = monotonic() - started
            return poll

        elapsed = monotonic() - started
        if elapsed >= timeout:
            poll.timed_out = True
            poll.elapsed = elapsed
            return poll
        sleep(min(interval, max(timeout - elapsed, 0.0)))


# ---------------------------------------------------------------------------
# Alert summarisation and thresholds. Pure, no network.
# ---------------------------------------------------------------------------


def alert_security_severity(alert: Mapping[str, Any]) -> str:
    """``rule.security_severity_level`` if present, else ``"unknown"``.

    Non-security rules (including code-quality rules) have no
    ``security_severity_level``; they are bucketed as ``unknown`` rather than
    being invented into a security band.
    """
    rule = alert.get("rule") or {}
    level = str(rule.get("security_severity_level") or "").strip().lower()
    return level if level in SECURITY_SEVERITIES else "unknown"


def alert_sarif_severity(alert: Mapping[str, Any]) -> str:
    """``rule.severity`` (``error|warning|note|none``), else ``"unknown"``."""
    rule = alert.get("rule") or {}
    level = str(rule.get("severity") or "").strip().lower()
    return level if level in SARIF_SEVERITIES else "unknown"


def summarize_alerts(
    alerts: Sequence[Mapping[str, Any]], *, sha: str | None = None
) -> dict[str, Any]:
    """Count alerts by both severity vocabularies.

    ``atSha`` counts alerts whose ``most_recent_instance.commit_sha`` equals
    ``sha``. It is reported for transparency but is **not** used to shrink the
    blocking set: for a PR analysed on ``refs/pull/N/merge`` the instance SHA is
    the merge commit, not the head SHA, so filtering on it would silently drop
    real findings.
    """
    by_security = {level: 0 for level in (*SECURITY_SEVERITIES, "unknown")}
    by_sarif = {level: 0 for level in (*SARIF_SEVERITIES, "unknown")}
    wanted = (sha or "").strip().lower()
    at_sha = 0
    rules: list[str] = []
    for alert in alerts:
        if not isinstance(alert, Mapping):
            continue
        by_security[alert_security_severity(alert)] += 1
        by_sarif[alert_sarif_severity(alert)] += 1
        instance = alert.get("most_recent_instance") or {}
        if wanted and str(instance.get("commit_sha") or "").strip().lower() == wanted:
            at_sha += 1
        rule_id = str((alert.get("rule") or {}).get("id") or "").strip()
        if rule_id and rule_id not in rules:
            rules.append(rule_id)
    return {
        "total": sum(by_security.values()),
        "bySeverity": by_security,
        "bySarifSeverity": by_sarif,
        "atSha": at_sha,
        "ruleIds": sorted(rules)[:50],
    }


def evaluate_threshold(
    counts: Mapping[str, int], max_by_severity: Mapping[str, int]
) -> list[dict[str, Any]]:
    """Return one violation record per breached severity band. ``[]`` == pass."""
    violations: list[dict[str, Any]] = []
    for level, allowed in max_by_severity.items():
        observed = int(counts.get(str(level).lower(), 0))
        try:
            allowed_int = int(allowed)
        except (TypeError, ValueError):
            allowed_int = 0
        if observed > allowed_int:
            violations.append({"severity": level, "observed": observed, "max": allowed_int})
    return violations


# ---------------------------------------------------------------------------
# GateResult helpers
# ---------------------------------------------------------------------------


def not_run_result(
    gate_id: str,
    cfg: Config | None,
    message: str,
    *,
    observed: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    severity: str = "high",
) -> GateResult:
    """Build a ``not_run`` GateResult. For a required gate the spine fails the build."""
    return {
        "id": gate_id,
        "required": _is_required(cfg, gate_id),
        "status": "not_run",
        "severity": severity,  # type: ignore[typeddict-item]
        "observed": observed or {},
        "expected": expected or {},
        "message": message,
        "evidence": [f"gates/{gate_id}.json"],
    }


def _is_required(cfg: Config | None, gate_id: str) -> bool:
    try:
        return bool(cfg.is_required(gate_id))  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - never let config shape crash a gate
        return False


def gate_options(cfg: Config | None, gate_id: str) -> dict[str, Any]:
    """Per-gate options from ``.adlc/config.yaml`` → ``gates.<gate_id>``."""
    try:
        options = (getattr(cfg, "gates", None) or {}).get(gate_id)
    except Exception:  # noqa: BLE001
        return {}
    return dict(options) if isinstance(options, Mapping) else {}


def _float_option(options: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(options[key])
    except (KeyError, TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class CodeQlGate:
    """``security`` gate — CodeQL code-scanning alerts for an exact head SHA."""

    id = "security"
    name = "codeql"
    kind = "gate"
    required_by_default = True

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return detect_github_credentials(cfg, feature="code scanning (CodeQL)")

    # -- helpers ----------------------------------------------------------
    def _threshold(self, options: Mapping[str, Any]) -> dict[str, int]:
        raw = options.get("maxBySeverity")
        if not isinstance(raw, Mapping) or not raw:
            return dict(DEFAULT_MAX_BY_SEVERITY)
        out: dict[str, int] = {}
        for level, allowed in raw.items():
            key = str(level).strip().lower()
            if key not in SECURITY_SEVERITIES:
                continue
            try:
                out[key] = max(int(allowed), 0)
            except (TypeError, ValueError):
                out[key] = 0
        return out or dict(DEFAULT_MAX_BY_SEVERITY)

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        options = gate_options(cfg, self.id)
        head_sha = str((run or {}).get("headSha") or "").strip()
        ref = options.get("ref") or _default_ref(run)
        category = options.get("category") or None
        analysis_key = options.get("analysisKey") or None
        tool_name = options.get("toolName", "CodeQL") or None
        timeout = _float_option(options, "timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
        interval = _float_option(options, "pollIntervalSeconds", DEFAULT_POLL_INTERVAL_SECONDS)
        max_by_severity = self._threshold(options)
        expected: dict[str, Any] = {
            "maxBySeverity": max_by_severity,
            "alertState": "open",
            "headSha": head_sha,
            "ref": ref,
            "category": category,
            "timeoutSeconds": timeout,
        }

        available, reason = self.detect(cfg)
        if not available:
            return not_run_result(self.id, cfg, reason, expected=expected)

        if not head_sha:
            return not_run_result(
                self.id,
                cfg,
                "run.headSha is empty — cannot pin the CodeQL analysis to an exact commit, "
                "and matching on 'latest analysis' would risk a stale-alert false green.",
                expected=expected,
            )

        repo = resolve_repo(run)
        token = resolve_token()
        if not repo or not token:  # pragma: no cover - detect() already covers this
            return not_run_result(self.id, cfg, reason, expected=expected)

        client = GitHubRestClient(token, repo)
        poll = poll_for_analysis(
            lambda: client.list_analyses(ref=ref, tool_name=tool_name),
            sha=head_sha,
            ref=ref,
            category=category,
            analysis_key=analysis_key,
            timeout=timeout,
            interval=interval,
        )

        if not poll.found:
            observed = {
                "headSha": head_sha,
                "ref": ref,
                "attempts": poll.attempts,
                "elapsedSeconds": round(poll.elapsed, 1),
                "errors": poll.errors[-3:],
                "timedOut": poll.timed_out,
            }
            detail = f" Last errors: {'; '.join(poll.errors[-2:])}" if poll.errors else ""
            return not_run_result(
                self.id,
                cfg,
                f"No CodeQL analysis for commit {head_sha[:12]} appeared within "
                f"{timeout:.0f}s ({poll.attempts} polls). Code scanning results are "
                f"uploaded asynchronously; the gate fails closed rather than reading "
                f"possibly-stale alerts from an earlier analysis.{detail}",
                observed=observed,
                expected=expected,
            )

        analysis = poll.analysis or {}
        analysis_ref = str(analysis.get("ref") or ref or "") or None
        try:
            alerts, truncated = client.list_alerts_paged(
                ref=analysis_ref, state="open", tool_name=tool_name
            )
        except GitHubApiError as exc:
            return not_run_result(
                self.id,
                cfg,
                f"CodeQL analysis {analysis.get('id')} for {head_sha[:12]} was found, but "
                f"reading its alerts failed: {exc}",
                observed={"headSha": head_sha, "analysisId": analysis.get("id")},
                expected=expected,
            )

        summary = summarize_alerts(alerts, sha=head_sha)
        violations = evaluate_threshold(summary["bySeverity"], max_by_severity)
        observed = {
            **summary,
            "headSha": head_sha,
            "ref": analysis_ref,
            "analysisId": analysis.get("id"),
            "analysisCreatedAt": analysis.get("created_at"),
            "category": analysis.get("category"),
            "analysisKey": analysis.get("analysis_key"),
            "attempts": poll.attempts,
            "elapsedSeconds": round(poll.elapsed, 1),
            "truncated": truncated,
            "violations": violations,
        }
        if violations:
            # SECURITY_SEVERITIES is ordered most-severe first, so the lowest
            # index is the worst breach.
            worst = min(violations, key=lambda v: SECURITY_SEVERITIES.index(str(v["severity"])))
            breach = ", ".join(
                f"{v['observed']} {v['severity']} (max {v['max']})" for v in violations
            )
            return {
                "id": self.id,
                "required": _is_required(cfg, self.id),
                "status": "fail",
                "severity": str(worst["severity"]),  # type: ignore[typeddict-item]
                "observed": observed,
                "expected": expected,
                "message": (
                    f"CodeQL found {breach} open alert(s) on {analysis_ref} at commit "
                    f"{head_sha[:12]} (analysis {analysis.get('id')})."
                ),
                "evidence": [f"gates/{self.id}.json"],
            }
        if truncated:
            # A clean sample drawn from a partial result set proves nothing.
            # Same discipline as deps_local: only pass what was actually checked.
            return not_run_result(
                self.id,
                cfg,
                f"CodeQL alerts for {analysis_ref} were truncated at {len(alerts)} results "
                "before the threshold could be proven clean, so only part of the alert set "
                "was verified. Triage the existing alerts or narrow the query.",
                observed=observed,
                expected=expected,
            )
        return {
            "id": self.id,
            "required": _is_required(cfg, self.id),
            "status": "pass",
            "severity": "low",
            "observed": observed,
            "expected": expected,
            "message": (
                f"CodeQL analysis {analysis.get('id')} for commit {head_sha[:12]} is within "
                f"threshold: {summary['total']} open alert(s), none exceeding "
                f"{max_by_severity}."
            ),
            "evidence": [f"gates/{self.id}.json"],
        }


def _default_ref(run: Run | Mapping[str, Any] | None) -> str | None:
    """Best-effort ref for the commit under test.

    A PR is analysed on ``refs/pull/<n>/merge``; ``GITHUB_REF`` is authoritative
    inside Actions. Returning ``None`` is safe — it simply widens the server-side
    analyses query, and the exact ``commit_sha`` match still does the real work.
    """
    env_ref = (os.environ.get("GITHUB_REF") or "").strip()
    if env_ref:
        return env_ref
    pr_number = (run or {}).get("prNumber")
    if isinstance(pr_number, int) and pr_number > 0:
        return f"refs/pull/{pr_number}/merge"
    return None
