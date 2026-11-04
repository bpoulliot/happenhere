from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .models import Event
from .scrapers.base import EventData

log = logging.getLogger(__name__)


def upsert_events(session: Session, events: list[EventData], source_id: int) -> tuple[int, int]:
    """Insert new events; update last_seen on existing. Returns (new, updated)."""
    new_count = 0
    updated_count = 0
    for ev in events:
        h = ev.dedup_hash
        row = session.query(Event).filter_by(dedup_hash=h).first()
        if row is None:
            row = Event(
                source_id=source_id,
                dedup_hash=h,
                title=ev.title,
                description=ev.description,
                start_dt=ev.start_dt,
                end_dt=ev.end_dt,
                venue_name=ev.venue_name,
                venue_addr=ev.venue_addr,
                url=ev.url,
                image_url=ev.image_url,
                categories=json.dumps(ev.categories),
                raw_data=json.dumps(ev.raw_data),
                first_seen=datetime.now(),
                last_seen=datetime.now(),
                notified=False,
            )
            session.add(row)
            new_count += 1
        else:
            row.last_seen = datetime.now()
            # Refresh mutable fields in case the event details changed
            row.title = ev.title
            row.description = ev.description or row.description
            row.end_dt = ev.end_dt or row.end_dt
            row.venue_addr = ev.venue_addr or row.venue_addr
            row.url = ev.url or row.url
            row.image_url = ev.image_url or row.image_url
            updated_count += 1
    session.commit()
    return new_count, updated_count
