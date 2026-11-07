from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Query, Session, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)  # slug, auto-generated
    display_name = Column(String)  # shown in UI + used as venue_name
    type = Column(String)  # scraper type key
    url = Column(String)  # events page URL
    categories = Column(Text)  # JSON list e.g. ["Brewery","Live Music"]
    tags = Column(Text)  # JSON list of slugs; what /feed/tags/<tag>.ics selects on (item 03)
    last_scraped = Column(DateTime)
    enabled = Column(Boolean, default=True, nullable=False)
    error_count = Column(Integer, default=0)
    last_error = Column(Text)
    # Item 06, the canary (app/canary.py): consecutive scrape runs the source had no upcoming
    # events / a non-zero error_count, and when it last alerted (NULL = not alerted, or an alert
    # whose POST failed, which the next run retries).
    empty_runs = Column(Integer, default=0)
    error_runs = Column(Integer, default=0)
    canary_alerted_at = Column(DateTime)

    events = relationship("Event", back_populates="source")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    dedup_hash = Column(String(64), unique=True, nullable=False, index=True)
    # Item 03. identity_key: the source's own identity for the event (see app/dedup.py), the
    # first thing a re-scrape is matched on. uid: the feed UID, set once and never changed;
    # NULL on rows from before item 03, which keep their old UID (app/ical_export.feed_uid).
    identity_key = Column(String(64), index=True)
    uid = Column(String, index=True)

    title = Column(String, nullable=False)
    description = Column(Text)
    start_dt = Column(DateTime, nullable=False, index=True)
    end_dt = Column(DateTime)
    venue_name = Column(String)
    venue_addr = Column(String)
    url = Column(String)
    image_url = Column(String)
    categories = Column(Text)  # JSON list
    raw_data = Column(Text)  # JSON blob

    first_seen = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    # Unused since item 14: the digest compares fingerprints with the last SENT digest.
    notified = Column(Boolean, default=False, nullable=False)
    # Item 25: set when a successful scrape of the row's source no longer returns it; cleared
    # when a scrape returns it again. A retired row keeps its UID and every other field, and
    # no reader sees it (shown_events below).
    retired_at = Column(DateTime)

    source = relationship("Source", back_populates="events")


def shown_events(
    session: Session, start: datetime | None = None, end: datetime | None = None
) -> Query:
    """Every event a reader may see, optionally starting inside [start, end].

    Every consumer (the feeds, the events page and calendar, the API, the digest) queries events
    through here, so none of them can forget that a retired row (item 25) is not shown.
    ``tests/test_retire.py`` refuses a ``start_dt`` filter written anywhere else.
    """
    q = session.query(Event).filter(Event.retired_at.is_(None))
    if start is not None:
        q = q.filter(Event.start_dt >= start)
    if end is not None:
        q = q.filter(Event.start_dt <= end)
    return q


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sent_at = Column(DateTime, default=datetime.now, nullable=False)
    event_count = Column(Integer)
    event_ids = Column(Text)
    ntfy_topic = Column(String)
    status = Column(String)  # sent | skipped-unchanged | skipped-empty | failed
    error_msg = Column(Text)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key = Column(String, primary_key=True)
    value = Column(Text)
