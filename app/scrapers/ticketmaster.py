from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from ..tz import tz_of
from .base import DEFAULT_HEADERS, BaseScraper, EventData

log = logging.getLogger(__name__)

_API = "https://app.ticketmaster.com/discovery/v2/events"
_PAGE_SIZE = 200

DISCOVERY_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
# Discovery refuses a deep page: ``size * page`` must stay under 1000, so a query returns at
# most 1000 events. A search with more must be split (a narrower query, date windows).
DISCOVERY_MAX_RESULTS = 1000


def page_discovery_events(
    client: httpx.Client,
    key: str,
    params: dict[str, str],
    *,
    page_size: int = _PAGE_SIZE,
    max_results: int = DISCOVERY_MAX_RESULTS,
) -> list[dict]:
    """Every raw event a Discovery ``events.json`` query returns, page by page.

    ``params`` is the query (``venueId``, ``latlong`` + ``radius``, ...); this adds the key,
    ``size``, ``page``, ``sort=date,asc`` and ``startDateTime`` (now, UTC) unless given. It
    stops at the last page, at an empty page, or when the next page would start at
    ``max_results`` (the API's cap). The key goes in the request only, never in a stored URL;
    the request log line is redacted by ``app.logredact`` (item 21).
    """
    base = {
        "size": str(page_size),
        "sort": "date,asc",
        "startDateTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    base.update(params)
    events: list[dict] = []
    page = 0
    while page * page_size < max_results:
        resp = client.get(DISCOVERY_EVENTS, params={**base, "apikey": key, "page": str(page)})
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("_embedded", {}).get("events", [])
        if not raw:
            break
        events.extend(raw)
        if page >= data.get("page", {}).get("totalPages", 1) - 1:
            break
        page += 1
    return events[:max_results]


class TicketmasterScraper(BaseScraper):
    SOURCE_NAME = "ticketmaster"
    SOURCE_TYPE = "api"

    def scrape(self) -> list[EventData]:
        key = self.config.ticketmaster_api_key
        if not key:
            raise ValueError("TICKETMASTER_API_KEY is not set")

        events: list[EventData] = []
        page = 0
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as client:
            while True:
                params = {
                    "apikey": key,
                    "postalCode": self.config.city_zip,
                    "radius": str(self.config.city_radius_miles),
                    "unit": "miles",
                    "size": str(_PAGE_SIZE),
                    "page": str(page),
                    "sort": "date,asc",
                    "startDateTime": now_iso,
                }
                resp = client.get(_API, params=params)
                resp.raise_for_status()
                data = resp.json()

                embedded = data.get("_embedded", {})
                raw_events = embedded.get("events", [])
                if not raw_events:
                    break

                for ev in raw_events:
                    parsed = _parse_event(ev)
                    if parsed:
                        events.append(parsed)

                page_info = data.get("page", {})
                total_pages = page_info.get("totalPages", 1)
                if page >= total_pages - 1:
                    break
                page += 1

        log.debug("Ticketmaster: fetched %d events", len(events))
        return events


def _parse_event(ev: dict) -> EventData | None:
    title = ev.get("name", "").strip()
    if not title:
        return None

    dates = ev.get("dates", {})
    start = dates.get("start", {})
    dt_str = start.get("dateTime")
    if dt_str:
        start_dt = (
            datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            .astimezone(tz_of())
            .replace(tzinfo=None)
        )
    elif start.get("localDate"):
        start_dt = datetime.fromisoformat(start["localDate"])
    else:
        return None

    # Venue
    venues = ev.get("_embedded", {}).get("venues", [{}])
    venue = venues[0] if venues else {}
    venue_name = venue.get("name", "")
    city = venue.get("city", {}).get("name", "")
    state = venue.get("state", {}).get("stateCode", "")
    addr_lines = venue.get("address", {})
    street = addr_lines.get("line1", "")
    venue_addr = ", ".join(filter(None, [street, city, state]))

    # Categories
    classifications = ev.get("classifications", [{}])
    cls = classifications[0] if classifications else {}
    segment = cls.get("segment", {}).get("name", "")
    genre = cls.get("genre", {}).get("name", "")
    cats = [c for c in [segment, genre] if c and c.lower() != "undefined"]

    # Image
    images = ev.get("images", [])
    img = next((i["url"] for i in images if i.get("ratio") == "16_9"), None)
    if not img and images:
        img = images[0].get("url")

    url = ev.get("url", "")

    return EventData(
        title=title,
        start_dt=start_dt,
        venue_name=venue_name,
        venue_addr=venue_addr,
        url=url,
        image_url=img,
        categories=cats,
        raw_data={"id": ev.get("id"), "source": "ticketmaster"},
        source_uid=ev.get("id") or None,
    )
