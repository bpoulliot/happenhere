"""Item 31: area feeds.

Each search location's label (item 30) is an area. A per-venue source is assigned one by the
admin (``Source.area_id``); a location-aware source's events carry the locations they were found
for (``event_locations``). ``/feed/area/<slug>.ics`` lists one area's events with the SAME UIDs
as every other feed, the events page and calendar take an ``area`` filter that lives in the URL,
and the import/export file carries a source's area.

Every label, ZIP code and host here is invented (the synthetic Gazetteer in
``tests/fixtures/gazetteer``).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from icalendar import Calendar

from app import areas, auth, config, db, locations, main, oidc
from app import sources as sio
from app.config import Config
from app.dedup import upsert_events
from app.ical_export import generate_ics
from app.models import Event, EventLocation, SearchLocation, Source
from app.scrapers.base import EventData

NOW = datetime(2030, 1, 1, 12, 0, 0)  # the fixed "now" of the byte-identical feed checks


def _when(days: int, hour: int = 19) -> datetime:
    return (datetime.now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _ev(title: str, start: datetime, **kw) -> EventData:
    return EventData(title=title, start_dt=start, venue_name=kw.pop("venue_name", "Hall"), **kw)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The app against a temp database and a fixed config; never the lifespan."""
    cfg = Config()
    cfg.secret_key = "k" * 64
    cfg.site_name = "Example Events"
    cfg.password_hash = auth.hash_password("pw")
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    import app.web.routes as routes

    monkeypatch.setattr(routes, "check_source_url", lambda url: None)
    monkeypatch.setattr(sio, "check_source_url", lambda url: None)
    db.init_db(tmp_path / "area.db")
    return cfg


@pytest.fixture
def session(env):
    with db.get_session() as s:
        yield s


def _client(admin: bool = False):
    c = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    if admin:
        cfg = config.get_config()
        c.cookies.set(
            oidc.SESSION_COOKIE, auth.create_session("admin", cfg.secret_key, cfg.password_hash)
        )
    return nullcontext(c)


def _titles(r) -> list[str]:
    cal = Calendar.from_ical(r.content)
    return sorted(str(c["SUMMARY"]) for c in cal.walk() if c.name == "VEVENT")


def _uid_by_title(content: bytes) -> dict[str, str]:
    cal = Calendar.from_ical(content)
    return {str(c["SUMMARY"]): str(c["UID"]) for c in cal.walk() if c.name == "VEVENT"}


def _add_source(s, name: str, stype: str = "ical", **kw) -> Source:
    src = Source(name=name, display_name=name.title(), type=stype, **kw)
    s.add(src)
    s.commit()
    return src


def _store(s, src: Source, title: str, days: int) -> Event:
    upsert_events(s, [_ev(title, _when(days), venue_name=src.display_name)], src.id)
    return s.query(Event).filter_by(source_id=src.id, title=title).one()


def _link(s, event: Event, *loc_ids: int) -> None:
    for i in loc_ids:
        s.add(EventLocation(event_id=event.id, location_id=i))
    s.commit()


def _location(s, label: str, entry: str) -> int:
    row = locations.add(s, label=label, entry=entry, radius=25, unit="miles")
    s.commit()
    return row.id


def _world(s) -> dict[str, int]:
    """Two areas, a regional source (events found for areas), two per-venue sources assigned an
    area, one with no area, and a regional event no search has linked yet.

    Returns the location ids by label. Nothing here assigns an area: ``_assign_areas`` does.
    """
    a = _location(s, "Area A", "00001")
    b = _location(s, "Area B", "00003")
    tm = _add_source(s, "regional", "ticketmaster", url="https://tm.example.com/events")
    hall = _add_source(s, "hall", tags=json.dumps(["music"]))
    bar = _add_source(s, "bar", tags=json.dumps(["music", "late"]))
    _add_source(s, "museum")
    _store(s, tm, "Both show", 3)
    _store(s, tm, "A only show", 4)
    _store(s, tm, "Unlinked show", 5)
    _store(s, hall, "Hall show", 6)
    _store(s, bar, "Bar show", 7)
    _store(s, s.query(Source).filter_by(name="museum").one(), "Museum show", 8)
    return {"Area A": a, "Area B": b, "tm": tm.id, "hall": hall.id, "bar": bar.id}


def _assign_areas(s, ids: dict[str, int]) -> None:
    for title, locs in {
        "Both show": ("Area A", "Area B"),
        "A only show": ("Area A",),
    }.items():
        _link(s, s.query(Event).filter_by(title=title).one(), *[ids[x] for x in locs])
    s.get(Source, ids["hall"]).area_id = ids["Area A"]
    s.get(Source, ids["bar"]).area_id = ids["Area B"]
    s.commit()


@pytest.fixture
def world(session):
    ids = _world(session)
    _assign_areas(session, ids)
    return ids


# --- the feeds -------------------------------------------------------------------------------


def test_an_area_feed_holds_exactly_that_areas_events(world):
    with _client() as c:
        a, b = c.get("/feed/area/area-a.ics"), c.get("/feed/area/area-b.ics")
        assert a.status_code == 200 and a.headers["content-type"].startswith("text/calendar")
        assert _titles(a) == ["A only show", "Both show", "Hall show"]
        assert _titles(b) == ["Bar show", "Both show"]
        assert len(_titles(c.get("/feed/all.ics"))) == 6  # the all-events feed keeps everything


def test_the_area_feed_url_is_the_slug_of_the_label(session):
    ny = _location(session, "New York, NY", "00002")
    hall = _add_source(session, "hall", area_id=ny)
    _store(session, hall, "Hall show", 3)
    with _client() as c:
        assert _titles(c.get("/feed/area/new-york-ny.ics")) == ["Hall show"]
        assert c.get("/feed/area/New York, NY.ics").status_code == 404
        cal = Calendar.from_ical(c.get("/feed/area/new-york-ny.ics").content)
        assert "New York, NY" in str(cal["X-WR-CALNAME"])


@pytest.mark.parametrize(
    "path",
    [
        "/feed/area/nosuch.ics",
        "/feed/area/area-a+nosuch.ics",
        "/feed/area/Area-A.ics",  # slugs are lowercase, as tags are
        "/feed/area/area-a%2Fb.ics",
    ],
)
def test_an_unknown_area_is_a_404(world, path):
    with _client() as c:
        assert c.get(path).status_code == 404


def test_a_known_area_with_no_events_is_an_empty_calendar(session):
    _location(session, "Area A", "00001")
    with _client() as c:
        r = c.get("/feed/area/area-a.ics")
    assert r.status_code == 200 and _titles(r) == []


def test_several_areas_in_one_feed_list_each_event_once(world):
    with _client() as c:
        both = c.get("/feed/area/area-a+area-b.ics")
        assert _titles(both) == ["A only show", "Bar show", "Both show", "Hall show"]
        assert _titles(c.get("/feed/area/area-b+area-a.ics")) == _titles(both)


def test_an_event_two_areas_share_has_one_uid_in_every_feed(world):
    with _client() as c:
        uids = {
            path: _uid_by_title(c.get(path).content)["Both show"]
            for path in (
                "/feed/area/area-a.ics",
                "/feed/area/area-b.ics",
                "/feed/area/area-a+area-b.ics",
                "/feed/all.ics",
                "/feed/events.ics",
                "/feed/venue/regional.ics",
            )
        }
        assert len(set(uids.values())) == 1, uids
        union = c.get("/feed/area/area-a+area-b.ics").content
        assert list(_uid_by_title(union).values()).count(next(iter(uids.values()))) == 1


def test_every_uid_in_an_area_feed_is_the_uid_the_all_feed_has(world):
    with _client() as c:
        everything = _uid_by_title(c.get("/feed/all.ics").content)
        for path in ("/feed/area/area-a.ics", "/feed/area/area-b.ics"):
            for title, uid in _uid_by_title(c.get(path).content).items():
                assert everything[title] == uid


def test_the_existing_feeds_are_byte_identical_with_areas_added(session):
    """The all-events, tag and venue feeds do not change when areas are set up (fixed ``now``)."""
    ids = _world(session)
    src = {s.name: s.id for s in session.query(Source)}
    selections = {
        "all": {},
        "music": {"source_ids": [src["hall"], src["bar"]]},
        "late": {"source_ids": [src["bar"]]},
        "venue": {"source_ids": [src["regional"]]},
    }
    now = datetime.now(UTC).replace(tzinfo=None)
    before = {k: generate_ics(session, now=now, **kw) for k, kw in selections.items()}
    _assign_areas(session, ids)
    after = {k: generate_ics(session, now=now, **kw) for k, kw in selections.items()}
    assert after == before
    assert all(before.values()) and len(before["all"]) > 0


def test_the_feed_routes_give_the_same_bytes_with_and_without_areas(session):
    ids = _world(session)
    paths = ["/feed/all.ics", "/feed/events.ics", "/feed/tags/music.ics", "/feed/venue/hall.ics"]
    with _client() as c:
        before = {p: c.get(p).content for p in paths}
    _assign_areas(session, ids)
    with _client() as c:
        assert {p: c.get(p).content for p in paths} == before


def test_an_area_feed_has_an_etag_that_follows_its_events(world, session):
    with _client() as c:
        r = c.get("/feed/area/area-b.ics")
        etag = r.headers["etag"]
        assert c.get("/feed/area/area-b.ics").headers["etag"] == etag
        assert c.get("/feed/area/area-b.ics", headers={"If-None-Match": etag}).status_code == 304
        _link(session, session.query(Event).filter_by(title="A only show").one(), world["Area B"])
        assert c.get("/feed/area/area-b.ics").headers["etag"] != etag


def test_a_regional_event_no_search_has_linked_yet_is_in_its_sources_area(session):
    """Item 30 links an event to its locations when it is scraped; until the next scrape an
    older row falls back to the area of its source, if the admin set one."""
    a = _location(session, "Area A", "00001")
    tm = _add_source(session, "regional", "ticketmaster", area_id=a)
    _store(session, tm, "Old row", 3)
    with _client() as c:
        assert _titles(c.get("/feed/area/area-a.ics")) == ["Old row"]
        _link(session, session.query(Event).one(), _location(session, "Area B", "00003"))
        assert _titles(c.get("/feed/area/area-a.ics")) == []  # a link decides, not the source
        assert _titles(c.get("/feed/area/area-b.ics")) == ["Old row"]


def test_a_retired_event_leaves_its_area_feed(world, session):
    session.query(Event).filter_by(title="Hall show").one().retired_at = NOW
    session.commit()
    with _client() as c:
        assert "Hall show" not in _titles(c.get("/feed/area/area-a.ics"))


def test_the_feeds_page_lists_each_area_feed_with_its_count(world):
    with _client() as c:
        r = c.get("/feeds")
    assert r.status_code == 200
    assert "/feed/area/area-a.ics" in r.text and "/feed/area/area-b.ics" in r.text
    html = r.text
    row_a = html[html.index("Area A") :].split("</tr>")[0]
    row_b = html[html.index("Area B") :].split("</tr>")[0]
    assert "<td>3</td>" in row_a and "<td>2</td>" in row_b


def test_the_feeds_page_without_areas_is_as_before(session):
    _store(session, _add_source(session, "hall"), "Hall show", 3)
    with _client() as c:
        r = c.get("/feeds")
    assert r.status_code == 200 and "/feed/area/" not in r.text


# --- slugs -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, slug",
    [
        ("Area A", "area-a"),
        ("New York, NY", "new-york-ny"),
        ("  Los   Angeles ", "los-angeles"),
        ("Café Zone", "cafe-zone"),
        ("A/B + C.ics", "a-b-c-ics"),
    ],
)
def test_a_slug_is_lowercase_ascii_without_url_syntax(label, slug):
    assert areas.slug(label) == slug


def test_a_label_with_no_ascii_letters_still_gets_a_usable_slug(session):
    row = locations.add(session, label="東京", entry="00001", radius=25, unit="miles")
    session.commit()
    assert areas.slug_of(row) == f"area-{row.id}"
    with _client() as c:
        assert c.get(f"/feed/area/{areas.slug_of(row)}.ics").status_code == 200


def test_two_labels_with_one_slug_are_refused(session):
    _location(session, "New York", "00001")
    with pytest.raises(locations.LocationInvalid) as exc:
        locations.add(session, label="new-york!", entry="00003", radius=25, unit="miles")
    assert exc.value.code == "label_taken"


# --- the events page and the calendar ---------------------------------------------------------


def _cards(html: str) -> list[str]:
    grid = html.split('id="event-grid"')[-1]
    names = ("Both show", "A only show", "Unlinked show", "Hall show", "Bar show", "Museum show")
    return sorted(t for t in names if t in grid)


def test_the_events_page_defaults_to_every_area(world):
    with _client() as c:
        r = c.get("/events")
    assert r.status_code == 200 and len(_cards(r.text)) == 6


def test_the_page_filter_narrows_to_an_area(world):
    with _client() as c:
        assert _cards(c.get("/events?area=area-a").text) == [
            "A only show",
            "Both show",
            "Hall show",
        ]
        assert _cards(c.get("/events?area=area-b").text) == ["Bar show", "Both show"]


def test_the_page_filter_combines_areas(world):
    with _client() as c:
        r = c.get("/events?area=area-a&area=area-b")
    assert _cards(r.text) == ["A only show", "Bar show", "Both show", "Hall show"]


def test_the_filter_keeps_other_filters_and_the_selection_in_the_form(world):
    with _client() as c:
        r = c.get("/events?area=area-b&days=60&search=show")
    html = r.text
    assert 'name="area" value="area-b" checked' in html
    assert 'name="area" value="area-a" checked' not in html
    assert 'name="days"' in html and 'value="show"' in html


def test_the_list_partial_and_the_api_take_the_same_filter(world):
    with _client() as c:
        part = c.get("/partials/event-list?area=area-b")
        assert _cards('id="event-grid"' + part.text) == ["Bar show", "Both show"]
        api = c.get("/api/events?area=area-a&area=area-b").json()
        assert sorted(e["title"] for e in api["events"]) == [
            "A only show",
            "Bar show",
            "Both show",
            "Hall show",
        ]


def test_an_unknown_area_in_the_url_is_ignored_not_an_error(world):
    with _client() as c:
        r = c.get("/events?area=nosuch")
        assert r.status_code == 200 and len(_cards(r.text)) == 6  # nothing valid: all areas
        r = c.get("/events?area=nosuch&area=area-b")
        assert _cards(r.text) == ["Bar show", "Both show"]


def test_the_area_filter_hides_nothing_when_there_are_no_areas(session):
    _store(session, _add_source(session, "hall"), "Hall show", 3)
    with _client() as c:
        r = c.get("/events")
    assert r.status_code == 200 and "Hall show" in r.text and 'name="area"' not in r.text


def test_the_calendar_takes_the_area_filter_and_keeps_it_across_months(world):
    month = _when(3).strftime("%Y-%m")
    with _client() as c:
        r = c.get(f"/events/calendar?month={month}&area=area-b")
        assert r.status_code == 200
        assert "Bar show" in r.text and "Both show" in r.text and "Hall show" not in r.text
        assert "area=area-b" in r.text.split("cal-grid")[0]  # prev/next keep the filter
        every = c.get(f"/events/calendar?month={month}")
        assert every.status_code == 200 and "area=" not in every.text.split("cal-grid")[0]


# --- the admin form ---------------------------------------------------------------------------

FORM = {"display_name": "Venue", "scraper_type": "ical", "url": "https://v.example.com/e.ics"}


def test_the_source_form_sets_the_area(world):
    with _client(admin=True) as c:
        r = c.get("/admin/sources/new")
        assert r.status_code == 200
        assert 'name="area_id"' in r.text and "Area A" in r.text and "Area B" in r.text
        assert (
            c.post("/admin/sources", data={**FORM, "area_id": str(world["Area B"])}).status_code
            == 302
        )
        with db.get_session() as s:
            src = s.query(Source).filter_by(display_name="Venue").one()
            name = src.name
            assert src.area_id == world["Area B"]
        edit = c.get(f"/admin/sources/{name}/edit")
        assert edit.status_code == 200
        assert f'value="{world["Area B"]}" selected' in edit.text
        r = c.post(f"/admin/sources/{name}/update", data={**FORM, "area_id": str(world["Area A"])})
        assert r.status_code == 302
        with db.get_session() as s:
            assert s.query(Source).filter_by(name=name).one().area_id == world["Area A"]
        # an older form without the field leaves it alone; "No area" clears it
        assert c.post(f"/admin/sources/{name}/update", data=FORM).status_code == 302
        with db.get_session() as s:
            assert s.query(Source).filter_by(name=name).one().area_id == world["Area A"]
        assert (
            c.post(f"/admin/sources/{name}/update", data={**FORM, "area_id": "none"}).status_code
            == 302
        )
    with db.get_session() as s:
        assert s.query(Source).filter_by(name=name).one().area_id is None


def test_the_source_form_refuses_an_unknown_area_with_fixed_text(world):
    with _client(admin=True) as c:
        r = c.post("/admin/sources", data={**FORM, "area_id": "99999"})
        assert r.status_code == 400 and r.text == locations.MESSAGES["not_found"]
        r = c.post("/admin/sources", data={**FORM, "area_id": "<b>x</b>"})
        assert r.status_code == 400 and r.text == locations.MESSAGES["not_found"]
    with db.get_session() as s:
        assert s.query(Source).filter_by(display_name="Venue").count() == 0


def test_deleting_a_location_takes_it_out_of_every_sources_area(world, session):
    locations.delete(session, world["Area A"])
    session.commit()
    session.expire_all()
    assert session.get(Source, world["hall"]).area_id is None
    assert session.get(Source, world["bar"]).area_id == world["Area B"]
    with _client() as c:
        assert c.get("/feed/area/area-a.ics").status_code == 404


def test_the_admin_sources_table_still_renders_with_areas(world):
    with _client(admin=True) as c:
        assert c.get("/admin").status_code == 200


# --- import / export ---------------------------------------------------------------------------

DOC = """
locations:
  - label: Area A
    where: "00001"
    radius: 25
    unit: miles
  - label: Area B
    where: "00003"
    radius: 25
    unit: miles
sources:
  - name: hall
    type: ical
    url: https://hall.example.com/events.ics
    area: Area A
  - name: bar
    type: ical
    url: https://bar.example.com/events.ics
    area: area b
  - name: museum
    type: ical
    url: https://museum.example.com/events.ics
"""


SEEDED = {"ticketmaster", "eventbrite", "meetup", "fathom"}  # the disabled regional sources


def _areas(s) -> dict[str, str | None]:
    labels = {r.id: r.label for r in s.query(SearchLocation)}
    s.expire_all()
    return {x.name: labels.get(x.area_id) for x in s.query(Source) if x.name not in SEEDED}


def test_import_sets_each_sources_area_by_label(session):
    plan = sio.run_import(session, DOC, apply=True)
    assert plan.applied
    assert _areas(session) == {"hall": "Area A", "bar": "Area B", "museum": None}


def test_export_then_import_keeps_every_area_unchanged(world, session):
    before = _areas(session)
    assert before["hall"] == "Area A" and before["bar"] == "Area B"
    text = sio.export_yaml(session)
    doc = yaml.safe_load(text)
    by_name = {e["name"]: e for e in doc["sources"]}
    assert by_name["hall"]["area"] == "Area A" and "area" not in by_name["museum"]
    plan = sio.run_import(session, text, apply=True)
    assert not plan.added and not plan.updated, plan.render()
    assert _areas(session) == before


def test_a_later_import_changes_an_area_and_a_null_clears_it(session):
    sio.run_import(session, DOC, apply=True)
    moved = DOC.replace("area: Area A", "area: Area B").replace(
        "https://museum.example.com/events.ics",
        "https://museum.example.com/events.ics\n    area: Area A",
    )
    plan = sio.run_import(session, moved, apply=True)
    assert sorted(e["name"] for e in plan.updated) == ["hall", "museum"]
    assert _areas(session) == {"hall": "Area B", "bar": "Area B", "museum": "Area A"}
    cleared = moved.replace("area: Area A", "area: null")
    sio.run_import(session, cleared, apply=True)
    assert _areas(session)["museum"] is None


def test_an_entry_without_area_leaves_the_sources_area_alone(session):
    sio.run_import(session, DOC, apply=True)
    plan = sio.run_import(session, DOC.replace("    area: Area A\n", ""), apply=True)
    assert not plan.updated, plan.render()
    assert _areas(session)["hall"] == "Area A"


@pytest.mark.parametrize("bad", ["area: Nowhere", "area: [Area A]", "area: 7"])
def test_an_unknown_or_malformed_area_refuses_the_whole_file(session, bad):
    with pytest.raises(sio.ImportRefused):
        sio.run_import(session, DOC.replace("area: Area A", bad), apply=True)
    assert _areas(session) == {} and session.query(SearchLocation).count() == 0


def test_the_dry_run_shows_the_area_and_writes_nothing(session):
    plan = sio.run_import(session, DOC, apply=False)
    assert not plan.applied and _areas(session) == {}
    assert {e["name"]: e.get("area") for e in plan.added} == {
        "hall": "Area A",
        "bar": "Area B",
        "museum": None,
    }
    assert "hall" in plan.render()


# --- the column on an older database -----------------------------------------------------------


def test_the_area_column_is_added_to_an_older_database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_cfg", Config())
    path = tmp_path / "old.db"
    db.init_db(path)
    with db.get_session() as s:
        _add_source(s, "hall")
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE sources DROP COLUMN area_id")  # a database from before item 31
    con.commit()
    con.close()
    db.init_db(path)
    with db.get_session() as s:
        src = s.query(Source).filter_by(name="hall").one()
        assert src.area_id is None
        src.area_id = 1
        s.commit()
