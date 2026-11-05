"""Shared base for WordPress Tribe Events iCal feed scrapers."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

from .base import DEFAULT_HEADERS, BaseScraper, EventData

log = logging.getLogger(__name__)


class ICalScraper(BaseScraper):
    SOURCE_TYPE = "ical"
    ICAL_URL: str = ""
    VENUE_NAME: str = ""

    def scrape(self) -> list[EventData]:
        url = self.ICAL_URL
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()

        if not resp.content or len(resp.content.strip()) < 20:
            log.debug("%s: empty iCal feed, no events", self.SOURCE_NAME)
            return []
        try:
            cal = Calendar.from_ical(resp.content)
        except Exception as exc:
            log.warning("%s: iCal parse error: %s", self.SOURCE_NAME, exc)
            return []
        events: list[EventData] = []
        now = datetime.now()

        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            ev = _parse_vevent(component, self.VENUE_NAME)
            if ev and ev.start_dt >= now:
                events.append(ev)

        log.debug("%s: fetched %d upcoming events", self.SOURCE_NAME, len(events))
        return events


def _parse_vevent(component, default_venue: str) -> EventData | None:
    summary = str(component.get("SUMMARY", "")).strip()
    if not summary:
        return None

    dtstart = component.get("DTSTART")
    if dtstart is None:
        return None
    start = dtstart.dt
    if hasattr(start, "hour"):
        # datetime
        if start.tzinfo is not None:
            start = start.astimezone(ZoneInfo("America/Denver")).replace(tzinfo=None)
    else:
        # date only → midnight
        start = datetime(start.year, start.month, start.day, 0, 0, 0)

    dtend = component.get("DTEND")
    end = None
    if dtend:
        e = dtend.dt
        if hasattr(e, "hour"):
            if e.tzinfo is not None:
                e = e.astimezone(ZoneInfo("America/Denver")).replace(tzinfo=None)
            end = e
        else:
            end = datetime(e.year, e.month, e.day, 23, 59, 0)

    location = str(component.get("LOCATION", "")).strip() or default_venue
    description = str(component.get("DESCRIPTION", "")).strip() or None
    url = str(component.get("URL", "")).strip() or None

    uid = str(component.get("UID", "")).strip() or None
    rid = component.get("RECURRENCE-ID")
    if uid and rid is not None:
        # One overridden occurrence of a recurring event shares the series' UID.
        uid = f"{uid}#{rid.to_ical().decode()}"

    return EventData(
        title=summary,
        start_dt=start,
        end_dt=end,
        venue_name=location or default_venue,
        venue_addr=location if location != default_venue else None,
        description=description,
        url=url,
        categories=[],
        source_uid=uid,
    )
