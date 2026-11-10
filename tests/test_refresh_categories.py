"""ROADMAP 34: a re-scraped event's categories reach its stored row.

``dedup._refresh`` updated title, description, end, address, URL and image of a seen row, never
its categories: a row kept the categories of its FIRST scrape. A source given a new category
(the operator adding ``hockey`` to a team's source) or a platform that re-labels an event never
changed the stored row, so the category filter kept missing it.

Rule: the row's OWN source replaces the row's categories with the scrape's (when the scrape has
any); ANOTHER source that merges into the row (same dedup hash) adds its categories, never
removing the owner's. A scrape without categories changes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app import db
from app.config import Config
from app.jobs import run_single_scraper
from app.models import Event, Source
from app.scrapers import registry
from app.scrapers.base import BaseScraper, EventData

START = (datetime.now() + timedelta(days=9)).replace(hour=19, minute=0, second=0, microsecond=0)
BATCHES: dict[str, list[EventData]] = {}


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item34"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.url = source_url

    def scrape(self) -> list[EventData]:
        return [
            EventData(**{**e.__dict__, "categories": list(e.categories)}) for e in BATCHES[self.url]
        ]


@pytest.fixture
def run(monkeypatch, tmp_path):
    db.init_db(tmp_path / "cats.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: Config())
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    registry.register_type("fake_item34", _Fake, "Fake")
    yield
    registry.unregister_type("fake_item34")
    BATCHES.clear()


def _source(name: str, categories: list[str]) -> str:
    url = f"https://{name}.example.com/events"
    with db.get_session() as s:
        s.add(
            Source(
                name=name,
                display_name=name,
                type="fake_item34",
                url=url,
                enabled=True,
                categories=json.dumps(categories),
            )
        )
        s.commit()
    return url


def _set_source_categories(name: str, categories: list[str]) -> None:
    with db.get_session() as s:
        s.query(Source).filter_by(name=name).one().categories = json.dumps(categories)
        s.commit()


def _game(categories=None, uid="game-1", title="Team vs Visitors") -> EventData:
    return EventData(
        title=title,
        start_dt=START,
        venue_name="Arena",
        url="https://team.example.com/schedule",
        source_uid=uid,
        categories=list(categories or []),
    )


def _cats() -> list[list[str]]:
    with db.get_session() as s:
        return [json.loads(e.categories or "[]") for e in s.query(Event).order_by(Event.id)]


def test_a_new_source_category_reaches_the_existing_row(run):
    url = _source("team", ["sports"])
    BATCHES[url] = [_game()]
    run_single_scraper("team")
    assert _cats() == [["sports"]]
    _set_source_categories("team", ["sports", "hockey"])
    result = run_single_scraper("team")
    assert result["new"] == 0 and result["updated"] == 1
    assert _cats() == [["sports", "hockey"]]


def test_the_events_own_new_categories_replace_the_rows(run):
    url = _source("tickets", [])
    BATCHES[url] = [_game(["Sports", "Basketball"])]
    run_single_scraper("tickets")
    BATCHES[url] = [_game(["Sports", "Hockey"])]
    run_single_scraper("tickets")
    assert _cats() == [["Sports", "Hockey"]]


def test_a_scrape_without_categories_keeps_the_rows(run):
    url = _source("bare", [])
    BATCHES[url] = [_game(["music"])]
    run_single_scraper("bare")
    BATCHES[url] = [_game([])]
    run_single_scraper("bare")
    assert _cats() == [["music"]]


def test_another_source_merging_in_adds_its_categories_and_removes_none(run):
    owner = _source("owner", ["music"])
    other = _source("other", ["comedy"])
    BATCHES[owner] = [_game(uid=None)]
    BATCHES[other] = [_game(uid=None)]  # same title, day and venue: the same event (dedup hash)
    run_single_scraper("owner")
    result = run_single_scraper("other")
    assert result["new"] == 0
    assert _cats() == [["music", "comedy"]]
    run_single_scraper("owner")  # the owner's re-scrape replaces with its own, the merge re-adds
    run_single_scraper("other")
    assert _cats() == [["music", "comedy"]]


def test_a_merge_does_not_repeat_a_category(run):
    owner = _source("owner", ["Sports", "Hockey"])
    other = _source("other", ["sports"])
    BATCHES[owner] = [_game(uid=None)]
    BATCHES[other] = [_game(uid=None)]
    run_single_scraper("owner")
    run_single_scraper("other")
    assert _cats() == [["Sports", "Hockey"]]  # "sports" is already there, case aside
