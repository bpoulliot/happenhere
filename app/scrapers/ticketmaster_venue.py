"""Ticketmaster events at one venue (or a few), through the Discovery API (item 12).

The source URL is a Discovery events URL carrying the venue ids and never the key:
``https://app.ticketmaster.com/discovery/v2/events?venueId=A[,B]`` (a venue often has
several ids). A ticketmaster.com venue page (``https://www.ticketmaster.com/<slug>/venue/<n>``)
is not one: its number is not a Discovery id. ``resolve_venue_url`` turns it into one once,
through venue search, when the source is detected or saved, so a scrape never searches.

The key is the configured ``TICKETMASTER_API_KEY``. Event times are converted from UTC to the
configured zone (``app.tz.tz_of``). Each event's identity is ``ticketmaster:<event id>``,
which a retitle does not change.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx

from ..tz import tz_of
from .base import DEFAULT_HEADERS, BaseScraper, EventData
from .ticketmaster import _parse_event, page_discovery_events

log = logging.getLogger(__name__)

API_HOST = "app.ticketmaster.com"
_EVENTS_PATHS = ("/discovery/v2/events", "/discovery/v2/events.json")
VENUES_API = f"https://{API_HOST}/discovery/v2/venues.json"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VENUE_PATH_RE = re.compile(r"^/(?P<slug>[^/]+)/venue/(?P<n>\d+)/?$")


# What the admin is told when a pasted URL cannot be resolved. Fixed text, keyed by reason, so
# no exception text reaches a response.
NOT_A_VENUE_URL, NO_KEY, NO_MATCH = "not_a_venue_url", "no_key", "no_match"
MESSAGES = {
    NOT_A_VENUE_URL: (
        "Not a Ticketmaster venue URL: paste a Discovery URL "
        "(https://app.ticketmaster.com/discovery/v2/events?venueId=<id>) "
        "or a ticketmaster.com /venue/<number> page."
    ),
    NO_KEY: (
        "TICKETMASTER_API_KEY is not set: it is needed to look up a venue page. Set it, "
        "or paste the Discovery URL with the venueId."
    ),
    NO_MATCH: (
        "No Ticketmaster venue matched this venue page: find the venue's Discovery id and paste "
        "https://app.ticketmaster.com/discovery/v2/events?venueId=<id>"
    ),
}


class VenueNotFound(ValueError):
    """A pasted URL that could not be resolved; ``reason`` keys its admin text in ``MESSAGES``."""

    def __init__(self, reason: str):
        super().__init__(MESSAGES[reason])
        self.reason = reason

    @property
    def message(self) -> str:
        return MESSAGES.get(self.reason, MESSAGES[NOT_A_VENUE_URL])


def venue_ids(url: str) -> list[str] | None:
    """The ``venueId`` list of a Discovery events URL, or ``None`` if it is not one."""
    p = urlparse(url or "")
    if p.scheme != "https" or (p.hostname or "").lower() != API_HOST:
        return None
    if p.path.rstrip("/") not in _EVENTS_PATHS:
        return None
    raw = parse_qs(p.query).get("venueId", [""])[0]
    ids = [i.strip() for i in raw.split(",") if i.strip()]
    if not ids or not all(_ID_RE.match(i) for i in ids):
        return None
    return ids


def venue_page_id(url: str) -> str | None:
    """The number of a ``ticketmaster.com/<slug>/venue/<n>`` page, or ``None``."""
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https"):
        return None
    if host != "ticketmaster.com" and not host.endswith(".ticketmaster.com"):
        return None
    m = _VENUE_PATH_RE.match(p.path)
    return m.group("n") if m else None


def is_venue_url(url: str) -> bool:
    """Whether the URL is one this type takes (decided from the URL alone, no request)."""
    return venue_ids(url) is not None or venue_page_id(url) is not None


def discovery_url(ids: list[str]) -> str:
    """The URL stored for a source: the venue ids, never the key."""
    return f"https://{API_HOST}/discovery/v2/events?venueId={','.join(ids)}"


def _keyword(url: str) -> str:
    """Search words from a venue page's slug: ``synthetic-hall-tickets-town`` -> ``synthetic hall``."""
    slug = _VENUE_PATH_RE.match(urlparse(url).path).group("slug")  # type: ignore[union-attr]
    name = re.split(r"-tickets(?:-|$)", slug, maxsplit=1)[0]
    return " ".join(w for w in name.split("-") if w)


def resolve_venue_url(url: str, key: str) -> str:
    """The Discovery URL to store for a pasted URL.

    A Discovery URL is normalised (the key, if pasted, dropped) without a request. A
    ticketmaster.com venue page is looked up once with ``venues.json?keyword=<slug words>``,
    keeping the venue whose ``url`` ends ``/venue/<n>``. Raises ``VenueNotFound`` with a
    message for the admin when that fails.
    """
    ids = venue_ids(url)
    if ids:
        return discovery_url(ids)
    n = venue_page_id(url)
    if n is None:
        raise VenueNotFound(NOT_A_VENUE_URL)
    if not key:
        raise VenueNotFound(NO_KEY)
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as client:
        resp = client.get(VENUES_API, params={"apikey": key, "keyword": _keyword(url)})
        resp.raise_for_status()
        venues = resp.json().get("_embedded", {}).get("venues", [])
    suffix = f"/venue/{n}"
    for v in venues:
        vurl = (v.get("url") or "").rstrip("/")
        vid = v.get("id") or ""
        if vurl.endswith(suffix) and _ID_RE.match(vid):
            return discovery_url([vid])
    raise VenueNotFound(NO_MATCH)


def parse_venue_event(raw: dict, cfg=None) -> EventData | None:
    """One Discovery event as an ``EventData``, keyed ``ticketmaster:<event id>``.

    ``dates.start.dateTime`` (UTC) becomes local time in ``cfg``'s zone (default: the loaded
    config). With ``dates.start.noSpecificTime`` the local date alone is the start: the
    ``dateTime`` Ticketmaster then sends is a placeholder, not a time.
    """
    start = raw.get("dates", {}).get("start", {})
    if start.get("noSpecificTime") and start.get("localDate"):
        raw = {**raw, "dates": {**raw["dates"], "start": {"localDate": start["localDate"]}}}
    ev = _parse_event(raw)
    if ev is None:
        return None
    dt = raw.get("dates", {}).get("start", {}).get("dateTime")
    if dt:
        ev.start_dt = (
            datetime.fromisoformat(dt.replace("Z", "+00:00"))
            .astimezone(tz_of(cfg))
            .replace(tzinfo=None)
        )
    ev.raw_data = {"id": raw.get("id"), "source": "ticketmaster_venue"}
    ev.source_uid = f"ticketmaster:{raw['id']}" if raw.get("id") else None
    return ev


class TicketmasterVenueScraper(BaseScraper):
    SOURCE_TYPE = "ticketmaster_venue"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or source_url
        self.source_url = source_url
        self.venue_name = venue_name

    def scrape(self) -> list[EventData]:
        ids = venue_ids(self.source_url)
        if not ids:
            raise ValueError(
                "source URL needs a Discovery venueId "
                f"(https://{API_HOST}/discovery/v2/events?venueId=<id>[,<id>]): "
                f"{self.source_url!r}"
            )
        key = self.config.ticketmaster_api_key
        if not key:
            raise ValueError("TICKETMASTER_API_KEY is not set")
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as client:
            raw = page_discovery_events(client, key, {"venueId": ",".join(ids)})
        events: list[EventData] = []
        for r in raw:
            ev = parse_venue_event(r, self.config)
            if ev:
                ev.venue_name = ev.venue_name or self.venue_name
                events.append(ev)
        log.debug("ticketmaster_venue %s: %d events", ",".join(ids), len(events))
        return events
