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


class BaseScraper(abc.ABC):
    # Names the source in log lines. A generic URL-driven scraper sets it per instance
    # (item 19: the bases log it, and a missing one failed every ical/spothopper source).
    SOURCE_NAME: str = ""
    SOURCE_TYPE: str

    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    def scrape(self) -> list[EventData]: ...

    def close(self) -> None:
        pass
