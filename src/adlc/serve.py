"""Loopback server -- an optional convenience wrapper over ``feedback apply``.

The evidence page is deliberately backend-less: it opens from ``file://``, and
exporting a pack file is *the* contract. This server exists only so a reviewer
can click "submit" instead of downloading a file and running a command. It adds
no capability, and nothing depends on it.

Because it is a local HTTP endpoint reachable by *any* page the browser happens
to be showing, it is hardened out of proportion to its size:

* bound to ``127.0.0.1`` only -- never a routable interface,
* every request must carry an ``X-ADLC-Nonce`` header. A custom header
  forces a CORS preflight, and this server answers no preflight, so a drive-by
  page cannot forge a submission even though it can reach the port,
* an ``Origin`` header, when present, must be this server's own,
* the request body is length-checked before it is read, not after,
* URLs are never turned into filesystem paths. Two routes are served and they
  are hard-coded, so there is no traversal surface to get wrong.

The nonce is never handed out by an unauthenticated route: it is printed to the
terminal that started the server. A page that cannot read a cross-origin
response body therefore cannot learn it. It is a bearer token for the lifetime
of the process, not a single-use one: anything that can read it (shell history,
a screen share, a browser extension) can submit until the server is stopped,
which is why the server is opt-in and short-lived by design.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from adlc.config import Config
from adlc.runs import RunDir
from adlc.stages.feedback import apply_feedback

#: Refused before the body is read. A report with inlined evidence is large, but
#: a *feedback pack* is text: the schema caps it at 500 items x 4 KB per field.
MAX_BODY_BYTES = 4 * 1024 * 1024

LOOPBACK = "127.0.0.1"
NONCE_HEADER = "X-ADLC-Nonce"
REPORT_PATH = "/report.html"
SUBMIT_PATH = "/feedback"


@dataclass
class ServerHandle:
    """A running server plus everything needed to talk to it or stop it."""

    host: str
    port: int
    nonce: str
    httpd: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{REPORT_PATH}?nonce={self.nonce}"

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _make_handler(cfg: Config, rd: RunDir, nonce: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "adlc-report"
        sys_version = ""

        # -- helpers ------------------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def _text(self, code: int, message: str) -> None:
            self._send(code, message.encode("utf-8"), "text/plain; charset=utf-8")

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # same-origin fetches and curl omit it
            port = self.server.server_address[1]
            return origin in {f"http://{LOOPBACK}:{port}", f"http://localhost:{port}"}

        def _authorised(self, supplied: str | None) -> bool:
            # Constant-time so the nonce cannot be recovered by timing a few
            # thousand guesses against a server the attacker can already reach.
            # Encoded first: `compare_digest` raises TypeError on non-ASCII str,
            # and the nonce arrives in a URL an attacker controls, so a stray
            # unicode character would be a 500 instead of a clean 403.
            if not supplied:
                return False
            return secrets.compare_digest(
                str(supplied).encode("utf-8", "surrogatepass"), nonce.encode("utf-8")
            )

        # -- routes -------------------------------------------------------
        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path != REPORT_PATH:
                self._text(404, "not found")
                return
            supplied = _query_value(query, "nonce")
            if not self._authorised(supplied):
                self._text(403, "missing or invalid nonce")
                return
            if not rd.report.is_file():
                self._text(404, "no report.html in this run - run `adlc report` first")
                return
            self._send(200, rd.report.read_bytes(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            path, _, _ = self.path.partition("?")
            if path != SUBMIT_PATH:
                self._text(404, "not found")
                return
            if not self._authorised(self.headers.get(NONCE_HEADER)):
                self._text(403, "missing or invalid nonce")
                return
            if not self._origin_ok():
                self._text(403, "cross-origin submissions are refused")
                return

            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._text(411, "Content-Length is required")
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._text(413, f"body exceeds {MAX_BODY_BYTES} bytes")
                return

            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(400, {"applied": False, "reason": f"invalid JSON: {exc}"})
                return

            try:
                result = apply_feedback(cfg, rd, payload)
            except Exception as exc:  # noqa: BLE001 - a bad pack must not kill the server
                self._json(500, {"applied": False, "reason": str(exc)})
                return
            self._json(200 if result.get("applied") else 422, result)

        def do_OPTIONS(self) -> None:
            # No CORS preflight is answered, deliberately: that is what stops a
            # third-party page from sending the custom nonce header at all.
            self._text(405, "method not allowed")

        def log_message(self, fmt: str, *args: Any) -> None:
            return  # the CLI prints what matters; access logs only add noise

    return Handler


def _query_value(query: str, key: str) -> str | None:
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == key:
            return value
    return None


def serve_report(cfg: Config, rd: RunDir, *, port: int = 0) -> ServerHandle:
    """Start the loopback server and return a handle. Never binds publicly.

    ``port=0`` asks the OS for a free port, which is the default because a fixed
    port is both a collision and a fingerprint: a page that knows the port can at
    least probe it. The real URL is printed by the caller.
    """
    nonce = secrets.token_urlsafe(32)
    httpd = ThreadingHTTPServer((LOOPBACK, port), _make_handler(cfg, rd, nonce))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="adlc-report-serve", daemon=True)
    thread.start()
    return ServerHandle(
        host=LOOPBACK, port=httpd.server_address[1], nonce=nonce, httpd=httpd, thread=thread
    )
