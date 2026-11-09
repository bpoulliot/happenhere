"""Ticketmaster through the Discovery API: the pager (item 12) and the regional source (item 22).

A regional ``ticketmaster`` source searches the search locations assigned to it (item 30): it is
location-aware, so the scrape run calls it once per location, and each search is
``latlong=<lat>,<lon>&radius=<n>&unit=<miles|km>`` from that location. Its URL carries no area
any more (item 22 kept the area in the URL; ``app/locations.migrate`` moves such a URL's area to a
location of its own). ``postalCode`` is not used: with it the API returns only the events at
venues inside that ZIP and ignores ``radius``.

Discovery returns at most 1,000 results per query, so a regional scrape runs over date windows:
the look-ahead is split into ``WINDOW_DAYS`` windows, a window whose ``page.totalElements`` is
over the cap is halved and re-run, and events are de-duplicated by Ticketmaster id. A window of
one day still over the cap keeps its first 1,000, and the scrape says so (log line and the
source's ``last_error``). Requests are paced to ``MIN_REQUEST_INTERVAL`` and stop at
``MAX_REQUESTS_PER_SCRAPE``, recorded the same way. A scrape that stopped short retires nothing.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx

from ..tz import tz_of
from .base import DEFAULT_HEADERS, BaseScraper, EventData

log = logging.getLogger(__name__)

_PAGE_SIZE = 200

API_HOST = "app.ticketmaster.com"
_EVENTS_PATHS = ("/discovery/v2/events", "/discovery/v2/events.json")
DISCOVERY_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
# Discovery refuses a deep page: ``size * page`` must stay under 1000, so a query returns at
# most 1000 events. A search with more must be split (a narrower query, date windows).
DISCOVERY_MAX_RESULTS = 1000

# The regional scrape (item 22).
WINDOW_DAYS = 30  # the look-ahead is first split into windows of this many days
MIN_WINDOW = timedelta(days=1)  # a window this short is never halved again
MIN_REQUEST_INTERVAL = 1.0  # seconds between two requests to the API
MAX_REQUESTS_PER_SCRAPE = 100  # hard ceiling on requests in one scrape of one source
_UNITS = ("miles", "km")

# Fixed text (no request data, no exception text): the admin form, the import and the scraper.
LOCATION_IN_URL = (
    "A regional Ticketmaster source takes its areas from the search locations (Admin > "
    "Locations), not from its URL: use https://app.ticketmaster.com/discovery/v2/events with no "
    "latlong, radius, unit or postalCode, and add the area as a location."
)
NO_COORDINATES = "This search location has no coordinates: edit it (Admin > Locations)."
_AREA_PARAMS = ("latlong", "radius", "unit", "postalcode", "geopoint")

# Indirections the tests replace: no test sleeps or depends on the wall clock.
_sleep = time.sleep
_clock = time.monotonic


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def page_discovery_events(
    client: httpx.Client,
    key: str,
    params: dict[str, str],
    *,
    page_size: int = _PAGE_SIZE,
    max_results: int = DISCOVERY_MAX_RESULTS,
    on_total=None,
) -> list[dict]:
    """Every raw event a Discovery ``events.json`` query returns, page by page.

    ``params`` is the query (``venueId``, ``latlong`` + ``radius``, ...); this adds the key,
    ``size``, ``page``, ``sort=date,asc`` and ``startDateTime`` (now, UTC) unless given. It
    stops at the last page, at an empty page, or when the next page would start at
    ``max_results`` (the API's cap). ``on_total``, if given, is called with the first page's
    ``page.totalElements`` before any further page is fetched (it may raise to stop there).
    The key goes in the request only, never in a stored URL; the request log line is redacted
    by ``app.logredact`` (item 21).
    """
    base = {
        "size": str(page_size),
        "sort": "date,asc",
        "startDateTime": _iso(_now()),
    }
    base.update(params)
    events: list[dict] = []
    page = 0
    while page * page_size < max_results:
        resp = client.get(DISCOVERY_EVENTS, params={**base, "apikey": key, "page": str(page)})
        resp.raise_for_status()
        data = resp.json()
        if page == 0 and on_total is not None:
            on_total(int(data.get("page", {}).get("totalElements", 0) or 0))
        raw = data.get("_embedded", {}).get("events", [])
        if not raw:
            break
        events.extend(raw)
        if page >= data.get("page", {}).get("totalPages", 1) - 1:
            break
        page += 1
    return events[:max_results]


# --- The regional source's location ----------------------------------------------------------


def _number(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def regional_location(url: str) -> dict[str, str] | None:
    """The ``latlong``/``radius``/``unit`` of a regional source URL, or ``None`` if it has none.

    Only these three parameters are taken from the URL; anything else in it is ignored. A URL
    carrying ``apikey`` is refused: the key lives in config, never in a stored URL.
    """
    p = urlparse(url or "")
    if p.scheme != "https" or (p.hostname or "").lower() != API_HOST:
        return None
    if p.path.rstrip("/") not in _EVENTS_PATHS:
        return None
    q = {k.lower(): v[0].strip() for k, v in parse_qs(p.query).items()}
    if "apikey" in q:
        return None
    parts = q.get("latlong", "").split(",")
    if len(parts) != 2:
        return None
    lat, lon = (_number(x) for x in parts)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    radius = _number(q.get("radius", ""))
    if radius is None or radius <= 0:
        return None
    unit = (q.get("unit") or "miles").lower()
    if unit not in _UNITS:
        return None
    return {
        "latlong": f"{parts[0].strip()},{parts[1].strip()}",
        "radius": q["radius"],
        "unit": unit,
    }


def location_problem(url: str) -> str | None:
    """The fixed refusal text when ``url`` still carries an area of its own (item 30), else None.

    The area is a search location now; ``regional_location`` reads an old URL's area for the
    migration only.
    """
    q = {k.lower() for k in parse_qs(urlparse(url or "").query)}
    return LOCATION_IN_URL if q & set(_AREA_PARAMS) else None


def area_params(location) -> dict[str, str]:
    """The Discovery area query of a search location (``app.scrapers.base.SearchArea``)."""
    if location is None:
        raise ValueError("no search location")
    if location.lat is None or location.lon is None:
        raise ValueError(NO_COORDINATES)
    return {
        "latlong": f"{location.lat},{location.lon}",
        "radius": location.radius_text,
        "unit": location.unit,
    }


# --- Date windows -----------------------------------------------------------------------------


def split_windows(
    start: datetime, end: datetime, size: timedelta
) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` as consecutive windows of at most ``size``: each one's end is the next
    one's start, the first starts at ``start`` and the last ends at ``end``."""
    out: list[tuple[datetime, datetime]] = []
    a = start
    while a < end:
        b = min(a + size, end)
        out.append((a, b))
        a = b
    return out


def halve(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Two windows covering ``[start, end)``, split at a whole second (the API's precision)."""
    mid = start + timedelta(seconds=(end - start) // timedelta(seconds=1) // 2)
    return [(start, mid), (mid, end)]


class _OverCap(Exception):
    def __init__(self, total: int):
        self.total = total


class _CeilingReached(Exception):
    pass


class _PacedClient:
    """``client.get`` at most once per ``MIN_REQUEST_INTERVAL``, refusing past the ceiling."""

    def __init__(self, client: httpx.Client, ceiling: int):
        self.client = client
        self.ceiling = ceiling
        self.times: list[float] = []

    def get(self, *args, **kw):
        if len(self.times) >= self.ceiling:
            raise _CeilingReached
        if self.times:
            wait = self.times[-1] + MIN_REQUEST_INTERVAL - _clock()
            if wait > 0:
                _sleep(wait)
        self.times.append(_clock())
        return self.client.get(*args, **kw)


class TicketmasterScraper(BaseScraper):
    """The regional source: every event within one search location's radius (item 30)."""

    SOURCE_TYPE = "api"
    LOCATION_AWARE = True

    def __init__(self, config, source_url: str = "", venue_name: str = "", location=None):
        super().__init__(config)
        self.SOURCE_NAME = venue_name or "ticketmaster"
        if location is not None:
            self.SOURCE_NAME = f"{self.SOURCE_NAME} [{location.label}]"
        self.source_url = source_url
        self.location = location
        self.warnings: list[str] = []  # stored as the source's last_error by app.jobs
        self.incomplete = False  # True: some events were not read, so nothing retires
        self.windows: list[dict] = []  # one record per request window: the probe's evidence
        self.request_times: list[float] = []

    @property
    def requests(self) -> int:
        return len(self.request_times)

    def _warn(self, msg: str) -> None:
        log.warning("%s: %s", self.SOURCE_NAME, msg)
        self.warnings.append(msg)
        self.incomplete = True

    def scrape(self) -> list[EventData]:
        loc = area_params(self.location)
        key = self.config.ticketmaster_api_key
        if not key:
            raise ValueError("TICKETMASTER_API_KEY is not set")

        start = _now()
        end = start + timedelta(days=max(1, int(self.config.feed_lookahead_days)))
        todo = deque(split_windows(start, end, timedelta(days=WINDOW_DAYS)))
        raw_by_id: dict[str, dict] = {}
        no_id: list[dict] = []
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as http:
            client = _PacedClient(http, MAX_REQUESTS_PER_SCRAPE)
            self.request_times = client.times
            while todo:
                a, b = todo.popleft()
                rec: dict = {"start": a, "end": b, "total": None, "action": "read"}
                self.windows.append(rec)
                splittable = b - a > MIN_WINDOW

                def on_total(total: int, rec=rec, splittable=splittable) -> None:
                    rec["total"] = total
                    if total > DISCOVERY_MAX_RESULTS and splittable:
                        raise _OverCap(total)

                try:
                    raw = page_discovery_events(
                        client,  # type: ignore[arg-type]
                        key,
                        {**loc, "startDateTime": _iso(a), "endDateTime": _iso(b)},
                        on_total=on_total,
                    )
                except _OverCap:
                    rec["action"] = "halved"
                    todo.extendleft(reversed(halve(a, b)))
                    continue
                except _CeilingReached:
                    rec["action"] = "not read"
                    self._warn(
                        f"request ceiling ({MAX_REQUESTS_PER_SCRAPE}) reached at the window "
                        f"from {_iso(a)}: later events were not read"
                    )
                    break
                if (rec["total"] or 0) > DISCOVERY_MAX_RESULTS:
                    rec["action"] = "capped"
                    self._warn(
                        f"{rec['total']:,} events from {_iso(a)} to {_iso(b)}, over the API's "
                        f"cap of {DISCOVERY_MAX_RESULTS:,} in a window of one day: "
                        f"the first {DISCOVERY_MAX_RESULTS:,} kept"
                    )
                rec["fetched"] = len(raw)
                for r in raw:
                    if r.get("id"):
                        raw_by_id.setdefault(r["id"], r)
                    else:
                        no_id.append(r)

        events = [e for r in [*raw_by_id.values(), *no_id] if (e := _parse_event(r, self.config))]
        log.info(
            "%s: %d events, %d windows, %d requests",
            self.SOURCE_NAME,
            len(events),
            len(self.windows),
            self.requests,
        )
        return events


def _parse_event(ev: dict, cfg=None) -> EventData | None:
    title = ev.get("name", "").strip()
    if not title:
        return None

    dates = ev.get("dates", {})
    start = dates.get("start", {})
    dt_str = start.get("dateTime")
    if dt_str:
        start_dt = (
            datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            .astimezone(tz_of(cfg))
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
