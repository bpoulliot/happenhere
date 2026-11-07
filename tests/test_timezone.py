"""Item 17: parsed times and the ICS feed use the configured timezone, never a fixed one.

Stored times are naive local wall-clock times; only the zone an offset-carrying time is
converted into (and the zone the feed labels them with) comes from ``Config.tz``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from icalendar import Calendar

from app import config, db
from app.config import Config
from app.ical_export import generate_ics
from app.models import Event, Source
from app.scrapers._html_base import _parse_dt, parse_jsonld_event
from app.scrapers._ical_base import _parse_vevent
from app.scrapers.ticketmaster import _parse_event

TOKYO = "Asia/Tokyo"  # UTC+9, no DST: easy to read, and far from any real deployment
LONDON = "Europe/London"


@pytest.fixture
def set_tz(monkeypatch):
    def _set(name: str) -> None:
        monkeypatch.setattr(config, "_cfg", Config(tz=name))

    return _set


# --- the helper -----------------------------------------------------------------------------


def test_the_helper_returns_the_configured_zone(set_tz):
    from app.tz import tz_of

    set_tz(TOKYO)
    assert tz_of().key == TOKYO
    assert tz_of(Config(tz=LONDON)).key == LONDON


@pytest.mark.parametrize("bad", ["Not/AZone", "", "  "])
def test_an_unknown_or_blank_zone_falls_back_to_utc(set_tz, bad):
    from app.tz import tz_of

    set_tz(bad)
    assert tz_of().key == "UTC"
    assert tz_of(Config(tz=bad)).key == "UTC"


def test_digest_uses_the_same_helper():
    from app import digest, tz

    assert digest.tz_of is tz.tz_of


# --- JSON-LD / HTML -------------------------------------------------------------------------


def test_jsonld_utc_offset_becomes_the_configured_wall_clock_time(set_tz):
    set_tz(TOKYO)
    assert _parse_dt("2030-11-05T19:00:00+00:00") == datetime(2030, 11, 6, 4, 0)
    assert _parse_dt("2030-11-05T19:00:00Z") == datetime(2030, 11, 6, 4, 0)


def test_jsonld_follows_daylight_saving_of_the_configured_zone(set_tz):
    set_tz(LONDON)
    assert _parse_dt("2030-07-05T18:00:00+00:00") == datetime(2030, 7, 5, 19, 0)  # BST
    assert _parse_dt("2030-11-05T19:00:00+00:00") == datetime(2030, 11, 5, 19, 0)  # GMT


def test_jsonld_without_an_offset_is_already_local(set_tz):
    set_tz(TOKYO)
    assert _parse_dt("2030-11-05T19:00:00") == datetime(2030, 11, 5, 19, 0)
    assert _parse_dt("2030-11-05") == datetime(2030, 11, 5, 0, 0)


def test_jsonld_event_end_to_end(set_tz):
    set_tz(TOKYO)
    ev = parse_jsonld_event(
        {
            "name": "Synthetic Show",
            "startDate": "2030-11-05T19:00:00+00:00",
            "endDate": "2030-11-05T21:00:00+00:00",
        },
        "Hall",
    )
    assert (ev.start_dt, ev.end_dt) == (datetime(2030, 11, 6, 4, 0), datetime(2030, 11, 6, 6, 0))


def test_jsonld_unknown_zone_is_utc(set_tz):
    set_tz("Not/AZone")
    assert _parse_dt("2030-11-05T19:00:00+09:00") == datetime(2030, 11, 5, 10, 0)


# --- iCal -----------------------------------------------------------------------------------


def _vevent(dtstart: str, dtend: str) -> object:
    cal = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a@example.com\r\n"
        f"SUMMARY:Synthetic Quartet\r\n{dtstart}\r\n{dtend}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return next(c for c in cal.walk() if c.name == "VEVENT")


def test_ical_utc_time_becomes_the_configured_wall_clock_time(set_tz):
    set_tz(TOKYO)
    ev = _parse_vevent(_vevent("DTSTART:20301105T190000Z", "DTEND:20301105T210000Z"), "Hall")
    assert (ev.start_dt, ev.end_dt) == (datetime(2030, 11, 6, 4, 0), datetime(2030, 11, 6, 6, 0))


def test_ical_floating_time_is_left_alone(set_tz):
    set_tz(TOKYO)
    ev = _parse_vevent(_vevent("DTSTART:20301105T190000", "DTEND:20301105T210000"), "Hall")
    assert ev.start_dt == datetime(2030, 11, 5, 19, 0)


def test_ical_unknown_zone_is_utc(set_tz):
    set_tz("Not/AZone")
    ev = _parse_vevent(_vevent("DTSTART:20301105T190000Z", "DTEND:20301105T210000Z"), "Hall")
    assert ev.start_dt == datetime(2030, 11, 5, 19, 0)


# --- Ticketmaster ---------------------------------------------------------------------------


def _tm(date_time: str) -> dict:
    return {"name": "Synthetic Show", "dates": {"start": {"dateTime": date_time}}}


def test_ticketmaster_utc_time_becomes_the_configured_wall_clock_time(set_tz):
    set_tz(TOKYO)
    assert _parse_event(_tm("2030-11-05T19:00:00Z")).start_dt == datetime(2030, 11, 6, 4, 0)


def test_ticketmaster_unknown_zone_is_utc(set_tz):
    set_tz("Not/AZone")
    assert _parse_event(_tm("2030-11-05T19:00:00Z")).start_dt == datetime(2030, 11, 5, 19, 0)


# --- the ICS feed ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    db.init_db(tmp_path / "tz.db")
    with db.get_session() as s:
        src = Source(name="hall", display_name="Hall", type="ical")
        s.add(src)
        s.commit()
        s.add(
            Event(
                source_id=src.id,
                title="Synthetic Show",
                start_dt=datetime(2030, 11, 6, 4, 0),
                dedup_hash="h" * 32,
                uid="tz-1@example.com",
            )
        )
        s.commit()
        yield s


def _parsed(ics: bytes) -> Calendar:
    return Calendar.from_ical(ics)


def test_feed_labels_times_with_the_configured_tzid(session, set_tz):
    set_tz(TOKYO)
    ics = generate_ics(session, now=datetime(2030, 1, 1))
    assert b"DTSTART;TZID=Asia/Tokyo:20301106T040000" in ics
    assert b"DTEND;TZID=Asia/Tokyo:20301106T060000" in ics
    cal = _parsed(ics)
    assert str(cal["X-WR-TIMEZONE"]) == TOKYO
    assert [str(c["TZID"]) for c in cal.walk("VTIMEZONE")] == [TOKYO]
    assert b"America/" not in ics


def test_feed_wall_clock_time_is_the_stored_time(session, set_tz):
    set_tz(LONDON)
    ev = next(c for c in _parsed(generate_ics(session, now=datetime(2030, 1, 1))).walk("VEVENT"))
    assert ev["DTSTART"].dt.replace(tzinfo=None) == datetime(2030, 11, 6, 4, 0)
    assert ev["DTSTART"].dt.tzinfo.key == LONDON


def test_feed_vtimezone_carries_the_zone_rules(session, set_tz):
    set_tz(LONDON)
    cal = _parsed(generate_ics(session, now=datetime(2030, 1, 1)))
    tz = cal.walk("VTIMEZONE")[0]
    names = {str(c["TZNAME"]) for c in tz.subcomponents}
    assert {"GMT", "BST"} <= names


def test_feed_unknown_zone_is_utc(session, set_tz):
    set_tz("Not/AZone")
    ics = generate_ics(session, now=datetime(2030, 1, 1))
    assert str(_parsed(ics)["X-WR-TIMEZONE"]) == "UTC"
    assert b"DTSTART:20301106T040000Z" in ics


def test_the_same_rows_give_the_same_bytes(session, set_tz):
    set_tz(LONDON)
    now = datetime(2030, 1, 1)
    assert generate_ics(session, now=now) == generate_ics(session, now=now)


# --- the scheduler and the source tree -------------------------------------------------------


def test_scheduler_zone_is_the_configured_one(set_tz):
    from apscheduler.schedulers.background import BackgroundScheduler

    from app import scheduler

    set_tz(TOKYO)
    assert str(BackgroundScheduler(timezone=scheduler._timezone()).timezone) == TOKYO


def test_no_fixed_zone_is_left_in_the_application_code():
    """Only the config default may name a region's zone; every conversion goes through app.tz."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    hits = [
        f"{p.relative_to(app_dir)}:{n}"
        for p in app_dir.rglob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if "America/" in line and p.name != "config.py"
    ]
    assert hits == []
