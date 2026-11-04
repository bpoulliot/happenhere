"""Meetup.com scraper — JS-rendered, no free API, scrape /find/ page."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from bs4 import BeautifulSoup

from ._html_base import extract_jsonld_events, parse_jsonld_event
from ._playwright_base import PlaywrightScraper
from .base import EventData

log = logging.getLogger(__name__)

# meetup.com/find/?eventType=inPerson&source=EVENTS&sortField=DATETIME
# Filter by location using lat/lon or zip in the search field
_SEARCH_URL_TPL = "https://www.meetup.com/find/?eventType=inPerson&source=EVENTS&sortField=DATETIME&location={location}&distance=tenMiles"
_VENUE = ""  # set from config


class MeetupScraper(PlaywrightScraper):
    SOURCE_NAME = "meetup"

    def _city_name(self) -> str:
        return getattr(self.config, "city_name", "") or "Local Events"

    def _get_url(self) -> str:
        loc = getattr(self.config, "meetup_location", "") or "us--co--Colorado+Springs"
        return _SEARCH_URL_TPL.format(location=loc)

    def _scrape_impl(self) -> list[EventData]:
        events: list[EventData] = []
        now = datetime.now()
        page = self._get_page()

        try:
            page.goto(self._get_url(), wait_until="domcontentloaded", timeout=35000)
            # Wait for event cards to appear
            try:
                page.wait_for_selector(
                    "[data-testid='event-card'], .event-card, article", timeout=10000
                )
            except Exception:
                pass
            time.sleep(3)

            # Scroll to load more events
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)

            soup = BeautifulSoup(page.content(), "lxml")
        finally:
            page.close()

        # Try JSON-LD first
        for item in extract_jsonld_events(soup):
            ev = parse_jsonld_event(item, self._city_name())
            if ev and ev.start_dt >= now:
                ev.categories = _guess_categories(ev.title)
                events.append(ev)

        if events:
            log.debug("Meetup: %d events via JSON-LD", len(events))
            return events

        # Fallback: scrape event cards
        # Meetup uses data-testid attributes consistently
        selectors = [
            "[data-testid='event-card']",
            ".event-card",
            "article",
            "[class*='eventCard']",
            "[class*='EventCard']",
        ]
        cards = []
        for sel in selectors:
            cards = soup.select(sel)
            if cards:
                break

        for card in cards:
            ev = _parse_meetup_card(card, now)
            if ev:
                events.append(ev)

        log.debug("Meetup: %d events via CSS scrape", len(events))
        return events


def _parse_meetup_card(card, now: datetime) -> EventData | None:
    # Title
    title_el = card.select_one(
        "[data-testid='event-title'], h3, h2, .event-title, [class*='title']"
    )
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    if not title or len(title) < 3:
        return None

    # Date/time
    date_el = card.select_one("time, [datetime], [data-testid='event-date'], [class*='date']")
    if not date_el:
        return None
    dt_str = date_el.get("datetime") or date_el.get_text(strip=True)
    try:
        start_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if start_dt.tzinfo:
            start_dt = start_dt.replace(tzinfo=None)
    except Exception:
        return None
    if start_dt < now:
        return None

    # Venue
    venue_el = card.select_one(
        "[data-testid='venue'], .venue, [class*='venue'], [class*='location']"
    )
    venue = venue_el.get_text(strip=True) if venue_el else _VENUE

    # URL
    link = card.find("a")
    url = link.get("href") if link else None
    if url and not url.startswith("http"):
        url = "https://www.meetup.com" + url

    return EventData(
        title=title,
        start_dt=start_dt,
        venue_name=venue or _VENUE,
        url=url,
        categories=_guess_categories(title),
    )


def _guess_categories(title: str) -> list[str]:
    t = title.lower()
    cats = ["Community"]
    if any(w in t for w in ("hike", "trail", "run", "bike", "cycling", "yoga", "fitness")):
        cats.append("Outdoors & Fitness")
    elif any(w in t for w in ("tech", "code", "python", "dev", "software", "data", "ai", "ml")):
        cats.append("Tech")
    elif any(w in t for w in ("beer", "brew", "wine", "tasting", "cocktail")):
        cats.append("Food & Drink")
    elif any(w in t for w in ("game", "board", "trivia", "rpg", "dnd")):
        cats.append("Games")
    elif any(w in t for w in ("music", "jam", "concert", "band")):
        cats.append("Music")
    elif any(w in t for w in ("art", "paint", "draw", "craft", "photo")):
        cats.append("Arts & Crafts")
    elif any(w in t for w in ("book", "read", "writing", "author")):
        cats.append("Books & Writing")
    return cats
