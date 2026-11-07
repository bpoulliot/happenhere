"""Item 12: URL detection recognises ticketing platforms by host, before any request.

Platforms that cannot be a source get a note naming them; a Ticketmaster venue URL is
detected as ``ticketmaster_venue``. No test reaches a platform.
"""

from __future__ import annotations

from contextlib import nullcontext

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, config, discovery, oidc
from app.config import Config
from app.main import app
from app.scrapers import ticketmaster_venue

PLATFORM_URLS = [
    ("https://dice.fm/venue/synthetic-hall-ab12", "DICE"),
    ("https://www.bandsintown.com/v/1000001-synthetic-hall", "Bandsintown"),
    ("https://www.axs.com/venues/100001/synthetic-hall-tickets", "AXS"),
    ("https://www.seetickets.com/venue/synthetic-hall/1001", "See Tickets"),
    ("https://wl.seetickets.us/SyntheticHall", "See Tickets"),
    ("https://www.eventim.us/venue/synthetic-hall-1001", "See Tickets"),
    ("https://www.etix.com/ticket/v/10001/synthetic-hall", "Etix"),
    ("https://www.eventbrite.com/o/synthetic-org-100000001", "Eventbrite"),
    ("https://www.eventbrite.co.uk/o/synthetic-org-100000001", "Eventbrite"),
    ("https://eventbrite.ca/e/synthetic-tickets-100000001", "Eventbrite"),
]


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test on any outbound request or DNS lookup."""
    calls: list[str] = []

    def refuse(*a, **kw):
        calls.append(repr(a[:1]))
        raise AssertionError(f"detection made a request: {a[:1]!r}")

    monkeypatch.setattr(discovery, "_fetch_soup", refuse)
    monkeypatch.setattr(discovery, "_probe_ical", refuse)
    monkeypatch.setattr(discovery, "_probe_playwright", refuse)
    monkeypatch.setattr(discovery, "require_public_url", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    return calls


@pytest.mark.parametrize("url, platform", PLATFORM_URLS)
def test_each_platform_host_is_recognised_without_a_request(no_network, url, platform):
    res = discovery.detect(url, config=Config())
    assert res.success is False and res.scraper_type is None
    text = " ".join(res.notes)
    assert platform in text
    assert "venue's own website" in text
    assert no_network == []


@pytest.mark.parametrize(
    "url",
    [
        "https://notdice.fm.example.com/events",
        "https://hall.example.com/?ref=dice.fm",
        "https://example.com/eventbrite.com",
        "https://myetix.com.example.com/",
    ],
)
def test_lookalike_hosts_are_not_recognised(url):
    assert discovery.platform_note(url) is None


def test_a_discovery_url_is_detected_without_a_request(no_network):
    url = "https://app.ticketmaster.com/discovery/v2/events?venueId=KovZA1,Z7rB2&apikey=x"
    res = discovery.detect(url, config=Config(ticketmaster_api_key="k"))
    assert res.success and res.scraper_type == "ticketmaster_venue"
    assert res.detected_url == "https://app.ticketmaster.com/discovery/v2/events?venueId=KovZA1,Z7rB2"
    assert no_network == []


def test_a_venue_page_is_resolved_through_the_api_only(monkeypatch, no_network):
    seen: list[str] = []

    def resolve(url, key):
        seen.append(url)
        return ticketmaster_venue.discovery_url(["KovZB"])

    monkeypatch.setattr(ticketmaster_venue, "resolve_venue_url", resolve)
    url = "https://www.ticketmaster.com/synthetic-hall-tickets-x/venue/123456"
    res = discovery.detect(url, config=Config(ticketmaster_api_key="k"))
    assert res.success and res.scraper_type == "ticketmaster_venue"
    assert res.detected_url.endswith("venueId=KovZB")
    assert seen == [url] and no_network == []


def test_an_unresolved_venue_page_says_what_to_paste(monkeypatch, no_network):
    def resolve(url, key):
        raise ticketmaster_venue.VenueNotFound("paste the Discovery venueId URL")

    monkeypatch.setattr(ticketmaster_venue, "resolve_venue_url", resolve)
    res = discovery.detect(
        "https://www.ticketmaster.com/synthetic-hall/venue/1", config=Config(ticketmaster_api_key="k")
    )
    assert not res.success and "venueId" in " ".join(res.notes)


# --- the admin form ----------------------------------------------------------------------------


@pytest.fixture
def admin(monkeypatch, tmp_path):
    from app.db import init_db

    init_db(tmp_path / "test.db")
    c = Config()
    c.secret_key = "k" * 64
    c.auth_mode = "oidc"
    c.oidc_discovery_url = "https://idp.example/.well-known/openid-configuration"
    c.oidc_client_id = "happenhere"
    c.oidc_client_secret = "s3cret"
    c.public_url = "https://events.example"
    c.ticketmaster_api_key = "k"
    monkeypatch.setattr(config, "_cfg", c)
    monkeypatch.setattr(config, "load_config", lambda: c)
    import app.main as main
    import app.web.routes as routes

    monkeypatch.setattr(main, "load_config", lambda: c)
    monkeypatch.setattr(routes, "check_source_url", lambda url: None)
    token = auth.create_session("bitmap", c.secret_key, "oidc:happenhere", expiry_secs=60)
    http = TestClient(app, base_url="https://testserver", follow_redirects=False)
    http.cookies.set(oidc.SESSION_COOKIE, token)
    return c, nullcontext(http)


def test_the_add_source_form_offers_ticketmaster_venue(admin):
    _, client = admin
    with client as http:
        r = http.get("/admin/sources/new")
    assert r.status_code == 200, r.text[:300]
    assert 'value="ticketmaster_venue"' in r.text and "Ticketmaster venue" in r.text
    assert "ticketmaster_venue:" in r.text  # its URL hint


def _form(url: str) -> dict:
    return {"display_name": "Synthetic Hall", "scraper_type": "ticketmaster_venue", "url": url}


def test_saving_a_venue_page_stores_the_resolved_discovery_url(admin, monkeypatch):
    from app.db import get_session
    from app.models import Source

    monkeypatch.setattr(
        ticketmaster_venue, "resolve_venue_url", lambda url, key: "https://app.ticketmaster.com/discovery/v2/events?venueId=KovZB"
    )
    _, client = admin
    with client as http:
        r = http.post("/admin/sources", data=_form("https://www.ticketmaster.com/x/venue/1"))
    assert r.status_code == 302, r.text
    with get_session() as s:
        src = s.query(Source).filter_by(name="synthetic_hall").one()
    assert src.url.endswith("venueId=KovZB")


def test_saving_an_unresolved_venue_page_is_refused_with_the_message(admin, monkeypatch):
    def resolve(url, key):
        raise ticketmaster_venue.VenueNotFound("No venue matched: paste the Discovery venueId URL")

    monkeypatch.setattr(ticketmaster_venue, "resolve_venue_url", resolve)
    _, client = admin
    with client as http:
        r = http.post("/admin/sources", data=_form("https://www.ticketmaster.com/x/venue/1"))
        u = http.post(
            "/admin/sources/synthetic_hall/update", data=_form("https://www.ticketmaster.com/x/venue/1")
        )
    assert r.status_code == 400 and "venueId" in r.text
    assert u.status_code == 400 and "venueId" in u.text
