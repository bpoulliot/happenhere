"""Fathom Entertainment scraper — JS-rendered releases page.

Each release card has multiple screening dates (no year). We create one
event per upcoming date. Venue is listed as the known local theaters since
Fathom doesn't expose a per-zip theater filter reliably.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from ._playwright_base import PlaywrightScraper
from .base import EventData

log = logging.getLogger(__name__)

_URL = "https://www.fathomentertainment.com/releases/"
_VENUE = "Fathom Events (local theaters)"

# "Jun 2", "Aug 14", etc.
_DATE_RE = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})$")


class FathomScraper(PlaywrightScraper):
    SOURCE_NAME = "fathom"

    def _scrape_impl(self) -> list[EventData]:
        events: list[EventData] = []
        now = datetime.now()
        page = self._get_page()

        try:
            page.goto(_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            soup = BeautifulSoup(page.content(), "lxml")
        finally:
            page.close()

        seen: set[tuple] = set()
        for card in soup.select("a.posters-item"):
            title_el = card.select_one("h3.headline, h2.headline, h3, h2")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue

            # Optional series label (e.g. "The Met: Live in HD")
            series_el = card.select_one(".preheadline")
            if series_el:
                series = series_el.get_text(strip=True)
                if series and series not in title:
                    title = f"{series}: {title}"

            img = card.select_one("img")
            img_url = img["src"] if img else None
            url = card.get("href") or _URL

            for date_span in card.select(".date-list span"):
                date_text = date_span.get_text(strip=True)
                start_dt = _parse_short_date(date_text, now)
                if start_dt is None or start_dt < now:
                    continue
                key = (title, start_dt.date())
                if key in seen:
                    continue
                seen.add(key)
                events.append(
                    EventData(
                        title=title,
                        start_dt=start_dt,
                        venue_name=_VENUE,
                        url=url,
                        image_url=img_url,
                        categories=["Movies", "Fathom Events"],
                    )
                )

        log.debug("Fathom: %d events", len(events))
        return events


def _parse_short_date(text: str, now: datetime) -> datetime | None:
    """Parse 'Jun 2' into a datetime, rolling to next year if the date is past."""
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {now.year}", "%b %d %Y")
    except ValueError:
        return None
    if dt < now.replace(hour=0, minute=0, second=0, microsecond=0):
        try:
            dt = dt.replace(year=now.year + 1)
        except ValueError:
            return None
    return dt
