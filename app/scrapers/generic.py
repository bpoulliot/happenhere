"""Generic scrapers that work from a URL + venue name stored in the Source record.

Each class accepts `source_url` and `venue_name` via constructor so they can be
instantiated dynamically from the database without any hardcoded constants.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from ._html_base import (
    EventData,
    extract_jsonld_events,
    fetch_soup,
    fetch_soup_cloudscraper,
    parse_jsonld_event,
)
from ._ical_base import ICalScraper
from ._playwright_base import PlaywrightScraper
from ._spothopper_base import SpotHopperScraper
from .base import BaseScraper

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)", re.I)


# ---------------------------------------------------------------------------
# JSON-LD / HTML
# ---------------------------------------------------------------------------


class GenericJsonLdScraper(BaseScraper):
    """Scrapes any site that publishes schema.org/Event JSON-LD markup."""

    SOURCE_TYPE = "jsonld"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or source_url
        self.source_url = source_url
        self.venue_name = venue_name

    def scrape(self) -> list[EventData]:
        events: list[EventData] = []
        now = datetime.now()

        soup = _fetch_with_fallback(self.source_url)
        if soup is None:
            log.warning("GenericJsonLd: could not fetch %s", self.source_url)
            return events

        for item in extract_jsonld_events(soup):
            ev = parse_jsonld_event(item, self.venue_name)
            if ev and ev.start_dt >= now:
                events.append(ev)

        log.debug("GenericJsonLd %s: %d events", self.source_url, len(events))
        return events


# ---------------------------------------------------------------------------
# iCal
# ---------------------------------------------------------------------------


class GenericICalScraper(ICalScraper):
    """Scrapes any WordPress / Tribe Events iCal feed URL."""

    SOURCE_TYPE = "ical"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or source_url
        self.ICAL_URL = source_url
        self.VENUE_NAME = venue_name


# ---------------------------------------------------------------------------
# SpotHopper
# ---------------------------------------------------------------------------


class GenericSpotHopperScraper(SpotHopperScraper):
    """Scrapes any SpotHopper-powered brewery events page."""

    SOURCE_TYPE = "spothopper"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or source_url
        self.EVENTS_URL = source_url
        self.VENUE_NAME = venue_name


# ---------------------------------------------------------------------------
# Squarespace stacked-events layout
# ---------------------------------------------------------------------------


class GenericSquarespaceScraper(BaseScraper):
    """Scrapes Squarespace sites using the stacked events layout.

    Looks for .eventlist-event articles with time[datetime] date elements.
    """

    SOURCE_TYPE = "squarespace"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or source_url
        self.source_url = source_url
        self.venue_name = venue_name

    def scrape(self) -> list[EventData]:
        events: list[EventData] = []
        now = datetime.now()

        soup = _fetch_with_fallback(self.source_url)
        if soup is None:
            log.warning("GenericSquarespace: could not fetch %s", self.source_url)
            return events

        base_url = "/".join(self.source_url.split("/")[:3])

        for card in soup.select(".eventlist-event"):
            title_el = card.select_one(".eventlist-title a, .eventlist-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue

            date_el = card.select_one("time.event-date[datetime]")
            if not date_el:
                continue
            try:
                base_dt = datetime.strptime(date_el["datetime"], "%Y-%m-%d")
            except (ValueError, KeyError):
                continue

            time_el = card.select_one(".event-time-localized-start")
            if time_el:
                m = _TIME_RE.search(time_el.get_text(strip=True))
                if m:
                    try:
                        raw = m.group(1).upper().replace(" ", "").replace(" ", "")
                        t = datetime.strptime(raw, "%I:%M%p")
                        base_dt = base_dt.replace(hour=t.hour, minute=t.minute)
                    except ValueError:
                        pass

            if base_dt < now:
                continue

            link = card.select_one(".eventlist-title a")
            href = link["href"] if link else self.source_url
            if href.startswith("/"):
                href = base_url + href

            img = card.select_one("img[src]")
            events.append(
                EventData(
                    title=title,
                    start_dt=base_dt,
                    venue_name=self.venue_name,
                    url=href,
                    image_url=img["src"] if img else None,
                )
            )

        log.debug("GenericSquarespace %s: %d events", self.source_url, len(events))
        return events


# ---------------------------------------------------------------------------
# Generic Playwright + JSON-LD (last resort for JS-rendered sites)
# ---------------------------------------------------------------------------


class GenericPlaywrightJsonLdScraper(PlaywrightScraper):
    """Renders a JS-heavy page then extracts JSON-LD events."""

    SOURCE_TYPE = "playwright_jsonld"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or source_url
        self.source_url = source_url
        self.venue_name = venue_name

    def _scrape_impl(self) -> list[EventData]:
        events: list[EventData] = []
        now = datetime.now()
        page = self._get_page()

        try:
            page.goto(self.source_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            soup = BeautifulSoup(page.content(), "lxml")
        finally:
            page.close()

        for item in extract_jsonld_events(soup):
            ev = parse_jsonld_event(item, self.venue_name)
            if ev and ev.start_dt >= now:
                events.append(ev)

        log.debug("GenericPlaywrightJsonLd %s: %d events", self.source_url, len(events))
        return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_with_fallback(url: str):
    """Try httpx, fall back to cloudscraper. Returns BeautifulSoup or None."""
    try:
        return fetch_soup(url)
    except Exception as exc:
        log.debug("httpx failed for %s (%s), trying cloudscraper", url, exc)
    try:
        return fetch_soup_cloudscraper(url)
    except Exception as exc:
        log.debug("cloudscraper failed for %s: %s", url, exc)
    return None
