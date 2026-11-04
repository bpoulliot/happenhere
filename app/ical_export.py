from __future__ import annotations

from datetime import UTC, datetime, timedelta

from icalendar import (
    Calendar,
    Event,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
)
from sqlalchemy.orm import Session

from .models import Event as EventRow

_TZ_NAME = "America/Denver"
_LOOKAHEAD_DAYS = 90


def generate_ics(session: Session) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//peakevents//Colorado Springs Events//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Colorado Springs Events")
    cal.add("x-wr-timezone", _TZ_NAME)
    cal.add("x-wr-caldesc", "Local events aggregated by peakevents")

    _add_vtimezone(cal)

    cutoff = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)
    rows = (
        session.query(EventRow)
        .filter(EventRow.start_dt >= datetime.utcnow())
        .filter(EventRow.start_dt <= cutoff)
        .order_by(EventRow.start_dt)
        .all()
    )

    for row in rows:
        vevent = Event()
        vevent.add("uid", f"{row.dedup_hash}@peakevents")
        vevent.add("summary", row.title)

        # Treat stored datetimes as Mountain Time (they were stored naive, in local time)
        dtstart = _naive_to_mountain(row.start_dt)
        vevent.add("dtstart", dtstart)

        if row.end_dt:
            vevent.add("dtend", _naive_to_mountain(row.end_dt))
        else:
            # Default 2-hour duration
            vevent.add("dtend", _naive_to_mountain(row.start_dt + timedelta(hours=2)))

        if row.venue_name:
            loc_parts = [row.venue_name]
            if row.venue_addr:
                loc_parts.append(row.venue_addr)
            vevent.add("location", ", ".join(loc_parts))

        if row.description:
            vevent.add("description", row.description[:500])

        if row.url:
            vevent.add("url", row.url)

        vevent.add("dtstamp", datetime.utcnow().replace(tzinfo=UTC))

        cal.add_component(vevent)

    return cal.to_ical()


def _naive_to_mountain(dt: datetime) -> datetime:
    """Attach Mountain Standard Time offset (UTC-7 MDT / UTC-7 MST approximation).
    For a home calendar app this is good enough; production would use pytz/zoneinfo.
    """
    from zoneinfo import ZoneInfo

    mtn = ZoneInfo(_TZ_NAME)
    return dt.replace(tzinfo=mtn)


def _add_vtimezone(cal: Calendar) -> None:
    tz = Timezone()
    tz.add("tzid", _TZ_NAME)

    std = TimezoneStandard()
    std.add("tzoffsetfrom", timedelta(hours=-6))
    std.add("tzoffsetto", timedelta(hours=-7))
    std.add("tzname", "MST")
    std.add("dtstart", datetime(1970, 11, 1, 2, 0, 0))
    std.add("rrule", {"FREQ": "YEARLY", "BYDAY": "1SU", "BYMONTH": 11})
    tz.add_component(std)

    dst = TimezoneDaylight()
    dst.add("tzoffsetfrom", timedelta(hours=-7))
    dst.add("tzoffsetto", timedelta(hours=-6))
    dst.add("tzname", "MDT")
    dst.add("dtstart", datetime(1970, 3, 8, 2, 0, 0))
    dst.add("rrule", {"FREQ": "YEARLY", "BYDAY": "2SU", "BYMONTH": 3})
    tz.add_component(dst)

    cal.add_component(tz)
