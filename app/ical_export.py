from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, Timezone
from sqlalchemy.orm import Session

from .dedup import legacy_uid
from .models import Event as EventRow
from .models import shown_events
from .tz import tz_of

PRODID = "-//happenhere//happenhere//EN"


def feed_uid(row: EventRow) -> str:
    """The UID a calendar app keys this event on: set once when the row is stored, never
    derived from the title (item 03). A row stored before item 03 keeps the UID it had."""
    return row.uid or legacy_uid(row.dedup_hash)


def feed_rows(
    session: Session,
    *,
    source_ids: list[int] | None = None,
    lookahead_days: int = 365,
    now: datetime | None = None,
) -> list[EventRow]:
    """Upcoming events within the look-ahead, optionally only from some sources."""
    now = now or datetime.utcnow()
    q = shown_events(session, now, now + timedelta(days=lookahead_days))
    if source_ids is not None:
        q = q.filter(EventRow.source_id.in_(source_ids))
    return q.order_by(EventRow.start_dt, EventRow.id).all()


def generate_ics(
    session: Session,
    *,
    name: str = "HappenHere",
    description: str = "",
    source_ids: list[int] | None = None,
    lookahead_days: int = 365,
    now: datetime | None = None,
) -> bytes:
    """One calendar. ``name`` is its X-WR-CALNAME; ``source_ids`` (None = every source) and
    the look-ahead select its events. The same rows always give the same bytes (the ETag)."""
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    tz = tz_of()
    cal.add("x-wr-timezone", tz.key)
    cal.add("x-wr-caldesc", description or f"Local events from {name}")

    _add_vtimezone(cal, tz)

    rows = feed_rows(session, source_ids=source_ids, lookahead_days=lookahead_days, now=now)

    for row in rows:
        vevent = Event()
        vevent.add("uid", feed_uid(row))
        vevent.add("summary", row.title)

        # Stored datetimes are naive local times: label them with the configured zone
        dtstart = _naive_to_local(row.start_dt, tz)
        vevent.add("dtstart", dtstart)

        if row.end_dt:
            vevent.add("dtend", _naive_to_local(row.end_dt, tz))
        else:
            # Default 2-hour duration
            vevent.add("dtend", _naive_to_local(row.start_dt + timedelta(hours=2), tz))

        if row.venue_name:
            loc_parts = [row.venue_name]
            if row.venue_addr:
                loc_parts.append(row.venue_addr)
            vevent.add("location", ", ".join(loc_parts))

        if row.description:
            vevent.add("description", row.description[:500])

        if row.url:
            vevent.add("url", row.url)

        # When the row was last refreshed, not "now": an unchanged feed is byte-identical,
        # so its ETag holds. Stored naive in the host's local time.
        stamp = row.last_seen or row.first_seen or row.start_dt
        vevent.add("dtstamp", stamp.astimezone(UTC))

        cal.add_component(vevent)

    return cal.to_ical()


def _naive_to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    """Attach the configured zone to a stored naive local time (the wall clock is unchanged)."""
    return dt.replace(tzinfo=tz)


def _add_vtimezone(cal: Calendar, tz: ZoneInfo) -> None:
    """The zone's rules, generated from the tz database, so TZID and VTIMEZONE always agree."""
    cal.add_component(Timezone.from_tzinfo(tz, tzid=tz.key))
