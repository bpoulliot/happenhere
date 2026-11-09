"""Item 30: search locations.

The admin keeps a list of search locations (a US ZIP code, a US city and state, or a
latitude/longitude typed by hand; each with a label and a radius), resolved OFFLINE from the
Census Gazetteer. Location-aware source types (Ticketmaster regional, Eventbrite, Meetup) run once
per location assigned to them; per-venue types do not fan out. The old single-city settings and
a regional Ticketmaster URL's ``latlong``/``radius`` migrate to locations, and no event's identity
key or feed UID moves.

Every place, ZIP code and coordinate here is invented (the synthetic Gazetteer in
``tests/fixtures/gazetteer`` sits in the open ocean); no test reaches Ticketmaster or any
geocoding service.
"""

from __future__ import annotations

import socket
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import auth, config, db, gazetteer, jobs, locations, main, oidc
from app import sources as sio
from app.config import Config
from app.dedup import identity_key, upsert_events
from app.models import AppMeta, Event, EventLocation, SearchLocation, Source
from app.scrapers import eventbrite, meetup, registry, ticketmaster
from app.scrapers.base import BaseScraper, EventData, SearchArea

KEY = "SyntheticKey0123456789abcdef"  # gitleaks:allow (synthetic test value)
API = "https://app.ticketmaster.com/discovery/v2/events"
A_LL, B_LL = "10.0,-20.0", "11.0,-21.0"  # ZIP 00001 and 00003 in the fixture
BASE = datetime.now(UTC).replace(microsecond=0)


def _day(n: int) -> datetime:
    return BASE + timedelta(days=n)


# --- the offline lookup -----------------------------------------------------------------------


def test_a_zip_code_resolves_from_the_gazetteer():
    r = gazetteer.resolve("00001")
    assert (r.kind, r.lat, r.lon) == ("zip", 10.0, -20.0)
    assert (r.city, r.state) == ("Exampleton", "NY")  # the nearest place: Meetup/Eventbrite


def test_a_zip_plus_four_resolves_as_its_zip():
    assert gazetteer.resolve("00002-1234").lat == 10.5


@pytest.mark.parametrize("entry", ["Exampleton, NY", "  exampleton ,ny ", "EXAMPLETON, ny"])
def test_a_city_and_state_resolve(entry):
    r = gazetteer.resolve(entry)
    assert (r.kind, r.lat, r.lon, r.city, r.state) == ("city", 10.01, -20.01, "Exampleton", "NY")


def test_the_same_name_in_another_state_is_another_place():
    assert gazetteer.resolve("Exampleton, CA").lat == 13.0


def test_saint_and_the_short_names_of_a_place_resolve():
    assert gazetteer.resolve("Saint Example, NY").city == "St. Example"
    assert gazetteer.resolve("Twinford, CA").lat == 12.0  # "Twinford-Example"
    assert gazetteer.resolve("New Name, CA").lat == 12.5  # "Old Name (New Name)"
    assert gazetteer.resolve("Old Name, CA").lat == 12.5


def test_latitude_longitude_is_taken_as_typed():
    r = gazetteer.resolve(" 40.5 , -73.25 ")
    assert (r.kind, r.lat, r.lon) == ("latlong", 40.5, -73.25)
    assert (r.city, r.state) == ("", "")  # no bundled place within reach: outside the US
    assert gazetteer.resolve("10.0,-20.0").city == "Exampleton"


@pytest.mark.parametrize(
    "entry,code",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("99999", "zip_unknown"),
        ("Nowhere, NY", "city_unknown"),
        ("Exampleton", "state_unknown"),
        ("Exampleton, ZZ", "state_unknown"),
        ("91, 0", "latlong_range"),
        ("0, 181", "latlong_range"),
        ("12 Main St, Apt 4", "unparsed"),
    ],
)
def test_an_entry_that_does_not_resolve_is_refused_with_fixed_text(entry, code):
    with pytest.raises(gazetteer.LocationRefused) as exc:
        gazetteer.resolve(entry)
    assert exc.value.code == code
    assert str(exc.value) == gazetteer.MESSAGES[code]


def test_the_tests_read_the_synthetic_gazetteer_never_the_bundled_one():
    assert "fixtures" in str(gazetteer._dir())
    assert not gazetteer.DEFAULT_DIR.exists()


def test_the_bundled_files_and_their_source_record_ship_with_the_app():
    real = Path(gazetteer.__file__).parent / "geodata"
    assert (real / "zcta.tsv").stat().st_size > 100_000  # stat only: their contents stay unread
    assert (real / "places.tsv").stat().st_size > 100_000
    record = (real / "SOURCE.md").read_text()
    assert "www2.census.gov" in record and "Gazetteer" in record and "public domain" in record


@pytest.fixture
def no_network(monkeypatch):
    """Any connection or DNS lookup is recorded and fails the code that made it."""
    calls: list = []

    def refuse(*args, **kw):
        calls.append(args)
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    return calls


def test_the_no_network_probe_catches_a_request(no_network):
    with pytest.raises(BaseException):  # noqa: B017 (any failure: the point is the record)
        httpx.get("http://192.0.2.1/", timeout=1)
    assert no_network, "the probe saw no request: a null result below would prove nothing"


def test_no_lookup_leaves_the_box(no_network):
    for entry in ("00001", "00002-1234", "Exampleton, NY", "Saint Example, NY", "10.0,-20.0"):
        gazetteer.resolve(entry)
    for entry in ("99999", "Nowhere, NY"):
        with pytest.raises(gazetteer.LocationRefused):
            gazetteer.resolve(entry)
    assert no_network == []


# --- the location list ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_cfg", Config())
    db.init_db(tmp_path / "loc.db")
    with db.get_session() as s:
        yield s


def test_a_location_is_a_label_an_entry_and_a_radius(session):
    loc = locations.add(session, label="Downtown", entry="00001", radius=25, unit="miles")
    session.commit()
    row = session.get(SearchLocation, loc.id)
    assert (row.label, row.entry, row.kind, row.lat, row.lon) == (
        "Downtown",
        "00001",
        "zip",
        10.0,
        -20.0,
    )
    assert (row.radius, row.unit, row.city, row.state) == (25, "miles", "Exampleton", "NY")


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"label": "", "entry": "00001"}, "label"),
        ({"label": "X", "entry": "99999"}, "zip_unknown"),
        ({"label": "X", "entry": "00001", "radius": 0}, "radius"),
        ({"label": "X", "entry": "00001", "radius": 501}, "radius"),
        ({"label": "X", "entry": "00001", "unit": "furlongs"}, "unit"),
    ],
)
def test_a_bad_location_is_refused_with_fixed_text(session, kw, code):
    kw = {"radius": 25, "unit": "miles", **kw}
    with pytest.raises(locations.LocationInvalid) as exc:
        locations.add(session, **kw)
    assert exc.value.code == code and str(exc.value) == locations.MESSAGES[code]


def test_two_locations_cannot_share_a_label(session):
    locations.add(session, label="Downtown", entry="00001", radius=25, unit="miles")
    with pytest.raises(locations.LocationInvalid) as exc:
        locations.add(session, label="downtown", entry="00003", radius=25, unit="miles")
    assert exc.value.code == "label_taken"


def test_no_default_location_ships(session):
    assert session.query(SearchLocation).count() == 0


# --- fan-out -------------------------------------------------------------------------------------


class FakeDiscovery:
    """Ticketmaster Discovery ``events.json``: events per ``latlong``, one page, by window."""

    def __init__(self):
        self.events: dict[str, list[tuple[str, datetime]]] = {}
        self.fail: set[str] = set()
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.requests.append(q)
        assert q.get("apikey") == KEY
        if q["latlong"] in self.fail:
            return httpx.Response(500, json={"fault": "synthetic"})
        lo = datetime.strptime(q["startDateTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        hi = datetime.strptime(q["endDateTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        hits = [e for e in self.events.get(q["latlong"], []) if lo <= e[1] < hi]
        body: dict = {"page": {"totalElements": len(hits), "totalPages": 1}}
        if hits:
            body["_embedded"] = {
                "events": [
                    {
                        "id": eid,
                        "name": f"Show {eid}",
                        "url": f"https://www.example.com/event/{eid}",
                        "dates": {"start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ")}},
                        "_embedded": {"venues": [{"name": f"Hall {eid}"}]},
                    }
                    for eid, start in hits
                ]
            }
        return httpx.Response(200, json=body)


@pytest.fixture
def api(monkeypatch, tmp_path):
    cfg = Config(ticketmaster_api_key=KEY, feed_lookahead_days=30, tz="UTC")
    monkeypatch.setattr(config, "_cfg", cfg)
    db.init_db(tmp_path / "fan.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: cfg)
    monkeypatch.setattr(jobs.canary, "check", lambda: None)
    monkeypatch.setattr(jobs, "_backfill_pexels_images", lambda: None)
    fake = FakeDiscovery()
    real_client = httpx.Client

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kw)

    monkeypatch.setattr(ticketmaster.httpx, "Client", client)
    monkeypatch.setattr(ticketmaster, "_sleep", lambda s: None)
    monkeypatch.setattr(ticketmaster, "_now", lambda: BASE)
    return fake


def _loc(label: str, entry: str, radius: float = 25, unit: str = "miles") -> int:
    with db.get_session() as s:
        loc = locations.add(s, label=label, entry=entry, radius=radius, unit=unit)
        s.commit()
        return loc.id


def _tm(name: str, assigned: list[int] | None = None, url: str = API) -> None:
    with db.get_session() as s:
        src = Source(name=name, display_name=name, type="ticketmaster", url=url, enabled=True)
        locations.assign(src, assigned)
        s.add(src)
        s.commit()


def _source(name: str) -> Source:
    with db.get_session() as s:
        row = s.query(Source).filter_by(name=name).one()
        s.expunge(row)
        return row


def _links() -> dict[str, set[str]]:
    """Event title -> the labels of the locations it was found for."""
    out: dict[str, set[str]] = {}
    with db.get_session() as s:
        for ev in s.query(Event).all():
            out[ev.title] = set()
        rows = (
            s.query(Event.title, SearchLocation.label)
            .join(EventLocation, EventLocation.event_id == Event.id)
            .join(SearchLocation, SearchLocation.id == EventLocation.location_id)
            .all()
        )
        for title, label in rows:
            out[title].add(label)
    return out


def _keys() -> dict[str, tuple[str, str, int]]:
    with db.get_session() as s:
        return {e.title: (e.identity_key, e.uid, e.source_id) for e in s.query(Event).all()}


def test_two_locations_make_one_search_each_for_a_location_aware_source(api):
    _loc("Area A", "00001")
    _loc("Area B", "00003", radius=10, unit="km")
    _tm("tm")
    api.events = {A_LL: [("a1", _day(2)), ("a2", _day(3))], B_LL: [("b1", _day(4))]}
    jobs.run_all_scrapers()
    assert sorted((r["latlong"], r["radius"], r["unit"]) for r in api.requests) == [
        (A_LL, "25", "miles"),
        (B_LL, "10", "km"),
    ]
    assert _links() == {"Show a1": {"Area A"}, "Show a2": {"Area A"}, "Show b1": {"Area B"}}
    src = _source("tm")
    assert src.last_error is None and (src.error_count or 0) == 0


def test_an_event_both_locations_return_is_one_row_with_one_uid(api):
    _loc("Area A", "00001")
    _loc("Area B", "00003")
    _tm("tm")
    api.events = {A_LL: [("x1", _day(2))], B_LL: [("x1", _day(2)), ("b1", _day(5))]}
    jobs.run_all_scrapers()
    assert _links() == {"Show x1": {"Area A", "Area B"}, "Show b1": {"Area B"}}
    with db.get_session() as s:
        assert s.query(Event).filter_by(title="Show x1").count() == 1


def test_a_source_searches_only_the_locations_assigned_to_it(api):
    _loc("Area A", "00001")
    b = _loc("Area B", "00003")
    _tm("tm", [b])
    api.events = {A_LL: [("a1", _day(2))], B_LL: [("b1", _day(2))]}
    jobs.run_all_scrapers()
    assert {r["latlong"] for r in api.requests} == {B_LL}
    assert set(_links()) == {"Show b1"}


def test_two_regional_sources_each_search_their_own_area_once(api):
    a = _loc("Area A", "00001")
    b = _loc("Area B", "00003")
    _tm("tm_a", [a])
    _tm("tm_b", [b])
    api.events = {A_LL: [("a1", _day(2))], B_LL: [("b1", _day(2))]}
    jobs.run_all_scrapers()
    assert sorted(r["latlong"] for r in api.requests) == [A_LL, B_LL]
    keys = _keys()
    assert keys["Show a1"][2] == _source("tm_a").id
    assert keys["Show b1"][2] == _source("tm_b").id


def test_an_empty_location_list_scrapes_nothing_and_records_why(api):
    _tm("tm")
    jobs.run_all_scrapers()
    assert api.requests == []
    src = _source("tm")
    assert src.last_error == locations.NO_LOCATIONS and src.error_count == 1


def test_a_source_assigned_no_location_scrapes_nothing_and_records_why(api):
    _loc("Area A", "00001")
    _tm("tm", [])
    jobs.run_all_scrapers()
    assert api.requests == []
    assert _source("tm").last_error == locations.NONE_ASSIGNED


CALLS: list[tuple[str, object]] = []


class _Aware(BaseScraper):
    SOURCE_TYPE = "api"
    LOCATION_AWARE = True

    def __init__(self, config, source_url="", venue_name="", location=None):
        super().__init__(config)
        self.location = location

    def scrape(self):
        CALLS.append(("aware", self.location))
        return []


class _PerVenue(BaseScraper):
    SOURCE_TYPE = "api"

    def __init__(self, config, source_url="", venue_name=""):
        super().__init__(config)

    def scrape(self):
        CALLS.append(("venue", None))
        return []


@pytest.fixture
def fakes(api):
    registry.register_type("fake30_aware", _Aware, "Aware")
    registry.register_type("fake30_venue", _PerVenue, "Per venue")
    CALLS.clear()
    yield
    registry.unregister_type("fake30_aware")
    registry.unregister_type("fake30_venue")


def test_only_location_aware_types_fan_out(fakes):
    _loc("Area A", "00001")
    _loc("Area B", "00003")
    with db.get_session() as s:
        s.add(Source(name="v", display_name="v", type="fake30_venue", url="", enabled=True))
        s.add(Source(name="w", display_name="w", type="fake30_aware", url="", enabled=True))
        s.commit()
    jobs.run_all_scrapers()
    assert [c for c in CALLS if c[0] == "venue"] == [("venue", None)]
    areas = [c[1] for c in CALLS if c[0] == "aware"]
    assert all(isinstance(a, SearchArea) for a in areas)
    assert sorted(a.label for a in areas) == ["Area A", "Area B"]
    assert registry.is_location_aware("ticketmaster")
    assert registry.is_location_aware("eventbrite") and registry.is_location_aware("meetup")
    assert not registry.is_location_aware("ticketmaster_venue")
    assert not registry.is_location_aware("jsonld")


def test_one_failing_location_keeps_the_others_events_and_retires_nothing(api):
    _loc("Area A", "00001")
    _loc("Area B", "00003")
    _tm("tm")
    api.events = {A_LL: [("a1", _day(2))], B_LL: [("b1", _day(2))]}
    jobs.run_all_scrapers()
    api.fail = {B_LL}
    api.events = {A_LL: [("a1", _day(2)), ("a2", _day(6))], B_LL: []}
    jobs.run_all_scrapers()
    with db.get_session() as s:
        assert s.query(Event).filter(Event.retired_at.isnot(None)).count() == 0
        assert {e.title for e in s.query(Event).all()} == {"Show a1", "Show a2", "Show b1"}
    src = _source("tm")
    assert src.error_count == 1 and "Area B" in (src.last_error or "")
    assert "Area A" not in (src.last_error or "")


def test_editing_the_location_list_never_moves_a_key_or_a_uid(api):
    a = _loc("Area A", "00001")
    _tm("tm")
    api.events = {A_LL: [("a1", _day(2)), ("a2", _day(3))], B_LL: [("a2", _day(3))]}
    jobs.run_all_scrapers()
    before = _keys()
    with db.get_session() as s:
        locations.update(s, a, label="Renamed", entry="00001", radius=30, unit="miles")
        s.commit()
    _loc("Area B", "00003")
    jobs.run_all_scrapers()
    assert _keys() == before
    assert _links()["Show a2"] == {"Renamed", "Area B"}


def test_deleting_a_location_takes_it_out_of_every_assignment(api):
    a = _loc("Area A", "00001")
    b = _loc("Area B", "00003")
    _tm("tm", [a, b])
    api.events = {A_LL: [("a1", _day(2))], B_LL: [("b1", _day(2))]}
    jobs.run_all_scrapers()
    with db.get_session() as s:
        locations.delete(s, b)
        s.commit()
    assert locations.assigned_ids(_source("tm")) == [a]
    assert _links()["Show b1"] == set()
    api.requests.clear()
    jobs.run_all_scrapers()
    assert {r["latlong"] for r in api.requests} == {A_LL}


# --- Eventbrite and Meetup take the location's city ----------------------------------------------


AREA = SearchArea(
    id=1,
    label="Area A",
    lat=11.0,
    lon=-21.0,
    radius=25,
    unit="miles",
    city="Sample City",
    state="CA",
)


def test_meetup_searches_the_locations_city():
    url = meetup.MeetupScraper(Config(), location=AREA)._get_url()
    assert "location=us--ca--Sample+City" in url


def test_eventbrite_searches_the_locations_city():
    url = eventbrite.EventbriteScraper(Config(), location=AREA)._get_url()
    assert url == "https://www.eventbrite.com/d/ca--sample-city/events/"


def test_a_locations_own_meetup_and_eventbrite_values_win():
    area = SearchArea(
        id=1,
        label="A",
        lat=11.0,
        lon=-21.0,
        radius=25,
        unit="miles",
        city="Sample City",
        state="CA",
        meetup_location="us--ny--Exampleton",
        eventbrite_region="ny--exampleton",
    )
    assert "location=us--ny--Exampleton" in meetup.MeetupScraper(Config(), location=area)._get_url()
    assert (
        eventbrite.EventbriteScraper(Config(), location=area)
        ._get_url()
        .endswith("/d/ny--exampleton/events/")
    )


@pytest.mark.parametrize("cls", [meetup.MeetupScraper, eventbrite.EventbriteScraper])
def test_a_location_without_a_us_city_refuses_before_chromium(cls, monkeypatch):
    monkeypatch.setattr(cls, "_get_page", lambda self: pytest.fail("Chromium started"))
    monkeypatch.setattr(cls, "_run", lambda self: pytest.fail("ran"), raising=False)
    abroad = SearchArea(id=2, label="Abroad", lat=40.5, lon=-73.25, radius=25, unit="km")
    for loc in (abroad, None):
        with pytest.raises(ValueError):
            cls(Config(), location=loc).scrape()


# --- the migration -------------------------------------------------------------------------------


def _migrated(monkeypatch, tmp_path, cfg: Config, name: str = "m.db"):
    monkeypatch.setattr(config, "_cfg", cfg)
    db.init_db(tmp_path / name)
    with db.get_session() as s:
        rows = s.query(SearchLocation).order_by(SearchLocation.id).all()
        for r in rows:
            s.expunge(r)
        return rows


def test_the_old_city_settings_become_one_location(monkeypatch, tmp_path):
    cfg = Config(
        city_zip="00001",
        city_name="Exampleton, NY",
        city_radius_miles=40,
        eventbrite_region="ny--exampleton",
        meetup_location="us--ny--Exampleton",
    )
    (row,) = _migrated(monkeypatch, tmp_path, cfg)
    assert (row.label, row.entry, row.lat, row.lon) == ("Exampleton, NY", "00001", 10.0, -20.0)
    assert (row.radius, row.unit) == (40, "miles")
    assert (row.eventbrite_region, row.meetup_location) == ("ny--exampleton", "us--ny--Exampleton")


def test_a_city_name_alone_resolves_as_a_city_and_no_name_is_default(monkeypatch, tmp_path):
    (row,) = _migrated(monkeypatch, tmp_path, Config(city_name="Sample City, CA"), "a.db")
    assert (row.label, row.lat, row.radius) == ("Sample City, CA", 11.01, 25)
    (row,) = _migrated(monkeypatch, tmp_path, Config(city_zip="00003"), "b.db")
    assert (row.label, row.lat) == ("Default", 11.0)


def test_an_upgrade_with_no_old_settings_creates_no_location(monkeypatch, tmp_path):
    assert _migrated(monkeypatch, tmp_path, Config()) == []
    with db.get_session() as s:
        tm = s.query(Source).filter_by(name="ticketmaster").one()
        assert tm.url == API and tm.last_error is None and tm.enabled is False


def test_the_settings_migrate_once(monkeypatch, tmp_path):
    cfg = Config(city_zip="00001")
    assert len(_migrated(monkeypatch, tmp_path, cfg)) == 1
    assert len(_migrated(monkeypatch, tmp_path, cfg)) == 1  # a restart adds nothing
    with db.get_session() as s:
        locations.delete(s, s.query(SearchLocation).one().id)
        s.commit()
    assert _migrated(monkeypatch, tmp_path, cfg) == []  # a deleted one does not come back


def _old_style(tmp_path, monkeypatch, *urls: tuple[str, str]) -> None:
    """A database as item 22 left it: regional sources with their area in the URL."""
    monkeypatch.setattr(config, "_cfg", Config())
    db.init_db(tmp_path / "old.db")
    with db.get_session() as s:
        for name, url in urls:
            s.add(Source(name=name, display_name=name, type="ticketmaster", url=url, enabled=True))
        # Before item 30 nothing was migrated: no mark, so the next start migrates the settings.
        s.query(AppMeta).filter_by(key=locations.META_MIGRATED).delete()
        s.commit()


def test_a_regional_url_area_becomes_that_sources_own_location(monkeypatch, tmp_path):
    _old_style(tmp_path, monkeypatch, ("tm", f"{API}?latlong=10.0,-20.0&radius=35&unit=km"))
    rows = _migrated(monkeypatch, tmp_path, Config(), "old.db")
    (row,) = rows
    assert (row.lat, row.lon, row.radius, row.unit) == (10.0, -20.0, 35, "km")
    assert row.label == "Exampleton, NY"  # the nearest bundled place
    src = _source("tm")
    assert src.url == API and locations.assigned_ids(src) == [row.id]


def test_two_regional_sources_each_keep_exactly_their_own_area(monkeypatch, tmp_path):
    _old_style(
        tmp_path,
        monkeypatch,
        ("tm_a", f"{API}?latlong=10.0,-20.0&radius=25&unit=miles"),
        ("tm_b", f"{API}?latlong=11.0,-21.0&radius=15&unit=miles"),
    )
    rows = _migrated(monkeypatch, tmp_path, Config(city_zip="00002"), "old.db")
    by_ll = {(r.lat, r.lon): r for r in rows}
    a, b = by_ll[(10.0, -20.0)], by_ll[(11.0, -21.0)]
    assert (a.radius, b.radius) == (25, 15)
    assert locations.assigned_ids(_source("tm_a")) == [a.id]
    assert locations.assigned_ids(_source("tm_b")) == [b.id]
    assert len(rows) == 3  # and the old city settings' own location, assigned to neither


def test_a_migrated_source_searches_exactly_what_its_url_did(api, monkeypatch, tmp_path):
    for url_ll, radius, unit in (("12.5,-45.25", "25", "miles"), ("-33.75,101.5", "10", "km")):
        api.requests.clear()
        name = f"tm{len(url_ll)}"
        with db.get_session() as s:
            s.add(
                Source(
                    name=name,
                    display_name=name,
                    type="ticketmaster",
                    url=f"{API}?latlong={url_ll}&radius={radius}&unit={unit}",
                    enabled=True,
                )
            )
            s.commit()
            locations.migrate(s, Config())
            s.commit()
        jobs.run_single_scraper(name)
        assert {(r["latlong"], r["radius"], r["unit"]) for r in api.requests} == {
            (url_ll, radius, unit)
        }


def test_a_migrated_sources_events_keep_their_identity_keys_and_uids(api):
    old_url = f"{API}?latlong={A_LL}&radius=25&unit=miles"
    with db.get_session() as s:
        src = Source(name="tm", display_name="tm", type="ticketmaster", url=old_url, enabled=True)
        s.add(src)
        s.commit()
        # What the item-22 scraper stored: the Ticketmaster id is the identity.
        old = [
            EventData(
                title=f"Show {i}",
                start_dt=_day(d).replace(tzinfo=None),
                venue_name=f"Hall {i}",
                url=f"https://www.example.com/event/{i}",
                source_uid=i,
            )
            for i, d in (("a1", 2), ("a2", 3))
        ]
        upsert_events(s, old, src.id)
        expected = {ev.title: identity_key(ev, src) for ev in old}
    before = _keys()
    assert {t: k[0] for t, k in before.items()} == expected
    with db.get_session() as s:
        locations.migrate(s, Config())
        s.commit()
    api.events = {A_LL: [("a1", _day(2)), ("a2", _day(3))]}
    jobs.run_all_scrapers()
    assert _keys() == before  # no new row, no moved key, no new UID
    assert _links() == {"Show a1": {"Exampleton, NY"}, "Show a2": {"Exampleton, NY"}}


def test_the_migration_keeps_a_rerun_from_adding_a_second_location(monkeypatch, tmp_path):
    _old_style(tmp_path, monkeypatch, ("tm", f"{API}?latlong=10.0,-20.0&radius=25&unit=miles"))
    _migrated(monkeypatch, tmp_path, Config(), "old.db")
    with db.get_session() as s:  # an old sources.yml re-applied by hand writes the URL back
        s.query(Source).filter_by(name="tm").one().url = (
            f"{API}?latlong=10.0,-20.0&radius=25&unit=miles"
        )
        s.commit()
    rows = _migrated(monkeypatch, tmp_path, Config(), "old.db")
    assert len(rows) == 1 and locations.assigned_ids(_source("tm")) == [rows[0].id]


# --- import and export -----------------------------------------------------------------------------


DOC = f"""
locations:
  - label: Area A
    where: "00001"
    radius: 25
    unit: miles
  - label: Area B
    where: "Sample City, CA"
    radius: 10
    unit: km
    meetup_location: us--ca--Sample+City
sources:
  - name: tm-a
    type: ticketmaster
    url: {API}
    locations: [Area A]
  - name: tm-all
    type: ticketmaster
    url: {API}
    locations: all
"""


@pytest.fixture
def io_session(session, monkeypatch):
    monkeypatch.setattr(sio, "check_source_url", lambda url: None)
    return session


def test_import_adds_locations_and_each_sources_assignment(io_session):
    s = io_session
    plan = sio.run_import(s, DOC, apply=True)
    assert [loc["label"] for loc in plan.locations_added] == ["Area A", "Area B"]
    s.expire_all()
    a = s.query(SearchLocation).filter_by(label="Area A").one()
    b = s.query(SearchLocation).filter_by(label="Area B").one()
    assert (a.lat, a.radius, a.unit) == (10.0, 25, "miles")
    assert (b.lat, b.unit, b.meetup_location) == (11.01, "km", "us--ca--Sample+City")
    assert locations.assigned_ids(s.query(Source).filter_by(name="tm-a").one()) == [a.id]
    assert locations.assigned_ids(s.query(Source).filter_by(name="tm-all").one()) is None


def test_export_then_import_changes_nothing(io_session):
    s = io_session
    sio.run_import(s, DOC, apply=True)
    text = sio.export_yaml(s)
    doc = yaml.safe_load(text)
    assert [loc["label"] for loc in doc["locations"]] == ["Area A", "Area B"]
    plan = sio.run_import(s, text, apply=False)
    assert not plan.added and not plan.updated, plan.render()
    assert not plan.locations_added and not plan.locations_updated, plan.render()


def test_a_later_import_updates_a_location_by_its_label(io_session):
    s = io_session
    sio.run_import(s, DOC, apply=True)
    plan = sio.run_import(s, DOC.replace("radius: 25", "radius: 40"), apply=True)
    assert [loc["label"] for loc in plan.locations_updated] == ["Area A"]
    s.expire_all()
    assert s.query(SearchLocation).filter_by(label="Area A").one().radius == 40


@pytest.mark.parametrize(
    "bad,fragment",
    [
        (DOC.replace("locations: [Area A]", "locations: [Nowhere]"), "unknown location"),
        (DOC.replace('where: "00001"', 'where: "99999"'), gazetteer.MESSAGES["zip_unknown"]),
        (DOC.replace("unit: km", "unit: furlongs"), locations.MESSAGES["unit"]),
    ],
)
def test_a_bad_location_refuses_the_whole_file(io_session, bad, fragment):
    with pytest.raises(sio.ImportRefused) as exc:
        sio.run_import(io_session, bad, apply=True)
    assert fragment in str(exc.value)
    assert io_session.query(SearchLocation).count() == 0


def test_an_enabled_regional_entry_with_its_area_in_the_url_is_refused(io_session):
    doc = (
        "sources:\n  - name: tm-old\n    type: ticketmaster\n"
        f"    url: {API}?latlong=10.0,-20.0&radius=25&unit=miles\n"
    )
    with pytest.raises(sio.ImportRefused) as exc:
        sio.run_import(io_session, doc, apply=True)
    assert ticketmaster.LOCATION_IN_URL in str(exc.value)


# --- the admin pages -------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    cfg = Config()
    cfg.secret_key = "k" * 64
    cfg.password_hash = auth.hash_password("pw")
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    db.init_db(tmp_path / "app.db")
    import app.web.routes as routes

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(routes, "check_source_url", lambda url: None)
    c = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    c.cookies.set(
        oidc.SESSION_COOKIE, auth.create_session("admin", cfg.secret_key, cfg.password_hash)
    )
    return nullcontext(c)


def _location_form(**kw) -> dict:
    return {"label": "Area A", "entry": "00001", "radius": "25", "unit": "miles", **kw}


def test_the_locations_page_renders_and_adds_a_location(client, no_network):
    with client as http:
        r = http.get("/admin/locations")
        assert r.status_code == 200, r.text[:300]
        assert "No search locations yet" in r.text
        r = http.post("/admin/locations", data=_location_form())
        assert r.status_code == 302
        r = http.get("/admin/locations")
    assert r.status_code == 200 and "Area A" in r.text and "Exampleton, NY" in r.text
    assert no_network == []


def test_the_locations_page_refuses_an_unknown_entry_with_fixed_text(client):
    with client as http:
        r = http.post("/admin/locations", data=_location_form(entry="<b>99999</b>"))
    assert r.status_code == 400 and r.text == gazetteer.MESSAGES["unparsed"]
    with db.get_session() as s:
        assert s.query(SearchLocation).count() == 0


def test_a_location_can_be_edited_and_deleted(client):
    with client as http:
        http.post("/admin/locations", data=_location_form())
        with db.get_session() as s:
            loc_id = s.query(SearchLocation).one().id
        r = http.post(
            f"/admin/locations/{loc_id}/update",
            data=_location_form(label="Uptown", entry="Sample City, CA", radius="40"),
        )
        assert r.status_code == 302
        with db.get_session() as s:
            row = s.get(SearchLocation, loc_id)
            assert (row.label, row.lat, row.radius) == ("Uptown", 11.01, 40)
        r = http.post(f"/admin/locations/{loc_id}/delete")
        assert r.status_code == 302
    with db.get_session() as s:
        assert s.query(SearchLocation).count() == 0


def test_the_source_form_assigns_locations(client):
    with client as http:
        http.post("/admin/locations", data=_location_form())
        http.post("/admin/locations", data=_location_form(label="Area B", entry="00003"))
        with db.get_session() as s:
            ids = {r.label: r.id for r in s.query(SearchLocation).all()}
        r = http.get("/admin/sources/new")
        assert r.status_code == 200 and "Area A" in r.text and "Area B" in r.text
        form = {
            "display_name": "Regional",
            "scraper_type": "ticketmaster",
            "url": API,
            "enabled": "on",
            "locations_mode": "selected",
            "location_ids": [str(ids["Area B"])],
        }
        assert http.post("/admin/sources", data=form).status_code == 302
        with db.get_session() as s:
            src = s.query(Source).filter_by(display_name="Regional").one()
            name = src.name
            assert locations.assigned_ids(src) == [ids["Area B"]]
        r = http.get(f"/admin/sources/{name}/edit")
        assert r.status_code == 200
        form["locations_mode"] = "all"
        assert http.post(f"/admin/sources/{name}/update", data=form).status_code == 302
    with db.get_session() as s:
        assert locations.assigned_ids(s.query(Source).filter_by(name=name).one()) is None


def test_the_form_refuses_a_regional_url_that_still_carries_an_area(client):
    form = {
        "display_name": "Regional",
        "scraper_type": "ticketmaster",
        "url": f"{API}?latlong=10.0,-20.0&radius=25",
        "enabled": "on",
    }
    with client as http:
        r = http.post("/admin/sources", data=form)
    assert r.status_code == 400 and r.text == ticketmaster.LOCATION_IN_URL


def test_the_settings_page_no_longer_carries_a_city(client):
    with client as http:
        r = http.get("/admin/settings")
    assert r.status_code == 200
    for gone in ('name="city_zip"', 'name="city_name"', 'name="meetup_location"'):
        assert gone not in r.text
    assert "/admin/locations" in r.text
