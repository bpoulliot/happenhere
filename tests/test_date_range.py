"""Item 33: a date-range filter on the events page and the API.

``/events``, ``/partials/event-list`` and ``/api/events`` take ``from`` and ``to`` (ISO dates,
``YYYY-MM-DD``, whole days in the configured zone, both inclusive). A range replaces the
``days`` window: ``from`` alone shows ``days`` days starting that day, ``to`` alone shows from now
until the end of that day. Nothing before now is shown (as without a range). An unusable range is
the default view with a notice on the page and a 400 with fixed text on the API.

Every label, ZIP code and host here is invented (the synthetic Gazetteer in
``tests/fixtures/gazetteer``).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import config, db, locations
from app.config import Config
from app.main import app
from app.models import Event, Source

ZONE = "America/New_York"

BAD_DATE = "invalid date range: dates must be YYYY-MM-DD"
REVERSED = "invalid date range: the end date is before the start date"
PAST = "invalid date range: the end date is before today"


def _today() -> datetime:
    """Midnight today in the configured zone, naive (how the database stores times)."""
    now = datetime.now(ZoneInfo(ZONE)).replace(tzinfo=None)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


@pytest.fixture
def client(monkeypatch, tmp_path):
    db.init_db(tmp_path / "range.db")
    cfg = Config()
    cfg.secret_key = "k" * 64
    cfg.tz = ZONE
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    return nullcontext(c)


def _source(s, name: str, area_id: int | None = None) -> Source:
    src = Source(
        name=name,
        display_name=name.title(),
        type="ical",
        url=f"https://{name}.example.com/events.ics",
        enabled=True,
        area_id=area_id,
    )
    s.add(src)
    s.commit()
    return src


def _event(s, src: Source, title: str, start: datetime, cats=(), retired: bool = False) -> None:
    key = hashlib.sha256(f"{src.name}|{title}|{start.isoformat()}".encode()).hexdigest()
    s.add(
        Event(
            source_id=src.id,
            dedup_hash=key,
            uid=f"{key[:16]}@example.com",
            title=title,
            start_dt=start,
            venue_name=src.display_name,
            categories=json.dumps(list(cats)),
            retired_at=datetime(2030, 1, 1) if retired else None,
        )
    )
    s.commit()


def _world() -> None:
    """Fixed far-future shows around 2030-06-10, plus shows near today for the default view."""
    today = _today()
    with db.get_session() as s:
        hall = _source(s, "hall")
        _event(s, hall, "Before Range", datetime(2030, 6, 9, 19, 0))
        _event(s, hall, "Late Show", datetime(2030, 6, 10, 23, 30), cats=["music"])
        _event(s, hall, "Next Morning", datetime(2030, 6, 11, 0, 15))
        _event(s, hall, "Called Off", datetime(2030, 6, 10, 20, 0), retired=True)
        _event(s, hall, "Yesterday Show", today - timedelta(days=1) + timedelta(hours=19))
        _event(s, hall, "Soon Show", today + timedelta(days=2, hours=19))


def _api(c, **params):
    if "from_" in params:
        params["from"] = params.pop("from_")
    return c.get("/api/events", params=params)


def _titles(r) -> list[str]:
    assert r.status_code == 200, r.text
    return sorted(e["title"] for e in r.json()["events"])


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_the_range_includes_a_late_evening_show_on_the_to_day(client):
    _world()
    with client as c:
        r = _api(c, from_="2030-06-10", to="2030-06-10")
    assert _titles(r) == ["Late Show"]


def test_the_range_excludes_the_day_after_and_the_day_before(client):
    _world()
    with client as c:
        r = _api(c, from_="2030-06-10", to="2030-06-10")
    titles = _titles(r)
    assert "Next Morning" not in titles
    assert "Before Range" not in titles


def test_a_two_day_range_takes_both_days(client):
    _world()
    with client as c:
        r = _api(c, from_="2030-06-10", to="2030-06-11")
    assert _titles(r) == ["Late Show", "Next Morning"]


def test_from_alone_is_days_long_starting_that_day(client):
    _world()
    with client as c:
        one = _api(c, from_="2030-06-10", days=1)
        thirty = _api(c, from_="2030-06-10")
    assert _titles(one) == ["Late Show"]
    assert _titles(thirty) == ["Late Show", "Next Morning"]


def test_to_alone_runs_from_now_to_the_end_of_that_day(client):
    _world()
    with client as c:
        r = _api(c, to="2030-06-10")
    assert _titles(r) == ["Before Range", "Late Show", "Soon Show"]


def test_a_range_never_shows_the_past(client):
    _world()
    with client as c:
        r = _api(c, from_="2000-01-01", to=_iso(_today() + timedelta(days=3)))
    assert _titles(r) == ["Soon Show"]


def test_empty_values_are_no_range(client):
    """The page's form sends ``from=&to=`` when the fields are blank."""
    _world()
    with client as c:
        r = _api(c, from_="", to="")
    assert _titles(r) == ["Soon Show"]


def test_no_range_keeps_the_days_window(client):
    _world()
    with client as c:
        r = _api(c, days=30)
    assert _titles(r) == ["Soon Show"]


# ---------------------------------------------------------------------------
# An unusable range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params,text",
    [
        ({"from_": "2030-13-01"}, BAD_DATE),
        ({"to": "June 10"}, BAD_DATE),
        ({"from_": "20300610"}, BAD_DATE),
        ({"from_": "2030-06-11", "to": "2030-06-10"}, REVERSED),
        ({"to": "2000-01-01"}, PAST),
    ],
)
def test_the_api_refuses_an_unusable_range_with_fixed_text(client, params, text):
    _world()
    with client as c:
        r = _api(c, **params)
    assert r.status_code == 400
    assert r.json() == {"detail": text}
    for v in params.values():
        assert v not in r.text  # the input is never echoed


def test_the_page_shows_the_default_view_and_a_notice_for_a_bad_date(client):
    _world()
    with client as c:
        r = c.get("/events", params={"from": "not-a-date", "to": "2030-06-10"})
    assert r.status_code == 200
    assert 'class="date-range-notice"' in r.text
    assert "dates must be YYYY-MM-DD" in r.text
    assert "Soon Show" in r.text  # the default window
    assert "Late Show" not in r.text
    assert "not-a-date" not in r.text


def test_the_page_notice_for_a_reversed_range(client):
    _world()
    with client as c:
        r = c.get("/events", params={"from": "2030-06-11", "to": "2030-06-10"})
    assert r.status_code == 200
    assert "the end date is before the start date" in r.text
    assert "Soon Show" in r.text


def test_the_partial_shows_the_notice_too(client):
    _world()
    with client as c:
        r = c.get("/partials/event-list", params={"from": "2030-06-11", "to": "2030-06-10"})
    assert r.status_code == 200
    assert 'class="date-range-notice"' in r.text
    assert "Soon Show" in r.text


def test_a_good_range_shows_no_notice(client):
    _world()
    with client as c:
        r = c.get("/events", params={"from": "2030-06-10", "to": "2030-06-10"})
    assert r.status_code == 200
    assert "date-range-notice" not in r.text


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_renders_with_a_range(client):
    _world()
    with client as c:
        r = c.get("/events", params={"from": "2030-06-10", "to": "2030-06-10"})
    assert r.status_code == 200
    assert "Late Show" in r.text
    for gone in ("Next Morning", "Before Range", "Called Off", "Soon Show"):
        assert gone not in r.text


def test_the_page_renders_without_a_range(client):
    _world()
    with client as c:
        r = c.get("/events")
    assert r.status_code == 200
    assert 'type="date" name="from"' in r.text
    assert 'type="date" name="to"' in r.text
    assert 'name="days"' in r.text


def test_the_date_fields_keep_their_values(client):
    _world()
    with client as c:
        r = c.get("/events", params={"from": "2030-06-10", "to": "2030-06-12"})
    assert 'type="date" name="from" value="2030-06-10"' in r.text
    assert 'type="date" name="to" value="2030-06-12"' in r.text


def test_the_partial_takes_the_range(client):
    _world()
    with client as c:
        r = c.get("/partials/event-list", params={"from": "2030-06-10", "to": "2030-06-10"})
    assert r.status_code == 200
    assert "Late Show" in r.text
    assert "Next Morning" not in r.text


def test_the_area_filter_keeps_the_range(client):
    _world()
    with db.get_session() as s:
        locations.add(s, label="Area A", entry="00001", radius=25, unit="miles")
        s.commit()
    with client as c:
        r = c.get("/events", params={"from": "2030-06-10", "to": "2030-06-12", "area": "area-a"})
    assert r.status_code == 200
    assert '<input type="hidden" name="from" value="2030-06-10">' in r.text
    assert '<input type="hidden" name="to" value="2030-06-12">' in r.text
    assert "from=2030-06-10" in r.text  # the "All areas" link
    assert "to=2030-06-12" in r.text


# ---------------------------------------------------------------------------
# Combined with the other filters
# ---------------------------------------------------------------------------


def test_range_area_and_category_combine(client):
    with db.get_session() as s:
        a = locations.add(s, label="Area A", entry="00001", radius=25, unit="miles")
        b = locations.add(s, label="Area B", entry="00002", radius=25, unit="miles")
        s.commit()
        east = _source(s, "east", area_id=a.id)
        west = _source(s, "west", area_id=b.id)
        _event(s, east, "East Music", datetime(2030, 6, 10, 21, 0), cats=["music"])
        _event(s, east, "East Comedy", datetime(2030, 6, 10, 21, 30), cats=["comedy"])
        _event(s, east, "East Music Later", datetime(2030, 6, 12, 21, 0), cats=["music"])
        _event(s, west, "West Music", datetime(2030, 6, 10, 22, 0), cats=["music"])
    with client as c:
        r = _api(c, from_="2030-06-10", to="2030-06-10", area="area-a", category="music")
        page = c.get(
            "/events",
            params={
                "from": "2030-06-10",
                "to": "2030-06-10",
                "area": "area-a",
                "category": "music",
            },
        )
    assert _titles(r) == ["East Music"]
    assert "East Music" in page.text
    for gone in ("East Comedy", "East Music Later", "West Music"):
        assert gone not in page.text


def test_a_retired_row_stays_hidden_in_a_range(client):
    _world()
    with client as c:
        api = _api(c, from_="2030-06-10", to="2030-06-10")
        page = c.get("/events", params={"from": "2030-06-10", "to": "2030-06-10"})
        part = c.get("/partials/event-list", params={"from": "2030-06-10", "to": "2030-06-10"})
    assert "Called Off" not in _titles(api)
    assert "Called Off" not in page.text
    assert "Called Off" not in part.text
