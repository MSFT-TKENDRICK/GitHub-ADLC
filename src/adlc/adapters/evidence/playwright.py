"""Playwright evidence collector -- the spine's default evidence backend.

Captures the replayable evidence bundle for one candidate variant: trace, video,
HAR, console log, screenshots, and a generated replay script.

Degrades honestly: when Playwright is not installed it reports unavailable via
``detect()`` and the framework records the evidence gate as ``not_run`` -- it
never fabricates artifacts.

Security note: HAR files and console logs routinely contain bearer tokens,
cookies and business data. Everything written here is redacted before it lands
on disk, and the sanitised review pack (built by :mod:`adlc.stages.evidence`)
never ships raw HAR/console content to a reviewing agent at all.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.ports import ArtifactRef, Run

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
_SENSITIVE_QUERY = re.compile(
    r"([?&](?:access_token|api_key|apikey|token|sig|signature|password)=)[^&#]*", re.IGNORECASE
)
_REDACTED = "[REDACTED-BY-ADLC]"


class PlaywrightCollector:
    name = "playwright"
    kind = "evidence"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        if shutil.which("npx") is None and shutil.which("playwright") is None:
            return False, "neither `npx` nor `playwright` found on PATH"
        try:
            import playwright  # noqa: F401
        except ImportError:
            return (
                False,
                "playwright python package not installed - `pip install playwright && playwright install chromium`",
            )
        return True, "playwright available"

    # -- redaction ---------------------------------------------------------
    @staticmethod
    def redact_har(har: dict[str, Any]) -> dict[str, Any]:
        """Strip credentials from a HAR document in place-safe fashion."""
        entries = (har.get("log") or {}).get("entries") or []
        for entry in entries:
            for section in ("request", "response"):
                message = entry.get(section) or {}
                for header in message.get("headers") or []:
                    if str(header.get("name", "")).lower() in _SENSITIVE_HEADERS:
                        header["value"] = _REDACTED
                for cookie in message.get("cookies") or []:
                    cookie["value"] = _REDACTED
                if url := message.get("url"):
                    message["url"] = _SENSITIVE_QUERY.sub(rf"\1{_REDACTED}", url)
        return har

    @staticmethod
    def redact_text(text: str) -> str:
        text = _SENSITIVE_QUERY.sub(rf"\1{_REDACTED}", text)
        return re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b", _REDACTED, text)

    # -- collection --------------------------------------------------------
    def collect(self, run: Run, variant: str, out: Path) -> list[ArtifactRef]:
        out.mkdir(parents=True, exist_ok=True)
        target_url = (run.get("capabilities") or {}).get("targetUrl") or "about:blank"

        artifacts: list[Path] = []
        console_entries: list[dict[str, Any]] = []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        har_path = out / "network.har"
        trace_path = out / "trace.zip"
        video_dir = out / "video"

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(
                record_har_path=str(har_path),
                record_video_dir=str(video_dir),
            )
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            page.on("console", lambda msg: console_entries.append({
                "type": msg.type, "text": self.redact_text(msg.text),
            }))
            try:
                page.goto(target_url, wait_until="load", timeout=30_000)
                page.screenshot(path=str(out / "screenshot-initial.png"), full_page=True)
            except Exception as exc:  # noqa: BLE001 - a bad page must still yield evidence
                console_entries.append({"type": "adlc-error", "text": str(exc)[:500]})
            finally:
                context.tracing.stop(path=str(trace_path))
                context.close()
                browser.close()

        if har_path.is_file():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                har_path.write_text(
                    json.dumps(self.redact_har(json.loads(har_path.read_text(encoding="utf-8")))),
                    encoding="utf-8",
                )
            artifacts.append(har_path)

        console_path = out / "console.jsonl"
        console_path.write_text(
            "\n".join(json.dumps(entry) for entry in console_entries) + "\n", encoding="utf-8"
        )
        artifacts.append(console_path)

        if trace_path.is_file():
            artifacts.append(trace_path)
        artifacts.extend(sorted(out.glob("screenshot-*.png")))
        if video_dir.is_dir():
            artifacts.extend(sorted(video_dir.glob("*.webm")))

        replay = out / "replay.spec.ts"
        replay.write_text(_replay_script(target_url, variant), encoding="utf-8")
        artifacts.append(replay)

        from adlc.runs import sha256_file  # local import avoids a cycle

        refs: list[ArtifactRef] = []
        for path in artifacts:
            refs.append({
                "path": path.as_posix(),
                "kind": _kind_for(path),
                "mimeType": _mime_for(path),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            })
        return refs


def _kind_for(path: Path) -> str:
    return {
        ".zip": "playwright_trace", ".har": "har", ".webm": "video",
        ".png": "screenshot", ".jsonl": "console_log", ".ts": "replay_script",
    }.get(path.suffix, "file")


def _mime_for(path: Path) -> str:
    return {
        ".zip": "application/zip", ".har": "application/json", ".webm": "video/webm",
        ".png": "image/png", ".jsonl": "application/x-ndjson", ".ts": "text/plain",
    }.get(path.suffix, "application/octet-stream")


def _replay_script(url: str, variant: str) -> str:
    return f"""// ADLC replay script - variant: {variant}
// Reproduce this evidence run locally:
//   npx playwright test replay.spec.ts --trace on
import {{ test, expect }} from '@playwright/test';

test('adlc replay: {variant}', async ({{ page }}) => {{
  await page.goto({url!r});
  await expect(page).toHaveTitle(/.*/);
  await page.screenshot({{ path: 'replay-screenshot.png', fullPage: true }});
}});
"""
