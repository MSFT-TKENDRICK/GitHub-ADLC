"""Evidence must never carry credentials out of the build.

HAR files, Lighthouse network audits and axe HTML snippets routinely contain
bearer tokens, session cookies and signed URLs. Everything the collectors write
goes through :func:`redact` first.
"""

from __future__ import annotations

import json

from adlc.adapters.evidence.axe import _truncate_html
from adlc.adapters.evidence.lighthouse import REDACTED, redact, redact_text, redact_url


def test_sensitive_mapping_keys_are_replaced() -> None:
    out = redact(
        {
            "Authorization": "Bearer abcdefghijklmnop",
            "Cookie": "session=deadbeef",
            "Set-Cookie": "sid=deadbeef; HttpOnly",
            "x-api-key": "k-123456",
            "refresh_token": "rt-123456",
            "csrfToken": "not-matched-by-key",
            "userAgent": "HeadlessChrome/129",
        }
    )
    assert out["Authorization"] == REDACTED
    assert out["Cookie"] == REDACTED
    assert out["Set-Cookie"] == REDACTED
    assert out["x-api-key"] == REDACTED
    assert out["refresh_token"] == REDACTED
    assert out["userAgent"] == "HeadlessChrome/129"


def test_har_shaped_name_value_pairs_are_redacted() -> None:
    har_entry = {
        "request": {
            "url": "https://api.test/v1/me?access_token=abcdef1234567890&page=2",
            "headers": [
                {"name": "Authorization", "value": "Bearer abcdefghijklmnopqrstuv"},
                {"name": "Accept", "value": "application/json"},
            ],
            "cookies": [{"name": "session_id", "value": "s3cr3t"}],
            "queryString": [
                {"name": "access_token", "value": "abcdef1234567890"},
                {"name": "page", "value": "2"},
            ],
        }
    }
    out = redact(har_entry)
    headers = {h["name"]: h["value"] for h in out["request"]["headers"]}
    assert headers["Authorization"] == REDACTED
    assert headers["Accept"] == "application/json"
    assert out["request"]["cookies"][0]["value"] == REDACTED
    query = {q["name"]: q["value"] for q in out["request"]["queryString"]}
    assert query["access_token"] == REDACTED
    assert query["page"] == "2"
    assert "abcdef1234567890" not in out["request"]["url"]
    assert "page=2" in out["request"]["url"]


def test_urls_keep_their_shape_but_lose_their_secrets() -> None:
    redacted = redact_url("https://app.test/a/b?access_token=xyz&id=7&sig=abc")
    assert redacted.startswith("https://app.test/a/b?")
    assert "xyz" not in redacted
    assert "abc" not in redacted
    assert "id=7" in redacted
    # Untouched when there is nothing sensitive.
    assert redact_url("https://app.test/a?id=7") == "https://app.test/a?id=7"
    assert redact_url("not a url") == "not a url"


def test_token_literals_are_scrubbed_anywhere() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    samples = {
        f"Authorization: Bearer {jwt}": jwt,
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "aws key AKIAIOSFODNN7EXAMPLE here": "AKIAIOSFODNN7EXAMPLE",
        "slack xoxb-1234567890-abcdefghijkl": "xoxb-1234567890-abcdefghijkl",
    }
    for text, secret in samples.items():
        assert secret not in redact_text(text), text
        assert REDACTED in redact_text(text)


def test_hidden_input_values_are_redacted() -> None:
    html = '<input type="hidden" name="csrf_token" value="9f8e7d6c5b4a3f2e1d0c">'
    out = redact_text(html)
    assert "9f8e7d6c5b4a3f2e1d0c" not in out
    assert 'name="csrf_token"' in out

    reversed_order = '<input value="9f8e7d6c5b4a3f2e1d0c" name="session_id">'
    assert "9f8e7d6c5b4a3f2e1d0c" not in redact_text(reversed_order)

    attribute_named_token = '<div data-access-token="abcd1234efgh">x</div>'
    assert "abcd1234efgh" not in redact_text(attribute_named_token)

    benign = '<a href="/terms" title="Terms">Terms</a>'
    assert redact_text(benign) == benign


def test_environment_style_assignments_are_scrubbed() -> None:
    """A recorded command line must not leak `--env TOKEN=...`."""
    assert redact_text("API_TOKEN=abc123def456") == f"API_TOKEN={REDACTED}"
    assert redact_text("--env SESSION_SECRET=hunter2") == f"--env SESSION_SECRET={REDACTED}"
    assert redact_text("VUS=5") == "VUS=5"
    assert redact_text("BASE_URL=http://app.test/") == "BASE_URL=http://app.test/"


def test_run_tool_redacts_the_recorded_command(tmp_path) -> None:
    import sys

    from adlc.adapters.evidence.lighthouse import run_tool

    result = run_tool(
        [sys.executable, "-c", "pass", "--env", "API_TOKEN=abc123def456"],
        cwd=tmp_path,
        timeout=60,
    )
    assert result["ran"] is True
    assert "abc123def456" not in json.dumps(result["command"])


def test_base64_data_uris_are_left_intact() -> None:
    """Screenshots carry no credentials; rewriting them would corrupt evidence."""
    data_uri = "data:image/webp;base64," + ("A" * 5000) + "-sk-abcdefghijklmnopqrstuvwx"
    assert redact_text(data_uri) == data_uri


def test_lighthouse_report_is_scrubbed(lhr) -> None:
    out = redact(lhr)
    serialised = json.dumps(out)

    assert "super-secret-token-value" not in serialised
    assert "abcdef1234567890" not in serialised
    assert out["configSettings"]["extraHeaders"]["Authorization"] == REDACTED
    assert out["configSettings"]["extraHeaders"]["Cookie"] == REDACTED
    # Structure preserved: the report is still a usable Lighthouse artifact.
    assert out["audits"]["largest-contentful-paint"]["numericValue"] == 1820.4
    assert out["categories"]["performance"]["score"] == 0.94
    assert out["audits"]["network-requests"]["details"]["items"][1]["url"].endswith("app.js")
    assert out["audits"]["final-screenshot"]["details"]["data"].startswith("data:image/webp")


def test_axe_results_are_scrubbed(axe_results) -> None:
    out = redact(_truncate_html(axe_results))
    serialised = json.dumps(out)

    assert "s3cr3t-session-value" not in serialised
    assert "9f8e7d6c5b4a3f2e1d0c" not in serialised
    assert out["violations"][0]["id"] == "color-contrast"
    assert out["violations"][0]["impact"] == "serious"
    assert len(out["violations"]) == 5


def test_long_html_snippets_are_truncated() -> None:
    node = {"nodes": [{"html": "<div>" + "x" * 4000 + "</div>", "target": ["div"]}]}
    out = _truncate_html(node)
    html = out["nodes"][0]["html"]
    assert len(html) < 700
    assert html.endswith("[truncated]")
    assert out["nodes"][0]["target"] == ["div"]


def test_redaction_is_non_destructive_to_the_input(lhr) -> None:
    before = json.dumps(lhr, sort_keys=True)
    redact(lhr)
    assert json.dumps(lhr, sort_keys=True) == before


def test_redact_handles_odd_shapes() -> None:
    assert redact(None) is None
    assert redact(7) == 7
    assert redact(True) is True
    assert redact([[["deep"]]]) == [[["deep"]]]
    cyclic_ish = {"a": {"b": {"c": {"d": "e"}}}}
    assert redact(cyclic_ish) == cyclic_ish
