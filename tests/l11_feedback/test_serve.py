"""L11 S8 -- the loopback submission server.

This is the only network surface in the whole framework, and it is reachable by
any page the reviewer's browser happens to be showing. Almost every test here is
therefore a refusal test.
"""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from adlc.config import Config
from adlc.runs import RunDir, read_json, sha256_bytes, write_json
from adlc.serve import MAX_BODY_BYTES, NONCE_HEADER, ServerHandle, serve_report
from tests.l11_feedback.conftest import CANDIDATE_SHA, make_run


@pytest.fixture
def run(cfg: Config) -> RunDir:
    rd = make_run(
        cfg, "2026-08-20-c0de", head_sha=CANDIDATE_SHA, screenshots={"home.png": (10, 20, 30)}
    )
    rd.report.write_text("<!doctype html><title>ADLC run</title>", encoding="utf-8")
    seed = read_json(rd.path / "seed.json")
    seed["artifacts"] = rd.scan_artifacts()
    write_json(rd.run_json, seed)
    return rd


@pytest.fixture
def server(cfg: Config, run: RunDir) -> Iterator[ServerHandle]:
    handle = serve_report(cfg, run)
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture
def pack(run: RunDir, valid_pack: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(valid_pack)
    doc["runId"] = run.run_id
    doc["candidateSha"] = CANDIDATE_SHA
    shot = run.evidence_dir / "candidate-a" / "home.png"
    doc["annotations"][0]["artifactSha256"] = sha256_bytes(shot.read_bytes())
    return doc


def _request(
    url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(server: ServerHandle, payload: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json", NONCE_HEADER: server.nonce}
    headers.update(kwargs.pop("headers", {}))
    status, body = _request(
        f"{server.origin}/feedback",
        method="POST",
        body=json.dumps(payload).encode("utf-8"),
        headers=headers,
        **kwargs,
    )
    return status, json.loads(body or b"{}") if body[:1] in (b"{", b"[") else {"raw": body}


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def test_binds_loopback_only(server: ServerHandle) -> None:
    """A routable bind would expose a write endpoint to the network."""
    assert server.httpd.server_address[0] == "127.0.0.1"
    assert server.host == "127.0.0.1"


def test_port_is_ephemeral_by_default(server: ServerHandle) -> None:
    assert server.port > 0


def test_nonce_is_long_enough_to_be_unguessable(server: ServerHandle) -> None:
    assert len(server.nonce) >= 32


def test_url_carries_the_nonce(server: ServerHandle) -> None:
    assert server.url.endswith(f"?nonce={server.nonce}")


# ---------------------------------------------------------------------------
# Serving the report
# ---------------------------------------------------------------------------


def test_report_is_served_with_the_nonce(server: ServerHandle) -> None:
    status, body = _request(server.url)
    assert status == 200
    assert body.startswith(b"<!doctype html>")


def test_report_without_a_nonce_is_refused(server: ServerHandle) -> None:
    status, _ = _request(f"{server.origin}/report.html")
    assert status == 403


def test_report_with_a_wrong_nonce_is_refused(server: ServerHandle) -> None:
    status, _ = _request(f"{server.origin}/report.html?nonce=guess")
    assert status == 403


def test_unknown_path_is_404(server: ServerHandle) -> None:
    status, _ = _request(f"{server.origin}/anything?nonce={server.nonce}")
    assert status == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../../../../etc/passwd",
        "/report.html/../seed.json",
        "/%2e%2e/%2e%2e/seed.json",
        "/.adlc/runs/2026-08-20-c0de/seed.json",
    ],
)
def test_no_path_is_ever_turned_into_a_file(server: ServerHandle, path: str) -> None:
    """Only two hard-coded routes exist, so traversal has nothing to traverse."""
    status, body = _request(f"{server.origin}{path}?nonce={server.nonce}")
    assert status == 404
    assert b"seed" not in body and b"root:" not in body


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def test_submission_applies_the_pack(
    cfg: Config, run: RunDir, server: ServerHandle, pack: dict[str, Any]
) -> None:
    status, result = _post(server, pack)

    assert status == 200
    assert result["applied"] is True
    assert result["outcome"] == "iterate"
    assert RunDir(cfg, result["successorRun"]).exists()


def test_a_refused_pack_returns_422_not_500(
    server: ServerHandle, pack: dict[str, Any]
) -> None:
    status, result = _post(server, dict(pack, candidateSha="f" * 40))
    assert status == 422
    assert result["applied"] is False


def test_submission_without_the_nonce_header_is_refused(
    run: RunDir, server: ServerHandle, pack: dict[str, Any]
) -> None:
    """A cross-origin page cannot set a custom header without a preflight."""
    status, _ = _request(
        f"{server.origin}/feedback",
        method="POST",
        body=json.dumps(pack).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 403
    assert run.latest_stage("feedback") is None


def test_submission_with_a_wrong_nonce_is_refused(
    server: ServerHandle, pack: dict[str, Any]
) -> None:
    status, _ = _post(server, pack, headers={NONCE_HEADER: "not-the-nonce"})
    assert status == 403


def test_cross_origin_submission_is_refused(
    server: ServerHandle, pack: dict[str, Any]
) -> None:
    status, _ = _post(server, pack, headers={"Origin": "https://evil.example"})
    assert status == 403


def test_same_origin_submission_is_allowed(
    server: ServerHandle, pack: dict[str, Any]
) -> None:
    status, _ = _post(server, pack, headers={"Origin": server.origin})
    assert status == 200


def test_preflight_is_not_answered(server: ServerHandle) -> None:
    """Answering OPTIONS is exactly what would let a third-party page in."""
    status, _ = _request(f"{server.origin}/feedback", method="OPTIONS")
    assert status == 405


def test_oversized_body_is_refused_before_it_is_read(server: ServerHandle) -> None:
    status, _ = _request(
        f"{server.origin}/feedback",
        method="POST",
        body=b"{}",
        headers={
            "Content-Type": "application/json",
            NONCE_HEADER: server.nonce,
            "Content-Length": str(MAX_BODY_BYTES + 1),
        },
    )
    assert status == 413


def test_invalid_json_does_not_crash_the_server(
    server: ServerHandle, pack: dict[str, Any]
) -> None:
    status, _ = _request(
        f"{server.origin}/feedback",
        method="POST",
        body=b"{not json",
        headers={"Content-Type": "application/json", NONCE_HEADER: server.nonce},
    )
    assert status == 400

    # still alive
    assert _post(server, pack)[0] == 200


def test_post_to_an_unknown_path_is_404(server: ServerHandle, pack: dict[str, Any]) -> None:
    status, _ = _request(
        f"{server.origin}/anything",
        method="POST",
        body=json.dumps(pack).encode("utf-8"),
        headers={"Content-Type": "application/json", NONCE_HEADER: server.nonce},
    )
    assert status == 404


def test_responses_forbid_sniffing_and_caching(server: ServerHandle) -> None:
    request = urllib.request.Request(server.url)
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"


def test_stop_releases_the_port(cfg: Config, run: RunDir) -> None:
    handle = serve_report(cfg, run)
    port = handle.port
    handle.stop()
    with pytest.raises(urllib.error.URLError):
        _request(f"http://127.0.0.1:{port}/report.html?nonce={handle.nonce}")
