"""Item 25: a same-title show first seen in a later run gets its own row; vanished rows retire.

Every source here is a fake scraper over a synthetic batch; nothing fetches a site.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar
from sqlalchemy import text

from app import auth, config, db, digest, ntfy
from app.config import Config
from app.dedup import upsert_events
from app.jobs import run_all_scrapers, run_single_scraper
from app.main import app
from app.models import Event, Source
from app.scrapers import registry
from app.scrapers.base import BaseScraper, EventData

DAY = (datetime.now() + timedelta(days=12)).replace(second=0, microsecond=0)
EARLY, LATE = DAY.replace(hour=19, minute=0), DAY.replace(hour=21, minute=30)
CLUB = "https://club.example.com/shows"
EARLY_URL, LATE_URL = f"{CLUB}/101", f"{CLUB}/102"

BATCHES: dict[str, list[EventData] | Exception] = {}


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item25"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.url = source_url

    def scrape(self) -> list[EventData]:
        result = BATCHES[self.url]
        if isinstance(result, Exception):
            raise result
        return list(result)


@pytest.fixture
def run(monkeypatch, tmp_path):
    db.init_db(tmp_path / "retire.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: Config())
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    registry.register_type("fake_item25", _Fake, "Fake")
    yield
    registry.unregister_type("fake_item25")
    BATCHES.clear()


def _add_source(name: str, url: str) -> None:
    with db.get_session() as s:
        s.add(Source(name=name, display_name=name, type="fake_item25", url=url, enabled=True))
        s.commit()


def _show(start, url=None, title="Synthetic Comic", venue="Club"):
    return EventData(title=title, start_dt=start, venue_name=venue, url=url)


def _rows() -> dict[str, dict]:
    """Every stored row by its URL (or title, when it has none)."""
    with db.get_session() as s:
        return {
            (e.url or e.title): {
                "id": e.id,
                "uid": e.uid,
                "title": e.title,
                "start": e.start_dt,
                "url": e.url,
                "retired": getattr(e, "retired_at", None) is not None,
            }
            for e in s.query(Event).all()
        }


def _scrape(name: str, batch) -> dict:
    url = {"club": CLUB}.get(name, f"https://{name}.example.com/events")
    BATCHES[url] = batch
    return run_single_scraper(name)


# --- ROADMAP 25's run 1-4 sequence ----------------------------------------------------------------


def test_runs_one_to_four(run):
    _add_source("club", CLUB)

    # Run 1: only the early show is listed.
    assert _scrape("club", [_show(EARLY, EARLY_URL)])["new"] == 1
    first = _rows()
    assert list(first) == [EARLY_URL]

    # Run 2: the late show was added. Listed first, it used to fold into the early row.
    _scrape("club", [_show(LATE, LATE_URL), _show(EARLY, EARLY_URL)])
    second = _rows()
    print(f"\nafter run 2: {second}")
    assert set(second) == {EARLY_URL, LATE_URL}, "the late show has no row of its own"
    assert second[EARLY_URL] == first[EARLY_URL], "the early row changed"
    assert second[LATE_URL]["uid"] != second[EARLY_URL]["uid"]
    assert second[LATE_URL]["start"] == LATE

    # Run 3: the early show is gone from the listing: retired, not deleted, and out of the feed.
    _scrape("club", [_show(LATE, LATE_URL)])
    third = _rows()
    assert third[EARLY_URL]["retired"] and not third[LATE_URL]["retired"]
    with db.get_session() as s:
        from app.ical_export import generate_ics

        assert _uids(generate_ics(s)) == [third[LATE_URL]["uid"]]

    # Run 4: it is listed again: un-retired, the same row and the same UID.
    _scrape("club", [_show(EARLY, EARLY_URL), _show(LATE, LATE_URL)])
    fourth = _rows()
    assert not fourth[EARLY_URL]["retired"]
    assert fourth[EARLY_URL] == first[EARLY_URL] | {"retired": False}


def test_a_late_show_first_seen_alone_gets_its_own_row(run):
    """The late show is the only one listed in run 2: it must not take over the early row."""
    _add_source("club", CLUB)
    _scrape("club", [_show(EARLY, EARLY_URL)])
    early = _rows()[EARLY_URL]
    _scrape("club", [_show(LATE, LATE_URL)])
    rows = _rows()
    assert set(rows) == {EARLY_URL, LATE_URL}, "the late show folded into the early row"
    assert rows[EARLY_URL] | {"retired": False} == early
    assert rows[LATE_URL]["start"] == LATE and rows[LATE_URL]["uid"] != early["uid"]
    assert rows[EARLY_URL]["retired"], "run 2 succeeded without the early show"


# --- what retires nothing ----------------------------------------------------------------------------


def test_a_failed_scrape_retires_nothing(run):
    _add_source("club", CLUB)
    _scrape("club", [_show(EARLY, EARLY_URL), _show(LATE, LATE_URL)])
    assert "error" in _scrape("club", RuntimeError("the site is down"))
    assert not any(r["retired"] for r in _rows().values())


def test_a_zero_event_scrape_retires_nothing(run):
    _add_source("club", CLUB)
    _scrape("club", [_show(EARLY, EARLY_URL), _show(LATE, LATE_URL)])
    result = _scrape("club", [])
    assert result["total"] == 0 and "error" not in result
    assert not any(r["retired"] for r in _rows().values())


def test_a_past_row_is_never_retired(run):
    _add_source("club", CLUB)
    past = datetime.now().replace(second=0, microsecond=0) - timedelta(days=2)
    _scrape("club", [_show(past, f"{CLUB}/1"), _show(EARLY, EARLY_URL)])
    _scrape("club", [_show(EARLY, EARLY_URL)])
    assert not _rows()[f"{CLUB}/1"]["retired"]


def test_an_unchanged_event_is_never_retired(run):
    """A batch matched on the dedup hash alone (two events sharing one URL) is still returned."""
    _add_source("club", CLUB)
    batch = [_show(EARLY, f"{CLUB}/series", title="A"), _show(LATE, f"{CLUB}/series", title="B")]
    _scrape("club", batch)
    _scrape("club", batch)
    with db.get_session() as s:
        assert [getattr(e, "retired_at", None) for e in s.query(Event)] == [None, None]


def test_a_retired_row_returned_by_a_full_run_keeps_its_uid(run):
    _add_source("club", CLUB)
    BATCHES[CLUB] = [_show(EARLY, EARLY_URL), _show(LATE, LATE_URL)]
    run_all_scrapers()
    uid = _rows()[EARLY_URL]["uid"]
    BATCHES[CLUB] = [_show(LATE, LATE_URL)]
    run_all_scrapers()
    assert _rows()[EARLY_URL]["retired"]
    BATCHES[CLUB] = [_show(EARLY, EARLY_URL), _show(LATE, LATE_URL)]
    run_all_scrapers()
    assert _rows()[EARLY_URL] | {"id": 0} == {
        "id": 0,
        "uid": uid,
        "title": "Synthetic Comic",
        "start": EARLY,
        "url": EARLY_URL,
        "retired": False,
    }


# --- cross-source ---------------------------------------------------------------------------------


def test_a_cross_source_duplicate_still_merges(run):
    _add_source("club", CLUB)
    _add_source("tickets", "https://tickets.example.com/events")
    BATCHES[CLUB] = [_show(EARLY, EARLY_URL)]
    BATCHES["https://tickets.example.com/events"] = [
        _show(EARLY, "https://tickets.example.com/e/9")
    ]
    run_all_scrapers()
    with db.get_session() as s:
        assert s.query(Event).count() == 1


def test_a_row_another_source_returned_this_run_is_not_retired(run):
    """The row belongs to the source scraped second; the first source returned it this run."""
    _add_source("tickets", "https://tickets.example.com/events")  # scraped first
    _add_source("club", CLUB)
    other = _show(EARLY + timedelta(days=1), f"{CLUB}/200", title="Other")
    unrelated = _show(LATE, "https://tickets.example.com/e/1", title="Ticketed", venue="Arena")
    BATCHES["https://tickets.example.com/events"] = [unrelated]
    BATCHES[CLUB] = [_show(EARLY, EARLY_URL), other]
    run_all_scrapers()
    early_id = _rows()[EARLY_URL]["id"]
    with db.get_session() as s:
        assert s.get(Event, early_id).source.name == "club"  # the premise

    BATCHES["https://tickets.example.com/events"] = [
        _show(EARLY, "https://tickets.example.com/e/9"),
        unrelated,
    ]
    BATCHES[CLUB] = [other]  # the club no longer lists it; the ticket site still does
    run_all_scrapers()
    by_id = {r["id"]: r for r in _rows().values()}
    assert not by_id[early_id]["retired"]


def test_a_same_source_row_with_no_identity_key_still_merges(run):
    """A row stored before item 03 has no identity key: a retitle still lands on it."""
    _add_source("club", CLUB)
    _scrape("club", [_show(EARLY, EARLY_URL)])
    with db.get_session() as s:
        s.execute(text("UPDATE events SET identity_key = NULL"))
        s.commit()
    _scrape("club", [_show(EARLY, f"{CLUB}/moved")])
    with db.get_session() as s:
        assert s.query(Event).count() == 1


# --- every consumer skips a retired row --------------------------------------------------------------


def _uids(ics: bytes) -> list[str]:
    return [str(c["UID"]) for c in Calendar.from_ical(ics).walk() if c.name == "VEVENT"]


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """The app on a temp database with one live and one retired event; never the lifespan."""
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yml"))
    db.init_db(tmp_path / "app.db")
    cfg = Config(
        secret_key="k" * 64,
        password_hash=auth.hash_password("pw"),
        ntfy_url="https://ntfy.example.com",
        ntfy_topic="events",
        city_name="Exampleton",
    )
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    posts: list[str] = []
    monkeypatch.setattr(
        ntfy, "post_to_ntfy", lambda c, title, body, **kw: (posts.append(body), (True, None))[1]
    )
    cfg.posts = posts  # type: ignore[attr-defined]
    when = (datetime.now() + timedelta(days=3)).replace(hour=20, minute=0, second=0, microsecond=0)
    with db.get_session() as s:
        src = Source(name="hall", display_name="Hall", type="ical", url="https://hall.example.com")
        s.add(src)
        s.commit()
        upsert_events(
            s,
            [
                EventData("Still On", when, "Hall", url="https://hall.example.com/e/1"),
                EventData("Called Off", when, "Hall", url="https://hall.example.com/e/2"),
            ],
            src.id,
        )
    return cfg


def _retire(title: str) -> None:
    with db.get_session() as s:
        s.execute(
            text("UPDATE events SET retired_at = :t WHERE title = :x"), {"t": DAY, "x": title}
        )
        s.commit()


def _client():
    return nullcontext(TestClient(app, base_url="https://testserver", follow_redirects=False))


def _feed_titles(content: bytes) -> list[str]:
    return sorted(
        str(c["SUMMARY"]) for c in Calendar.from_ical(content).walk() if c.name == "VEVENT"
    )


def test_the_feeds_skip_a_retired_row(app_env):
    _retire("Called Off")
    with _client() as c:
        for path in ("/feed/events.ics", "/feed/all.ics", "/feed/venue/hall.ics"):
            r = c.get(path)
            assert r.status_code == 200
            assert _feed_titles(r.content) == ["Still On"], path


def test_the_events_page_skips_a_retired_row(app_env):
    _retire("Called Off")
    with _client() as c:
        for path in ("/events", "/partials/event-list", "/events/calendar"):
            r = c.get(path, params={"month": DAY.strftime("%Y-%m")} if "calendar" in path else {})
            assert r.status_code == 200
            assert "Still On" in r.text or "calendar" in path, path
            assert "Called Off" not in r.text, path


def test_the_api_skips_a_retired_row(app_env):
    _retire("Called Off")
    with _client() as c:
        r = c.get("/api/events")
    assert r.status_code == 200
    assert [e["title"] for e in r.json()["events"]] == ["Still On"]


def test_the_digest_skips_a_retired_row_and_a_retirement_alone_sends_nothing(app_env):
    assert digest.run_digest().status == "sent"
    assert "Called Off" in app_env.posts[-1]
    _retire("Called Off")
    with db.get_session() as s:
        d = digest.decide(s, app_env)
    assert [e.line.split(" — ")[0] for e in d.entries] == ["• Still On"]
    assert digest.run_digest().status == "skipped-unchanged"  # a removal, not news
    assert len(app_env.posts) == 1


# --- the one filter -------------------------------------------------------------------------------


def test_every_event_window_query_goes_through_the_one_filter():
    """A query on ``start_dt`` outside ``app/models.py`` could forget ``retired_at``."""
    offenders = []
    for path in Path("app").rglob("*.py"):
        if path.as_posix() in ("app/models.py", "app/dedup.py") or "scrapers" in path.parts:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.filter\(.*\b(Event|EventRow)\.start_dt\b", line):
                offenders.append(f"{path}:{n}: {line.strip()}")
    assert offenders == []


def test_an_existing_database_gains_retired_at(tmp_path, caplog):
    import logging
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE NOT NULL,"
        " display_name VARCHAR, type VARCHAR, url VARCHAR, enabled BOOLEAN NOT NULL DEFAULT 1,"
        " last_scraped DATETIME);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL,"
        " dedup_hash VARCHAR(64) UNIQUE NOT NULL, title VARCHAR NOT NULL,"
        " description TEXT, start_dt DATETIME NOT NULL, end_dt DATETIME, venue_name VARCHAR,"
        " venue_addr VARCHAR, url VARCHAR, first_seen DATETIME, last_seen DATETIME,"
        " notified BOOLEAN NOT NULL DEFAULT 0);"
        "INSERT INTO sources (id, name, type) VALUES (1, 'old', 'ical');"
        "INSERT INTO events (source_id, dedup_hash, title, start_dt)"
        " VALUES (1, 'abc', 'Old Show', '2099-01-01 19:00:00');"
    )
    con.commit()
    con.close()
    with caplog.at_level(logging.INFO, logger="app.db"):
        db.init_db(path)
    assert "Schema: added events.retired_at" in caplog.text
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    retired = con.execute("SELECT retired_at FROM events").fetchall()
    con.close()
    assert "retired_at" in cols and retired == [(None,)]
    db.init_db(path)  # idempotent
