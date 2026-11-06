from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime
from urllib.parse import urldefrag

from sqlalchemy.orm import Session

from .models import Event, Source
from .scrapers.base import EventData

log = logging.getLogger(__name__)

UID_DOMAIN = "happenhere"


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _norm_url(url: str | None) -> str:
    url = urldefrag((url or "").strip())[0]
    return url.rstrip("/") if url.lower().startswith(("http://", "https://")) else ""


def identity(ev: EventData, source: Source | None) -> tuple[str, str]:
    """The most stable identity the source gives this event, as (kind, value).

    1. ``uid``: the source's own event id (an iCal ``UID``, a ticketing platform's id);
    2. ``url``: the event's own URL, when it is not just the source's listing page;
    3. ``slot``: start time + venue, which a retitle does not change.

    The title is never part of it: a calendar app keys an event on its UID, and a UID that
    changes with the title ("... SOLD OUT") leaves a ghost event and a duplicate.
    """
    if ev.source_uid and ev.source_uid.strip():
        return "uid", ev.source_uid.strip()
    url = _norm_url(ev.url)
    if url and url != _norm_url(source.url if source else ""):
        return "url", url
    return "slot", f"{ev.start_dt.isoformat()}|{_norm(ev.venue_name)}"


def identity_key(ev: EventData, source: Source | None) -> str:
    kind, value = identity(ev, source)
    name = source.name if source else ""
    return hashlib.sha256(f"{name}\x1f{kind}\x1f{value}".encode()).hexdigest()


def upsert_events(session: Session, events: list[EventData], source_id: int) -> tuple[int, int]:
    """Insert new events; refresh seen ones. Returns (new, updated).

    A scraped event is matched first on its identity key (above), then on the dedup hash
    (title + date + venue), which also merges the same event from two sources. Two events in
    one batch with the same identity key (a series sharing one URL, two shows in one slot)
    have no usable identity: both fall back to the dedup hash alone.

    Two events in one batch with the same dedup hash (an early and a late show of one title,
    item 18) are one event when their identity keys agree or are absent, and two when the keys
    differ: the second is stored under ``timed_hash`` (the hash plus the start time). The batch
    is tracked here because the session does not autoflush, so a query cannot see it.
    """
    source = session.get(Source, source_id)
    keys = [identity_key(ev, source) for ev in events]
    shared = {k for k, n in Counter(keys).items() if n > 1}
    taken_hashes: set[str] = set()  # this batch's, which a query before commit cannot see
    batch: dict[str, tuple[Event, str | None]] = {}  # dedup hash -> (row, its key) this batch

    new_count = 0
    updated_count = 0
    for ev, key in zip(events, keys, strict=True):
        key = None if key in shared else key
        h = ev.dedup_hash
        row = None
        if key:
            row = session.query(Event).filter_by(identity_key=key).first()
        if row is not None:
            _refresh(row, ev)
            # The source says this is the same event, so its time and venue are current too
            # (a dedup-hash match still keeps its first start time: item 15).
            row.start_dt = ev.start_dt
            row.venue_name = ev.venue_name or row.venue_name
            if row.dedup_hash != h and h not in taken_hashes and not _hash_used(session, h):
                if row.uid is None:
                    row.uid = legacy_uid(row.dedup_hash)  # its feed UID must not move with it
                row.dedup_hash = h
            taken_hashes.add(row.dedup_hash)
            batch[row.dedup_hash] = (row, key)
            updated_count += 1
            continue

        if h in batch:
            first, first_key = batch[h]
            if key is None or key == first_key:
                _refresh(first, ev)  # the same event listed twice
                updated_count += 1
                continue
            h = timed_hash(ev)  # a different event with the same title, day and venue
            if h in batch:
                _refresh(batch[h][0], ev)
                updated_count += 1
                continue

        row = session.query(Event).filter_by(dedup_hash=h).first()
        if row is None:
            row = Event(
                source_id=source_id,
                dedup_hash=h,
                identity_key=key,
                uid=f"{key or h}@{UID_DOMAIN}",
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
            _refresh(row, ev)
            if row.identity_key is None and key and not _key_used(session, key):
                row.identity_key = key  # a row from before item 03, or from another source
            updated_count += 1
        taken_hashes.add(h)
        batch[h] = (row, key)
    session.commit()
    return new_count, updated_count


def timed_hash(ev: EventData) -> str:
    """The dedup hash with the start time in it: a second show of one title on one day."""
    key = f"{_norm(ev.title)}|{ev.start_dt.isoformat()}|{_norm(ev.venue_name)}"
    return hashlib.sha256(key.encode()).hexdigest()


def legacy_uid(dedup_hash: str) -> str:
    """The feed UID of a row stored before item 03."""
    return f"{dedup_hash}@peakevents"


def _refresh(row: Event, ev: EventData) -> None:
    row.last_seen = datetime.now()
    row.title = ev.title
    row.description = ev.description or row.description
    row.end_dt = ev.end_dt or row.end_dt
    row.venue_addr = ev.venue_addr or row.venue_addr
    row.url = ev.url or row.url
    row.image_url = ev.image_url or row.image_url


def _hash_used(session: Session, h: str) -> bool:
    return session.query(Event.id).filter_by(dedup_hash=h).first() is not None


def _key_used(session: Session, key: str) -> bool:
    return session.query(Event.id).filter_by(identity_key=key).first() is not None
