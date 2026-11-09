"""Areas (item 31): each search location's label is an area.

An event is in an area when it was found for that search location (``event_locations``, item 30)
or, having no such link, when its source's ``Source.area_id`` names it (a per-venue source is
assigned an area by the admin; a regional event no search has linked yet falls back to it). An
area is addressed in URLs by its *slug*, a lowercase ASCII form of the label, so a label with
spaces, commas or a slash is still a plain path segment: ``/feed/area/<slug>.ics`` and
``/events?area=<slug>``.

Nothing here touches an event's UID: an area only selects rows, and a row has one UID in every
feed it is in.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Query, Session

from .models import Event, EventLocation, SearchLocation, Source


def slug(label: str) -> str:
    """The URL form of a label: ``"New York, NY"`` -> ``"new-york-ny"``; "" if it has no
    ASCII letter or digit (``slug_of`` then falls back to ``area-<id>``)."""
    text = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def slug_of(row: SearchLocation) -> str:
    return slug(row.label) or f"area-{row.id}"


def by_slug(session: Session) -> dict[str, SearchLocation]:
    """Every area by slug. If two labels ever share one (the admin refuses that), the older
    location keeps it."""
    out: dict[str, SearchLocation] = {}
    for row in session.query(SearchLocation).order_by(SearchLocation.id):
        out.setdefault(slug_of(row), row)
    return out


def area_clause(area_ids: list[int]):
    """A filter on ``Event`` for events in any of ``area_ids``. It selects rows, never joins
    them, so an event in two chosen areas is one row."""
    linked = exists().where(
        EventLocation.event_id == Event.id, EventLocation.location_id.in_(area_ids)
    )
    has_links = exists().where(EventLocation.event_id == Event.id)
    in_source_area = exists().where(Source.id == Event.source_id, Source.area_id.in_(area_ids))
    return or_(linked, and_(~has_links, in_source_area))


def in_areas(query: Query, area_ids: list[int] | None) -> Query:
    """``query`` (of ``Event``) narrowed to ``area_ids``; None = every area (no filter)."""
    if area_ids is None:
        return query
    if not area_ids:
        return query.filter(False)
    return query.filter(area_clause(area_ids))


def pick(session: Session, slugs: list[str]) -> list[SearchLocation]:
    """The areas named by ``slugs`` (unknown ones dropped, repeats and order kept once)."""
    known = by_slug(session)
    out: list[SearchLocation] = []
    for s in dict.fromkeys(slugs):
        row = known.get(s)
        if row is not None:
            out.append(row)
    return out
