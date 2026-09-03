"""``adapters.evidence.playwright`` -- the spine's default evidence collector.

Previously exercised only incidentally (33% coverage) via other tests. This
file directly covers: `detect()` (both branches), the redaction helpers (HAR
headers/cookies/query-strings, PAT-shaped tokens in free text), the kind/mime
mapping tables, the generated replay script, and `collect()` itself --
both the real "playwright not installed" branch (this environment) and a
fully mocked `sync_playwright` context manager so the collection/redaction/
artifact-hashing pipeline is exercised end-to-end without a real browser.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from adlc.adapters.evidence.playwright import PlaywrightCollector
from adlc.config import Config


class TestDetect:
    def test_detect_false_when_neither_npx_nor_playwright_on_path(
        self, no_tools: Config
    ) -> None:
        available, reason = PlaywrightCollector.detect(no_tools)
        assert available is False
        assert "npx" in reason

    def test_detect_false_when_binary_present_but_python_package_missing(
        self, no_tools: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "adlc.adapters.evidence.playwright.shutil.which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        available, reason = PlaywrightCollector.detect(no_tools)
        assert available is False
        assert "pip install playwright" in reason

    def test_detect_true_when_binary_and_package_both_present(
        self, no_tools: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "adlc.adapters.evidence.playwright.shutil.which",
            lambda name: "/usr/bin/npx",
        )
        fake_module = types.ModuleType("playwright")
        monkeypatch.setitem(sys.modules, "playwright", fake_module)
        available, reason = PlaywrightCollector.detect(no_tools)
        assert available is True
        assert "available" in reason


class TestRedactHar:
    def test_redacts_sensitive_request_and_response_headers(self) -> None:
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "headers": [
                                {"name": "Authorization", "value": "Bearer secret-token"},
                                {"name": "X-Custom", "value": "keep-me"},
                            ]
                        },
                        "response": {
                            "headers": [{"name": "Set-Cookie", "value": "session=abc123"}]
                        },
                    }
                ]
            }
        }
        redacted = PlaywrightCollector.redact_har(har)
        req_headers = redacted["log"]["entries"][0]["request"]["headers"]
        assert req_headers[0]["value"] == "[REDACTED-BY-ADLC]"
        assert req_headers[1]["value"] == "keep-me"
        resp_headers = redacted["log"]["entries"][0]["response"]["headers"]
        assert resp_headers[0]["value"] == "[REDACTED-BY-ADLC]"

    def test_redacts_cookies_on_both_request_and_response(self) -> None:
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"cookies": [{"name": "session", "value": "raw-value"}]},
                        "response": {"cookies": [{"name": "csrf", "value": "raw-csrf"}]},
                    }
                ]
            }
        }
        redacted = PlaywrightCollector.redact_har(har)
        assert redacted["log"]["entries"][0]["request"]["cookies"][0]["value"] == (
            "[REDACTED-BY-ADLC]"
        )
        assert redacted["log"]["entries"][0]["response"]["cookies"][0]["value"] == (
            "[REDACTED-BY-ADLC]"
        )

    def test_redacts_sensitive_query_string_params_in_urls(self) -> None:
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": "https://api.example.com/x?access_token=abc123&keep=1"
                        }
                    }
                ]
            }
        }
        redacted = PlaywrightCollector.redact_har(har)
        url = redacted["log"]["entries"][0]["request"]["url"]
        assert "abc123" not in url
        assert "keep=1" in url

    def test_handles_missing_log_and_entries_keys_gracefully(self) -> None:
        assert PlaywrightCollector.redact_har({}) == {}
        assert PlaywrightCollector.redact_har({"log": {}}) == {"log": {}}

    def test_handles_entry_with_no_headers_or_cookies(self) -> None:
        har = {"log": {"entries": [{"request": {}, "response": {}}]}}
        # must not raise
        PlaywrightCollector.redact_har(har)


class TestRedactText:
    def test_redacts_query_string_tokens(self) -> None:
        text = "GET /callback?token=super-secret&page=1"
        redacted = PlaywrightCollector.redact_text(text)
        assert "super-secret" not in redacted
        assert "page=1" in redacted

    def test_redacts_github_pat_shaped_tokens(self) -> None:
        text = "leaked ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa in console"
        redacted = PlaywrightCollector.redact_text(text)
        assert "ghp_" not in redacted
        assert "[REDACTED-BY-ADLC]" in redacted

    def test_leaves_ordinary_text_untouched(self) -> None:
        text = "navigation complete in 120ms"
        assert PlaywrightCollector.redact_text(text) == text


class TestKindAndMimeMapping:
    @pytest.mark.parametrize(
        ("suffix", "expected_kind", "expected_mime"),
        [
            (".zip", "playwright_trace", "application/zip"),
            (".har", "har", "application/json"),
            (".webm", "video", "video/webm"),
            (".png", "screenshot", "image/png"),
            (".jsonl", "console_log", "application/x-ndjson"),
            (".ts", "replay_script", "text/plain"),
            (".xyz", "file", "application/octet-stream"),
        ],
    )
    def test_kind_and_mime_for_known_and_unknown_suffixes(
        self, suffix: str, expected_kind: str, expected_mime: str
    ) -> None:
        from adlc.adapters.evidence.playwright import _kind_for, _mime_for

        path = Path(f"artifact{suffix}")
        assert _kind_for(path) == expected_kind
        assert _mime_for(path) == expected_mime


class TestReplayScript:
    def test_replay_script_embeds_url_and_variant(self) -> None:
        from adlc.adapters.evidence.playwright import _replay_script

        script = _replay_script("https://example.com/app", "candidate-a")
        assert "https://example.com/app" in script
        assert "candidate-a" in script
        assert "@playwright/test" in script


class TestCollectWhenPlaywrightNotInstalled:
    def test_collect_returns_empty_list_rather_than_raising(
        self, run: dict[str, Any], evidence_out: Path
    ) -> None:
        """This environment provably has no playwright package installed --
        the real, common "not available" path, not a simulation."""
        with pytest.MonkeyPatch.context() as mp:
            mp.delitem(sys.modules, "playwright", raising=False)
            mp.delitem(sys.modules, "playwright.sync_api", raising=False)
            refs = PlaywrightCollector().collect(run, "candidate-a", evidence_out)
        assert refs == []


class TestCollectWithMockedPlaywright:
    """Exercise the full collection pipeline with a stubbed `sync_playwright`
    so the artifact-writing, redaction and hashing logic is proven without a
    real browser -- something the environment cannot provide."""

    @pytest.fixture
    def fake_playwright_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        console_handler_holder: dict[str, Any] = {}

        page = MagicMock()

        def _on(event: str, handler: Any) -> None:
            if event == "console":
                console_handler_holder["handler"] = handler

        page.on.side_effect = _on

        def _goto(url: str, **kwargs: Any) -> None:
            msg = MagicMock(type="log", text="page loaded")
            console_handler_holder["handler"](msg)

        page.goto.side_effect = _goto

        def _screenshot(path: str, **kwargs: Any) -> None:
            Path(path).write_bytes(b"\x89PNG-fake")

        page.screenshot.side_effect = _screenshot

        context = MagicMock()
        context.new_page.return_value = page

        def _tracing_stop(path: str) -> None:
            Path(path).write_bytes(b"PK-fake-trace")

        context.tracing.stop.side_effect = _tracing_stop

        def _new_context(**kwargs: Any) -> MagicMock:
            har_path = kwargs.get("record_har_path")
            if har_path:
                Path(har_path).write_text(
                    json.dumps({"log": {"entries": []}}), encoding="utf-8"
                )
            video_dir = kwargs.get("record_video_dir")
            if video_dir:
                Path(video_dir).mkdir(parents=True, exist_ok=True)
            return context

        browser = MagicMock()
        browser.new_context.side_effect = _new_context

        pw_instance = MagicMock()
        pw_instance.chromium.launch.return_value = browser

        sync_playwright_cm = MagicMock()
        sync_playwright_cm.__enter__.return_value = pw_instance
        sync_playwright_cm.__exit__.return_value = False

        sync_api_module = types.ModuleType("playwright.sync_api")
        sync_api_module.sync_playwright = MagicMock(return_value=sync_playwright_cm)
        playwright_module = types.ModuleType("playwright")

        monkeypatch.setitem(sys.modules, "playwright", playwright_module)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)
        return page, context

    def test_collect_produces_hashed_artifacts_for_every_evidence_kind(
        self, run: dict[str, Any], evidence_out: Path, fake_playwright_module
    ) -> None:
        refs = PlaywrightCollector().collect(run, "candidate-a", evidence_out)
        kinds = {ref["kind"] for ref in refs}
        assert "har" in kinds
        assert "console_log" in kinds
        assert "playwright_trace" in kinds
        assert "replay_script" in kinds
        for ref in refs:
            assert ref["sha256"]
            assert ref["bytes"] > 0
            assert Path(ref["path"]).is_absolute() or Path(ref["path"]).exists() is False

    def test_collect_writes_console_entries_captured_during_navigation(
        self, run: dict[str, Any], evidence_out: Path, fake_playwright_module
    ) -> None:
        PlaywrightCollector().collect(run, "candidate-a", evidence_out)
        console_path = evidence_out / "console.jsonl"
        assert console_path.is_file()
        lines = [
            json.loads(line)
            for line in console_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert any(entry["text"] == "page loaded" for entry in lines)

    def test_collect_appends_error_entry_when_goto_raises(
        self, run: dict[str, Any], evidence_out: Path, fake_playwright_module
    ) -> None:
        page, _context = fake_playwright_module
        page.goto.side_effect = RuntimeError("net::ERR_CONNECTION_REFUSED")

        refs = PlaywrightCollector().collect(run, "candidate-a", evidence_out)

        console_path = evidence_out / "console.jsonl"
        lines = [
            json.loads(line)
            for line in console_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert any(entry["type"] == "adlc-error" for entry in lines)
        # tracing.stop / context.close / browser.close still ran (finally
        # block), so a trace artifact is still produced despite the failure.
        assert any(ref["kind"] == "playwright_trace" for ref in refs)

    def test_collect_uses_target_url_from_run_capabilities(
        self, run: dict[str, Any], evidence_out: Path, fake_playwright_module
    ) -> None:
        run = {**run, "capabilities": {"targetUrl": "https://staging.example.com"}}
        PlaywrightCollector().collect(run, "candidate-a", evidence_out)
        replay = (evidence_out / "replay.spec.ts").read_text(encoding="utf-8")
        assert "staging.example.com" in replay

    def test_collect_defaults_to_about_blank_when_no_target_url_configured(
        self, run: dict[str, Any], evidence_out: Path, fake_playwright_module
    ) -> None:
        PlaywrightCollector().collect(run, "candidate-a", evidence_out)
        replay = (evidence_out / "replay.spec.ts").read_text(encoding="utf-8")
        assert "about:blank" in replay

    def test_collect_redacts_har_content_written_to_disk(
        self, run: dict[str, Any], evidence_out: Path, monkeypatch: pytest.MonkeyPatch,
        fake_playwright_module,
    ) -> None:
        page, context = fake_playwright_module

        def _new_context_with_secret(**kwargs: Any) -> MagicMock:
            har_path = kwargs.get("record_har_path")
            Path(har_path).write_text(
                json.dumps(
                    {
                        "log": {
                            "entries": [
                                {
                                    "request": {
                                        "headers": [
                                            {"name": "Authorization", "value": "Bearer x"}
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            return context

        context.new_page.return_value = page
        import playwright.sync_api as sync_api_module  # the stubbed module

        pw_instance = sync_api_module.sync_playwright.return_value.__enter__.return_value
        browser = pw_instance.chromium.launch.return_value
        browser.new_context.side_effect = _new_context_with_secret

        PlaywrightCollector().collect(run, "candidate-a", evidence_out)

        har_on_disk = json.loads((evidence_out / "network.har").read_text(encoding="utf-8"))
        header_value = har_on_disk["log"]["entries"][0]["request"]["headers"][0]["value"]
        assert header_value == "[REDACTED-BY-ADLC]"
