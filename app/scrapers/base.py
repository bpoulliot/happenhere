from __future__ import annotations

import abc
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class EventData:
    title: str
    start_dt: datetime
    venue_name: str
    end_dt: datetime | None = None
    venue_addr: str | None = None
    description: str | None = None
    url: str | None = None
    image_url: str | None = None
    categories: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)
    # The source's own identity for this event (an iCal UID, a platform's event id), if it
    # gives one. It decides the feed UID before the event URL does (app/dedup.py).
    source_uid: str | None = None

    @property
    def dedup_hash(self) -> str:
        def _norm(s: str | None) -> str:
            return re.sub(r"\s+", " ", (s or "").lower().strip())

        key = f"{_norm(self.title)}|{self.start_dt.date().isoformat()}|{_norm(self.venue_name)}"
        return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class SearchArea:
    """Item 30: one search location, as a location-aware scraper receives it.

    A scraper class that sets ``LOCATION_AWARE = True`` takes ``location=`` (one of these) in
    its constructor and runs once per location assigned to its source (``app/jobs.py``).
    ``lat``/``lon`` are None for a location saved without coordinates; ``city``/``state`` are
    empty outside the US.
    """

    id: int
    label: str
    lat: float | None
    lon: float | None
    radius: float
    unit: str  # "miles" | "km"
    city: str = ""
    state: str = ""
    meetup_location: str = ""
    eventbrite_region: str = ""

    @property
    def radius_text(self) -> str:
        """The radius as a query parameter: "25", or "12.5"."""
        return f"{self.radius:g}"


class BaseScraper(abc.ABC):
    # Names the source in log lines. A generic URL-driven scraper sets it per instance
    # (item 19: the bases log it, and a missing one failed every ical/spothopper source).
    SOURCE_NAME: str = ""
    SOURCE_TYPE: str
    # Item 30: True = the type searches an area; it takes ``location=`` and fans out.
    LOCATION_AWARE: bool = False

    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    def scrape(self) -> list[EventData]: ...

    def close(self) -> None:
        pass
