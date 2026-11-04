"""Shared base for SpotHopper-powered brewery event pages.

SpotHopper has two HTML template variants:
  A) .calendar-day-event / .ev-title / .ev-date / .ev-time  (Gunslinger)
  B) .event-text-holder / h2 / .event-main-text.event-day   (Cogstone, OCC)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from .base import DEFAULT_HEADERS, BaseScraper, EventData

log = logging.getLogger(__name__)

_DATE_FULL_RE = re.compile(r"(\w+),\s+(\w+)\s+(\d+)(?:st|nd|rd|th),?\s+(\d{4})")
_DATE_NOYEAR_RE = re.compile(r"(\w+)\s+(\d+)(?:st|nd|rd|th)?")
_TIME_RE = re.compile(r"(\d{1,2}(?::\d{2})?)\s*(am|pm)", re.I)


class SpotHopperScraper(BaseScraper):
    SOURCE_TYPE = "scrape"
    EVENTS_URL: str = ""
    VENUE_NAME: str = ""

    def scrape(self) -> list[EventData]:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as client:
            resp = client.get(self.EVENTS_URL)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        events = _scrape_variant_a(soup, self.VENUE_NAME, self.EVENTS_URL)
        if not events:
            events = _scrape_variant_b(soup, self.VENUE_NAME, self.EVENTS_URL)

        log.debug("%s: %d events", self.SOURCE_NAME, len(events))
        return events


def _scrape_variant_a(soup: BeautifulSoup, venue: str, url: str) -> list[EventData]:
    """Variant A: .calendar-day-event cards with .ev-title / .ev-date / .ev-time."""
    events: list[EventData] = []
    now = datetime.now()
    seen: set[str] = set()

    for card in soup.select(".calendar-day-event"):
        title_el = card.select_one(".ev-title")
        date_el = card.select_one(".ev-date")
        time_el = card.select_one(".ev-time")
        img_el = card.select_one(".ev-image img")
        if not title_el or not date_el:
            continue

        title = title_el.get_text(strip=True)
        start_dt = _parse_full_date(date_el.get_text(strip=True), time_el)
        if not start_dt or start_dt < now:
            continue

        key = f"{title}|{start_dt.date()}"
        if key in seen:
            continue
        seen.add(key)

        img_src = _img_src(img_el)
        link = card.find("a")
        events.append(
            EventData(
                title=title,
                start_dt=start_dt,
                venue_name=venue,
                url=link["href"] if link else url,
                image_url=img_src,
                categories=["Brewery"],
            )
        )
    return events


def _scrape_variant_b(soup: BeautifulSoup, venue: str, url: str) -> list[EventData]:
    """Variant B: .event-text-holder cards with h2 + .event-main-text.event-day."""
    events: list[EventData] = []
    now = datetime.now()
    seen: set[str] = set()
    current_year = now.year

    for card in soup.select(".event-text-holder"):
        title_el = card.select_one("h2, h3")
        date_el = card.select_one(".event-main-text.event-day, .event-day, .event-date")
        time_el = card.select_one(".event-main-text.event-time, .event-time")
        img_el = card.select_one("img")
        if not title_el or not date_el:
            continue

        title = title_el.get_text(strip=True)
        date_str = date_el.get_text(strip=True)  # e.g. "Wednesday June 3rd"
        start_dt = _parse_noyear_date(date_str, current_year, now)
        if not start_dt:
            continue

        key = f"{title}|{start_dt.date()}"
        if key in seen:
            continue
        seen.add(key)

        if time_el:
            start_dt = _apply_time(start_dt, time_el.get_text(strip=True))

        img_src = _img_src(img_el)
        link = card.find("a")
        events.append(
            EventData(
                title=title,
                start_dt=start_dt,
                venue_name=venue,
                url=link["href"] if link else url,
                image_url=img_src,
                categories=["Brewery"],
            )
        )
    return events


def _parse_full_date(date_str: str, time_el) -> datetime | None:
    m = _DATE_FULL_RE.search(date_str)
    if not m:
        return None
    _, month_name, day, year = m.groups()
    try:
        base = datetime.strptime(f"{month_name} {int(day)} {year}", "%B %d %Y")
    except ValueError:
        return None
    if time_el:
        base = _apply_time(base, time_el.get_text(strip=True))
    return base


def _parse_noyear_date(date_str: str, year: int, now: datetime) -> datetime | None:
    m = _DATE_NOYEAR_RE.search(date_str)
    if not m:
        return None
    month_name, day = m.groups()
    try:
        base = datetime.strptime(f"{month_name} {int(day)} {year}", "%B %d %Y")
    except ValueError:
        return None
    if base < now:
        base = base.replace(year=year + 1)
    return base


def _apply_time(base: datetime, time_str: str) -> datetime:
    m = _TIME_RE.search(time_str)
    if not m:
        return base
    time_part, ampm = m.groups()
    try:
        fmt = "%I:%M %p" if ":" in time_part else "%I %p"
        t = datetime.strptime(f"{time_part} {ampm.upper()}", fmt)
        return base.replace(hour=t.hour, minute=t.minute)
    except ValueError:
        return base


def _img_src(img_el) -> str | None:
    if not img_el:
        return None
    src = img_el.get("src", "")
    if src and not src.endswith("/"):
        return src if src.startswith("http") else "https:" + src
    return None
