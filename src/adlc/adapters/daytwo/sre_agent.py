"""Azure SRE Agent → ADLC intake bridge (``daytwo`` adapter).

Why this module exists, stated plainly
--------------------------------------
The Azure SRE Agent (preview, onboarded at ``sre.azure.com``) **cannot
autonomously generate and open a code pull request.** Through its GitHub
integration it can create and update issues, comment on issues and pull
requests, read Dependabot alerts, and **trigger and track GitHub Actions
workflow runs**. That last capability is the whole integration: the SRE Agent
is a *dispatcher*, and ADLC is the *receiver*.

So this adapter is deliberately **not** "SRE Agent writes the fix". It is a
parser. It turns whatever the SRE Agent hands us — a ``repository_dispatch``
``client_payload``, a ``workflow_dispatch`` input, or an issue it filed — into
a normalised ``adlc-incident/v1`` document and then into a **plain
``brief.md``**.

The KISS win
------------
That ``brief.md`` is the *same artifact day-1 intake produces*. Day-2 therefore
re-enters the pipeline through the **existing** front door::

    incident  ──►  brief.md  ──►  adlc run new --brief brief.md
                                  └─ qualify → spec → enrich → graph → build
                                     → evidence → eval → gate → reduce → report

There is no second pipeline, no day-2-specific stage machinery, no parallel
set of gates. An incident is just another way to author a brief. Everything
downstream — including the fail-closed gate aggregator — is byte-for-byte the
day-1 path. See ``docs/day2-operations.md``.

Tier
----
Per the KISS ladder (``docs/PLAN.md`` §7) this sits in **"documented + disabled
example"**: the *parsing* below is real, tested and credential-free; the Azure
side (creating the SRE Agent, wiring its GitHub connector) is example-only and
requires an Azure subscription. See ``examples/azure/sre-agent-dispatch.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adlc.config import Config

INCIDENT_SCHEMA_VERSION = "adlc-incident/v1"

#: The ``repository_dispatch`` event type this receiver answers to. Chosen to be
#: namespaced so it cannot collide with a consumer repo's own dispatch types.
DISPATCH_EVENT_TYPE = "adlc-incident"

#: Label an SRE-Agent-filed issue must carry for the issue path to claim it.
#: Mirrors day-1's ``adlc:brief`` label convention (``docs/PLAN.md`` §3).
INCIDENT_ISSUE_LABEL = "adlc:incident"

#: Environment variables that indicate we are running as a dispatch receiver.
#: ``GITHUB_EVENT_PATH`` is set by every GitHub Actions run; the others are how
#: a caller hands us a payload directly.
PAYLOAD_ENV_VARS = ("ADLC_INCIDENT_PAYLOAD", "ADLC_INCIDENT_FILE", "GITHUB_EVENT_PATH")

_SEVERITY_ALIASES: dict[str, str] = {
    "0": "sev1", "1": "sev1", "2": "sev2", "3": "sev3", "4": "sev4",
    "sev0": "sev1", "sev1": "sev1", "sev2": "sev2", "sev3": "sev3", "sev4": "sev4",
    "critical": "sev1", "fatal": "sev1", "page": "sev1",
    "high": "sev2", "error": "sev2", "major": "sev2",
    "medium": "sev3", "moderate": "sev3", "warning": "sev3", "warn": "sev3",
    "low": "sev4", "minor": "sev4", "info": "sev4", "informational": "sev4",
}

_SEVERITY_LABEL_RE = re.compile(r"^(?:severity|sev)[:/-]?\s*(.+)$", re.IGNORECASE)

Severity = Literal["sev1", "sev2", "sev3", "sev4"]


class IncidentSignal(TypedDict, total=False):
    """One observed measurement that justified raising the incident."""

    id: str
    kind: str            # metric | log | trace | eval | alert | dependabot
    description: str
    query: str           # e.g. the KQL that produced it
    value: float | int | str
    threshold: float | int | str
    unit: str


class IncidentResource(TypedDict, total=False):
    """The Azure resource the SRE Agent was watching. Free-form on purpose."""

    id: str              # /subscriptions/.../resourceGroups/.../providers/...
    name: str
    type: str
    subscriptionId: str
    resourceGroup: str
    region: str


class Incident(TypedDict, total=False):
    """Normalised ``adlc-incident/v1``.

    This shape is **ours**. It is not an Azure schema and must not be mistaken
    for one — the SRE Agent's own payload shape is configured by the operator
    when they wire the connector (see ``examples/azure/sre-agent-dispatch.md``),
    so we normalise defensively rather than assume.
    """

    schemaVersion: str
    id: str
    title: str
    severity: Severity
    detectedAt: str
    source: str          # azure-sre-agent | github-issue | manual
    summary: str
    impact: str
    suspectedCause: str
    resource: IncidentResource
    signals: list[IncidentSignal]
    deployment: dict[str, Any]   # {"commit": ..., "runId": ..., "environment": ...}
    links: list[dict[str, str]]  # [{"title": ..., "url": ...}]
    labels: list[str]
    writeSet: list[str]          # optional hint at the files to change
    raw: dict[str, Any]          # the untouched inbound payload, for audit


class SreAgentReceiver:
    """Parse an SRE-Agent-delivered incident into an ADLC brief.

    Deliberately has **no** ``dispatch``/``mitigate``/``remediate`` method: this
    adapter never calls Azure and never acts on production. It only reads a
    payload that was already delivered to us.
    """

    name = "sre-agent"
    kind = "daytwo"

    # -- Adapter protocol -------------------------------------------------

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, non-raising, no network.

        Available only when an incident payload is actually in hand. Merely
        having Azure credentials proves nothing: this receiver is passive.
        """
        try:
            inline = os.environ.get("ADLC_INCIDENT_PAYLOAD")
            if inline and inline.strip():
                return True, "incident payload supplied via ADLC_INCIDENT_PAYLOAD"

            explicit = os.environ.get("ADLC_INCIDENT_FILE")
            if explicit:
                if Path(explicit).is_file():
                    return True, f"incident payload file present at {explicit}"
                return False, f"ADLC_INCIDENT_FILE set but not a file: {explicit}"

            event_name = os.environ.get("GITHUB_EVENT_NAME", "")
            event_path = os.environ.get("GITHUB_EVENT_PATH", "")
            if event_name in {"repository_dispatch", "workflow_dispatch", "issues"}:
                if event_path and Path(event_path).is_file():
                    return True, (
                        f"GitHub Actions {event_name} event payload present at {event_path}"
                    )
                return False, (
                    f"GITHUB_EVENT_NAME={event_name} but GITHUB_EVENT_PATH is missing "
                    "or not a file"
                )

            return False, (
                "no incident payload: set ADLC_INCIDENT_PAYLOAD or ADLC_INCIDENT_FILE, "
                f"or run on a repository_dispatch '{DISPATCH_EVENT_TYPE}' / issues event "
                "(see docs/day2-operations.md)"
            )

        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"incident payload probe failed: {exc}"

    # -- Public API -------------------------------------------------------

    def load(self, source: Path | str | None = None) -> Incident:
        """Read a payload from ``source`` (or the environment) and normalise it."""
        return self.parse(self._read_payload(source))

    def parse(self, payload: Any) -> Incident:
        """Normalise any supported inbound payload into an :class:`Incident`.

        Accepts, in order of preference:

        1. a ``repository_dispatch`` event  → ``payload["client_payload"]``
        2. a ``workflow_dispatch`` event    → ``payload["inputs"]`` (values may be
           JSON-encoded strings, which is how Actions passes structured inputs)
        3. an ``issues`` event or a bare issue object → title/body/labels
        4. a bare incident object already in our shape

        Unknown keys are never dropped — the whole inbound payload is preserved
        under ``incident["raw"]`` so the audit trail stays complete.
        """
        if isinstance(payload, (str, bytes, bytearray)):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise TypeError(f"incident payload must be a JSON object, got {type(payload).__name__}")

        raw: dict[str, Any] = payload
        body = self._unwrap(payload)

        if self._looks_like_issue(body):
            incident = self._from_issue(body)
        else:
            incident = self._from_object(body)

        incident["schemaVersion"] = INCIDENT_SCHEMA_VERSION
        incident["raw"] = raw
        incident.setdefault("detectedAt", _utcnow())
        incident.setdefault("severity", "sev3")
        incident.setdefault("source", "azure-sre-agent")
        incident.setdefault("title", "Untitled incident")
        incident["id"] = incident.get("id") or _slug_id(incident["title"], incident["detectedAt"])
        return incident

    def to_brief(self, incident: Incident) -> str:
        """Render the incident as a **day-1-shaped** ``brief.md``.

        The section vocabulary deliberately matches what
        :mod:`adlc.stages.autoresearch` produces and what
        :func:`adlc.stages.intake.qualify_text` scores: a stated **Problem**, a
        **Desired outcome**, **Acceptance criteria**, bounded **Scope**, a named
        audience and a measurable target. An incident brief that omitted those
        would score below ``qualify.minScore`` and be parked -- which would
        silently break the day-2 loop for exactly the terse incidents that
        matter most.

        Nothing here is keyword-stuffing: every section carries real content,
        and where the incident payload genuinely lacks something (a measurable
        target, say) the brief **says so** rather than inventing one. That
        honest gap is itself useful review signal.
        """
        front = [
            "---",
            "adlc: brief",
            "origin: day-2-incident",
            f"incidentId: {_yaml_scalar(incident.get('id', ''))}",
            f"severity: {_yaml_scalar(incident.get('severity', 'sev3'))}",
            f"detectedAt: {_yaml_scalar(incident.get('detectedAt', ''))}",
            f"source: {_yaml_scalar(incident.get('source', 'azure-sre-agent'))}",
        ]
        labels = [str(x) for x in incident.get("labels", []) if str(x).strip()]
        if labels:
            front.append("labels: [" + ", ".join(_yaml_scalar(x) for x in labels) + "]")
        front.append("---")

        out: list[str] = ["\n".join(front), "", f"# {incident.get('title', 'Incident')}", ""]

        summary = (incident.get("summary") or "").strip()
        severity = incident.get("severity", "sev3")
        out += [
            "## Problem",
            "",
            summary or (
                "The incident payload carried no summary, so the failure is described only "
                "by the signals and metadata below. Treat that as the first thing to fix."
            ),
            "",
        ]

        impact = (incident.get("impact") or "").strip()
        out += [
            "## Impact",
            "",
            impact or (
                "The effect on users was not quantified in the incident payload. "
                "Establishing who is affected, and how badly, is part of this work."
            ),
            "",
        ]

        out += ["## Desired outcome", "", _desired_outcome(incident), ""]

        resource = incident.get("resource") or {}
        if any(resource.values()):
            out += ["## Affected resource", ""]
            for key in ("name", "type", "id", "resourceGroup", "subscriptionId", "region"):
                if value := resource.get(key):
                    out.append(f"- **{key}**: `{value}`")
            out.append("")

        signals = incident.get("signals") or []
        if signals:
            out += [
                "## Observed signals",
                "",
                "| signal | kind | observed | threshold |",
                "| --- | --- | --- | --- |",
            ]
            for sig in signals:
                unit = sig.get("unit", "")
                observed = _cell(sig.get("value"), unit)
                threshold = _cell(sig.get("threshold"), unit)
                label = sig.get("description") or sig.get("id") or "—"
                out.append(f"| {_cell(label)} | {_cell(sig.get('kind', '—'))} | {observed} "
                           f"| {threshold} |")
            out.append("")
            for sig in signals:
                if query := (sig.get("query") or "").strip():
                    out += [f"<details><summary>Query for {sig.get('id') or 'signal'}</summary>",
                            "", "```kusto", query, "```", "", "</details>", ""]
        else:
            out += [
                "## Observed signals",
                "",
                ("**None supplied.** The incident carried no measurable target, so there is "
                 "no objective threshold to restore. Define one before trusting a fix: "
                 "without a measured signal, \"resolved\" is an opinion."),
                "",
            ]

        if cause := (incident.get("suspectedCause") or "").strip():
            out += ["## Suspected cause", "", cause, ""]

        deployment = incident.get("deployment") or {}
        if any(deployment.values()):
            out += ["## Deployment context", ""]
            out += [f"- **{k}**: `{v}`" for k, v in deployment.items() if v]
            out.append("")

        out += ["## Scope", "", _scope(severity), ""]

        out += ["## Acceptance criteria", "", *_acceptance_criteria(incident), ""]

        links = incident.get("links") or []
        if links:
            out += ["## References", ""]
            out += [f"- [{link.get('title') or link.get('url')}]({link.get('url')})"
                    for link in links if link.get("url")]
            out.append("")

        out += [
            "---",
            "",
            (f"_Generated by `adlc.adapters.daytwo.sre_agent` from "
             f"`{INCIDENT_SCHEMA_VERSION}` incident `{incident.get('id', '')}`. This brief "
             "enters the **standard day-1 intake path**; day-2 has no separate pipeline._"),
            "",
        ]
        return "\n".join(out)

    def write_brief(self, incident: Incident, out_dir: Path) -> Path:
        """Write ``brief.md`` (and ``incident.json`` for audit) into ``out_dir``."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        brief_path = out_dir / "brief.md"
        brief_path.write_text(self.to_brief(incident), encoding="utf-8")
        (out_dir / "incident.json").write_text(
            json.dumps(incident, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        return brief_path

    # -- Payload plumbing -------------------------------------------------

    def _read_payload(self, source: Path | str | None) -> Any:
        if source is not None:
            return json.loads(Path(source).read_text(encoding="utf-8"))
        if inline := os.environ.get("ADLC_INCIDENT_PAYLOAD"):
            return json.loads(inline)
        for var in ("ADLC_INCIDENT_FILE", "GITHUB_EVENT_PATH"):
            path = os.environ.get(var)
            if path and Path(path).is_file():
                return json.loads(Path(path).read_text(encoding="utf-8"))
        raise FileNotFoundError(
            "no incident payload found; pass a path or set one of " + ", ".join(PAYLOAD_ENV_VARS)
        )

    @staticmethod
    def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
        """Peel the GitHub event envelope off, if there is one."""
        if isinstance(payload.get("client_payload"), dict):        # repository_dispatch
            return payload["client_payload"]
        if isinstance(payload.get("inputs"), dict):                # workflow_dispatch
            return SreAgentReceiver._decode_inputs(payload["inputs"])
        if isinstance(payload.get("issue"), dict):                 # issues event
            return payload["issue"]
        return payload

    @staticmethod
    def _decode_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
        """``workflow_dispatch`` inputs are strings; structured ones arrive as JSON."""
        for key in ("incident", "payload", "client_payload"):
            value = inputs.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    return decoded
        return dict(inputs)

    @staticmethod
    def _looks_like_issue(body: dict[str, Any]) -> bool:
        return "body" in body and "title" in body and "summary" not in body

    def _from_issue(self, issue: dict[str, Any]) -> Incident:
        """Parse an issue the SRE Agent filed — its best-verified capability.

        The body may embed a fenced ``json`` block holding the structured
        incident; if so that wins, and the issue supplies only what is missing.
        """
        text = issue.get("body") or ""
        labels = _issue_labels(issue)
        prose = _strip_json_blocks(text).strip()

        incident: Incident = {}
        if embedded := _extract_json_block(text):
            incident = self._from_object(embedded)

        incident.setdefault("title", (issue.get("title") or "").strip() or "Untitled incident")
        incident.setdefault("source", "github-issue")

        # The structured block wins for machine fields, but the human prose is
        # never discarded -- it is usually where the "why" lives.
        summary = incident.get("summary", "")
        if prose and prose not in summary:
            incident["summary"] = f"{summary}\n\n{prose}".strip()
        elif summary:
            incident["summary"] = summary
        else:
            incident["summary"] = prose
        if labels:
            incident["labels"] = sorted({*incident.get("labels", []), *labels})
            if "severity" not in incident and (sev := _severity_from_labels(labels)):
                incident["severity"] = sev
        if url := issue.get("html_url"):
            links = list(incident.get("links", []))
            links.append({"title": f"Incident issue #{issue.get('number', '?')}", "url": url})
            incident["links"] = links
        if created := issue.get("created_at"):
            incident.setdefault("detectedAt", str(created))
        return incident

    def _from_object(self, body: dict[str, Any]) -> Incident:
        """Normalise a bare incident object, tolerating common key spellings."""
        incident: Incident = {}

        if title := _first(body, "title", "name", "alertName", "summaryTitle", "subject"):
            incident["title"] = str(title).strip()
        if summary := _first(body, "summary", "description", "details", "message", "body"):
            incident["summary"] = str(summary).strip()
        if impact := _first(body, "impact", "customerImpact", "blastRadius"):
            incident["impact"] = str(impact).strip()
        if cause := _first(body, "suspectedCause", "probableCause", "rootCause", "diagnosis"):
            incident["suspectedCause"] = str(cause).strip()
        if incident_id := _first(body, "id", "incidentId", "alertId", "correlationId"):
            incident["id"] = str(incident_id).strip()
        if detected := _first(body, "detectedAt", "firedAt", "timestamp", "startTime", "occurredAt"):
            incident["detectedAt"] = str(detected).strip()
        if source := _first(body, "source", "origin", "detector"):
            incident["source"] = str(source).strip()
        if severity := _first(body, "severity", "sev", "priority", "level"):
            incident["severity"] = _normalise_severity(severity)

        if resource := _coerce_resource(body):
            incident["resource"] = resource
        if signals := _coerce_signals(body):
            incident["signals"] = signals
        if deployment := _coerce_deployment(body):
            incident["deployment"] = deployment
        if links := _coerce_links(body):
            incident["links"] = links
        if labels := body.get("labels"):
            incident["labels"] = sorted({str(x) for x in _as_list(labels) if str(x).strip()})
        if hint := _clean_str_list(_first(body, "writeSet", "suspectedFiles", "affectedFiles")):
            # A hint, not an analysis. `adlc hotfix` records where it came from.
            incident["writeSet"] = hint

        return incident


# ---------------------------------------------------------------------------
# Small helpers — deliberately dependency-free
# ---------------------------------------------------------------------------


def _primary_signal(incident: Incident) -> IncidentSignal | None:
    """The signal with a measurable threshold, preferring the first."""
    for signal in incident.get("signals") or []:
        if signal.get("value") is not None and signal.get("threshold") is not None:
            return signal
    signals = incident.get("signals") or []
    return signals[0] if signals else None


def _desired_outcome(incident: Incident) -> str:
    """State the outcome in "so that" terms -- what good looks like, not how."""
    signal = _primary_signal(incident)
    if signal and signal.get("threshold") is not None:
        unit = f" {signal['unit']}" if signal.get("unit") else ""
        what = signal.get("description") or signal.get("id") or "the observed signal"
        return (
            f"Return **{what}** to at or below its threshold of "
            f"`{signal['threshold']}{unit}` in production, so that affected users stop "
            "experiencing the failure and operators can close the incident. The value of "
            "this work is measured by that signal recovering and staying recovered -- not "
            "by the change being merged."
        )
    return (
        "Restore correct behaviour for the affected users so that operators can close the "
        "incident, and leave behind a regression test that proves the failure cannot "
        "silently return. Because the incident carried no measured signal, part of the "
        "desired outcome is establishing one."
    )


def _scope(severity: str) -> str:
    """Bound the work. A hotfix that grows is no longer a hotfix."""
    return (
        "**In scope**: the smallest change that clears the signal above, a regression test "
        "that fails without it, and a short incident record.\n\n"
        "**Out of scope**: refactors, dependency upgrades, unrelated cleanups, and any "
        "redesign the incident merely made visible. File those separately -- this run is "
        f"constrained to the {severity} incident.\n\n"
        "**Constraint**: the change must pass the same required gates as any other change. "
        "Urgency does not lower the bar; it only narrows the scope."
    )


def _acceptance_criteria(incident: Incident) -> list[str]:
    signal = _primary_signal(incident)
    if signal and signal.get("threshold") is not None:
        unit = f" {signal['unit']}" if signal.get("unit") else ""
        what = signal.get("description") or signal.get("id") or "the observed signal"
        first = (
            f"1. {what} must be at or below `{signal['threshold']}{unit}` after the change, "
            "demonstrated by captured evidence rather than asserted."
        )
    else:
        first = (
            "1. A measurable target must be defined for this failure, and the change must "
            "be shown to meet it with captured evidence rather than asserted."
        )
    return [
        first,
        "2. A regression test must reproduce the incident and must fail without the fix.",
        "3. All required gates must pass; no required gate may be left `not_run`.",
        "4. The incident record must name the suspected cause and the deployed commit.",
    ]


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug_id(title: str, detected_at: str) -> str:
    """Deterministic incident id when the payload carried none.

    Stable for a given (title, timestamp) pair so re-processing the same
    payload does not mint a second incident id.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-") or "incident"
    digest = hashlib.sha256(f"{title}|{detected_at}".encode()).hexdigest()[:6]
    return f"{slug[:40].strip('-')}-{digest}"


def _first(body: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = body.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _clean_str_list(value: Any) -> list[str]:
    """Normalise a string or list-of-strings into a de-duplicated list."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalise_severity(value: Any) -> Severity:
    key = str(value).strip().lower()
    return _SEVERITY_ALIASES.get(key, "sev3")  # type: ignore[return-value]


def _severity_from_labels(labels: list[str]) -> Severity | None:
    for label in labels:
        text = label.strip().lower()
        if text in _SEVERITY_ALIASES:
            return _SEVERITY_ALIASES[text]  # type: ignore[return-value]
        if match := _SEVERITY_LABEL_RE.match(text):
            candidate = match.group(1).strip()
            if candidate in _SEVERITY_ALIASES:
                return _SEVERITY_ALIASES[candidate]  # type: ignore[return-value]
    return None


def _issue_labels(issue: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for label in _as_list(issue.get("labels")):
        if isinstance(label, dict):
            if name := label.get("name"):
                out.append(str(name))
        elif str(label).strip():
            out.append(str(label))
    return out


def _coerce_resource(body: dict[str, Any]) -> IncidentResource:
    raw = body.get("resource")
    src: dict[str, Any] = raw if isinstance(raw, dict) else {}
    out: IncidentResource = {}
    mapping = {
        "id": ("id", "resourceId", "armId"),
        "name": ("name", "resourceName"),
        "type": ("type", "resourceType"),
        "subscriptionId": ("subscriptionId", "subscription"),
        "resourceGroup": ("resourceGroup", "resourceGroupName"),
        "region": ("region", "location"),
    }
    for field, keys in mapping.items():
        if value := (_first(src, *keys) or _first(body, *keys)):
            out[field] = str(value)  # type: ignore[literal-required]
    if isinstance(raw, str) and raw.strip():
        out.setdefault("id", raw.strip())
    return out


def _coerce_signals(body: dict[str, Any]) -> list[IncidentSignal]:
    out: list[IncidentSignal] = []
    for index, item in enumerate(_as_list(_first(body, "signals", "metrics", "alerts", "evidence"))):
        if isinstance(item, str):
            out.append({"id": f"S{index + 1:03d}", "kind": "alert", "description": item})
            continue
        if not isinstance(item, dict):
            continue
        signal: IncidentSignal = {"id": str(_first(item, "id", "name") or f"S{index + 1:03d}")}
        if kind := _first(item, "kind", "type", "signalType"):
            signal["kind"] = str(kind)
        if description := _first(item, "description", "title", "message", "name"):
            signal["description"] = str(description)
        if query := _first(item, "query", "kql", "expression"):
            signal["query"] = str(query)
        for field, keys in (("value", ("value", "observed", "actual")),
                            ("threshold", ("threshold", "budget", "expected", "slo")),
                            ("unit", ("unit", "units"))):
            if (value := _first(item, *keys)) is not None:
                signal[field] = value if field != "unit" else str(value)  # type: ignore[literal-required]
        out.append(signal)
    return out


def _coerce_deployment(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("deployment")
    src: dict[str, Any] = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for field, keys in (
        ("commit", ("commit", "sha", "headSha", "suspectedCommit", "revision")),
        ("environment", ("environment", "env", "stage", "slot")),
        ("workflowRunId", ("workflowRunId", "runId", "buildId")),
        ("deployedAt", ("deployedAt", "releasedAt")),
        ("version", ("version", "imageTag", "release")),
    ):
        if value := (_first(src, *keys) or _first(body, *keys)):
            out[field] = str(value)
    return out


def _coerce_links(body: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in _as_list(_first(body, "links", "references", "urls")):
        if isinstance(item, str) and item.strip():
            out.append({"title": item.strip(), "url": item.strip()})
        elif isinstance(item, dict) and (url := _first(item, "url", "href", "link")):
            out.append({"title": str(_first(item, "title", "name", "text") or url), "url": str(url)})
    for key in ("portalUrl", "dashboardUrl", "runbookUrl"):
        if url := body.get(key):
            out.append({"title": key, "url": str(url)})
    return out


_JSON_BLOCK_RE = re.compile(r"```(?:json|jsonc)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _extract_json_block(text: str) -> dict[str, Any] | None:
    for match in _JSON_BLOCK_RE.finditer(text or ""):
        try:
            decoded = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _strip_json_blocks(text: str) -> str:
    return _JSON_BLOCK_RE.sub("", text or "")


def _yaml_scalar(value: Any) -> str:
    text = str(value)
    if text == "" or re.search(r"[:#\-\[\]{},&*?|>%@`\"']", text) or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _cell(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    text = f"{value}{(' ' + unit) if unit else ''}"
    return text.replace("|", "\\|").replace("\n", " ")
