"""Shared transport guards for adapters that call remote HTTP APIs.

These adapters attach an ``Authorization`` header to a URL built from a
configurable API base (``GITHUB_API_URL``, a GHES hostname, a Foundry endpoint).
If that base were ever ``http://`` or a non-HTTP scheme, the credential would be
sent in clear text or handed to a scheme that was never intended -- ``file:``
and ``data:`` are both accepted by ``urllib`` by default.

Checking the scheme immediately before the request is cheap and turns a silent
credential leak into a loud failure.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["InsecureEndpointError", "require_https"]

#: Loopback is permitted so tests and local fakes can run without TLS. Nothing
#: else is: a credential must never leave the machine unencrypted.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class InsecureEndpointError(ValueError):
    """Raised when a credentialed request would go somewhere unsafe."""


def require_https(url: str) -> str:
    """Return ``url`` unchanged, or raise if it is not safe to send a token to.

    Raises:
        InsecureEndpointError: the scheme is not ``https``, and the host is not
            loopback.
    """
    parts = urlsplit(url)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS:
        return url
    raise InsecureEndpointError(
        f"refusing to send a credentialed request to {url!r}: "
        f"scheme {parts.scheme!r} is not https "
        f"(only http on {sorted(_LOCAL_HOSTS)} is allowed, for local testing)"
    )
