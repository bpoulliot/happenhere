"""Shared helpers for httpx + BeautifulSoup HTML scrapers."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from ..tz import tz_of
from .base import DEFAULT_HEADERS, EventData

log = logging.getLogger(__name__)


def fetch_soup(url: str, *, timeout: int = 30) -> BeautifulSoup:
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def fetch_soup_cloudscraper(url: str) -> BeautifulSoup:
    """Use cloudscraper for Cloudflare-protected pages."""
    import cloudscraper  # type: ignore

    scraper = cloudscraper.create_scraper()
    resp = scraper.get(url, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


# Keys under which a top-level or ``@graph`` item (a ``Place``, an ``Organization``) lists its
# events. ``Events`` is not schema.org, but a ticketing platform publishes it (items 12, 23).
NESTED_EVENT_KEYS = ("event", "events", "Events")


def is_event_type(item: object) -> bool:
    """Whether a JSON-LD item is a schema.org Event: ``Event``, any ``…Event`` subtype
    (``EducationEvent``, ``ComedyEvent``) or ``Festival``; ``@type`` a string or a list."""
    if not isinstance(item, dict):
        return False
    types = item.get("@type")
    if not isinstance(types, list):
        types = [types]
    return any(isinstance(t, str) and (t.endswith("Event") or t == "Festival") for t in types)


def extract_jsonld_events(soup: BeautifulSoup) -> list[dict]:
    """Extract schema.org Event objects from JSON-LD script tags.

    Reads top-level items and ``@graph`` items, and the events nested one level down under
    any such item's ``event`` / ``events`` / ``Events`` key (a list or a single object). A
    page that lists one event both top level and nested gives it once: one event is the
    same ``url`` at the same ``startDate``, or with no ``url`` the same ``(name, startDate)``.
    """
    results: list[dict] = []
    seen: set[tuple] = set()

    def add(item: object) -> None:
        if not isinstance(item, dict) or not is_event_type(item):
            return
        start = str(item.get("startDate") or "")
        url = item.get("url")
        key = ("url", url, start) if url else ("name", str(item.get("name") or ""), start)
        if key in seen:
            return
        seen.add(key)
        results.append(item)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and data.get("@graph"):
            items = data["@graph"]
        else:
            items = [data]
        if not isinstance(items, list):
            items = [items]
        for item in items:
            if not isinstance(item, dict):
                continue
            add(item)
            for k in NESTED_EVENT_KEYS:
                nested = item.get(k)
                for sub in nested if isinstance(nested, list) else [nested]:
                    add(sub)
    return results


def parse_jsonld_event(item: dict, default_venue: str) -> EventData | None:
    name = item.get("name", "").strip()
    if not name:
        return None

    start_raw = item.get("startDate", "")
    try:
        start_dt = _parse_dt(start_raw)
    except Exception:
        return None
    if start_dt is None:
        return None

    end_raw = item.get("endDate", "")
    end_dt = None
    try:
        end_dt = _parse_dt(end_raw)
    except Exception:
        pass

    location = item.get("location", {})
    if isinstance(location, dict):
        venue_name = location.get("name", default_venue)
        addr = location.get("address", {})
        if isinstance(addr, dict):
            parts = filter(
                None,
                [
                    addr.get("streetAddress"),
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                ],
            )
            venue_addr = ", ".join(parts)
        else:
            venue_addr = str(addr) if addr else None
    else:
        venue_name = default_venue
        venue_addr = None

    url = item.get("url") or item.get("@id") or None
    img = None
    image = item.get("image")
    if isinstance(image, str):
        img = image
    elif isinstance(image, dict):
        img = image.get("url")
    elif isinstance(image, list) and image:
        img = image[0] if isinstance(image[0], str) else image[0].get("url")

    desc = item.get("description", "").strip() or None

    return EventData(
        title=name,
        start_dt=start_dt,
        end_dt=end_dt,
        venue_name=venue_name,
        venue_addr=venue_addr,
        description=desc,
        url=url,
        image_url=img,
        categories=[],
    )


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    # ISO with timezone
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz_of()).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    # Date only
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw[:10], fmt)
        except ValueError:
            continue
    return None
