"""Dependency review / Dependabot gate (``dependency``) — L4.

Two GitHub APIs answer "did this change introduce a vulnerable dependency?", and
they answer different questions:

* ``GET /repos/{o}/{r}/dependency-graph/compare/{base}...{head}`` — the
  **dependency review** API. Diff-scoped: it reports dependencies *added* by this
  change and the advisories affecting them. This is the honest "new risk" signal
  for a PR gate, and it is what this gate prefers.
* ``GET /repos/{o}/{r}/dependabot/alerts`` — repo-scoped standing alerts. Useful,
  but it answers "is this repo vulnerable?", not "did this PR make it worse". It
  is used only as a fallback when dependency review is unavailable.

This gate is **advisory by default** (``required_by_default = False``): the spine
already ships a credential-free ``deps_local`` gate (``pip-audit`` / ``npm
audit``), so this one adds GitHub-side advisory data rather than replacing it.

Severity vocabularies differ between the two APIs — dependency review says
``moderate`` where Dependabot says ``medium``. :func:`normalize_severity`
reconciles them; see ``docs/security-gates.md``.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from adlc.adapters.gate.codeql import (
    GitHubApiError,
    GitHubRestClient,
    _is_required,
    detect_github_credentials,
    evaluate_threshold,
    gate_options,
    not_run_result,
    resolve_repo,
    resolve_token,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config
    from adlc.ports import GateResult, Run

__all__ = [
    "DependencyReviewGate",
    "normalize_severity",
    "summarize_dependabot_alerts",
    "summarize_dependency_review",
]

SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")

#: Dependency review reports ``moderate``; Dependabot reports ``medium``.
#: They mean the same band, so they are folded together.
_SEVERITY_ALIASES: dict[str, str] = {"moderate": "medium", "unknown": "unknown"}

#: Default: block only on newly-introduced critical/high advisories.
DEFAULT_MAX_BY_SEVERITY: dict[str, int] = {"critical": 0, "high": 0}


def normalize_severity(value: Any) -> str:
    """Map either API's severity vocabulary onto ``critical|high|medium|low``."""
    level = str(value or "").strip().lower()
    level = _SEVERITY_ALIASES.get(level, level)
    return level if level in SEVERITIES else "unknown"


def _empty_counts() -> dict[str, int]:
    return {level: 0 for level in (*SEVERITIES, "unknown")}


def summarize_dependency_review(
    changes: Sequence[Mapping[str, Any]], *, added_only: bool = True
) -> dict[str, Any]:
    """Count advisories on dependencies introduced by the diff.

    Each element of the dependency-review response is a dependency change with a
    ``change_type`` of ``added`` or ``removed`` and a ``vulnerabilities`` list.
    Only ``added`` changes can introduce risk, so ``removed`` entries are ignored
    by default — counting them would fail a PR for *fixing* a vulnerability.
    """
    counts = _empty_counts()
    packages: list[str] = []
    advisories: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        if added_only and str(change.get("change_type") or "").lower() != "added":
            continue
        vulns = change.get("vulnerabilities")
        if not isinstance(vulns, Sequence):
            continue
        name = str(change.get("name") or "?")
        version = str(change.get("version") or "?")
        for vuln in vulns:
            if not isinstance(vuln, Mapping):
                continue
            level = normalize_severity(vuln.get("severity"))
            counts[level] += 1
            label = f"{name}@{version}"
            if label not in packages:
                packages.append(label)
            advisories.append(
                {
                    "package": label,
                    "ecosystem": change.get("ecosystem"),
                    "severity": level,
                    "ghsaId": vuln.get("advisory_ghsa_id"),
                    "summary": str(vuln.get("advisory_summary") or "")[:200],
                }
            )
    return {
        "source": "dependency-review",
        "total": sum(counts.values()),
        "bySeverity": counts,
        "packages": sorted(packages)[:50],
        "advisories": advisories[:50],
    }


def summarize_dependabot_alerts(alerts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count open Dependabot alerts by severity (repo-scoped, not diff-scoped)."""
    counts = _empty_counts()
    packages: list[str] = []
    advisories: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, Mapping):
            continue
        vuln = alert.get("security_vulnerability") or {}
        advisory = alert.get("security_advisory") or {}
        level = normalize_severity(vuln.get("severity") or advisory.get("severity"))
        counts[level] += 1
        package = (vuln.get("package") or {}) if isinstance(vuln, Mapping) else {}
        name = str(package.get("name") or "?")
        if name not in packages:
            packages.append(name)
        advisories.append(
            {
                "package": name,
                "ecosystem": package.get("ecosystem"),
                "severity": level,
                "ghsaId": advisory.get("ghsa_id") if isinstance(advisory, Mapping) else None,
                "number": alert.get("number"),
            }
        )
    return {
        "source": "dependabot-alerts",
        "total": sum(counts.values()),
        "bySeverity": counts,
        "packages": sorted(packages)[:50],
        "advisories": advisories[:50],
    }


class DependencyReviewGate:
    """``dependency`` gate — newly-introduced vulnerable dependencies."""

    id = "dependency"
    name = "dependency-review"
    kind = "gate"
    required_by_default = False

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return detect_github_credentials(cfg, feature="dependency review / Dependabot alerts")

    def _threshold(self, options: Mapping[str, Any]) -> dict[str, int]:
        raw = options.get("maxBySeverity")
        if not isinstance(raw, Mapping) or not raw:
            return dict(DEFAULT_MAX_BY_SEVERITY)
        out: dict[str, int] = {}
        for level, allowed in raw.items():
            key = normalize_severity(level)
            if key == "unknown":
                continue
            try:
                out[key] = max(int(allowed), 0)
            except (TypeError, ValueError):
                out[key] = 0
        return out or dict(DEFAULT_MAX_BY_SEVERITY)

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        options = gate_options(cfg, self.id)
        max_by_severity = self._threshold(options)
        base_sha = str((run or {}).get("baseSha") or "").strip()
        head_sha = str((run or {}).get("headSha") or "").strip()
        allow_fallback = bool(options.get("allowDependabotFallback", True))
        expected: dict[str, Any] = {
            "maxBySeverity": max_by_severity,
            "scope": "dependencies added between baseSha and headSha",
            "baseSha": base_sha,
            "headSha": head_sha,
        }

        available, reason = self.detect(cfg)
        if not available:
            return not_run_result(self.id, cfg, reason, expected=expected, severity="medium")

        repo = resolve_repo(run)
        token = resolve_token()
        if not repo or not token:  # pragma: no cover - detect() already covers this
            return not_run_result(self.id, cfg, reason, expected=expected, severity="medium")

        client = GitHubRestClient(token, repo)
        summary: dict[str, Any] | None = None
        notes: list[str] = []

        if base_sha and head_sha:
            basehead = urllib.parse.quote(f"{base_sha}...{head_sha}", safe=".")
            try:
                changes = client.get_list(
                    f"/repos/{repo}/dependency-graph/compare/{basehead}", max_pages=1
                )
                summary = summarize_dependency_review(changes)
            except GitHubApiError as exc:
                notes.append(f"dependency review unavailable ({exc})")
        else:
            notes.append("run is missing baseSha/headSha, so the diff-scoped API cannot be used")

        if summary is None and allow_fallback:
            try:
                alerts = client.get_list(
                    f"/repos/{repo}/dependabot/alerts", {"state": "open"}, max_pages=5
                )
                summary = summarize_dependabot_alerts(alerts)
                notes.append(
                    "fell back to repo-scoped open Dependabot alerts, which include "
                    "pre-existing findings not introduced by this change"
                )
            except GitHubApiError as exc:
                notes.append(f"Dependabot alerts unavailable ({exc})")

        if summary is None:
            return not_run_result(
                self.id,
                cfg,
                "Could not read GitHub dependency data: " + "; ".join(notes) + ". "
                "The credential-free 'deps_local' gate still applies.",
                observed={"notes": notes},
                expected=expected,
                severity="medium",
            )

        violations = evaluate_threshold(summary["bySeverity"], max_by_severity)
        observed = {**summary, "notes": notes, "baseSha": base_sha, "headSha": head_sha}
        expected["source"] = summary["source"]
        if violations:
            breach = ", ".join(
                f"{v['observed']} {v['severity']} (max {v['max']})" for v in violations
            )
            worst = min(violations, key=lambda v: SEVERITIES.index(str(v["severity"])))
            return {
                "id": self.id,
                "required": _is_required(cfg, self.id),
                "status": "fail",
                "severity": str(worst["severity"]),  # type: ignore[typeddict-item]
                "observed": observed,
                "expected": expected,
                "message": (
                    f"{summary['source']} reports {breach} vulnerable dependency advisory(ies): "
                    f"{', '.join(summary['packages'][:5]) or 'see advisories'}."
                ),
                "evidence": [f"gates/{self.id}.json"],
            }
        return {
            "id": self.id,
            "required": _is_required(cfg, self.id),
            "status": "pass",
            "severity": "low",
            "observed": observed,
            "expected": expected,
            "message": (
                f"{summary['source']}: {summary['total']} advisory(ies), none exceeding "
                f"{max_by_severity}."
            ),
            "evidence": [f"gates/{self.id}.json"],
        }
