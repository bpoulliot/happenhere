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
    return _key(source, *identity(ev, source))


def _key(source: Source | None, kind: str, value: str) -> str:
    name = source.name if source else ""
    return hashlib.sha256(f"{name}\x1f{kind}\x1f{value}".encode()).hexdigest()


def _is_slot_key(
    key: str | None, start: datetime, venue: str | None, source: Source | None
) -> bool:
    """Whether ``key`` is the ``slot`` identity (start time + venue) of this start and venue."""
    return key is not None and key == _key(source, "slot", f"{start.isoformat()}|{_norm(venue)}")


DIFFERENT, MOVED, SAME = "different", "moved", "same"


def hash_match(
    row: Event,
    ev: EventData,
    key: str | None,
    source: Source | None,
    batch_keys: frozenset[str],
) -> str:
    """What a scraped event that matched ``row`` on the dedup hash (title + date + venue) is.

    * ``DIFFERENT`` (item 25): the stored row comes from the **same source** and carries a
      **different, non-null identity key** that this batch also returned: a second show of one
      title, day and venue (the early show is still listed). It gets a row of its own under
      ``timed_hash``.
    * ``MOVED`` (item 15): the same event, whose start time moved within the day. Either both
      keys are ``slot`` keys (start time + venue, the only kind that changes with the time) and
      the stored key is **not** in ``batch_keys`` (nothing in this batch matched the stored row
      on its own key), or neither side has a usable key (hash fallback) and the row is the same
      source's. The row is updated in place and keeps its UID.
    * ``SAME``: everything else merges as before, keeping the stored start time: the same event
      from another source (whose time a second source must not move), and a same-source match
      where only one side has a key.

    A ``url`` or ``uid`` key does not change when an event's time moves, so a different one is
    the source saying "another event" (item 25), listed alongside or not.

    ``batch_keys`` is every identity key this batch returned (keys shared within it excluded).
    """
    if source is None or row.source_id != source.id:
        return SAME
    if key is None and row.identity_key is None:
        return MOVED
    if key is None or row.identity_key is None or row.identity_key == key:
        return SAME
    if (
        row.identity_key not in batch_keys
        and _is_slot_key(key, ev.start_dt, ev.venue_name, source)
        and _is_slot_key(row.identity_key, row.start_dt, row.venue_name, source)
    ):
        return MOVED
    return DIFFERENT


def upsert_events(
    session: Session,
    events: list[EventData],
    source_id: int,
    *,
    returned: set[int] | None = None,
    rows: list[Event] | None = None,
) -> tuple[int, int]:
    """Insert new events; refresh seen ones. Returns (new, updated).

    ``returned``, when given, receives the id of every row this batch resolved to, new or seen:
    what ``retire_unreturned`` must not retire. A retired row that a batch returns is un-retired.
    ``rows``, when given, receives the row each event resolved to, in the order of ``events``
    (item 30 links each row to the search locations that returned it).

    A scraped event is matched first on its identity key (above), then on the dedup hash
    (title + date + venue), which also merges the same event from two sources. Two events in
    one batch with the same identity key (a series sharing one URL, two shows in one slot)
    have no usable identity: both fall back to the dedup hash alone.

    Two events in one batch with the same dedup hash (an early and a late show of one title,
    item 18) are one event when their identity keys agree or are absent, and two when the keys
    differ: the second is stored under ``timed_hash`` (the hash plus the start time). Two with
    the same exact start are one event whatever their keys (item 28: a ticketing platform lists
    one show under two ids). The batch
    is tracked here because the session does not autoflush, so a query cannot see it.

    A dedup-hash match with a stored row that ``hash_match`` says is another event is stored
    under ``timed_hash`` too (item 25: the late show first seen in a later run); one it says is
    the same event moved within the day takes the new start time and keeps its row and UID
    (item 15).
    """
    source = session.get(Source, source_id)
    keys = [identity_key(ev, source) for ev in events]
    shared = {k for k, n in Counter(keys).items() if n > 1}
    batch_keys = frozenset(keys) - shared
    resolved: list[Event] = []
    taken_hashes: set[str] = set()  # this batch's, which a query before commit cannot see
    batch: dict[str, tuple[Event, str | None]] = {}  # dedup hash -> (row, its key) this batch
    batch_start: dict[str, datetime] = {}  # dedup hash -> the start its first event gave (item 28)

    per_event: list[Event] = []  # the row each event resolved to, in order (``rows``)
    new_count = 0
    updated_count = 0
    for ev, key in zip(events, keys, strict=True):
        key = None if key in shared else key
        h = ev.dedup_hash
        row = None
        if key:
            row = session.query(Event).filter_by(identity_key=key).first()
        if row is not None:
            per_event.append(row)
            _refresh(row, ev)
            # The source says this is the same event, so its time and venue are current too.
            row.start_dt = ev.start_dt
            row.venue_name = ev.venue_name or row.venue_name
            if row.dedup_hash != h and h not in taken_hashes and not _hash_used(session, h):
                if row.uid is None:
                    row.uid = legacy_uid(row.dedup_hash)  # its feed UID must not move with it
                row.dedup_hash = h
            taken_hashes.add(row.dedup_hash)
            batch[row.dedup_hash] = (row, key)
            batch_start.setdefault(row.dedup_hash, ev.start_dt)
            resolved.append(row)
            updated_count += 1
            continue

        if h in batch:
            first, first_key = batch[h]
            if key is None or key == first_key or ev.start_dt == batch_start.get(h):
                _refresh(first, ev)  # the same event listed twice (item 28: under two ids)
                per_event.append(first)
                updated_count += 1
                continue
            h = timed_hash(ev)  # a different event with the same title, day and venue
            if h in batch:
                _refresh(batch[h][0], ev)
                per_event.append(batch[h][0])
                updated_count += 1
                continue

        row = session.query(Event).filter_by(dedup_hash=h).first()
        match = hash_match(row, ev, key, source, batch_keys) if row is not None else None
        if match == DIFFERENT:
            h = timed_hash(ev)  # stored in an earlier run: the same rule as within a batch
            if h in batch:
                _refresh(batch[h][0], ev)
                per_event.append(batch[h][0])
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
            if match == MOVED:  # the same event, its time moved within the day (item 15)
                row.start_dt = ev.start_dt
                row.venue_name = ev.venue_name or row.venue_name
                if key and not _key_used(session, key):
                    row.identity_key = key  # the uid stays: the feed UID must not move
            elif row.identity_key is None and key and not _key_used(session, key):
                row.identity_key = key  # a row from before item 03, or from another source
            updated_count += 1
        taken_hashes.add(h)
        batch[h] = (row, key)
        batch_start.setdefault(h, ev.start_dt)
        resolved.append(row)
        per_event.append(row)
    session.commit()
    if returned is not None:
        returned.update(row.id for row in resolved)
    if rows is not None:
        rows.extend(per_event)
    return new_count, updated_count


def retire_unreturned(session: Session, source_id: int, returned: set[int], now: datetime) -> int:
    """Retire the source's upcoming rows that its scrape did not return (item 25).

    ``returned`` holds the rows the upsert resolved this batch to (its own identity: the
    identity key, else the dedup hash), so an unchanged event is never retired; the caller
    adds the rows other sources returned earlier in the same run, so a row that another source
    merged into and still lists is kept. Call it only after a scrape that succeeded with at
    least one event: a failed or empty scrape retires nothing (item 06's canary owns that).
    Nothing is deleted and nothing else changes, ``last_seen`` included. Returns the count.
    """
    ids = [
        row_id
        for (row_id,) in session.query(Event.id).filter(
            Event.source_id == source_id,
            Event.retired_at.is_(None),
            Event.start_dt >= now,
        )
        if row_id not in returned
    ]
    if ids:
        session.query(Event).filter(Event.id.in_(ids)).update(
            {Event.retired_at: now, Event.last_seen: Event.last_seen}, synchronize_session=False
        )
    return len(ids)


def timed_hash(ev: EventData) -> str:
    """The dedup hash with the start time in it: a second show of one title on one day."""
    key = f"{_norm(ev.title)}|{ev.start_dt.isoformat()}|{_norm(ev.venue_name)}"
    return hashlib.sha256(key.encode()).hexdigest()


def legacy_uid(dedup_hash: str) -> str:
    """The feed UID of a row stored before item 03."""
    return f"{dedup_hash}@peakevents"


def _refresh(row: Event, ev: EventData) -> None:
    row.last_seen = datetime.now()
    row.retired_at = None  # returned again: un-retired, with the UID it always had (item 25)
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
