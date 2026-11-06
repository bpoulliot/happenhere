from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime

from .config import get_config
from .db import get_session
from .dedup import upsert_events
from .models import Event, Source
from .scrapers.registry import get_scraper

log = logging.getLogger(__name__)


def run_all_scrapers() -> None:
    log.info("Starting scrape run")
    cfg = get_config()
    with get_session() as session:
        sources = session.query(Source).filter_by(enabled=True).all()
        for src in sources:
            scraper = get_scraper(src, cfg)
            if scraper is None:
                log.warning("No scraper for type %r (source: %s)", src.type, src.name)
                continue
            try:
                events = scraper.scrape()
                # Tag events with source categories if not already set
                src_cats = json.loads(src.categories or "[]")
                for ev in events:
                    if not ev.categories and src_cats:
                        ev.categories = src_cats
                new, updated = upsert_events(session, events, src.id)
                src.last_scraped = datetime.now()
                src.error_count = 0
                src.last_error = None
                session.commit()
                log.info("%s: %d new, %d updated", src.name, new, updated)
            except Exception as exc:
                # A failed flush leaves the session unusable until rolled back (item 18): without
                # this the commit below raised and ended the whole run.
                session.rollback()
                src.error_count = (src.error_count or 0) + 1
                src.last_error = str(exc)
                session.commit()
                log.exception("Scraper %s failed: %s", src.name, exc)
            finally:
                scraper.close()
            if getattr(scraper, "SOURCE_TYPE", "") in ("playwright", "playwright_jsonld"):
                time.sleep(random.uniform(2, 8))

    _backfill_pexels_images()
    log.info("Scrape run complete")


def run_single_scraper(source_name: str) -> dict:
    cfg = get_config()
    with get_session() as session:
        src = session.query(Source).filter_by(name=source_name).first()
        if src is None:
            return {"error": f"Source '{source_name}' not found"}
        scraper = get_scraper(src, cfg)
        if scraper is None:
            return {"error": f"No scraper class for type '{src.type}'"}
        try:
            events = scraper.scrape()
            src_cats = json.loads(src.categories or "[]")
            for ev in events:
                if not ev.categories and src_cats:
                    ev.categories = src_cats
            new, updated = upsert_events(session, events, src.id)
            src.last_scraped = datetime.now()
            src.error_count = 0
            src.last_error = None
            session.commit()
            return {"new": new, "updated": updated, "total": len(events)}
        except Exception as exc:
            session.rollback()
            src.error_count = (src.error_count or 0) + 1
            src.last_error = str(exc)
            session.commit()
            log.exception("Scraper %s failed", source_name)
            return {"error": str(exc)}
        finally:
            scraper.close()


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
