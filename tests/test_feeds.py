"""Item 03: stable feed UIDs, feeds by tag and by venue, the /feeds page, caching."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from icalendar import Calendar

from app import db
from app.dedup import upsert_events
from app.ical_export import generate_ics
from app.models import Source
from app.scrapers._ical_base import _parse_vevent


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
    start = (datetime.now() + timedelta(days=10)).replace(hour=19, minute=0, second=0, microsecond=0)

    upsert_events(session, [_parse_vevent(_vevent("Quartet Night", start), "Hall")], src.id)
    before = _uids(generate_ics(session))

    upsert_events(
        session, [_parse_vevent(_vevent("Quartet Night — SOLD OUT", start), "Hall")], src.id
    )
    after = _uids(generate_ics(session))

    print(f"\nUIDs before the retitle: {before}\nUIDs after the retitle:  {after}")
    assert len(before) == 1
    assert after == before, "a retitle changed the feed UID or left a ghost event"
