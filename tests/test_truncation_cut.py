"""Item 26: a successful scrape retires only the source's rows that start before the latest
event it returned, so a list cut off at its far end retires nothing past the cut.

Every source here is a fake scraper over a synthetic batch, or a built-in type over a saved
synthetic page on an ``example.com`` host; nothing fetches a site.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app import db, locations, plugins
from app.config import Config
from app.dedup import upsert_events
from app.jobs import run_all_scrapers, run_single_scraper
from app.models import Event, EventLocation, SearchLocation, Source
from app.scrapers import _spothopper_base, eventbrite, registry
from app.scrapers.base import BaseScraper, EventData

NOW = datetime.now().replace(second=0, microsecond=0)
CLUB = "https://club.example.com/shows"
HALL = "https://hall.example.com/events"

BATCHES: dict[str, list[EventData] | Exception] = {}
AREA_BATCHES: dict[str, list[EventData]] = {}  # by search-location label


def _day(n: int, hour: int = 19, minute: int = 0) -> datetime:
    return (NOW + timedelta(days=n)).replace(hour=hour, minute=minute)


def _show(start, url, title="Synthetic Comic", venue="Club") -> EventData:
    return EventData(title=title, start_dt=start, venue_name=venue, url=url)


def _at(n: int, base: str = CLUB, **kw) -> EventData:
    """One show on day ``n`` at 19:00 with its own URL."""
    return _show(_day(n), f"{base}/{n}", **kw)


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item26"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.url = source_url

    def scrape(self) -> list[EventData]:
        result = BATCHES[self.url]
        if isinstance(result, Exception):
            raise result
        return list(result)


class _Aware(BaseScraper):
    SOURCE_TYPE = "fake_item26_aware"
    LOCATION_AWARE = True

    def __init__(self, config, source_url: str = "", venue_name: str = "", location=None):
        super().__init__(config)
        self.location = location

    def scrape(self) -> list[EventData]:
        return list(AREA_BATCHES[self.location.label])


@pytest.fixture
def run(monkeypatch, tmp_path):
    db.init_db(tmp_path / "cut.db")
    monkeypatch.setattr("app.jobs.get_config", lambda: Config())
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    monkeypatch.setattr("app.jobs.canary.check", lambda: None)
    registry.register_type("fake_item26", _Fake, "Fake")
    registry.register_type("fake_item26_aware", _Aware, "Fake (areas)")
    yield
    registry.unregister_type("fake_item26")
    registry.unregister_type("fake_item26_aware")
    BATCHES.clear()
    AREA_BATCHES.clear()


def _add_source(name: str, url: str, type_: str = "fake_item26") -> None:
    with db.get_session() as s:
        s.add(Source(name=name, display_name=name, type=type_, url=url, enabled=True))
        s.commit()


def _scrape(name: str, url: str, batch) -> dict:
    BATCHES[url] = batch
    return run_single_scraper(name)


def _retired() -> dict[str, bool]:
    """Every stored row's URL -> whether it is retired."""
    with db.get_session() as s:
        return {e.url: e.retired_at is not None for e in s.query(Event).all()}


# --- the cut -----------------------------------------------------------------------------------


def test_a_truncated_list_keeps_its_rows_past_the_cut(run):
    """Run 2's list stops at day 3 (a fixed scroll count, one page): days 4 and 5 stay on."""
    _add_source("club", CLUB)
    _scrape("club", CLUB, [_at(n) for n in (1, 2, 3, 4, 5)])
    result = _scrape("club", CLUB, [_at(n) for n in (1, 2, 3)])
    assert result["retired"] == 0
    assert _retired() == {f"{CLUB}/{n}": False for n in (1, 2, 3, 4, 5)}


def test_a_row_before_the_cut_that_was_not_returned_still_retires(run):
    _add_source("club", CLUB)
    _scrape("club", CLUB, [_at(n) for n in (1, 2, 3, 4, 5)])
    result = _scrape("club", CLUB, [_at(1), _at(3)])  # day 2 is gone; the list ends at day 3
    assert result["retired"] == 1
    assert _retired() == {
        f"{CLUB}/1": False,
        f"{CLUB}/2": True,
        f"{CLUB}/3": False,
        f"{CLUB}/4": False,
        f"{CLUB}/5": False,
    }


def test_a_row_with_the_same_start_as_the_cut_is_kept(run):
    """The list may have been cut between two shows at one time: the other one is kept."""
    _add_source("club", CLUB)
    alpha = _show(_day(3), f"{CLUB}/alpha", title="Alpha")
    beta = _show(_day(3), f"{CLUB}/beta", title="Beta")
    gone = _show(_day(2), f"{CLUB}/gone", title="Gone")
    _scrape("club", CLUB, [gone, alpha, beta])
    assert _scrape("club", CLUB, [alpha])["retired"] == 1
    assert _retired() == {f"{CLUB}/gone": True, f"{CLUB}/alpha": False, f"{CLUB}/beta": False}


def test_the_cut_is_the_sources_own_never_the_runs(run):
    """Another source's longer list must not move this source's cut."""
    _add_source("hall", HALL)  # scraped first: its list reaches day 30
    _add_source("club", CLUB)  # its list is cut at day 10
    BATCHES[HALL] = [_at(n, HALL, title="Quartet", venue="Hall") for n in (1, 30)]
    BATCHES[CLUB] = [_at(n) for n in (1, 10, 15)]
    run_all_scrapers()
    BATCHES[CLUB] = [_at(n) for n in (1, 10)]  # day 15 fell off the end of the list
    run_all_scrapers()
    assert _retired()[f"{CLUB}/15"] is False
    assert not any(_retired().values())


def test_a_scrape_of_only_past_events_retires_nothing(run):
    _add_source("club", CLUB)
    _scrape("club", CLUB, [_at(n) for n in (1, 2, 3)])
    past = _show(NOW - timedelta(days=2), f"{CLUB}/past")
    assert _scrape("club", CLUB, [past])["retired"] == 0
    assert not any(_retired().values())


def test_a_scrape_of_one_day_retires_only_what_came_before_it(run):
    _add_source("club", CLUB)
    late = _show(_day(3, 21, 30), f"{CLUB}/3-late")
    _scrape("club", CLUB, [_at(1), _at(2), _at(3), late, _at(4)])
    assert _scrape("club", CLUB, [_at(3), late])["retired"] == 2
    assert _retired() == {
        f"{CLUB}/1": True,
        f"{CLUB}/2": True,
        f"{CLUB}/3": False,
        f"{CLUB}/3-late": False,
        f"{CLUB}/4": False,
    }


def test_an_event_without_a_start_fails_the_scrape_and_retires_nothing(run):
    """A scraper that returns a start-less event is a broken scraper: the scrape is an error
    for the source (nothing stored), so it cannot retire anything either."""
    _add_source("club", CLUB)
    _scrape("club", CLUB, [_at(1), _at(2)])
    no_start = EventData(title="Undated", start_dt=None, venue_name="Club", url=f"{CLUB}/x")
    result = _scrape("club", CLUB, [_at(1), no_start])
    assert "error" in result
    assert _retired() == {f"{CLUB}/1": False, f"{CLUB}/2": False}
    with db.get_session() as s:
        src = s.query(Source).filter_by(name="club").one()
        assert src.error_count == 1 and src.last_error


# --- item 25's guards, unchanged ------------------------------------------------------------------


def test_a_failed_and_an_empty_scrape_still_retire_nothing(run):
    _add_source("club", CLUB)
    _scrape("club", CLUB, [_at(1), _at(2)])
    assert "error" in _scrape("club", CLUB, RuntimeError("the site is down"))
    assert _scrape("club", CLUB, [])["retired"] == 0
    assert not any(_retired().values())


def test_a_returned_row_is_unretired_with_its_uid(run):
    _add_source("club", CLUB)
    _scrape("club", CLUB, [_at(1), _at(2)])
    with db.get_session() as s:
        uid = s.query(Event).filter_by(url=f"{CLUB}/1").one().uid
    _scrape("club", CLUB, [_at(2)])
    assert _retired()[f"{CLUB}/1"] is True
    _scrape("club", CLUB, [_at(1), _at(2)])
    with db.get_session() as s:
        row = s.query(Event).filter_by(url=f"{CLUB}/1").one()
        assert row.retired_at is None and row.uid == uid


# --- item 30's fan-out: each location's list has its own cut ------------------------------------


def _loc(label: str, entry: str) -> int:
    with db.get_session() as s:
        loc = locations.add(s, label=label, entry=entry, radius=25, unit="miles")
        s.commit()
        return loc.id


def _unlink(url: str) -> None:
    with db.get_session() as s:
        row = s.query(Event).filter_by(url=url).one()
        s.query(EventLocation).filter_by(event_id=row.id).delete()
        s.commit()


def test_each_locations_list_has_its_own_cut(run):
    _loc("Area A", "00001")
    _loc("Area B", "00003")
    _add_source("search", "", "fake_item26_aware")
    both = _show(_day(8), f"{CLUB}/both", title="Both areas")
    AREA_BATCHES["Area A"] = [_at(2, title="A"), _at(5, title="A"), both, _at(15, title="A")]
    AREA_BATCHES["Area B"] = [_at(3, HALL, title="B"), both, _at(20, HALL, title="B")]
    run_all_scrapers()
    assert not any(_retired().values())

    # Area A's list is now cut at day 5; Area B's reaches day 20 and no longer lists day 3.
    # Neither lists the day-8 show both found.
    AREA_BATCHES["Area A"] = [_at(2, title="A"), _at(5, title="A")]
    AREA_BATCHES["Area B"] = [_at(20, HALL, title="B")]
    run_all_scrapers()
    assert _retired() == {
        f"{CLUB}/2": False,
        f"{CLUB}/5": False,
        f"{CLUB}/15": False,  # past Area A's cut: A's list says nothing about it
        f"{HALL}/3": True,  # before Area B's cut and not listed
        f"{HALL}/20": False,
        f"{CLUB}/both": True,  # Area B's list reached past day 8 without it
    }


def test_a_row_linked_to_no_searched_location_is_judged_against_the_sources_cut(run):
    """A row found for a location the source no longer searches (or stored before item 30)
    has no list of its own: the source's latest event is its cut."""
    _loc("Area A", "00001")
    _loc("Area B", "00003")
    _add_source("search", "", "fake_item26_aware")
    AREA_BATCHES["Area A"] = [_at(2, title="A"), _at(12, title="A"), _at(25, title="A")]
    AREA_BATCHES["Area B"] = [_at(3, HALL, title="B"), _at(20, HALL, title="B")]
    run_all_scrapers()
    _unlink(f"{CLUB}/12")
    _unlink(f"{CLUB}/25")
    AREA_BATCHES["Area A"] = [_at(2, title="A")]  # cut at day 2
    AREA_BATCHES["Area B"] = [_at(3, HALL, title="B"), _at(20, HALL, title="B")]  # cut at day 20
    run_all_scrapers()
    assert _retired()[f"{CLUB}/12"] is True  # before the source's cut (day 20)
    assert _retired()[f"{CLUB}/25"] is False  # past every cut


# --- two same-title shows on one day reach the store as two rows ---------------------------------


def _two_rows(events: list[EventData], stype: str) -> None:
    assert len(events) == 2, [e.start_dt for e in events]
    with db.get_session() as s:
        src = Source(name=f"two-{stype}", display_name="Two", type=stype, url=CLUB)
        s.add(src)
        s.commit()
        assert upsert_events(s, events, src.id) == (2, 0)
        assert s.query(Event).filter_by(source_id=src.id).count() == 2


class _Page:
    """What ``_scrape_impl`` asks of a Playwright page: one saved synthetic page."""

    def __init__(self, html: str):
        self.html = html

    def goto(self, *a, **kw):
        pass

    def evaluate(self, *a, **kw):
        pass

    def content(self) -> str:
        return self.html

    def close(self):
        pass


def test_eventbrite_keeps_two_same_title_shows_on_one_day(run, monkeypatch):
    when = _day(20)
    card = (
        '<div class="event-card"><a aria-label="View Synthetic Comic at Club" href="{url}">'
        "</a><p>{date} • {time}</p></div>"
    )
    html = "<html><body>{}{}</body></html>".format(
        card.format(url=f"{CLUB}/1", date=when.strftime("%A, %B %d"), time="7:00 PM"),
        card.format(url=f"{CLUB}/2", date=when.strftime("%A, %B %d"), time="9:30 PM"),
    )
    monkeypatch.setattr(eventbrite.time, "sleep", lambda s: None)
    monkeypatch.setattr(eventbrite.EventbriteScraper, "_get_page", lambda self: _Page(html))
    area = locations.area(_area_row(_loc("Exampleton", "00001")))
    events = eventbrite.EventbriteScraper(Config(), location=area)._scrape_impl()
    assert [e.start_dt.strftime("%H:%M") for e in events] == ["19:00", "21:30"]
    _two_rows(events, "eventbrite")


def _area_row(loc_id: int) -> SearchLocation:
    with db.get_session() as s:
        row = s.get(SearchLocation, loc_id)
        s.expunge(row)
        return row


SPOT_A = """<html><body>
<div class="calendar-day-event"><a href="{club}/1"></a>
  <div class="ev-title">Synthetic Comic</div><div class="ev-date">{date}</div>
  <div class="ev-time">7:00 pm</div></div>
<div class="calendar-day-event"><a href="{club}/2"></a>
  <div class="ev-title">Synthetic Comic</div><div class="ev-date">{date}</div>
  <div class="ev-time">9:30 pm</div></div>
</body></html>"""

SPOT_B = """<html><body>
<div class="event-text-holder"><a href="{club}/1"></a><h2>Synthetic Comic</h2>
  <div class="event-main-text event-day">{date}</div>
  <div class="event-main-text event-time">7:00 pm</div></div>
<div class="event-text-holder"><a href="{club}/2"></a><h2>Synthetic Comic</h2>
  <div class="event-main-text event-day">{date}</div>
  <div class="event-main-text event-time">9:30 pm</div></div>
</body></html>"""


def _spothopper(monkeypatch, body: str) -> list[EventData]:
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kw)

    monkeypatch.setattr(_spothopper_base.httpx, "Client", client)
    src = Source(name="spot", display_name="Club", type="spothopper", url=CLUB)
    return registry.get_scraper(src, Config()).scrape()


@pytest.mark.parametrize("variant", ["A", "B"])
def test_spothopper_keeps_two_same_title_shows_on_one_day(run, monkeypatch, variant):
    when = _day(20)
    if variant == "A":
        body = SPOT_A.format(club=CLUB, date=f"{when:%A}, {when:%B} {when.day}th, {when.year}")
    else:
        body = SPOT_B.format(club=CLUB, date=f"{when:%A} {when:%B} {when.day}th")
    events = _spothopper(monkeypatch, body)
    assert [e.start_dt.strftime("%H:%M") for e in events] == ["19:00", "21:30"]
    _two_rows(events, "spothopper")


PLUGIN = """
from datetime import datetime, timedelta

from app.scrapers.base import BaseScraper, EventData

BATCH = {"n": 0}


class ExampleClubScraper(BaseScraper):
    SOURCE_NAME = "Example Club"
    SOURCE_TYPE = "example_club"

    def __init__(self, config, source_url="", venue_name=""):
        super().__init__(config)

    def scrape(self):
        day = datetime.now().replace(second=0, microsecond=0) + timedelta(days=20)
        show = lambda n, h, m=0: EventData(  # noqa: E731
            title="Synthetic Comic",
            start_dt=(day + timedelta(days=n)).replace(hour=h, minute=m),
            venue_name="Club",
            url=f"https://club.example.com/shows/{n}-{h}",
        )
        BATCH["n"] += 1
        if BATCH["n"] == 1:
            return [show(0, 19), show(0, 21, 30), show(3, 19), show(6, 19)]
        return [show(0, 19), show(0, 21, 30)]  # the later days fell off the list
"""


def test_a_plugin_type_keeps_two_shows_and_its_truncated_list_keeps_rows_past_the_cut(
    run, tmp_path
):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "example_club.py").write_text(PLUGIN)
    plugins.unload()
    try:
        assert plugins.load_plugins(str(pdir)).errors == []
        _add_source("plug", CLUB, "example_club")
        first = run_single_scraper("plug")
        assert (first["new"], first["retired"]) == (4, 0)
        with db.get_session() as s:
            starts = sorted(e.start_dt.strftime("%d %H:%M") for e in s.query(Event).all())
        assert len(starts) == 4 and starts[0][3:] == "19:00" and starts[1][3:] == "21:30"
        second = run_single_scraper("plug")
        assert second["retired"] == 0
        assert not any(_retired().values())
    finally:
        plugins.unload()
