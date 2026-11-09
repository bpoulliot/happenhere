from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime

from . import canary, locations
from .config import get_config
from .db import get_session
from .dedup import identity_key, retire_unreturned, upsert_events
from .logredact import redact
from .models import Event, Source
from .scrapers.registry import get_scraper, has_type, is_location_aware

log = logging.getLogger(__name__)


def run_all_scrapers() -> None:
    log.info("Starting scrape run")
    cfg = get_config()
    with get_session() as session:
        sources = session.query(Source).filter_by(enabled=True).all()
        seen: set[int] = set()  # rows any source returned this run: none of them retires
        for src in sources:
            if not has_type(src.type or ""):
                log.warning("No scraper for type %r (source: %s)", src.type, src.name)
                continue
            try:
                new, updated, retired, _total = _run_source(session, src, cfg, seen, pause=True)
                log.info("%s: %d new, %d updated, %d retired", src.name, new, updated, retired)
            except Exception as exc:
                # A failed flush leaves the session unusable until rolled back (item 18): without
                # this the commit below raised and ended the whole run.
                session.rollback()
                src.error_count = (src.error_count or 0) + 1
                src.last_error = redact(str(exc))
                session.commit()
                log.exception("Scraper %s failed: %s", src.name, exc)

    try:
        canary.check()
    except Exception:  # an alerting fault must never cost the run its image backfill
        log.exception("canary check failed")
    _backfill_pexels_images()
    log.info("Scrape run complete")


def run_single_scraper(source_name: str) -> dict:
    cfg = get_config()
    with get_session() as session:
        src = session.query(Source).filter_by(name=source_name).first()
        if src is None:
            return {"error": f"Source '{source_name}' not found"}
        if not has_type(src.type or ""):
            return {"error": f"No scraper class for type '{src.type}'"}
        try:
            new, updated, retired, total = _run_source(session, src, cfg, set())
            log.info("%s: %d new, %d updated, %d retired", src.name, new, updated, retired)
            return {"new": new, "updated": updated, "retired": retired, "total": total}
        except Exception as exc:
            session.rollback()
            src.error_count = (src.error_count or 0) + 1
            src.last_error = redact(str(exc))
            session.commit()
            log.exception("Scraper %s failed", source_name)
            return {"error": redact(str(exc))}


def _pause_after(scraper) -> None:
    """A browser-driven scrape waits a little before the next one (fewer bot walls)."""
    if getattr(scraper, "SOURCE_TYPE", "") in ("playwright", "playwright_jsonld"):
        time.sleep(random.uniform(2, 8))


def _run_source(
    session, src: Source, cfg, seen: set[int], pause: bool = False
) -> tuple[int, int, int, int]:
    """Scrape one source and store it; returns (new, updated, retired, events). Raises when
    nothing could be scraped (the caller records the error).

    A location-aware type (item 30) runs once per search location assigned to the source. Each
    location's events are merged into one batch: an event an earlier location already returned
    (same identity key, dedup hash and start) is not stored twice, it is linked to both. A
    location whose search fails costs only its own events: the others are stored, nothing
    retires, and the failure is the source's error.
    """
    if not is_location_aware(src.type or ""):
        scraper = get_scraper(src, cfg)
        try:
            events = scraper.scrape()
            result = _store(
                session,
                src,
                events,
                seen,
                warnings=list(getattr(scraper, "warnings", None) or []),
                complete=not getattr(scraper, "incomplete", False),
            )
        finally:
            scraper.close()
            if pause:
                _pause_after(scraper)
        return (*result, len(events))

    areas = locations.areas_for(session, src)  # ValueError (fixed text) when there are none
    events: list = []
    where: list[set[int]] = []  # aligned with events: the locations that returned each one
    index: dict[tuple, int] = {}
    warnings: list[str] = []
    failures: list[str] = []
    complete = True
    searched: set[int] = set()
    for n, area in enumerate(areas, 1):
        scraper = get_scraper(src, cfg, location=area)
        try:
            got = scraper.scrape()
        except Exception as exc:
            failures.append(f"{area.label}: {redact(str(exc))}")
            log.exception("Scraper %s failed for location %r", src.name, area.label)
            continue
        finally:
            scraper.close()
            if pause or n < len(areas):
                _pause_after(scraper)
        searched.add(area.id)
        warnings += [f"{area.label}: {w}" for w in getattr(scraper, "warnings", None) or []]
        complete = complete and not getattr(scraper, "incomplete", False)
        prior = dict(index)  # only earlier locations' events: one location's own list is kept
        for ev in got:
            k = (identity_key(ev, src), ev.dedup_hash, ev.start_dt)
            if k in prior:
                where[prior[k]].add(area.id)
                continue
            index.setdefault(k, len(events))
            events.append(ev)
            where.append({area.id})
    if not searched:
        raise RuntimeError("; ".join(failures))
    rows: list[Event] = []
    new, updated, retired = _store(
        session,
        src,
        events,
        seen,
        warnings=warnings,
        complete=complete and not failures,
        rows=rows,
    )
    found: dict[int, set[int]] = {}
    for row, locs in zip(rows, where, strict=True):
        found.setdefault(row.id, set()).update(locs)
    locations.link_events(session, found, searched, replace=not failures)
    if failures:  # a partial scrape: stored, nothing retired, and the source reports the failure
        src.error_count = (src.error_count or 0) + 1
        src.last_error = redact("; ".join(failures + warnings))
    session.commit()
    return new, updated, retired, len(events)


def _store(
    session,
    src: Source,
    events: list,
    seen: set[int],
    *,
    warnings: list[str],
    complete: bool,
    rows: list | None = None,
) -> tuple[int, int, int]:
    """Store one successful scrape of ``src``; returns (new, updated, retired).

    ``seen`` is the rows earlier sources returned in this run, and gains this scrape's. Only a
    scrape with at least one event retires anything (item 25), and only one that read all it
    meant to: a scraper that sets ``incomplete`` (item 22: an API cap or a request ceiling)
    retires nothing, and its ``warnings`` become the source's ``last_error``. ``rows`` receives
    the row of each event, in order (``upsert_events``).
    """
    started = datetime.now()
    src_cats = json.loads(src.categories or "[]")
    for ev in events:  # tag events with the source's categories if they have none
        if not ev.categories and src_cats:
            ev.categories = src_cats
    returned: set[int] = set()
    new, updated = upsert_events(session, events, src.id, returned=returned, rows=rows)
    seen |= returned
    retired = retire_unreturned(session, src.id, seen, started) if events and complete else 0
    src.last_scraped = datetime.now()
    src.error_count = 0
    src.last_error = redact("; ".join(warnings)) if warnings else None
    session.commit()
    return new, updated, retired


def _backfill_pexels_images() -> None:
    cfg = get_config()
    if not cfg.pexels_api_key:
        return

    from .pexels import fetch_photo_url

    with get_session() as session:
        rows = (
            session.query(Event)
            .filter(Event.image_url.is_(None))
            .order_by(Event.first_seen.desc())
            .limit(50)
            .all()
        )
        if not rows:
            return

        log.info("pexels: fetching images for %d events", len(rows))
        title_cache: dict[str, str | None] = {}
        for row in rows:
            try:
                cats = json.loads(row.categories or "[]")
            except Exception:
                cats = []
            cache_key = f"{row.title}|{cats[0] if cats else ''}"
            if cache_key not in title_cache:
                title_cache[cache_key] = fetch_photo_url(row.title, cats, cfg.pexels_api_key)
                time.sleep(0.5)
            if title_cache[cache_key]:
                row.image_url = title_cache[cache_key]

        session.commit()
        log.info("pexels: backfill complete")
