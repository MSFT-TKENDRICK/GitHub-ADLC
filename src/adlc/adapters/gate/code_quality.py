"""GitHub Code Quality gate (``code_quality``) — L4.

GitHub Code Quality went GA on 2026-07-20. It is a **distinct product** from code
scanning, delivered through CodeQL's ``code-quality`` analysis kind, and it has
its own REST namespace: ``/repos/{o}/{r}/code-quality/{setup,findings}``.

Three facts drive this module's design.

**1. It is enabled in Settings, not by a workflow.**
Code Quality is turned on at repo Settings → Security → Code quality (or granted
org-wide). A workflow file cannot self-enable it. Accordingly this gate
*preflights* ``GET /repos/{o}/{r}/code-quality/setup`` and reports ``not_run``
when ``state != "configured"``. It never attempts to enable anything.

**2. The findings endpoint cannot be pinned to a commit.**
``GET /repos/{o}/{r}/code-quality/findings`` accepts only ``state``,
``direction``, ``per_page``, ``before``, ``after`` — there is no ``ref``, ``pr``,
``sha`` or ``commit_sha`` parameter, and a finding object carries **no**
``commit_sha``/``ref``/``analysis_key`` linkage at all (verified against the
published schema; see ``docs/security-gates.md``). The endpoint is a
point-in-time snapshot of the findings store, so on its own it is vulnerable to
exactly the stale-result false green that the ``security`` gate exists to
prevent.

Since Code Quality is produced by the *same* CodeQL run
(``analysis-kinds: code-scanning,code-quality``), this gate mitigates that by
first requiring a CodeQL analysis for the **exact head SHA** to have completed —
reusing :func:`adlc.adapters.gate.codeql.poll_for_analysis`. That proves a fresh
analysis of this commit finished before the snapshot was taken. It is a
corroboration, not a guarantee, and the residual gap is recorded in ``observed``
and documented rather than hidden.

**3. The `analysis-kinds` action input is `[Internal]`.**
``github/codeql-action/init`` exposes ``analysis-kinds:
code-scanning,code-quality``, but its own description says it is
"intended for internal-use only at this time and the behaviour is subject to
changes". Treat it as best-effort; the supported enablement path is Settings.

Quality findings use ``rule.severity`` (``error|warning|note|none``) and
``rule.category`` (``none|maintainability|reliability``). They have **no**
``security_severity_level`` — that field is security-only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from adlc.adapters.gate.codeql import (
    GitHubApiError,
    GitHubRestClient,
    _default_ref,
    _float_option,
    _is_required,
    detect_github_credentials,
    evaluate_threshold,
    gate_options,
    not_run_result,
    poll_for_analysis,
    resolve_repo,
    resolve_token,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config
    from adlc.ports import GateResult, Run

__all__ = [
    "NOT_ENABLED_REASON",
    "CodeQualityGate",
    "classify_setup",
    "setup_failure_reason",
    "summarize_findings",
]

#: The exact operator-facing remedy for a repo that has not turned the product on.
NOT_ENABLED_REASON = (
    "Code Quality not enabled in repository settings — enable at "
    "Settings → Security → Code quality"
)

#: ``rule.severity`` on a code quality finding.
FINDING_SEVERITIES: tuple[str, ...] = ("error", "warning", "note", "none")
#: ``rule.category`` on a code quality finding. The clean discriminator between
#: quality and security rules -- it exists only in the ``/code-quality/`` schema.
FINDING_CATEGORIES: tuple[str, ...] = ("none", "maintainability", "reliability")

#: Default threshold: zero ``error``-severity quality findings. Warnings and
#: notes are reported but do not block, because quality rules are advisory noise
#: at those levels in most repos.
DEFAULT_MAX_BY_SEVERITY: dict[str, int] = {"error": 0}

#: ``/code-quality/findings`` uses cursor pagination via the ``Link`` header,
#: which this stdlib client does not read. One page is fetched; a full page is
#: treated as truncation and fails closed rather than risking an undercount.
DEFAULT_MAX_FINDINGS = 100


def classify_setup(payload: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Interpret ``GET /code-quality/setup``. Returns ``(configured, reason)``.

    ``state`` is ``"configured"`` or ``"not-configured"``. A licensed-but-unset
    repo answers HTTP 200 with ``not-configured`` -- so a 200 is not by itself
    permission to proceed.
    """
    if not isinstance(payload, Mapping):
        return False, (
            "Code Quality setup could not be read (unexpected response shape). "
            + NOT_ENABLED_REASON
        )
    state = str(payload.get("state") or "").strip().lower()
    if state == "configured":
        languages = payload.get("languages")
        if isinstance(languages, Sequence) and not isinstance(languages, str) and languages:
            langs = ", ".join(str(x) for x in languages)
        else:
            langs = "unspecified"
        return True, f"Code Quality is configured (languages: {langs})"
    if state == "not-configured":
        return False, NOT_ENABLED_REASON
    return False, f"Code Quality setup returned unrecognised state {state!r}. {NOT_ENABLED_REASON}"


def setup_failure_reason(status: int | None, detail: str = "") -> str:
    """Map a ``/code-quality/setup`` HTTP failure to a precise, honest reason.

    The API documents a single 403 for both "not licensed" and "token lacks
    permission", and a bare 404 for both "no such repo" and "token cannot see the
    repo". Those really are indistinguishable from the response alone, so the
    reason strings say so instead of guessing.
    """
    suffix = f" ({detail})" if detail else ""
    if status == 403:
        return (
            "Not authorized to read Code Quality for this repository. The REST API returns "
            "403 both when Code Quality is not licensed for the org and when the token lacks "
            "permission, and does not distinguish them. Code Quality is a standalone paid "
            f"product and is enabled at Settings → Security → Code quality.{suffix}"
        )
    if status == 404:
        return (
            "Code Quality setup endpoint returned 404: the repository does not exist, or the "
            f"token cannot see it, or this GitHub plan does not expose Code Quality.{suffix}"
        )
    if status == 503:
        return f"GitHub Code Quality service is temporarily unavailable (503).{suffix}"
    return f"Could not read Code Quality setup (HTTP {status}).{suffix}"


def summarize_findings(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count quality findings by ``rule.severity`` and ``rule.category``."""
    by_severity = {level: 0 for level in (*FINDING_SEVERITIES, "unknown")}
    by_category = {cat: 0 for cat in (*FINDING_CATEGORIES, "unknown")}
    rules: list[str] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        rule = finding.get("rule") or {}
        severity = str(rule.get("severity") or "").strip().lower()
        category = str(rule.get("category") or "").strip().lower()
        by_severity[severity if severity in FINDING_SEVERITIES else "unknown"] += 1
        by_category[category if category in FINDING_CATEGORIES else "unknown"] += 1
        rule_id = str(rule.get("id") or "").strip()
        if rule_id and rule_id not in rules:
            rules.append(rule_id)
    return {
        "total": sum(by_severity.values()),
        "bySeverity": by_severity,
        "byCategory": by_category,
        "ruleIds": sorted(rules)[:50],
    }


class CodeQualityGate:
    """``code_quality`` gate — GitHub Code Quality findings, settings-gated."""

    id = "code_quality"
    name = "github-code-quality"
    kind = "gate"
    required_by_default = True

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return detect_github_credentials(cfg, feature="Code Quality")

    def _threshold(self, options: Mapping[str, Any]) -> dict[str, int]:
        raw = options.get("maxBySeverity")
        if not isinstance(raw, Mapping) or not raw:
            return dict(DEFAULT_MAX_BY_SEVERITY)
        out: dict[str, int] = {}
        for level, allowed in raw.items():
            key = str(level).strip().lower()
            if key not in FINDING_SEVERITIES:
                continue
            try:
                out[key] = max(int(allowed), 0)
            except (TypeError, ValueError):
                out[key] = 0
        return out or dict(DEFAULT_MAX_BY_SEVERITY)

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        options = gate_options(cfg, self.id)
        max_by_severity = self._threshold(options)
        head_sha = str((run or {}).get("headSha") or "").strip()
        require_analysis = bool(options.get("requireAnalysisAtHeadSha", True))
        try:
            max_findings = int(options.get("maxFindings", DEFAULT_MAX_FINDINGS))
        except (TypeError, ValueError):
            max_findings = DEFAULT_MAX_FINDINGS
        # One request, so clamp into the endpoint's legal per_page range.
        max_findings = max(1, min(max_findings, 100))
        timeout = _float_option(options, "timeoutSeconds", 900.0)
        interval = _float_option(options, "pollIntervalSeconds", 10.0)
        ref = options.get("ref") or _default_ref(run)
        expected: dict[str, Any] = {
            "maxBySeverity": max_by_severity,
            "setupState": "configured",
            "findingState": "open",
            "headSha": head_sha,
            "requireAnalysisAtHeadSha": require_analysis,
        }

        available, reason = self.detect(cfg)
        if not available:
            return not_run_result(self.id, cfg, reason, expected=expected)

        repo = resolve_repo(run)
        token = resolve_token()
        if not repo or not token:  # pragma: no cover - detect() already covers this
            return not_run_result(self.id, cfg, reason, expected=expected)

        client = GitHubRestClient(token, repo)

        # -- 1. Preflight: is the product actually turned on for this repo? ----
        try:
            setup = client.get(f"/repos/{repo}/code-quality/setup")
        except GitHubApiError as exc:
            return not_run_result(
                self.id, cfg, setup_failure_reason(exc.status, str(exc)), expected=expected
            )
        configured, setup_reason = classify_setup(setup)
        if not configured:
            return not_run_result(
                self.id,
                cfg,
                setup_reason,
                observed={"setup": _safe_setup(setup)},
                expected=expected,
            )

        # -- 2. Corroborate freshness against the exact head SHA --------------
        analysis_note = (
            "Findings are a repository-level snapshot: the Code Quality REST API exposes no "
            "commit or ref linkage, so freshness is corroborated via the CodeQL analysis for "
            "this commit rather than proven by the findings themselves."
        )
        analysis_id: Any = None
        if require_analysis:
            if not head_sha:
                return not_run_result(
                    self.id,
                    cfg,
                    "run.headSha is empty, so the Code Quality findings snapshot cannot be "
                    "corroborated against an analysis of this commit. Set "
                    "gates.code_quality.requireAnalysisAtHeadSha=false to accept an "
                    "unpinned snapshot.",
                    expected=expected,
                )
            poll = poll_for_analysis(
                lambda: client.list_analyses(ref=ref, tool_name="CodeQL"),
                sha=head_sha,
                ref=ref,
                timeout=timeout,
                interval=interval,
            )
            if not poll.found:
                return not_run_result(
                    self.id,
                    cfg,
                    f"No CodeQL analysis for commit {head_sha[:12]} appeared within "
                    f"{timeout:.0f}s ({poll.attempts} polls), so the Code Quality findings "
                    "snapshot cannot be attributed to this commit. Failing closed rather than "
                    "reporting possibly-stale quality findings as a pass.",
                    observed={
                        "headSha": head_sha,
                        "attempts": poll.attempts,
                        "elapsedSeconds": round(poll.elapsed, 1),
                        "timedOut": poll.timed_out,
                        "errors": poll.errors[-3:],
                    },
                    expected=expected,
                )
            analysis_id = (poll.analysis or {}).get("id")

        # -- 3. Read the findings ---------------------------------------------
        try:
            findings = client.get(
                f"/repos/{repo}/code-quality/findings",
                {"state": "open", "per_page": max_findings},
            )
        except GitHubApiError as exc:
            return not_run_result(
                self.id,
                cfg,
                f"Code Quality is configured for {repo}, but reading findings failed: {exc}",
                observed={"headSha": head_sha, "analysisId": analysis_id},
                expected=expected,
            )
        if not isinstance(findings, list):
            findings = []

        summary = summarize_findings(findings)
        violations = evaluate_threshold(summary["bySeverity"], max_by_severity)
        truncated = len(findings) >= max_findings
        observed = {
            **summary,
            "headSha": head_sha,
            "analysisId": analysis_id,
            "truncated": truncated,
            "violations": violations,
            "provenanceNote": analysis_note,
        }

        if violations:
            breach = ", ".join(
                f"{v['observed']} {v['severity']} (max {v['max']})" for v in violations
            )
            return {
                "id": self.id,
                "required": _is_required(cfg, self.id),
                "status": "fail",
                "severity": "medium",
                "observed": observed,
                "expected": expected,
                "message": (
                    f"GitHub Code Quality reports {breach} open finding(s) "
                    f"({summary['byCategory']['maintainability']} maintainability, "
                    f"{summary['byCategory']['reliability']} reliability)."
                ),
                "evidence": [f"gates/{self.id}.json"],
            }

        if truncated:
            # A full page means there may be unread findings. Reporting `pass`
            # from a possibly-truncated clean sample is exactly the false green
            # this gate exists to avoid.
            return not_run_result(
                self.id,
                cfg,
                f"Code Quality returned a full page of {len(findings)} findings, so the result "
                "set is truncated and a clean verdict cannot be proven. The Link-header cursor "
                "is not followed by this stdlib client; raise gates.code_quality.maxFindings or "
                "triage the existing findings.",
                observed=observed,
                expected=expected,
                severity="medium",
            )

        return {
            "id": self.id,
            "required": _is_required(cfg, self.id),
            "status": "pass",
            "severity": "low",
            "observed": observed,
            "expected": expected,
            "message": (
                f"GitHub Code Quality: {summary['total']} open finding(s), none exceeding "
                f"{max_by_severity}. {analysis_note}"
            ),
            "evidence": [f"gates/{self.id}.json"],
        }


def _safe_setup(setup: Any) -> dict[str, Any]:
    """Echo only the non-sensitive parts of the setup payload into evidence."""
    if not isinstance(setup, Mapping):
        return {}
    return {
        key: setup.get(key)
        for key in ("state", "languages", "schedule", "runner_type", "updated_at")
        if key in setup
    }
