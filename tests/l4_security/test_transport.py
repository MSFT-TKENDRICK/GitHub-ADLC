"""``adlc.adapters._transport`` -- the shared HTTPS-only credential guard.

Previously had zero dedicated tests despite gating every credentialed
adapter request (75% coverage, only 12 statements but no test file at
all). Covers the accept path (https, loopback http variants) and the
reject path (plain http to a non-loopback host, and non-http(s) schemes
that urllib would otherwise happily open).
"""

from __future__ import annotations

import pytest

from adlc.adapters._transport import InsecureEndpointError, require_https


class TestAccepted:
    def test_https_url_is_returned_unchanged(self) -> None:
        url = "https://api.github.com/repos/acme/widget"
        assert require_https(url) == url

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_plain_http_to_a_loopback_host_is_allowed(self, host: str) -> None:
        url = f"http://{host}:8080/flagd"
        assert require_https(url) == url


class TestRejected:
    def test_plain_http_to_a_non_loopback_host_is_rejected(self) -> None:
        with pytest.raises(InsecureEndpointError, match="not https"):
            require_https("http://api.github.com/repos/acme/widget")

    def test_file_scheme_is_rejected(self) -> None:
        with pytest.raises(InsecureEndpointError):
            require_https("file:///etc/passwd")

    def test_data_scheme_is_rejected(self) -> None:
        with pytest.raises(InsecureEndpointError):
            require_https("data:text/plain,hello")

    def test_error_message_names_the_offending_url_and_scheme(self) -> None:
        with pytest.raises(InsecureEndpointError, match="ftp") as exc_info:
            require_https("ftp://example.invalid/x")
        assert "ftp://example.invalid/x" in str(exc_info.value)
