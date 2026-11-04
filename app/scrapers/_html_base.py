"""Shared helpers for httpx + BeautifulSoup HTML scrapers."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

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


def extract_jsonld_events(soup: BeautifulSoup) -> list[dict]:
    """Extract schema.org Event objects from JSON-LD script tags."""
    results = []
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
        for item in items:
            if isinstance(item, dict) and item.get("@type") in (
                "Event",
                "MusicEvent",
                "TheaterEvent",
            ):
                results.append(item)
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
            dt = dt.astimezone(ZoneInfo("America/Denver")).replace(tzinfo=None)
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
