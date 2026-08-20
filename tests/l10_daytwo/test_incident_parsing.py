"""Incident payload → ``adlc-incident/v1`` → ``brief.md``.

The claim under test is the one the whole leaf rests on: an incident becomes a
**day-1-shaped brief**, so day-2 needs no pipeline of its own.
"""

from __future__ import annotations

import json
import re

import pytest

from adlc.adapters.daytwo.sre_agent import INCIDENT_SCHEMA_VERSION, SreAgentReceiver
from tests.l10_daytwo.conftest import FIXTURES, load_fixture


@pytest.fixture
def receiver() -> SreAgentReceiver:
    return SreAgentReceiver()


# -- repository_dispatch ----------------------------------------------------


def test_repository_dispatch_payload_is_normalised(receiver: SreAgentReceiver) -> None:
    incident = receiver.parse(load_fixture("repository_dispatch.json"))

    assert incident["schemaVersion"] == INCIDENT_SCHEMA_VERSION
    assert incident["id"] == "INC-2026-08-19-0007"
    assert incident["severity"] == "sev2"
    assert incident["source"] == "azure-sre-agent"
    assert incident["detectedAt"] == "2026-08-19T14:07:00Z"
    assert "p95 latency" in incident["summary"]
    assert incident["resource"]["name"] == "adlc-day2-demo"
    assert incident["resource"]["resourceGroup"] == "rg-adlc-demo"
    assert incident["deployment"]["commit"].startswith("9f2c1ab")
    assert [s["id"] for s in incident["signals"]] == ["S001", "S002"]
    assert incident["signals"][0]["value"] == 2400
    assert incident["signals"][0]["threshold"] == 800


def test_the_entire_inbound_payload_is_preserved_for_audit(receiver: SreAgentReceiver) -> None:
    raw = load_fixture("repository_dispatch.json")
    incident = receiver.parse(raw)
    # The envelope, not just the client_payload - nothing is dropped.
    assert incident["raw"] == raw
    assert incident["raw"]["sender"]["login"] == "azure-sre-agent[bot]"


def test_parse_accepts_a_json_string(receiver: SreAgentReceiver) -> None:
    text = (FIXTURES / "repository_dispatch.json").read_text(encoding="utf-8")
    assert receiver.parse(text)["id"] == "INC-2026-08-19-0007"


def test_load_reads_a_file(receiver: SreAgentReceiver) -> None:
    assert receiver.load(FIXTURES / "repository_dispatch.json")["severity"] == "sev2"


def test_load_reads_the_github_event_path(receiver: SreAgentReceiver, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(FIXTURES / "repository_dispatch.json"))
    assert receiver.load()["id"] == "INC-2026-08-19-0007"


# -- issues -----------------------------------------------------------------


def test_issue_payload_uses_the_embedded_json_block(receiver: SreAgentReceiver) -> None:
    incident = receiver.parse(load_fixture("sre_issue.json"))

    assert incident["id"] == "INC-2026-08-19-0009"          # from the fenced block
    assert incident["title"] == "Checkout error rate above 5% since 14:07 UTC"
    assert incident["source"] == "github-issue"
    assert incident["severity"] == "sev2"                    # from `severity: high`
    assert incident["signals"][0]["value"] == 0.061
    assert incident["detectedAt"] == "2026-08-19T14:11:32Z"
    assert any("issues/412" in link["url"] for link in incident["links"])
    assert "adlc:incident" in incident["labels"]


def test_issue_summary_keeps_both_the_prose_and_the_structured_block(
    receiver: SreAgentReceiver,
) -> None:
    """The JSON block wins for machine fields; the human "why" is never lost."""
    incident = receiver.parse(load_fixture("sre_issue.json"))
    assert "Rolling back is not an option" in incident["summary"]     # prose kept
    assert "returns 502" in incident["summary"]                       # block kept
    assert "```" not in incident["summary"]                           # fence stripped
    assert "INC-2026-08-19-0009" not in incident["summary"]           # raw JSON not inlined


def test_issue_without_a_json_block_still_parses(receiver: SreAgentReceiver) -> None:
    incident = receiver.parse({
        "issue": {"title": "Disk full on worker", "body": "No structured payload here.",
                  "labels": ["critical"], "number": 7},
    })
    assert incident["title"] == "Disk full on worker"
    assert incident["summary"] == "No structured payload here."
    assert incident["severity"] == "sev1"                    # `critical` -> sev1
    assert incident["id"]                                    # deterministic id was minted


# -- workflow_dispatch and bare objects -------------------------------------


def test_workflow_dispatch_inputs_are_json_decoded(receiver: SreAgentReceiver) -> None:
    inner = {"title": "Queue depth climbing", "severity": "3", "summary": "Backlog growing."}
    incident = receiver.parse({"inputs": {"incident": json.dumps(inner)}})
    assert incident["title"] == "Queue depth climbing"
    assert incident["severity"] == "sev3"


def test_alternative_key_spellings_are_tolerated(receiver: SreAgentReceiver) -> None:
    incident = receiver.parse({
        "alertName": "Memory saturation",
        "probableCause": "leak in the cache layer",
        "firedAt": "2026-08-19T09:00:00Z",
        "priority": "critical",
        "resourceId": "/subscriptions/x/resourceGroups/y/providers/Microsoft.App/containerApps/z",
        "metrics": [{"name": "rss", "observed": 3900, "slo": 2048, "units": "MiB"}],
        "portalUrl": "https://portal.azure.com/#blade",
    })
    assert incident["title"] == "Memory saturation"
    assert incident["suspectedCause"] == "leak in the cache layer"
    assert incident["severity"] == "sev1"
    assert incident["detectedAt"] == "2026-08-19T09:00:00Z"
    assert incident["resource"]["id"].endswith("/z")
    assert incident["signals"][0]["value"] == 3900
    assert incident["signals"][0]["threshold"] == 2048
    assert incident["signals"][0]["unit"] == "MiB"
    assert any(link["url"].startswith("https://portal.azure.com") for link in incident["links"])


def test_minted_ids_are_deterministic(receiver: SreAgentReceiver) -> None:
    payload = {"title": "Same thing", "detectedAt": "2026-08-19T00:00:00Z"}
    assert receiver.parse(dict(payload))["id"] == receiver.parse(dict(payload))["id"]


def test_unknown_severity_falls_back_rather_than_guessing_high(
    receiver: SreAgentReceiver,
) -> None:
    assert receiver.parse({"title": "x", "severity": "banana"})["severity"] == "sev3"


def test_a_non_object_payload_is_a_clear_error(receiver: SreAgentReceiver) -> None:
    with pytest.raises(TypeError, match="must be a JSON object"):
        receiver.parse([1, 2, 3])


# -- brief rendering --------------------------------------------------------


def test_brief_is_day_one_shaped_markdown(receiver: SreAgentReceiver) -> None:
    incident = receiver.parse(load_fixture("repository_dispatch.json"))
    brief = receiver.to_brief(incident)

    assert brief.startswith("---\n")
    assert "adlc: brief" in brief
    assert "origin: day-2-incident" in brief
    assert "# Checkout p95 latency breached SLO after deploy" in brief
    for heading in ("## Problem", "## Impact", "## Affected resource",
                    "## Observed signals", "## Suspected cause",
                    "## Deployment context", "## Acceptance criteria", "## References"):
        assert heading in brief, f"missing {heading}"

    # The signal table carries the numbers a reviewer needs.
    assert "| 2400 ms |" in brief
    assert "| 800 ms |" in brief
    # The KQL that produced the signal is preserved.
    assert "```kusto" in brief
    assert "percentile(DurationMs, 95)" in brief
    # And the reuse claim is stated in the artifact itself.
    assert "standard day-1 intake path" in brief


def test_brief_survives_an_almost_empty_incident(receiver: SreAgentReceiver) -> None:
    brief = receiver.to_brief(receiver.parse({}))
    assert "# Untitled incident" in brief
    assert "No summary supplied" in brief
    assert "## Acceptance criteria" in brief


def test_markdown_table_cells_are_escaped(receiver: SreAgentReceiver) -> None:
    """A pipe in a signal description must not break the markdown table."""
    incident = receiver.parse({
        "title": "pipe test",
        "signals": [{"id": "S001", "description": "a | b", "value": 1, "threshold": 2}],
    })
    row = next(line for line in receiver.to_brief(incident).splitlines() if "a \\| b" in line)

    # Split on unescaped pipes only: 4 columns => 5 fragments (empty ends).
    cells = re.split(r"(?<!\\)\|", row)
    assert len(cells) == 6, f"escaping produced the wrong column count: {row!r}"
    assert cells[1].strip() == "a \\| b"


def test_write_set_hint_is_carried_onto_the_incident(receiver: SreAgentReceiver) -> None:
    incident = receiver.parse(load_fixture("repository_dispatch.json"))
    assert incident["writeSet"] == ["src/checkout/handler.py", "src/checkout/inventory.py"]


def test_write_set_hint_is_absent_when_not_supplied(receiver: SreAgentReceiver) -> None:
    assert "writeSet" not in receiver.parse({"title": "no hint"})


def test_write_brief_emits_brief_and_incident_json(receiver: SreAgentReceiver, tmp_path) -> None:
    incident = receiver.parse(load_fixture("repository_dispatch.json"))
    path = receiver.write_brief(incident, tmp_path / "run")

    assert path.name == "brief.md"
    assert path.read_text(encoding="utf-8") == receiver.to_brief(incident)

    stored = json.loads((tmp_path / "run" / "incident.json").read_text(encoding="utf-8"))
    assert stored["id"] == incident["id"]
    assert stored["schemaVersion"] == INCIDENT_SCHEMA_VERSION
