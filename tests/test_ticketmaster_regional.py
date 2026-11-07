"""Item 22: the regional ``ticketmaster`` source carries its own location and pages past the cap.

Every response comes from a fake Discovery API behind ``httpx.MockTransport``: no test reaches
Ticketmaster. Coordinates are made up (no real place), ids and names invented.
"""

from __future__ import annotations

import io
import logging
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, jobs, main, oidc
from app.config import Config
from app.models import Event, Source
from app.scrapers import ticketmaster
from app.scrapers.registry import get_scraper
from app.sources import ImportRefused, run_import

KEY = "SyntheticKey0123456789abcdef"  # gitleaks:allow (synthetic test value)
API = "https://app.ticketmaster.com/discovery/v2/events"
URL_A = f"{API}?latlong=12.5,-45.25&radius=25&unit=miles"
URL_B = f"{API}?latlong=-33.75,101.5&radius=10&unit=miles"
T0 = datetime(2031, 1, 1, tzinfo=UTC)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class FakeDiscovery:
    """A Discovery ``events.json`` over a list of (id, UTC start) events, per ``latlong``.

    Like the API it filters by ``startDateTime``/``endDateTime`` (both inclusive here, so a
    boundary event is returned twice and only de-duplication saves it), pages by ``size`` and
    ``page``, refuses a page at or past the 1,000 cap, and reports ``page.totalElements``.
    """

    def __init__(self, events_by_latlong: dict[str, list[tuple[str, datetime]]]):
        self.events = events_by_latlong
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.requests.append(q)
        assert q.get("apikey") == KEY
        assert "postalCode" not in q
        size, page = int(q["size"]), int(q["page"])
        if size * page >= 1000:
            return httpx.Response(400, json={"errors": [{"detail": "deep paging"}]})
        lo, hi = _parse(q["startDateTime"]), _parse(q["endDateTime"])
        hits = sorted(
            (e for e in self.events.get(q["latlong"], []) if lo <= e[1] <= hi),
            key=lambda e: (e[1], e[0]),
        )
        chunk = hits[page * size : (page + 1) * size]
        body: dict = {
            "page": {
                "size": size,
                "number": page,
                "totalElements": len(hits),
                "totalPages": (len(hits) + size - 1) // size,
            }
        }
        if chunk:
            body["_embedded"] = {
                "events": [
                    {
                        "id": eid,
                        "name": f"Synthetic Show {eid}",
                        "url": f"https://www.example.com/event/{eid}",
                        "dates": {"start": {"dateTime": _iso(start)}},
                        "_embedded": {"venues": [{"name": f"Hall {q['latlong']}"}]},
                    }
                    for eid, start in chunk
                ]
            }
        return httpx.Response(200, json=body)


def _spread(prefix: str, n: int, start: datetime, span: timedelta) -> list[tuple[str, datetime]]:
    step = span / n
    return [(f"{prefix}{i:05d}", start + step * i) for i in range(n)]


@pytest.fixture
def fake(monkeypatch):
    """Install a fake API (set ``.events`` per test); pacing is recorded, never slept."""
    api = FakeDiscovery({})
    real_client = httpx.Client

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(api)
        return real_client(*args, **kw)

    monkeypatch.setattr(ticketmaster.httpx, "Client", client)
    clock = {"t": 0.0}
    api.sleeps = []

    def sleep(s):
        api.sleeps.append(s)
        clock["t"] += s

    monkeypatch.setattr(ticketmaster, "_sleep", sleep)
    monkeypatch.setattr(ticketmaster, "_clock", lambda: clock["t"])
    monkeypatch.setattr(ticketmaster, "_now", lambda: T0)
    return api


def _cfg(**kw) -> Config:
    return Config(ticketmaster_api_key=KEY, feed_lookahead_days=365, tz="UTC", **kw)


def _scraper(url: str = URL_A, cfg: Config | None = None):
    return ticketmaster.TicketmasterScraper(cfg or _cfg(), source_url=url, venue_name="Regional")


# --- the location lives in the source URL --------------------------------------------------


def test_the_location_is_read_from_the_url():
    assert ticketmaster.regional_location(URL_A) == {
        "latlong": "12.5,-45.25",
        "radius": "25",
        "unit": "miles",
    }
    assert ticketmaster.location_problem(URL_A) is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        API,
        f"{API}?postalCode=00000&radius=25&unit=miles",
        f"{API}?latlong=12.5,-45.25&unit=miles",  # no radius
        f"{API}?radius=25&unit=miles",  # no latlong
        f"{API}?latlong=north,west&radius=25",
        f"{API}?latlong=95,0&radius=25",  # latitude out of range
        f"{API}?latlong=12.5,-45.25&radius=0",
        f"{API}?latlong=12.5,-45.25&radius=25&unit=furlongs",
        "https://www.example.com/discovery/v2/events?latlong=12.5,-45.25&radius=25",
        f"{API}?latlong=12.5,-45.25&radius=25&apikey={KEY}",  # never the key
    ],
)
def test_a_url_without_a_usable_location_is_a_problem(url):
    assert ticketmaster.regional_location(url) is None
    msg = ticketmaster.location_problem(url)
    assert msg and "latlong" in msg and "radius" in msg
    assert KEY not in msg


def test_the_scraper_uses_the_url_location_never_the_global_zip(fake):
    fake.events = {"12.5,-45.25": _spread("a", 3, T0 + timedelta(days=2), timedelta(days=5))}
    events = _scraper(cfg=_cfg(city_zip="00000", city_radius_miles=75)).scrape()
    assert len(events) == 3
    assert {r["latlong"] for r in fake.requests} == {"12.5,-45.25"}
    assert {r["radius"] for r in fake.requests} == {"25"}
    assert {r["unit"] for r in fake.requests} == {"miles"}


def test_an_enabled_source_without_a_location_refuses_to_scrape(fake):
    with pytest.raises(ValueError, match="latlong"):
        _scraper(url=API).scrape()
    assert fake.requests == []


def test_a_second_source_with_another_location_does_not_change_the_first(fake):
    fake.events = {
        "12.5,-45.25": _spread("a", 40, T0 + timedelta(days=1), timedelta(days=200)),
        "-33.75,101.5": _spread("b", 7, T0 + timedelta(days=1), timedelta(days=200)),
    }
    alone = {e.source_uid for e in _scraper(URL_A).scrape()}
    _scraper(URL_B).scrape()
    again = {e.source_uid for e in _scraper(URL_A).scrape()}
    b = {e.source_uid for e in _scraper(URL_B).scrape()}
    assert len(alone) == 40 and again == alone
    assert len(b) == 7 and not (b & alone)


def test_get_scraper_passes_the_source_url():
    src = Source(name="tm", display_name="Regional", type="ticketmaster", url=URL_A)
    s = get_scraper(src, _cfg())
    assert isinstance(s, ticketmaster.TicketmasterScraper) and s.source_url == URL_A


# --- date windows ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [1, 29, 30, 31, 365, 366])
def test_the_window_split_covers_the_range_exactly_once(days):
    start = T0 + timedelta(hours=7, seconds=13)
    end = start + timedelta(days=days)
    windows = ticketmaster.split_windows(start, end, timedelta(days=ticketmaster.WINDOW_DAYS))
    assert windows[0][0] == start and windows[-1][1] == end
    for (_, a_end), (b_start, _) in zip(windows, windows[1:], strict=False):
        assert a_end == b_start, "a gap or an overlap between two windows"
    assert all(a < b for a, b in windows)
    assert all(b - a <= timedelta(days=ticketmaster.WINDOW_DAYS) for a, b in windows)


def test_halving_covers_the_window_exactly_once():
    start, end = T0, T0 + timedelta(days=3, seconds=1)
    (a, m1), (m2, b) = ticketmaster.halve(start, end)
    assert a == start and b == end and m1 == m2 and start < m1 < end
    assert m1.microsecond == 0  # the API takes whole seconds


def test_an_event_on_a_window_boundary_is_kept_once(fake):
    edge = T0 + timedelta(days=ticketmaster.WINDOW_DAYS)  # the first boundary
    fake.events = {"12.5,-45.25": [("edge", edge), ("before", edge - timedelta(seconds=1))]}
    events = _scraper().scrape()
    assert sorted(e.source_uid for e in events) == ["before", "edge"]


def test_every_requested_window_together_covers_the_look_ahead(fake):
    _scraper().scrape()
    spans = sorted(
        {(_parse(r["startDateTime"]), _parse(r["endDateTime"])) for r in fake.requests}
    )
    assert spans[0][0] == T0 and spans[-1][1] == T0 + timedelta(days=365)
    for (_, a_end), (b_start, _) in zip(spans, spans[1:], strict=False):
        assert a_end == b_start


def test_a_window_over_the_cap_is_halved_and_nothing_is_lost(fake):
    # 2,500 events inside the first 30-day window: it must be split until each piece is under
    # 1,000, and every event read once.
    fake.events = {"12.5,-45.25": _spread("a", 2500, T0, timedelta(days=29))}
    s = _scraper()
    events = s.scrape()
    ids = [e.source_uid for e in events]
    assert len(ids) == 2500 and len(set(ids)) == 2500
    assert not s.warnings and not s.incomplete
    totals = [w["total"] for w in s.windows]
    assert max(totals) > 1000, "the over-cap window was never seen"
    assert any(w["action"] == "halved" for w in s.windows)
    assert all(w["total"] <= 1000 for w in s.windows if w["action"] == "read")


def test_a_one_day_window_still_over_the_cap_keeps_its_first_1000_and_warns(fake, caplog):
    day = T0 + timedelta(days=3)
    fake.events = {"12.5,-45.25": _spread("a", 1300, day, timedelta(hours=20))}
    s = _scraper()
    with caplog.at_level(logging.WARNING, logger="app.scrapers.ticketmaster"):
        events = s.scrape()
    assert len(events) == 1000
    assert s.incomplete
    assert s.warnings and "1,000" in s.warnings[0]
    assert "1,000" in caplog.text
    capped = [w for w in s.windows if w["action"] == "capped"]
    assert len(capped) == 1 and capped[0]["end"] - capped[0]["start"] <= timedelta(days=1)


# --- pacing and the request ceiling ---------------------------------------------------------


def test_requests_are_at_most_one_per_second(fake):
    fake.events = {"12.5,-45.25": _spread("a", 900, T0, timedelta(days=300))}
    s = _scraper()
    s.scrape()
    assert s.requests == len(fake.requests) > 10
    times = s.request_times
    assert all(b - a >= ticketmaster.MIN_REQUEST_INTERVAL for a, b in zip(times, times[1:]))


def test_the_request_ceiling_stops_the_scrape_and_says_so(fake, monkeypatch, caplog):
    monkeypatch.setattr(ticketmaster, "MAX_REQUESTS_PER_SCRAPE", 5)
    fake.events = {"12.5,-45.25": _spread("a", 50, T0, timedelta(days=360))}
    s = _scraper()
    with caplog.at_level(logging.WARNING, logger="app.scrapers.ticketmaster"):
        events = s.scrape()
    assert len(fake.requests) == 5
    assert s.incomplete and any("ceiling" in w for w in s.warnings)
    assert "ceiling" in caplog.text
    assert 0 < len(events) < 50


# --- the warning reaches the source, and a partial scrape retires nothing --------------------


@pytest.fixture
def stored(monkeypatch, tmp_path):
    db.init_db(tmp_path / "tm.db")
    cfg = _cfg()
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr("app.jobs.get_config", lambda: cfg)
    monkeypatch.setattr(jobs.canary, "check", lambda: None)
    monkeypatch.setattr(jobs, "_backfill_pexels_images", lambda: None)
    with db.get_session() as s:
        s.add(Source(name="tm", display_name="Regional", type="ticketmaster", url=URL_A))
        s.commit()


def _source() -> Source:
    with db.get_session() as s:
        row = s.query(Source).filter_by(name="tm").one()
        s.expunge(row)
        return row


def test_a_capped_scrape_records_the_warning_and_retires_nothing(fake, stored, monkeypatch):
    # The fake's events on the real clock, so the stored rows are upcoming: 5 in the first
    # window, 20 later ones that a one-request scrape never reaches.
    base = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(ticketmaster, "_now", lambda: base)
    fake.events = {
        "12.5,-45.25": _spread("a", 5, base + timedelta(days=2), timedelta(days=5))
        + _spread("b", 20, base + timedelta(days=40), timedelta(days=20))
    }
    jobs.run_all_scrapers()
    assert _source().last_error is None
    monkeypatch.setattr(ticketmaster, "MAX_REQUESTS_PER_SCRAPE", 1)
    jobs.run_all_scrapers()  # only the first window: the 20 later events are not read
    src = _source()
    assert src.last_error and "ceiling" in src.last_error
    assert (src.error_count or 0) == 0
    with db.get_session() as s:
        assert s.query(Event).filter(Event.retired_at.isnot(None)).count() == 0
    monkeypatch.setattr(ticketmaster, "MAX_REQUESTS_PER_SCRAPE", 100)
    jobs.run_all_scrapers()
    assert _source().last_error is None


def test_no_apikey_in_the_captured_logs(fake, stored):
    root = logging.getLogger()
    saved = root.handlers[:], root.level, logging.getLogRecordFactory()
    root.handlers = []
    buf = io.StringIO()
    try:
        main.configure_logging(Config(log_level="DEBUG"))
        for name in ("httpx", "httpcore"):  # louder than the app sets them: the worst case
            logging.getLogger(name).setLevel(logging.DEBUG)
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
        fake.events = {"12.5,-45.25": _spread("a", 5, T0, timedelta(days=60))}
        _scraper().scrape()
    finally:
        root.handlers, lvl, factory = saved
        root.setLevel(lvl)
        logging.setLogRecordFactory(factory)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    out = buf.getvalue()
    assert "latlong" in out, "the request lines were not logged: the test proves nothing"
    assert "apikey=" not in out.replace("apikey=REDACTED", "")
    assert KEY not in out


# --- import and the admin form ---------------------------------------------------------------


def _doc(url: str, enabled: bool) -> str:
    return (
        "sources:\n"
        f"  - name: tm-a\n    type: ticketmaster\n    url: {url!r}\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
    )


@pytest.fixture
def session(tmp_path):
    db.init_db(tmp_path / "imp.db")
    with db.get_session() as s:
        yield s


def test_import_refuses_an_enabled_regional_entry_without_a_location(session):
    with pytest.raises(ImportRefused) as exc:
        run_import(session, _doc(API, True), apply=True)
    assert "latlong" in str(exc.value) and "radius" in str(exc.value)
    assert session.query(Source).count() == 0


def test_import_stores_a_disabled_regional_entry_without_a_location_with_a_warning(session):
    plan = run_import(session, _doc("", False), apply=True)
    assert "tm-a" in plan.warnings and "latlong" in plan.warnings["tm-a"]
    row = session.query(Source).filter_by(name="tm-a").one()
    assert row.enabled is False and "latlong" in (row.last_error or "")
    # Given a location later, the import clears its own record.
    plan = run_import(session, _doc(URL_A, True), apply=True)
    session.expire_all()
    row = session.query(Source).filter_by(name="tm-a").one()
    assert row.enabled is True and row.last_error is None and not plan.warnings


def test_import_takes_an_enabled_regional_entry_with_a_location(session):
    plan = run_import(session, _doc(URL_A, True), apply=True)
    assert not plan.warnings and len(plan.added) == 1


@pytest.fixture
def client(monkeypatch, tmp_path):
    db.init_db(tmp_path / "app.db")
    cfg = Config()
    cfg.secret_key = "k" * 64
    cfg.password_hash = auth.hash_password("pw")
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    import app.web.routes as routes

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(routes, "check_source_url", lambda url: None)
    c = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    c.cookies.set(
        oidc.SESSION_COOKIE, auth.create_session("admin", cfg.secret_key, cfg.password_hash)
    )
    return nullcontext(c)


def _form(url: str, enabled: str = "on") -> dict:
    return {"display_name": "Regional A", "scraper_type": "ticketmaster", "url": url, "enabled": enabled}


def test_the_form_refuses_an_enabled_regional_source_without_a_location(client):
    with client as http:
        r = http.post("/admin/sources", data=_form(API))
    assert r.status_code == 400 and "latlong" in r.text and "radius" in r.text
    with db.get_session() as s:
        assert s.query(Source).filter_by(type="ticketmaster").count() == 0


def test_the_form_stores_a_disabled_one_and_the_toggle_refuses_to_enable_it(client):
    with client as http:
        r = http.post("/admin/sources", data=_form(API, enabled="off"))
        assert r.status_code == 302
        with db.get_session() as s:
            row = s.query(Source).filter_by(display_name="Regional A").one()
            name, err = row.name, row.last_error
        assert "latlong" in (err or "")
        r = http.post(f"/admin/sources/{name}/toggle")
        assert r.status_code == 400 and "latlong" in r.text
        r = http.post(f"/admin/sources/{name}/update", data=_form(URL_A))
        assert r.status_code == 302
    with db.get_session() as s:
        row = s.query(Source).filter_by(name=name).one()
        assert row.enabled and row.url == URL_A and row.last_error is None


def test_the_form_shows_the_regional_url_form(client):
    with client as http:
        r = http.get("/admin/sources/new")
    assert r.status_code == 200, r.text[:300]
    assert "latlong=&lt;lat&gt;,&lt;lon&gt;" in r.text or "latlong=<lat>,<lon>" in r.text
    assert "radius=" in r.text
    assert "uses your city ZIP" not in r.text
