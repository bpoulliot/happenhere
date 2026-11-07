"""Item 15: a re-scraped event whose time moved keeps its row and its feed UID.

Every source here is a fake scraper or a direct ``upsert_events`` call over a synthetic batch;
nothing fetches a site.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from icalendar import Calendar

from app import db
from app.config import Config
from app.dedup import identity, upsert_events
from app.ical_export import generate_ics
from app.jobs import run_single_scraper
from app.models import Event, Source
from app.scrapers import registry
from app.scrapers.base import BaseScraper, EventData

DAY = (datetime.now() + timedelta(days=9)).replace(second=0, microsecond=0)
SEVEN, NINE = DAY.replace(hour=19, minute=0), DAY.replace(hour=21, minute=0)
LISTING = "https://room.example.com/calendar"


def _ev(start, title="Synthetic Trio", venue="Room", **kw) -> EventData:
    return EventData(title=title, start_dt=start, venue_name=venue, **kw)


def _vevents(ics: bytes) -> list[tuple[str, datetime]]:
    cal = Calendar.from_ical(ics)
    return [
        (str(c["UID"]), c["DTSTART"].dt.replace(tzinfo=None))
        for c in cal.walk()
        if c.name == "VEVENT"
    ]


# --- direct upserts -------------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    db.init_db(tmp_path / "moved.db")
    with db.get_session() as s:
        yield s


def _source(session, name="room", url=LISTING) -> Source:
    src = Source(name=name, display_name=name.title(), type="ical", url=url)
    session.add(src)
    session.commit()
    return src


def test_roadmap_15_reproduction_the_start_time_moves(session):
    """No URL, no UID: two upserts of one event, 19:00 then 21:00 (ROADMAP 15)."""
    src = _source(session)
    assert identity(_ev(SEVEN), src)[0] == "slot"
    upsert_events(session, [_ev(SEVEN)], src.id)
    uid = session.query(Event).one().uid
    upsert_events(session, [_ev(NINE)], src.id)
    rows = [(e.start_dt, e.uid) for e in session.query(Event)]
    print(f"\nrows after the move: {rows}")
    assert rows == [(NINE, uid)], "the moved event is not one row at 21:00 with its first UID"


def test_the_feed_carries_the_moved_event_once_with_its_uid_and_new_start(session):
    src = _source(session)
    upsert_events(session, [_ev(SEVEN)], src.id)
    before = _vevents(generate_ics(session))
    upsert_events(session, [_ev(NINE)], src.id)
    after = _vevents(generate_ics(session))
    print(f"\nfeed before: {before}\nfeed after:  {after}")
    assert [d for _, d in before] == [SEVEN]
    assert after == [(before[0][0], NINE)], "a ghost, a duplicate or a new UID in the feed"


def test_an_early_and_a_late_slot_show_listed_together_stay_two_rows(session):
    """Rule 2 with slot keys: the stored 19:00 show is still listed, so 21:00 is another show."""
    src = _source(session)
    upsert_events(session, [_ev(SEVEN)], src.id)
    early = session.query(Event).one()
    early_uid = early.uid
    upsert_events(session, [_ev(NINE), _ev(SEVEN)], src.id)
    rows = {e.start_dt: e.uid for e in session.query(Event)}
    assert rows.keys() == {SEVEN, NINE}, "the late show took over the early row"
    assert rows[SEVEN] == early_uid and rows[NINE] != early_uid


def test_a_hash_fallback_event_takes_its_new_start(session):
    """No usable key on either side (a series under one URL): a dedup-hash match moves the time."""
    src = _source(session)
    url = "https://room.example.com/series/weekly"
    week = timedelta(days=7)
    upsert_events(session, [_ev(SEVEN, url=url), _ev(SEVEN + week, url=url)], src.id)
    assert all(e.identity_key is None for e in session.query(Event))
    uids = {e.start_dt.date(): e.uid for e in session.query(Event)}
    upsert_events(session, [_ev(NINE, url=url), _ev(SEVEN + week, url=url)], src.id)
    rows = {e.start_dt.date(): (e.start_dt, e.uid) for e in session.query(Event)}
    assert rows == {
        SEVEN.date(): (NINE, uids[SEVEN.date()]),
        (SEVEN + week).date(): (SEVEN + week, uids[(SEVEN + week).date()]),
    }


def test_a_cross_source_match_does_not_move_the_stored_start(session):
    first = _source(session, "room")
    other = _source(session, "listings", url="https://listings.example.com/events")
    upsert_events(session, [_ev(SEVEN)], first.id)
    upsert_events(session, [_ev(NINE)], other.id)
    rows = [(e.source_id, e.start_dt) for e in session.query(Event)]
    assert rows == [(first.id, SEVEN)], "another source moved the stored start time"


def test_a_url_identity_event_that_changed_its_url_is_not_a_moved_time(session):
    """Only a ``slot`` key changes with the time; a new URL or source UID is a new event (25)."""
    src = _source(session)
    upsert_events(session, [_ev(SEVEN, url="https://room.example.com/e/1")], src.id)
    upsert_events(session, [_ev(NINE, url="https://room.example.com/e/2")], src.id)
    assert sorted(e.start_dt for e in session.query(Event)) == [SEVEN, NINE]


# --- through a scrape run: the moved row is matched, so it does not retire --------------------------

BATCHES: dict[str, list[EventData]] = {}


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item15"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.url = source_url

    def scrape(self) -> list[EventData]:
        return list(BATCHES[self.url])


@pytest.fixture
def run(monkeypatch, tmp_path):
    db.init_db(tmp_path / "moved-run.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: Config())
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    registry.register_type("fake_item15", _Fake, "Fake")
    with db.get_session() as s:
        s.add(Source(name="room", display_name="Room", type="fake_item15", url=LISTING))
        s.commit()
    yield
    registry.unregister_type("fake_item15")
    BATCHES.clear()


def _scrape(batch) -> dict:
    BATCHES[LISTING] = batch
    return run_single_scraper("room")


def test_a_slot_event_moved_in_a_later_run_is_one_row_not_retired(run):
    _scrape([_ev(SEVEN)])
    with db.get_session() as s:
        first = s.query(Event).one()
        uid = first.uid
    result = _scrape([_ev(NINE)])
    print(f"\nrun 2: {result}")
    with db.get_session() as s:
        rows = [(e.start_dt, e.uid, e.retired_at) for e in s.query(Event)]
        feed = _vevents(generate_ics(s))
    assert rows == [(NINE, uid, None)], f"not one live row at 21:00 with its UID: {rows}"
    assert feed == [(uid, NINE)]
