"""Refuse server-side fetches of non-public addresses.

Source URLs are typed in by an admin and fetched by the server, which usually sits on
a LAN next to services that trust their network (routers, admin APIs, metadata
endpoints). Every URL is resolved and refused unless all of its addresses are
globally routable. Checked when a source is saved and on every request discovery
makes, redirects included.

Not covered: DNS rebinding between this check and the connection.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class NonPublicURL(ValueError):
    """The URL is refused. ``code`` names why (``REASONS``), so a response can say it in fixed text."""

    def __init__(self, message: str, code: str = "non_public"):
        super().__init__(message)
        self.code = code


REASONS = {
    "scheme": "only http(s) URLs are allowed",
    "no_host": "the URL has no host",
    "unresolvable": "the host does not resolve",
    "non_public": "the host resolves to a non-public address",
}


def require_public_url(url: str) -> None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise NonPublicURL(
            f"only http(s) URLs are allowed, got {parts.scheme or 'none'!r}", "scheme"
        )
    host = parts.hostname
    if not host:
        raise NonPublicURL("URL has no host", "no_host")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NonPublicURL(f"cannot resolve {host}: {exc}", "unresolvable") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not addr.is_global:
            raise NonPublicURL(f"{host} resolves to non-public address {addr}", "non_public")


def httpx_request_hook(request) -> None:
    """httpx event hook: runs before every request, including each redirect hop."""
    require_public_url(str(request.url))
