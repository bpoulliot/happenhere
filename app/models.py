from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


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
    last_scraped = Column(DateTime)
    enabled = Column(Boolean, default=True, nullable=False)
    error_count = Column(Integer, default=0)
    last_error = Column(Text)

    events = relationship("Event", back_populates="source")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    dedup_hash = Column(String(64), unique=True, nullable=False, index=True)

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

    source = relationship("Source", back_populates="events")


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
