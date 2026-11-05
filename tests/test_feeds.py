"""Item 03: stable feed UIDs, feeds by tag and by venue, the /feeds page, caching."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar

from app import auth, config, db, oidc
from app.config import Config
from app.dedup import UID_DOMAIN, identity, upsert_events
from app.ical_export import generate_ics
from app.main import app
from app.models import Event, Source
from app.scrapers._ical_base import _parse_vevent
from app.scrapers.base import EventData


@pytest.fixture
def session(tmp_path):
    db.init_db(tmp_path / "feeds.db")
    with db.get_session() as s:
        yield s


def _source(session, name="hall", **kw) -> Source:
    src = Source(name=name, display_name=kw.pop("display_name", name.title()), type="ical", **kw)
    session.add(src)
    session.commit()
    return src


def _uids(ics: bytes) -> list[str]:
    cal = Calendar.from_ical(ics)
    return [str(c["UID"]) for c in cal.walk() if c.name == "VEVENT"]


def _vevent(summary: str, start: datetime, uid: str = "evt-1@example.com"):
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//example//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART:{start:%Y%m%dT%H%M%S}\r\n"
        "URL:https://hall.example.com/events/evt-1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return next(c for c in Calendar.from_ical(text).walk() if c.name == "VEVENT")


# --- Stable UIDs (spec point 4) ----------------------------------------------------------


def test_uid_is_stable_when_an_ical_event_is_retitled(session):
    src = _source(session)
    start = (datetime.now() + timedelta(days=10)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )

    upsert_events(session, [_parse_vevent(_vevent("Quartet Night", start), "Hall")], src.id)
    before = _uids(generate_ics(session))

    upsert_events(
        session, [_parse_vevent(_vevent("Quartet Night — SOLD OUT", start), "Hall")], src.id
    )
    after = _uids(generate_ics(session))

    print(f"\nUIDs before the retitle: {before}\nUIDs after the retitle:  {after}")
    assert len(before) == 1
    assert after == before, "a retitle changed the feed UID or left a ghost event"


def _ev(title, start, **kw) -> EventData:
    return EventData(title=title, start_dt=start, venue_name=kw.pop("venue_name", "Hall"), **kw)


START = (datetime.now() + timedelta(days=10)).replace(hour=19, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize(
    "kind, extra",
    [
        ("uid", {"source_uid": "tm-123"}),  # a ticketing platform's id
        ("url", {"url": "https://hall.example.com/events/quartet"}),  # JSON-LD, most HTML
        ("slot", {}),  # nothing: start time + venue
    ],
)
def test_uid_is_stable_across_a_retitle_for_every_identity_kind(session, kind, extra):
    src = _source(session, url="https://hall.example.com/events")
    assert identity(_ev("x", START, **extra), src)[0] == kind
    upsert_events(session, [_ev("Quartet Night", START, **extra)], src.id)
    before = _uids(generate_ics(session))
    upsert_events(session, [_ev("Quartet Night (moved to the big room)", START, **extra)], src.id)
    assert _uids(generate_ics(session)) == before
    assert session.query(Event).count() == 1


def test_the_listing_page_url_is_not_an_event_identity(session):
    src = _source(session, url="https://hall.example.com/events/")
    ev = _ev("Quartet Night", START, url="https://hall.example.com/events#top")
    assert identity(ev, src)[0] == "slot"


def test_a_moved_event_keeps_its_uid_and_its_new_time(session):
    """Matched on the source's own UID, the row takes the new start time (no ghost)."""
    src = _source(session)
    upsert_events(session, [_parse_vevent(_vevent("Quartet Night", START), "Hall")], src.id)
    before = _uids(generate_ics(session))
    later = START + timedelta(days=2, hours=1)
    upsert_events(session, [_parse_vevent(_vevent("Quartet Night", later), "Hall")], src.id)
    assert _uids(generate_ics(session)) == before
    assert [e.start_dt for e in session.query(Event)] == [later]


def test_events_sharing_one_identity_in_a_batch_fall_back_to_the_dedup_hash(session):
    """A series listed under one URL is not one event: both are kept, with distinct UIDs."""
    src = _source(session)
    url = "https://hall.example.com/events/weekly-trivia"
    batch = [_ev("Trivia", START, url=url), _ev("Trivia", START + timedelta(days=7), url=url)]
    upsert_events(session, batch, src.id)
    rows = session.query(Event).order_by(Event.start_dt).all()
    assert len(rows) == 2 and all(r.identity_key is None for r in rows)
    assert {r.uid for r in rows} == {f"{e.dedup_hash}@{UID_DOMAIN}" for e in batch}
    upsert_events(session, batch, src.id)  # the next scrape matches them by hash again
    assert session.query(Event).count() == 2


def test_a_row_from_before_item_03_keeps_its_uid_through_a_retitle(session):
    src = _source(session)
    first = _ev("Quartet Night", START, url="https://hall.example.com/events/q")
    session.add(  # as stored by the old code: no identity_key, no uid
        Event(source_id=src.id, dedup_hash=first.dedup_hash, title=first.title, start_dt=START)
    )
    session.commit()
    legacy = [f"{first.dedup_hash}@peakevents"]
    assert _uids(generate_ics(session)) == legacy
    upsert_events(session, [first], src.id)  # backfills the identity key
    upsert_events(session, [_ev("Quartet Night — SOLD OUT", START, url=first.url)], src.id)
    assert _uids(generate_ics(session)) == legacy
    assert session.query(Event).one().title == "Quartet Night — SOLD OUT"


def test_an_ical_recurrence_override_is_its_own_event():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:series@example.com\r\n"
        "SUMMARY:Open Mic\r\nDTSTART:20300107T190000\r\nRECURRENCE-ID:20300107T190000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    comp = next(c for c in Calendar.from_ical(text).walk() if c.name == "VEVENT")
    assert _parse_vevent(comp, "Hall").source_uid == "series@example.com#20300107T190000"


# --- Feed routes (spec points 2, 3, 5, 7) -------------------------------------------------


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """The app against a temp database and a fixed config; never the lifespan."""
    db.init_db(tmp_path / "app.db")
    cfg = Config()
    cfg.secret_key = "k" * 64
    cfg.site_name = "Example Events"
    cfg.password_hash = auth.hash_password("pw")
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    return cfg


def _client():
    return nullcontext(TestClient(app, base_url="https://testserver", follow_redirects=False))


def _seed(sources: dict[str, list[str]], days: int = 10) -> None:
    """One upcoming event per source; each source carries the given tags."""
    with db.get_session() as s:
        for i, (name, tags) in enumerate(sources.items()):
            src = Source(name=name, display_name=name.title(), type="ical", tags=json.dumps(tags))
            s.add(src)
            s.commit()
            when = START + timedelta(days=days - 10, hours=i)
            upsert_events(s, [_ev(f"{name} show", when, venue_name=name.title())], src.id)


def _titles(r) -> list[str]:
    cal = Calendar.from_ical(r.content)
    return sorted(str(c["SUMMARY"]) for c in cal.walk() if c.name == "VEVENT")


def test_every_feed_url_form(app_env):
    _seed({"jazzhall": ["music", "downtown"], "rockbar": ["music"], "museum": ["downtown"]})
    with _client() as c:
        everything = c.get("/feed/all.ics")
        assert everything.status_code == 200
        assert everything.headers["content-type"].startswith("text/calendar")
        assert _titles(everything) == ["jazzhall show", "museum show", "rockbar show"]
        assert c.get("/feed/events.ics").content == everything.content  # the old URL
        assert _titles(c.get("/feed/tags/music.ics")) == ["jazzhall show", "rockbar show"]
        assert _titles(c.get("/feed/venue/museum.ics")) == ["museum show"]


def test_a_tag_intersection_needs_every_tag(app_env):
    _seed({"jazzhall": ["music", "downtown"], "rockbar": ["music"], "museum": ["downtown"]})
    with _client() as c:
        assert _titles(c.get("/feed/tags/music+downtown.ics")) == ["jazzhall show"]
        assert _titles(c.get("/feed/tags/downtown+music.ics")) == ["jazzhall show"]


def test_known_tags_with_no_common_source_are_an_empty_calendar(app_env):
    _seed({"rockbar": ["music"], "museum": ["museums"]})
    with _client() as c:
        r = c.get("/feed/tags/music+museums.ics")
        assert r.status_code == 200 and _titles(r) == []


@pytest.mark.parametrize(
    "path",
    [
        "/feed/tags/nosuchtag.ics",
        "/feed/tags/music+nosuchtag.ics",
        "/feed/tags/Music.ics",
        "/feed/venue/nosuchsource.ics",
    ],
)
def test_an_unknown_tag_or_source_is_a_404(app_env, path):
    _seed({"rockbar": ["music"]})
    with _client() as c:
        assert c.get(path).status_code == 404


def test_calendar_names_and_prodid(app_env):
    _seed({"jazzhall": ["music", "downtown"]})
    with _client() as c:
        cal = Calendar.from_ical(c.get("/feed/tags/music+downtown.ics").content)
        assert str(cal["X-WR-CALNAME"]) == "Example Events — music + downtown"
        assert "Example Events — music + downtown" in str(cal["X-WR-CALDESC"])
        assert str(Calendar.from_ical(c.get("/feed/all.ics").content)["X-WR-CALNAME"]) == (
            "Example Events"
        )
        venue = Calendar.from_ical(c.get("/feed/venue/jazzhall.ics").content)
        assert str(venue["X-WR-CALNAME"]) == "Example Events — Jazzhall"
        assert str(cal["PRODID"]) == "-//happenhere//happenhere//EN"


def test_the_lookahead_boundary(app_env):
    app_env.feed_lookahead_days = 30
    _seed({"early": ["x"]}, days=29)
    _seed({"late": ["x"]}, days=31)
    with _client() as c:
        assert _titles(c.get("/feed/all.ics")) == ["early show"]
    app_env.feed_lookahead_days = 365
    with _client() as c:
        assert _titles(c.get("/feed/all.ics")) == ["early show", "late show"]


def test_lookahead_defaults_to_a_year():
    assert Config().feed_lookahead_days == 365


def test_etag_round_trip(app_env):
    _seed({"rockbar": ["music"]})
    with _client() as c:
        r = c.get("/feed/tags/music.ics")
        etag = r.headers["etag"]
        assert r.headers["cache-control"] == "public, max-age=900"
        assert c.get("/feed/tags/music.ics").headers["etag"] == etag  # same rows, same bytes
        r304 = c.get("/feed/tags/music.ics", headers={"If-None-Match": etag})
        assert r304.status_code == 304 and r304.content == b""
        assert r304.headers["etag"] == etag
        assert (
            c.get("/feed/tags/music.ics", headers={"If-None-Match": '"stale"'}).status_code == 200
        )
        _seed({"jazzhall": ["music"]})  # the feed changed
        r2 = c.get("/feed/tags/music.ics", headers={"If-None-Match": etag})
        assert r2.status_code == 200 and r2.headers["etag"] != etag


def test_the_feeds_page_renders_every_feed_with_an_event(app_env):
    _seed({"jazzhall": ["music", "downtown"], "rockbar": ["music"]})
    with db.get_session() as s:
        s.add(Source(name="quiet", display_name="Quiet Place", type="ical", tags='["calm"]'))
        s.commit()
    with _client() as c:
        r = c.get("/feeds")
    assert r.status_code == 200
    html = r.text
    for path in (
        "/feed/all.ics",
        "/feed/tags/music.ics",
        "/feed/tags/downtown.ics",
        "/feed/venue/jazzhall.ics",
        "/feed/venue/rockbar.ics",
    ):
        assert f"https://testserver{path}" in html
        assert f"webcal://testserver{path}" in html
        assert "https://calendar.google.com/calendar/r?cid=" + quote(
            f"webcal://testserver{path}", safe=""
        ) in html.replace("&amp;", "&")
    assert "calm" not in html and "Quiet Place" not in html  # no events, no feed
    assert html.count("copy-btn") >= 5


def test_the_feeds_page_renders_with_no_events(app_env):
    with _client() as c:
        r = c.get("/feeds")
    assert r.status_code == 200 and "no feed to subscribe to" in r.text


def test_the_feeds_page_uses_public_url(app_env):
    app_env.public_url = "https://events.example.org/"
    _seed({"rockbar": ["music"]})
    with _client() as c:
        assert "webcal://events.example.org/feed/all.ics" in c.get("/feeds").text


# --- Tags on a source (spec point 1) ------------------------------------------------------


def _admin(c, cfg):
    c.cookies.set(
        oidc.SESSION_COOKIE, auth.create_session("admin", cfg.secret_key, cfg.password_hash)
    )


def test_the_admin_form_stores_tags(app_env):
    with _client() as c:
        _admin(c, app_env)
        form = {"display_name": "Jazz Hall", "scraper_type": "ical", "url": "", "enabled": "on"}
        r = c.post("/admin/sources", data={**form, "tags": "Music, downtown, music"})
        assert r.status_code == 302
        with db.get_session() as s:
            assert json.loads(s.query(Source).filter_by(name="jazz_hall").one().tags) == [
                "music",
                "downtown",
            ]
        assert "music, downtown" in c.get("/admin/sources/jazz_hall/edit").text
        r = c.post("/admin/sources/jazz_hall/update", data={**form, "tags": "late night"})
        assert r.status_code == 400
        r = c.post("/admin/sources/jazz_hall/update", data={**form, "tags": "late-night"})
        assert r.status_code == 302
        with db.get_session() as s:
            assert s.query(Source).filter_by(name="jazz_hall").one().tags == '["late-night"]'


def test_an_existing_database_gains_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE NOT NULL,"
        " display_name VARCHAR, type VARCHAR, url VARCHAR, enabled BOOLEAN NOT NULL DEFAULT 1,"
        " last_scraped DATETIME);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL,"
        " dedup_hash VARCHAR(64) UNIQUE NOT NULL, title VARCHAR NOT NULL, description TEXT,"
        " start_dt DATETIME NOT NULL,"
        " end_dt DATETIME, venue_name VARCHAR, venue_addr VARCHAR, url VARCHAR, first_seen DATETIME,"
        " last_seen DATETIME, notified BOOLEAN NOT NULL DEFAULT 0);"
        "INSERT INTO sources (id, name, type) VALUES (1, 'old', 'ical');"
        "INSERT INTO events (source_id, dedup_hash, title, start_dt)"
        " VALUES (1, 'abc', 'Old Show', '2099-01-01 19:00:00');"
    )
    con.commit()
    con.close()
    db.init_db(path)
    con = sqlite3.connect(path)
    event_cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    source_cols = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
    indexes = {r[1] for r in con.execute("PRAGMA index_list(events)")}
    con.close()
    assert {"identity_key", "uid"} <= event_cols and "tags" in source_cols
    assert {"ix_events_identity_key", "ix_events_uid"} <= indexes
    db.init_db(path)  # idempotent
    with db.get_session() as s:
        assert _uids(generate_ics(s, now=datetime(2098, 12, 31))) == ["abc@peakevents"]
