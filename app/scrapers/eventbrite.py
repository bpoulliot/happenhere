"""Eventbrite regional events: a scroll-loaded React app, parsed from div.event-card containers.

Location-aware (item 30): one search per search location, at
``eventbrite.com/d/<state>--<city-slug>/events/`` from the location's city (or the location's own
``eventbrite_region``). The site takes no radius: the location's radius is not used.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime

from bs4 import BeautifulSoup

from ._playwright_base import PlaywrightScraper
from .base import EventData

log = logging.getLogger(__name__)

_URL_TPL = "https://www.eventbrite.com/d/{region}/events/"
MISSING_LOCATION = (
    "Eventbrite needs a search location in the US (a ZIP code or a city), "
    "or the location's own Eventbrite region: nothing to search"
)


def region_slug(city: str, state: str) -> str:
    """``"ca--sample-city"`` for Sample City, CA: the site's region path."""
    s = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return f"{state.lower()}--{s}" if s and state else ""


# Date text: "Mon, Jun 8" or "Sat, Jun 13 • 7:00 PM"
_DATE_RE = re.compile(r"(?:\w+,\s+)?(\w+)\s+(\d+)(?:\s*[•·]\s*(\d{1,2}:\d{2}\s*(?:AM|PM)))?", re.I)


class EventbriteScraper(PlaywrightScraper):
    SOURCE_NAME = "eventbrite"
    LOCATION_AWARE = True

    def __init__(self, config, location=None):
        super().__init__(config)
        self.location = location

    def _region(self) -> str:
        loc = self.location
        if loc is None:
            return ""
        return loc.eventbrite_region or region_slug(loc.city, loc.state)

    def _get_url(self) -> str:
        return _URL_TPL.format(region=self._region())

    def scrape(self) -> list[EventData]:
        if not self._region():  # refuse before Chromium starts (as item 29 did for Meetup)
            raise ValueError(MISSING_LOCATION)
        return super().scrape()

    def _scrape_impl(self) -> list[EventData]:
        events: list[EventData] = []
        now = datetime.now()
        page = self._get_page()

        try:
            page.goto(self._get_url(), wait_until="domcontentloaded", timeout=40000)
            # Scroll to load lazy content
            for scroll_y in [1000, 2000, 3500, 5000]:
                page.evaluate(f"window.scrollTo(0, {scroll_y})")
                time.sleep(1.5)

            soup = BeautifulSoup(page.content(), "lxml")
        finally:
            page.close()

        year = now.year
        for card in soup.select("div.event-card"):
            # Title from aria-label on the link, strip leading "View "
            link = card.select_one("a[aria-label]")
            if not link:
                continue
            title = re.sub(r"^View\s+", "", link.get("aria-label", "")).strip()
            # Remove trailing venue if it's appended: "Title at Venue"
            # (aria-label format: "View [Title] at [Venue]")
            url = link.get("href", "")
            if not title:
                continue

            # Date text from card body text
            card_text = card.get_text(" ", strip=True)
            start_dt = _parse_date_from_text(card_text, year, now)
            if not start_dt or start_dt < now:
                continue

            img = card.select_one("img.event-card-image")
            img_src = img.get("src") if img else None

            events.append(
                EventData(
                    title=title,
                    start_dt=start_dt,
                    venue_name=self.location.label if self.location else "Events",
                    url=url,
                    image_url=img_src,
                    categories=[],
                )
            )

        log.debug("Eventbrite: %d events", len(events))
        return events


def _parse_date_from_text(text: str, year: int, now: datetime) -> datetime | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    month_str, day_str = m.group(1), m.group(2)
    time_str = m.group(3)
    try:
        base = datetime.strptime(f"{month_str} {day_str} {year}", "%b %d %Y")
    except ValueError:
        try:
            base = datetime.strptime(f"{month_str} {day_str} {year}", "%B %d %Y")
        except ValueError:
            return None
    if base < now.replace(hour=0, minute=0, second=0):
        base = base.replace(year=year + 1)
    if time_str:
        try:
            t = datetime.strptime(time_str.strip(), "%I:%M %p")
            base = base.replace(hour=t.hour, minute=t.minute)
        except ValueError:
            pass
    return base
