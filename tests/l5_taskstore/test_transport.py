"""`RestTransport` -- the real urllib client, exercised with a fake opener.

No sockets are opened: `urllib.request.build_opener()` is replaced wholesale, so
these tests cover auth headers, pagination, retries and error handling without
credentials or network.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.error

import pytest

from adlc.adapters.taskstore import github as gh


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, payload, headers: dict[str, str] | None = None) -> None:
        super().__init__(json.dumps(payload).encode() if payload is not None else b"")
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def http_error(code: int, payload=None, headers=None) -> urllib.error.HTTPError:
    body = json.dumps(payload or {}).encode()
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", headers or {}, io.BytesIO(body)
    )


def transport(*responses, **kwargs) -> tuple[gh.RestTransport, FakeOpener]:
    opener = FakeOpener(*responses)
    kwargs.setdefault("sleep", lambda _: None)
    return gh.RestTransport("ghp_test", opener=opener, **kwargs), opener


def test_requests_carry_bearer_auth_and_the_pinned_api_version() -> None:
    client, opener = transport(FakeResponse(200, {"number": 1}))
    client.request("GET", "/repos/a/b/issues/1")

    req = opener.requests[0]
    assert req.get_full_url() == "https://api.github.com/repos/a/b/issues/1"
    assert req.get_header("Authorization") == "Bearer ghp_test"
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert req.get_header("X-github-api-version") == gh.API_VERSION


def test_bodies_are_json_encoded_and_absent_for_gets() -> None:
    client, opener = transport(FakeResponse(201, {"id": 1}), FakeResponse(200, []))
    client.request("POST", "/repos/a/b/issues", {"title": "hi"})
    client.request("GET", "/repos/a/b/issues")

    assert json.loads(opener.requests[0].data) == {"title": "hi"}
    assert opener.requests[0].get_method() == "POST"
    assert opener.requests[1].data is None


def test_a_custom_api_url_is_honoured_for_ghes() -> None:
    client, opener = transport(FakeResponse(200, {}), api_url="https://ghe.example.com/api/v3")
    client.request("GET", "/repos/a/b/issues/1")
    assert opener.requests[0].get_full_url() == "https://ghe.example.com/api/v3/repos/a/b/issues/1"


def test_404_is_returned_not_raised() -> None:
    """`_call(..., allow=(404,))` depends on this; a missing issue is data."""
    client, _ = transport(http_error(404, {"message": "Not Found"}))
    status, data = client.request("GET", "/repos/a/b/issues/9")
    assert status == 404
    assert data["message"] == "Not Found"


def test_paginate_follows_the_link_header() -> None:
    page1 = FakeResponse(
        200, [{"number": 1}], {"Link": '<https://api.github.com/x?page=2>; rel="next"'}
    )
    page2 = FakeResponse(200, [{"number": 2}], {})
    client, opener = transport(page1, page2)

    assert client.paginate("/repos/a/b/issues") == [{"number": 1}, {"number": 2}]
    assert opener.requests[1].get_full_url() == "https://api.github.com/x?page=2"


def test_paginate_treats_a_404_as_empty() -> None:
    client, _ = transport(http_error(404))
    assert client.paginate("/repos/a/b/issues/9/sub_issues") == []


def test_paginate_raises_on_a_real_error() -> None:
    client, _ = transport(http_error(401, {"message": "Bad credentials"}))
    with pytest.raises(gh.GitHubTaskStoreError, match="Bad credentials"):
        client.paginate("/repos/a/b/issues")


def test_rate_limits_are_retried_honouring_retry_after() -> None:
    slept: list[float] = []
    client, opener = transport(
        http_error(429, {"message": "slow down"}, {"Retry-After": "3"}),
        FakeResponse(200, {"ok": True}),
        sleep=slept.append,
    )
    status, data = client.request("GET", "/repos/a/b/issues")

    assert (status, data) == (200, {"ok": True})
    assert slept == [3.0]
    assert len(opener.requests) == 2


def test_retries_are_bounded() -> None:
    client, opener = transport(*[http_error(503) for _ in range(5)], retries=3)
    status, _ = client.request("GET", "/repos/a/b/issues")
    assert status == 503
    assert len(opener.requests) == 3


def test_client_errors_are_not_retried() -> None:
    client, opener = transport(http_error(422, {"message": "nope"}))
    status, _ = client.request("POST", "/repos/a/b/issues", {})
    assert status == 422
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), ConnectionResetError("reset by peer"),
     http.client.IncompleteRead(b"partial")],
)
def test_raw_socket_failures_become_typed_errors(failure) -> None:
    """urllib leaks these through `resp.read()`; the transport must normalise them."""
    client, _ = transport(failure, retries=1)
    with pytest.raises(gh.GitHubTaskStoreError, match="failed:"):
        client.request("GET", "/repos/a/b/issues")


def test_connection_failures_surface_as_a_typed_error() -> None:
    client, _ = transport(urllib.error.URLError("name resolution failed"))
    with pytest.raises(gh.GitHubTaskStoreError, match="name resolution failed"):
        client.request("GET", "/repos/a/b/issues")


def test_graphql_errors_are_raised_not_silently_ignored() -> None:
    client, _ = transport(FakeResponse(200, {"data": None, "errors": [{"message": "no access"}]}))
    with pytest.raises(gh.GitHubTaskStoreError, match="no access"):
        client.graphql("query {}", {})


def test_graphql_returns_the_data_payload() -> None:
    client, opener = transport(FakeResponse(200, {"data": {"viewer": {"login": "octocat"}}}))
    assert client.graphql("query {}", {"a": 1}) == {"viewer": {"login": "octocat"}}
    assert json.loads(opener.requests[0].data) == {"query": "query {}", "variables": {"a": 1}}
    assert opener.requests[0].get_full_url() == "https://api.github.com/graphql"


def test_empty_and_non_json_responses_decode_to_none() -> None:
    client, _ = transport(FakeResponse(204, None))
    assert client.request("DELETE", "/repos/a/b/issues/1") == (204, None)


@pytest.mark.parametrize(
    "failure",
    [
        gh.GitHubTaskStoreError("POST … failed: connection reset"),
        TimeoutError("read timed out"),
        ConnectionResetError("reset by peer"),
    ],
)
def test_dependency_transport_failure_is_downgraded_to_a_warning(fake_github, failure) -> None:
    """A network blip while mirroring edges must not fail a sync."""
    from .conftest import make_graph

    real_request = fake_github.request

    def flaky(method, path, body=None):
        if method.upper() == "POST" and path.endswith("/dependencies/blocked_by"):
            raise failure
        return real_request(method, path, body)

    fake_github.request = flaky
    store = gh.GitHubTaskStore(None, transport=fake_github, owner="acme", repo="widgets")

    assert len(store.sync(make_graph(3))) == 3
    assert len(store.warnings) == 2
    assert all("could not record" in w for w in store.warnings)
