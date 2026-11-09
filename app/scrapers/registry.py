"""Dynamic scraper registry.

Scrapers are looked up by `source.type` (not source.name) and instantiated
with the source's URL and display_name so no hardcoded venue data is needed.
"""

from __future__ import annotations

from .base import BaseScraper

# Regional aggregators keep their own classes. They are location-aware (item 30): the scrape run
# calls them once per search location assigned to the source (app/jobs.py).
from .eventbrite import EventbriteScraper
from .generic import (
    GenericICalScraper,
    GenericJsonLdScraper,
    GenericPlaywrightJsonLdScraper,
    GenericSpotHopperScraper,
    GenericSquarespaceScraper,
)
from .meetup import MeetupScraper
from .ticketmaster import TicketmasterScraper
from .ticketmaster_venue import TicketmasterVenueScraper

try:
    from .fathom import FathomScraper

    _HAS_FATHOM = True
except ImportError:
    _HAS_FATHOM = False

_TYPE_MAP: dict[str, type[BaseScraper]] = {
    "jsonld": GenericJsonLdScraper,
    "ical": GenericICalScraper,
    "spothopper": GenericSpotHopperScraper,
    "squarespace": GenericSquarespaceScraper,
    "playwright_jsonld": GenericPlaywrightJsonLdScraper,
    "ticketmaster": TicketmasterScraper,
    "ticketmaster_venue": TicketmasterVenueScraper,
    "eventbrite": EventbriteScraper,
    "meetup": MeetupScraper,
}

if _HAS_FATHOM:
    _TYPE_MAP["fathom"] = FathomScraper

# Human-readable labels for the admin UI
TYPE_LABELS: dict[str, str] = {
    "jsonld": "JSON-LD / HTML",
    "ical": "iCal Feed",
    "spothopper": "SpotHopper Widget",
    "squarespace": "Squarespace Events",
    "playwright_jsonld": "JavaScript (JSON-LD)",
    "ticketmaster": "Ticketmaster (regional)",
    "ticketmaster_venue": "Ticketmaster venue",
    "eventbrite": "Eventbrite (regional)",
    "meetup": "Meetup (regional)",
    "fathom": "Fathom Entertainment",
}

# Map detection result type → registered scraper type
DETECTION_TYPE_MAP: dict[str, str] = {
    "jsonld": "jsonld",
    "ical": "ical",
    "spothopper": "spothopper",
    "squarespace": "squarespace",
    "playwright_jsonld": "playwright_jsonld",
    "ticketmaster_venue": "ticketmaster_venue",
}


def has_type(source_type: str) -> bool:
    return source_type in _TYPE_MAP


def is_location_aware(source_type: str) -> bool:
    """Whether a type searches an area and so runs once per search location (item 30)."""
    cls = _TYPE_MAP.get(source_type)
    return bool(cls is not None and getattr(cls, "LOCATION_AWARE", False))


def get_scraper(source, config, location=None) -> BaseScraper | None:
    """Instantiate the right scraper for a Source row; ``location`` (a ``SearchArea``) is
    passed to a location-aware type only."""
    cls = _TYPE_MAP.get(source.type)
    if cls is None:
        return None
    kw = {"location": location} if getattr(cls, "LOCATION_AWARE", False) else {}

    # These regional aggregators don't use URL/venue_name from source
    if source.type in ("eventbrite", "meetup", "fathom"):
        return cls(config, **kw)

    return cls(config, source_url=source.url or "", venue_name=source.display_name or "", **kw)


def register_type(key: str, cls: type[BaseScraper], label: str) -> None:
    """Add a scraper type (used by the plugin loader). Never replaces a registered type."""
    if key in _TYPE_MAP:
        raise ValueError(f"scraper type {key!r} is already registered")
    _TYPE_MAP[key] = cls
    TYPE_LABELS[key] = label


def unregister_type(key: str) -> None:
    _TYPE_MAP.pop(key, None)
    TYPE_LABELS.pop(key, None)


def scraper_types() -> list[tuple[str, str]]:
    """Return (type_key, label) pairs for the admin UI type selector."""
    return [(k, TYPE_LABELS.get(k, k)) for k in _TYPE_MAP]
