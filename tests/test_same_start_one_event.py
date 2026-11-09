"""Item 28: one batch listing the same show twice (same title, venue and exact start) under two ids.

A ticketing platform can list one show under two event ids. Before item 28 the second id made the
show a "second show of the day" (item 18): a row of its own under ``timed_hash``, i.e. the same
show twice in the feed. Two shows of one title on one day keep their own rows only when their
start times differ. Every source here is a fake scraper over a synthetic batch.
"""

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
CLUB = "https://club.example.com/shows"
TICKETS = "https://tickets.example.com/events"

BATCHES: dict[str, list[EventData]] = {}


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item28"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.url = source_url

    def scrape(self) -> list[EventData]:
        return list(BATCHES[self.url])


@pytest.fixture
def run(monkeypatch, tmp_path):
    db.init_db(tmp_path / "same_start.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: Config())
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    registry.register_type("fake_item28", _Fake, "Fake")
    yield
    registry.unregister_type("fake_item28")
    BATCHES.clear()


def _add_source(name: str, url: str) -> int:
    with db.get_session() as s:
        src = Source(name=name, display_name=name, type="fake_item28", url=url, enabled=True)
        s.add(src)
        s.commit()
        return src.id


def _show(start, uid, title="Synthetic Comic", venue="Club"):
    return EventData(
        title=title, start_dt=start, venue_name=venue, url=f"{TICKETS}/e/{uid}", source_uid=uid
    )


def _shown() -> list[tuple]:
    with db.get_session() as s:
        return sorted(
            (e.title, e.start_dt, e.venue_name)
            for e in s.query(Event).filter(Event.retired_at.is_(None))
        )


def test_two_ids_for_one_show_in_one_batch_are_one_row(run):
    src_id = _add_source("tickets", TICKETS)
    with db.get_session() as s:
        new, updated = upsert_events(s, [_show(EARLY, "A1"), _show(EARLY, "B2")], src_id)
    assert (new, updated) == (1, 1)
    assert _shown() == [("Synthetic Comic", EARLY, "Club")]


def test_two_ids_for_one_show_merge_into_another_sources_row(run):
    """The regional case: the venue's own source stored the show; the regional batch has it twice."""
    _add_source("club", CLUB)
    _add_source("tickets", TICKETS)
    BATCHES[CLUB] = [_show(EARLY, "A1")]
    BATCHES[TICKETS] = [_show(EARLY, "A1"), _show(EARLY, "B2")]
    run_all_scrapers()
    assert _shown() == [("Synthetic Comic", EARLY, "Club")]


def test_the_merged_id_is_returned_and_nothing_retires(run):
    _add_source("tickets", TICKETS)
    BATCHES[TICKETS] = [_show(EARLY, "A1"), _show(EARLY, "B2")]
    assert run_single_scraper("tickets")["new"] == 1
    result = run_single_scraper("tickets")
    assert (result["new"], result["retired"]) == (0, 0)
    assert _shown() == [("Synthetic Comic", EARLY, "Club")]


def test_two_ids_at_two_times_stay_two_shows(run):
    """Item 18's rule is unchanged: a different start is another show."""
    src_id = _add_source("tickets", TICKETS)
    with db.get_session() as s:
        new, _ = upsert_events(s, [_show(EARLY, "A1"), _show(LATE, "B2")], src_id)
    assert new == 2
    assert _shown() == [("Synthetic Comic", EARLY, "Club"), ("Synthetic Comic", LATE, "Club")]


def test_same_start_at_another_venue_is_another_event(run):
    src_id = _add_source("tickets", TICKETS)
    with db.get_session() as s:
        new, _ = upsert_events(
            s, [_show(EARLY, "A1"), _show(EARLY, "B2", venue="Other Hall")], src_id
        )
    assert new == 2


def test_same_start_and_venue_with_another_title_is_another_event(run):
    src_id = _add_source("tickets", TICKETS)
    with db.get_session() as s:
        new, _ = upsert_events(
            s, [_show(EARLY, "A1"), _show(EARLY, "B2", title="Another Comic")], src_id
        )
    assert new == 2
