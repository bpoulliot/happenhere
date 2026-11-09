"""The SSRF guard: public hosts pass, every non-public address is refused."""

from __future__ import annotations

import socket

import httpx
import pytest

from app import netguard
from app.netguard import NonPublicURL, require_public_url


@pytest.fixture
def dns(monkeypatch):
    table: dict[str, list[str]] = {}

    def fake(host, port, *a, **kw):
        if host not in table:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)
    return table


def test_public_host_passes(dns):
    dns["venue.example"] = ["93.184.216.34"]
    require_public_url("https://venue.example/events")


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.0.10",
        "172.16.0.2",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.10",
        "198.51.100.7",
        "203.0.113.5",
        "::1",
        "fc00::1",
        "2001:db8::1",
    ],
)
def test_non_public_addresses_refused(dns, ip):
    dns["sneaky.example"] = [ip]
    with pytest.raises(NonPublicURL):
        require_public_url("http://sneaky.example/")


def test_any_private_answer_refuses(dns):
    dns["mixed.example"] = ["93.184.216.34", "192.0.2.10"]
    with pytest.raises(NonPublicURL):
        require_public_url("https://mixed.example/")


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "gopher://x/", "ftp://x/", "https:///nohost"]
)
def test_bad_scheme_or_host_refused(dns, url):
    with pytest.raises(NonPublicURL):
        require_public_url(url)


def test_unresolvable_refused(dns):
    with pytest.raises(NonPublicURL):
        require_public_url("https://nowhere.example/")


def test_httpx_hook_blocks_redirect_to_lan(dns):
    """A public page redirecting to a LAN address is refused before the hop is sent."""
    dns["venue.example"] = ["93.184.216.34"]
    dns["router.internal"] = ["192.0.2.1"]
    sent = []

    def handler(request):
        sent.append(request.url.host)
        if request.url.host == "venue.example":
            return httpx.Response(302, headers={"location": "http://router.internal/admin"})
        return httpx.Response(200, text="router admin")

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        event_hooks={"request": [netguard.httpx_request_hook]},
    ) as client:
        with pytest.raises(NonPublicURL):
            client.get("https://venue.example/events")
    assert sent == ["venue.example"]


def test_detect_refuses_lan_url_without_fetching(dns, monkeypatch):
    from app import discovery

    dns["router.internal"] = ["192.0.2.1"]
    monkeypatch.setattr(discovery, "_fetch_soup", lambda *a, **k: pytest.fail("fetched"))
    result = discovery.detect("http://router.internal/")
    assert not result.success
    assert any("Refused" in n for n in result.notes)
