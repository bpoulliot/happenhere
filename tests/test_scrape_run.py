"""Item 18: one source's batch never aborts the scrape run, and two same-title shows both stay."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import db
from app.config import Config
from app.dedup import upsert_events
from app.jobs import run_all_scrapers, run_single_scraper
from app.models import Event, Source
from app.scrapers import registry
from app.scrapers.base import BaseScraper, EventData

DAY = (datetime.now() + timedelta(days=12)).replace(second=0, microsecond=0)
EARLY, LATE = DAY.replace(hour=19, minute=0), DAY.replace(hour=21, minute=30)

BATCHES: dict[str, list[EventData]] = {}


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item18"

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
    db.init_db(tmp_path / "run.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: Config())
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    registry.register_type("fake_item18", _Fake, "Fake")
    yield
    registry.unregister_type("fake_item18")
    BATCHES.clear()


def _add_source(name: str, url: str) -> None:
    with db.get_session() as s:
        s.add(Source(name=name, display_name=name, type="fake_item18", url=url, enabled=True))
        s.commit()


def _show(start, url=None, title="Synthetic Comic", venue="Club"):
    return EventData(title=title, start_dt=start, venue_name=venue, url=url)


def _stored():
    with db.get_session() as s:
        return sorted((e.title, e.start_dt, e.url) for e in s.query(Event).all())


def test_two_same_title_shows_on_one_day_do_not_abort_the_run(run):
    """The early and the late show share the dedup hash (title + date + venue)."""
    _add_source("club", "https://club.example.com/shows")
    _add_source("hall", "https://hall.example.com/events")
    BATCHES["https://club.example.com/shows"] = [
        _show(EARLY, "https://club.example.com/shows/1"),
        _show(LATE, "https://club.example.com/shows/2"),
    ]
    BATCHES["https://hall.example.com/events"] = [_show(EARLY, title="Quartet", venue="Hall")]

    run_all_scrapers()

    stored = _stored()
    print(f"\nstored after the run: {stored}")
    assert ("Quartet", EARLY, None) in stored, "the run stopped before the next source"
    assert ("Synthetic Comic", EARLY, "https://club.example.com/shows/1") in stored
    assert ("Synthetic Comic", LATE, "https://club.example.com/shows/2") in stored
    with db.get_session() as s:
        club = s.query(Source).filter_by(name="club").one()
        assert club.error_count == 0 and club.last_error is None


def test_both_shows_survive_a_second_run_unchanged(run):
    _add_source("club", "https://club.example.com/shows")
    BATCHES["https://club.example.com/shows"] = [
        _show(EARLY, "https://club.example.com/shows/1"),
        _show(LATE, "https://club.example.com/shows/2"),
    ]
    run_all_scrapers()
    first = _stored()
    run_all_scrapers()
    assert _stored() == first and len(first) == 2


def test_the_same_event_twice_in_one_batch_is_one_row(run):
    """No identity apart from the hash: the two entries are one event, merged."""
    with db.get_session() as s:
        src = Source(name="club", display_name="club", type="fake_item18", url="https://x.example")
        s.add(src)
        s.commit()
        new, updated = upsert_events(s, [_show(EARLY), _show(EARLY)], src.id)
        assert (new, updated) == (1, 1)
        assert s.query(Event).count() == 1


def test_a_failing_source_is_recorded_and_the_run_goes_on(run, monkeypatch):
    """Whatever fails inside one source's commit, the session is rolled back for the next."""
    _add_source("broken", "https://broken.example.com/")
    _add_source("hall", "https://hall.example.com/events")
    BATCHES["https://broken.example.com/"] = [_show(EARLY, title="Will Fail", venue="Broken")]
    BATCHES["https://hall.example.com/events"] = [_show(EARLY, title="Quartet", venue="Hall")]

    import app.jobs as jobs

    real = jobs.upsert_events

    def flaky(session, events, source_id, **kw):
        if events and events[0].title == "Will Fail":
            # A row the database refuses, flushed: the session is now in a failed transaction.
            session.add(Event(source_id=source_id, dedup_hash=None, title="x", start_dt=EARLY))
            session.flush()
        return real(session, events, source_id, **kw)

    monkeypatch.setattr(jobs, "upsert_events", flaky)
    run_all_scrapers()

    assert ("Quartet", EARLY, None) in _stored()
    with db.get_session() as s:
        broken = s.query(Source).filter_by(name="broken").one()
        assert broken.error_count == 1 and "NOT NULL" in broken.last_error
        assert s.query(Event).filter_by(title="Will Fail").count() == 0


def test_run_single_scraper_reports_the_error_instead_of_raising(run, monkeypatch):
    _add_source("broken", "https://broken.example.com/")
    BATCHES["https://broken.example.com/"] = [_show(EARLY, title="Will Fail", venue="Broken")]
    import app.jobs as jobs

    def failing(session, events, source_id, **kw):
        session.add(Event(source_id=source_id, dedup_hash=None, title="x", start_dt=EARLY))
        session.flush()

    monkeypatch.setattr(jobs, "upsert_events", failing)
    result = run_single_scraper("broken")
    assert "NOT NULL" in result["error"]
    with db.get_session() as s:
        assert s.query(Source).filter_by(name="broken").one().error_count == 1
