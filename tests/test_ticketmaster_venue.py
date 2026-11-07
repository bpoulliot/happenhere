"""Item 12: the built-in ``ticketmaster_venue`` source type, its pager and its URL resolution.

Every response is a saved synthetic fixture served through ``httpx.MockTransport``: no test
reaches Ticketmaster. Ids, names and addresses are invented.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import main
from app.config import Config
from app.models import Source
from app.scrapers import ticketmaster, ticketmaster_venue
from app.scrapers.registry import TYPE_LABELS, get_scraper, scraper_types
from app.scrapers.ticketmaster import page_discovery_events
from app.scrapers.ticketmaster_venue import (
    VenueNotFound,
    discovery_url,
    resolve_venue_url,
    venue_ids,
    venue_page_id,
)

KEY = "SyntheticKey0123456789abcdef"  # gitleaks:allow (synthetic test value)


def _event(n: int, **kw) -> dict:
    ev = {
        "id": f"vvG1SyntheticEv{n:03d}",
        "name": f"Synthetic Band {n}",
        "url": f"https://www.example.com/synthetic-band-{n}/event/{n:04d}",
        "dates": {
            "start": {
                "localDate": "2031-03-0" + str(1 + n % 9),
                "localTime": "19:30:00",
                "dateTime": f"2031-03-0{1 + n % 9}T02:30:00Z",
                "noSpecificTime": False,
            }
        },
        "images": [
            {"ratio": "3_2", "url": f"https://img.example.com/{n}-3_2.jpg"},
            {"ratio": "16_9", "url": f"https://img.example.com/{n}-16_9.jpg"},
        ],
        "classifications": [
            {"segment": {"name": "Music"}, "genre": {"name": "Rock"}},
        ],
        "_embedded": {
            "venues": [
                {
                    "name": "Synthetic Hall",
                    "address": {"line1": "1 Example Street"},
                    "city": {"name": "Exampleton"},
                    "state": {"stateCode": "EX"},
                }
            ]
        },
    }
    ev.update(kw)
    return ev


def _events_page(events: list[dict], number: int, total_pages: int) -> dict:
    out: dict = {"page": {"size": 200, "number": number, "totalPages": total_pages}}
    if events:
        out["_embedded"] = {"events": events}
    return out


# Two pages; the second carries an event with no specific time.
PAGE0 = _events_page([_event(1), _event(2)], 0, 2)
PAGE1 = _events_page(
    [
        _event(
            3,
            dates={
                "start": {
                    "localDate": "2031-04-05",
                    "dateTime": "2031-04-05T06:00:00Z",
                    "noSpecificTime": True,
                }
            },
            images=[{"ratio": "4_3", "url": "https://img.example.com/3-4_3.jpg"}],
            classifications=[{"segment": {"name": "Arts"}, "genre": {"name": "Undefined"}}],
        )
    ],
    1,
    2,
)

VENUES = {
    "_embedded": {
        "venues": [
            {"id": "KovZSyntheticA", "name": "Synthetic Hall Annex"},
            {
                "id": "KovZSyntheticB",
                "name": "Synthetic Hall",
                "url": "https://www.ticketmaster.com/synthetic-hall-tickets-exampleton/venue/123456",
            },
            {
                "id": "KovZSyntheticC",
                "name": "Synthetic Hall",
                "url": "https://www.ticketmaster.com/synthetic-hall-tickets-exampleton/venue/9123456",
            },
        ]
    },
    "page": {"size": 20, "number": 0, "totalPages": 1},
}


class Recorder:
    """A mock transport that records every request and answers from a function."""

    def __init__(self, answer):
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.answer(request)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def params(self, i: int) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(str(self.requests[i].url)).query).items()}


def _paged(pages: dict[int, dict]):
    def answer(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(request.url.query.decode()).get("page", ["0"])[0])
        return httpx.Response(200, json=pages[page])

    return answer


@pytest.fixture
def patch_client(monkeypatch):
    """Route the type's own httpx.Client through a recorder."""

    def install(rec: Recorder):
        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            rec.requests.append(request)
            return rec.answer(request)

        def client(*args, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kw)

        monkeypatch.setattr(ticketmaster_venue.httpx, "Client", client)
        return rec

    return install


def _scraper(url="https://app.ticketmaster.com/discovery/v2/events?venueId=KovZSyntheticB"):
    cfg = Config(ticketmaster_api_key=KEY, tz="America/New_York")
    src = Source(name="hall", display_name="Synthetic Hall", type="ticketmaster_venue", url=url)
    return get_scraper(src, cfg)


# --- the pager ----------------------------------------------------------------------------------


def test_pager_stops_at_the_last_page():
    rec = Recorder(_paged({0: PAGE0, 1: PAGE1}))
    with rec.client() as c:
        got = page_discovery_events(c, KEY, {"venueId": "X"})
    assert [e["id"] for e in got] == [
        "vvG1SyntheticEv001",
        "vvG1SyntheticEv002",
        "vvG1SyntheticEv003",
    ]
    assert [rec.params(i)["page"] for i in range(len(rec.requests))] == ["0", "1"]
    p = rec.params(0)
    assert p["apikey"] == KEY and p["size"] == "200" and p["sort"] == "date,asc"
    assert p["venueId"] == "X" and p["startDateTime"].endswith("Z")
    assert rec.requests[0].url.path == "/discovery/v2/events.json"


def test_pager_stops_at_1000_items():
    full = {n: _events_page([_event(n * 200 + i) for i in range(200)], n, 12) for n in range(12)}
    rec = Recorder(_paged(full))
    with rec.client() as c:
        got = page_discovery_events(c, KEY, {"venueId": "X"})
    assert len(got) == 1000
    assert len(rec.requests) == 5  # pages 0..4: size * page stays under 1000
    assert max(int(rec.params(i)["page"]) * 200 for i in range(5)) < 1000


def test_pager_stops_on_an_empty_page():
    rec = Recorder(_paged({0: _events_page([], 0, 3)}))
    with rec.client() as c:
        assert page_discovery_events(c, KEY, {"venueId": "X"}) == []
    assert len(rec.requests) == 1


# --- the scraper and its field mapping ---------------------------------------------------------


def test_scrape_maps_every_field(patch_client):
    rec = patch_client(Recorder(_paged({0: PAGE0, 1: PAGE1})))
    events = _scraper().scrape()
    assert len(events) == 3 and len(rec.requests) == 2
    assert rec.params(0)["venueId"] == "KovZSyntheticB"

    e1 = events[0]
    assert e1.title == "Synthetic Band 1"
    # 02:30 UTC is 21:30 the previous evening in the configured zone (America/New_York, EST).
    assert e1.start_dt == datetime(2031, 3, 1, 21, 30)
    assert e1.venue_name == "Synthetic Hall"
    assert e1.venue_addr == "1 Example Street, Exampleton, EX"
    assert e1.url == "https://www.example.com/synthetic-band-1/event/0001"
    assert e1.image_url == "https://img.example.com/1-16_9.jpg"
    assert e1.categories == ["Music", "Rock"]
    assert e1.raw_data["id"] == "vvG1SyntheticEv001"
    assert e1.source_uid == "ticketmaster:vvG1SyntheticEv001"

    e3 = events[2]
    assert e3.start_dt == datetime(2031, 4, 5)  # noSpecificTime: the local date alone
    assert e3.image_url == "https://img.example.com/3-4_3.jpg"  # no 16_9: the first
    assert e3.categories == ["Arts"]  # "Undefined" skipped
    assert e3.source_uid == "ticketmaster:vvG1SyntheticEv003"


def test_the_uid_does_not_change_with_the_title(patch_client):
    patch_client(Recorder(lambda r: httpx.Response(200, json=_events_page([_event(1)], 0, 1))))
    a = _scraper().scrape()[0]
    patch_client(
        Recorder(
            lambda r: httpx.Response(
                200, json=_events_page([_event(1, name="Synthetic Band 1 - SOLD OUT")], 0, 1)
            )
        )
    )
    b = _scraper().scrape()[0]
    assert a.source_uid == b.source_uid == "ticketmaster:vvG1SyntheticEv001"


def test_venue_name_falls_back_to_the_display_name(patch_client):
    ev = _event(1)
    ev["_embedded"] = {"venues": []}
    patch_client(Recorder(lambda r: httpx.Response(200, json=_events_page([ev], 0, 1))))
    assert _scraper().scrape()[0].venue_name == "Synthetic Hall"


def test_several_venue_ids(patch_client):
    rec = patch_client(Recorder(lambda r: httpx.Response(200, json=_events_page([], 0, 1))))
    _scraper("https://app.ticketmaster.com/discovery/v2/events.json?venueId=KovZA1,Z7rB2").scrape()
    assert rec.params(0)["venueId"] == "KovZA1,Z7rB2"


def test_missing_key_is_an_error():
    src = Source(name="h", display_name="H", type="ticketmaster_venue", url=discovery_url(["A"]))
    with pytest.raises(ValueError, match="TICKETMASTER_API_KEY"):
        get_scraper(src, Config(ticketmaster_api_key="")).scrape()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://app.ticketmaster.com/discovery/v2/events",
        "https://app.ticketmaster.com/discovery/v2/events?venueId=bad%20id",
        "https://www.ticketmaster.com/synthetic-hall-tickets-exampleton/venue/123456",
    ],
)
def test_a_url_without_venue_ids_is_refused_without_a_request(patch_client, url):
    rec = patch_client(Recorder(lambda r: httpx.Response(500)))
    with pytest.raises(ValueError, match="venueId"):
        _scraper(url).scrape()
    assert rec.requests == []


def test_no_key_in_the_log(patch_client):
    """Item 21's redaction covers this type's request lines."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level, logging.getLogRecordFactory()
    root.handlers = []
    buf = io.StringIO()
    try:
        main.configure_logging(Config(log_level="DEBUG"))
        h = logging.StreamHandler(buf)
        root.addHandler(h)
        logging.getLogger("httpx").setLevel(logging.DEBUG)  # force the request lines out
        rec = patch_client(Recorder(lambda r: httpx.Response(401, json={"fault": "bad key"})))
        with pytest.raises(httpx.HTTPStatusError) as exc:
            _scraper().scrape()
        logging.getLogger("x").error("scrape failed", exc_info=exc.value)
    finally:
        root.handlers, lvl, factory = saved
        root.setLevel(lvl)
        logging.setLogRecordFactory(factory)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    out = buf.getvalue()
    assert rec.requests and "HTTP Request" in out, "the request line was not logged as staged"
    assert KEY not in out
    assert "apikey=" + KEY[:4] not in out
    assert "apikey=REDACTED" in out


# --- URLs: recognition and resolution ----------------------------------------------------------


@pytest.mark.parametrize(
    "url, ids",
    [
        ("https://app.ticketmaster.com/discovery/v2/events?venueId=KovZA1", ["KovZA1"]),
        ("https://app.ticketmaster.com/discovery/v2/events.json?venueId=A1,B2", ["A1", "B2"]),
        ("https://app.ticketmaster.com/discovery/v2/events?venueId=A1&apikey=x", ["A1"]),
        ("https://www.ticketmaster.com/x/venue/123", None),
        ("https://app.ticketmaster.com/discovery/v2/events?keyword=x", None),
        ("https://app.example.com/discovery/v2/events?venueId=A1", None),
        ("https://evil.example/app.ticketmaster.com?venueId=A1", None),
    ],
)
def test_venue_ids(url, ids):
    assert venue_ids(url) == ids


@pytest.mark.parametrize(
    "url, n",
    [
        ("https://www.ticketmaster.com/synthetic-hall-tickets-exampleton/venue/123456", "123456"),
        ("https://ticketmaster.com/synthetic-hall/venue/42", "42"),
        ("https://www.ticketmaster.ca/synthetic-hall/venue/42", None),
        ("https://www.ticketmaster.com/synthetic-hall/venue/abc", None),
        ("https://www.example.com/synthetic-hall/venue/42", None),
        ("https://notticketmaster.com/synthetic-hall/venue/42", None),
    ],
)
def test_venue_page_id(url, n):
    assert venue_page_id(url) == n


def test_the_stored_url_never_carries_the_key():
    url = discovery_url(["A1", "B2"])
    assert url == "https://app.ticketmaster.com/discovery/v2/events?venueId=A1,B2"
    assert "apikey" not in resolve_venue_url(
        "https://app.ticketmaster.com/discovery/v2/events?venueId=A1&apikey=x", KEY
    )


def test_a_venue_page_resolves_through_venue_search(patch_client):
    rec = patch_client(Recorder(lambda r: httpx.Response(200, json=VENUES)))
    url = resolve_venue_url(
        "https://www.ticketmaster.com/synthetic-hall-tickets-exampleton/venue/123456", KEY
    )
    assert url == discovery_url(["KovZSyntheticB"])
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.url.host == "app.ticketmaster.com"
    assert req.url.path == "/discovery/v2/venues.json"
    assert rec.params(0)["keyword"] == "synthetic hall"
    assert rec.params(0)["apikey"] == KEY


def test_a_venue_page_with_no_match_tells_the_admin_what_to_paste(patch_client):
    patch_client(Recorder(lambda r: httpx.Response(200, json=VENUES)))
    with pytest.raises(VenueNotFound, match="venueId"):
        resolve_venue_url("https://www.ticketmaster.com/synthetic-hall-tickets-x/venue/777", KEY)


def test_a_venue_page_needs_the_key(patch_client):
    rec = patch_client(Recorder(lambda r: httpx.Response(200, json=VENUES)))
    with pytest.raises(VenueNotFound, match="TICKETMASTER_API_KEY"):
        resolve_venue_url("https://www.ticketmaster.com/synthetic-hall/venue/123456", "")
    assert rec.requests == []


def test_a_discovery_url_resolves_without_a_request(patch_client):
    rec = patch_client(Recorder(lambda r: httpx.Response(500)))
    url = "https://app.ticketmaster.com/discovery/v2/events.json?venueId=A1"
    assert resolve_venue_url(url, KEY) == discovery_url(["A1"])
    assert rec.requests == []


def test_the_type_is_registered_and_offered():
    assert "ticketmaster_venue" in dict(scraper_types())
    assert TYPE_LABELS["ticketmaster_venue"] == "Ticketmaster venue"


def test_the_regional_type_is_unchanged():
    """The shared parser keeps the bare id: the regional type's and plugins' identities hold."""
    ev = ticketmaster._parse_event(_event(1))
    assert ev is not None and ev.source_uid == "vvG1SyntheticEv001"
