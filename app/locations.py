"""Search locations (item 30): the areas location-aware sources search.

The admin keeps a list of locations, each a label, what was typed (a US ZIP code, "City, ST" or
"lat,lon"; resolved offline by ``app/gazetteer.py``) and a radius in miles or km. No location
ships: a fresh install has none, and a location-aware source does nothing until one exists.

A location-aware source type (``LOCATION_AWARE`` on its scraper class: Ticketmaster regional,
Eventbrite, Meetup, and a plugin that sets it) runs once per location assigned to it
(``app/jobs.py``). A source's assignment is ``Source.location_ids``: NULL means every location
(the default), a list means exactly those. An event's identity key is built from its source's
name and the source's own id/URL for it (``app/dedup.py``), never from a location, so editing the
list never moves a feed UID.

``migrate`` runs at every start (``app/db.py``): it turns the settings this replaced
(``city_zip``, ``city_name``, ``city_radius_miles``, ``eventbrite_region``, ``meetup_location``)
into one location, once, and a regional Ticketmaster URL's ``latlong``/``radius``/``unit``
(item 22) into a location assigned to that source alone.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import areas, gazetteer
from .models import AppMeta, EventLocation, SearchLocation, Source
from .scrapers.base import SearchArea

log = logging.getLogger(__name__)

UNITS = ("miles", "km")
MAX_RADIUS = 500
MAX_LABEL = 60

# Fixed text per reason: the admin form and the import show these, never the input.
MESSAGES = {
    **gazetteer.MESSAGES,
    "label": f"A location needs a label of 1 to {MAX_LABEL} characters.",
    "label_taken": "Another location already has that label (or one that reads the same in a URL).",
    "radius": f"The radius must be a number from 1 to {MAX_RADIUS}.",
    "unit": "The radius unit must be miles or km.",
    "not_found": "No such location.",
}

NO_LOCATIONS = "No search locations: add one under Admin > Locations. Nothing was searched."
NONE_ASSIGNED = "This source is assigned no search location (Edit source). Nothing was searched."

META_MIGRATED = "locations_migrated"  # app_meta: the old settings were turned into a location


class LocationInvalid(ValueError):
    """A location cannot be saved; ``code`` keys ``MESSAGES``."""

    def __init__(self, code: str):
        super().__init__(MESSAGES[code])
        self.code = code


# --- the list -------------------------------------------------------------------------------


def clean_fields(
    label: str,
    entry: str,
    radius,
    unit: str,
    meetup_location: str = "",
    eventbrite_region: str = "",
) -> dict:
    """The columns of a valid location, resolved offline; raise ``LocationInvalid``.

    An empty entry is allowed only beside a Meetup location or an Eventbrite region of the
    location's own (what the migration keeps of old settings that never had coordinates).
    """
    label = " ".join((label or "").split())
    if not label or len(label) > MAX_LABEL:
        raise LocationInvalid("label")
    meetup_location = (meetup_location or "").strip()
    eventbrite_region = (eventbrite_region or "").strip()
    entry = " ".join((entry or "").split())
    try:
        radius = float(radius)
    except (TypeError, ValueError):
        raise LocationInvalid("radius") from None
    if not (1 <= radius <= MAX_RADIUS):
        raise LocationInvalid("radius")
    unit = (unit or "").strip().lower()
    if unit not in UNITS:
        raise LocationInvalid("unit")
    out = {
        "label": label,
        "entry": entry,
        "kind": "",
        "lat": None,
        "lon": None,
        "city": "",
        "state": "",
        "radius": radius,
        "unit": unit,
        "meetup_location": meetup_location or None,
        "eventbrite_region": eventbrite_region or None,
    }
    if not entry and (meetup_location or eventbrite_region):
        return out
    try:
        r = gazetteer.resolve(entry)
    except gazetteer.LocationRefused as exc:
        raise LocationInvalid(exc.code) from None
    out.update(kind=r.kind, lat=r.lat, lon=r.lon, city=r.city, state=r.state)
    return out


def _clean(session: Session, *, exclude_id: int | None = None, **fields) -> dict:
    out = clean_fields(**fields)
    q = session.query(SearchLocation.id).filter(
        func.lower(SearchLocation.label) == out["label"].lower()
    )
    if exclude_id is not None:
        q = q.filter(SearchLocation.id != exclude_id)
    if q.first() is not None:
        raise LocationInvalid("label_taken")
    # Item 31: an area is addressed by the slug of its label, so no two labels share one.
    slug = areas.slug(out["label"])
    if slug and any(
        areas.slug_of(r) == slug for r in session.query(SearchLocation) if r.id != exclude_id
    ):
        raise LocationInvalid("label_taken")
    return out


def by_label(session: Session, label: str) -> SearchLocation | None:
    return (
        session.query(SearchLocation)
        .filter(func.lower(SearchLocation.label) == " ".join(label.split()).lower())
        .first()
    )


def export_entry(row: SearchLocation) -> dict:
    """A location as the import/export file writes it (``app/sources.py``)."""
    out: dict = {
        "label": row.label,
        "where": row.entry or "",
        "radius": int(row.radius) if float(row.radius).is_integer() else float(row.radius),
        "unit": row.unit,
    }
    if row.meetup_location:
        out["meetup_location"] = row.meetup_location
    if row.eventbrite_region:
        out["eventbrite_region"] = row.eventbrite_region
    return out


def add(session: Session, **fields) -> SearchLocation:
    """Validate and add a location (``label``, ``entry``, ``radius``, ``unit`` and the optional
    ``meetup_location``/``eventbrite_region``); raise ``LocationInvalid``. The caller commits."""
    row = SearchLocation(**_clean(session, **fields))
    session.add(row)
    session.flush()
    return row


def update(session: Session, location_id: int, **fields) -> SearchLocation:
    row = session.get(SearchLocation, location_id)
    if row is None:
        raise LocationInvalid("not_found")
    for k, v in _clean(session, **fields, exclude_id=location_id).items():
        setattr(row, k, v)
    session.flush()
    return row


def delete(session: Session, location_id: int) -> None:
    """Remove a location, its event links, and its id from every source's assignment and area."""
    session.query(EventLocation).filter_by(location_id=location_id).delete()
    session.query(Source).filter_by(area_id=location_id).update({Source.area_id: None})
    for src in session.query(Source).filter(Source.location_ids.isnot(None)).all():
        ids = assigned_ids(src)
        if ids is not None and location_id in ids:
            assign(src, [i for i in ids if i != location_id])
    session.query(SearchLocation).filter_by(id=location_id).delete()
    session.flush()


def all_locations(session: Session) -> list[SearchLocation]:
    return session.query(SearchLocation).order_by(SearchLocation.label).all()


# --- a source's assignment --------------------------------------------------------------------


def assigned_ids(src: Source) -> list[int] | None:
    """The location ids assigned to ``src``; None = every location."""
    if src.location_ids is None:
        return None
    try:
        ids = json.loads(src.location_ids)
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []


def assign(src: Source, ids: list[int] | None) -> None:
    """Assign exactly ``ids`` (in order, without repeats) to ``src``; None = every location."""
    if ids is None:
        src.location_ids = None
        return
    out: list[int] = []
    for i in ids:
        if int(i) not in out:
            out.append(int(i))
    src.location_ids = json.dumps(out)


def area(row: SearchLocation) -> SearchArea:
    return SearchArea(
        id=row.id,
        label=row.label,
        lat=row.lat,
        lon=row.lon,
        radius=float(row.radius),
        unit=row.unit or "miles",
        city=row.city or "",
        state=row.state or "",
        meetup_location=row.meetup_location or "",
        eventbrite_region=row.eventbrite_region or "",
    )


def areas_for(session: Session, src: Source) -> list[SearchArea]:
    """The locations ``src`` runs over; raise ``ValueError`` (fixed text) when there are none."""
    rows = session.query(SearchLocation).order_by(SearchLocation.id).all()
    if not rows:
        raise ValueError(NO_LOCATIONS)
    ids = assigned_ids(src)
    if ids is not None:
        by_id = {r.id: r for r in rows}
        rows = [by_id[i] for i in ids if i in by_id]
        if not rows:
            raise ValueError(NONE_ASSIGNED)
    return [area(r) for r in rows]


def link_events(
    session: Session, found: dict[int, set[int]], searched: set[int], replace: bool
) -> None:
    """Record which locations each event row was found for. ``found`` is row id -> location ids.

    With ``replace`` (every location of the source was searched successfully), a returned row's
    links among ``searched`` become exactly the locations that returned it; otherwise links are
    only added, so a failed search never takes an area away from an event.
    """
    if not found:
        return
    existing: dict[int, set[int]] = {}
    for ev_id, loc_id in session.query(EventLocation.event_id, EventLocation.location_id).filter(
        EventLocation.event_id.in_(list(found))
    ):
        existing.setdefault(ev_id, set()).add(loc_id)
    for ev_id, locs in found.items():
        have = existing.get(ev_id, set())
        for loc_id in locs - have:
            session.add(EventLocation(event_id=ev_id, location_id=loc_id))
        if replace:
            stale = (have & searched) - locs
            if stale:
                session.query(EventLocation).filter(
                    EventLocation.event_id == ev_id, EventLocation.location_id.in_(list(stale))
                ).delete(synchronize_session=False)
    session.flush()


# --- the migration (item 30) ------------------------------------------------------------------


def _unique_label(session: Session, label: str) -> str:
    base, n = label[:MAX_LABEL], 2
    while (
        session.query(SearchLocation.id)
        .filter(func.lower(SearchLocation.label) == label.lower())
        .first()
    ):
        suffix = f" ({n})"
        label, n = base[: MAX_LABEL - len(suffix)] + suffix, n + 1
    return label


def _nearest(lat: float, lon: float) -> tuple[str, str]:
    try:
        return gazetteer.nearest_place(lat, lon)
    except OSError:  # the bundled files are missing: a start-up must not fail on a label
        log.warning("Locations: the Gazetteer files cannot be read", exc_info=True)
        return "", ""


def _place_label(city: str, state: str, fallback: str) -> str:
    return f"{city}, {state}" if city else (fallback or "Default")


def _bare_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _migrate_regional_urls(session: Session) -> None:
    """A regional ``ticketmaster`` URL that still carries an area becomes a location of its own,
    assigned to that source alone; the URL keeps only its scheme, host and path."""
    from .scrapers import ticketmaster
    from .sources import LOCATION_MISSING_MARK

    for src in session.query(Source).filter_by(type="ticketmaster").all():
        if src.last_error and src.last_error.startswith(LOCATION_MISSING_MARK):
            src.last_error = None  # item 22's "no area in the URL": the area is a location now
        loc = ticketmaster.regional_location(src.url or "")
        if loc is None:
            continue
        lat_s, lon_s = loc["latlong"].split(",")
        lat, lon, radius = float(lat_s), float(lon_s), float(loc["radius"])
        row = (
            session.query(SearchLocation)
            .filter_by(lat=lat, lon=lon, radius=radius, unit=loc["unit"])
            .order_by(SearchLocation.id)
            .first()
        )
        if row is None:
            city, state = _nearest(lat, lon)
            row = SearchLocation(
                label=_unique_label(
                    session, _place_label(city, state, src.display_name or src.name)
                ),
                entry=f"{lat_s},{lon_s}",
                kind="latlong",
                lat=lat,
                lon=lon,
                radius=radius,
                unit=loc["unit"],
                city=city,
                state=state,
            )
            session.add(row)
            session.flush()
            log.info("Locations: %s's area became location %r", src.name, row.label)
        ids = assigned_ids(src) or []
        assign(src, [*ids, row.id])
        src.url = _bare_url(src.url)


def _migrate_settings(session: Session, cfg) -> None:
    """The old single-city settings become one location, once per database."""
    if session.get(AppMeta, META_MIGRATED) is not None:
        return
    zip_ = str(getattr(cfg, "city_zip", "") or "").strip()
    name = str(getattr(cfg, "city_name", "") or "").strip()
    eventbrite_region = str(getattr(cfg, "eventbrite_region", "") or "").strip()
    meetup_location = str(getattr(cfg, "meetup_location", "") or "").strip()
    try:
        radius = float(getattr(cfg, "city_radius_miles", 25) or 25)
    except (TypeError, ValueError):
        radius = 25.0
    if any((zip_, name, eventbrite_region, meetup_location)):
        resolved, entry = None, ""
        for candidate in (zip_, name):
            if not candidate:
                continue
            try:
                resolved, entry = gazetteer.resolve(candidate), candidate
                break
            except (gazetteer.LocationRefused, OSError):
                log.warning("Locations: an old location setting does not resolve; kept as text")
        if resolved is not None or eventbrite_region or meetup_location:
            row = SearchLocation(
                label=_unique_label(session, name or "Default"),
                entry=entry,
                kind=resolved.kind if resolved else "",
                lat=resolved.lat if resolved else None,
                lon=resolved.lon if resolved else None,
                radius=min(max(radius, 1.0), float(MAX_RADIUS)),
                unit="miles",
                city=resolved.city if resolved else "",
                state=resolved.state if resolved else "",
                meetup_location=meetup_location or None,
                eventbrite_region=eventbrite_region or None,
            )
            session.add(row)
            log.info("Locations: the old city settings became location %r", row.label)
    session.add(AppMeta(key=META_MIGRATED, value=datetime.now().isoformat(timespec="seconds")))


def migrate(session: Session, cfg) -> None:
    """Run at every start; the caller commits. Idempotent."""
    _migrate_regional_urls(session)
    _migrate_settings(session, cfg)
    session.flush()
