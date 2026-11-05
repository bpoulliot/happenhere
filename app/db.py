from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import AppMeta, Base

log = logging.getLogger(__name__)

_engine = None
_SessionLocal = None

# Regional aggregator sources seeded on first run — these work for any city
# via config.city_zip / city_radius_miles. Users enable/disable in the UI.
_SEED_REGIONAL: list[dict] = [
    {
        "name": "ticketmaster",
        "display_name": "Ticketmaster",
        "type": "ticketmaster",
        "url": "https://app.ticketmaster.com/discovery/v2/events",
        "enabled": False,  # requires API key — user must configure
    },
    {
        "name": "eventbrite",
        "display_name": "Eventbrite",
        "type": "eventbrite",
        "url": "",  # URL set dynamically from city config
        "enabled": False,
    },
    {
        "name": "meetup",
        "display_name": "Meetup",
        "type": "meetup",
        "url": "",
        "enabled": False,
    },
    {
        "name": "fathom",
        "display_name": "Fathom Events",
        "type": "fathom",
        "url": "https://www.fathomentertainment.com/releases/",
        "enabled": False,
    },
]


def _configure_sqlite(dbapi_conn, _connection_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _migrate_schema(engine) -> None:
    with engine.connect() as conn:
        event_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(events)"))}
        for col, typ in {
            "image_url": "VARCHAR",
            "categories": "TEXT",
            "raw_data": "TEXT",
            "identity_key": "VARCHAR(64)",
            "uid": "VARCHAR",
        }.items():
            if col not in event_cols:
                conn.execute(text(f"ALTER TABLE events ADD COLUMN {col} {typ}"))
        for col in ("identity_key", "uid"):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_events_{col} ON events ({col})"))

        source_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(sources)"))}
        for col, typ in {
            "error_count": "INTEGER DEFAULT 0",
            "last_error": "TEXT",
            "categories": "TEXT",
            "tags": "TEXT",
        }.items():
            if col not in source_cols:
                conn.execute(text(f"ALTER TABLE sources ADD COLUMN {col} {typ}"))

        conn.commit()


def _seed_regional(session: Session) -> None:
    from .models import Source

    for row in _SEED_REGIONAL:
        if session.query(Source).filter_by(name=row["name"]).first() is None:
            session.add(Source(**row))
    session.commit()


def init_db(db_path: str | Path | None = None) -> None:
    global _engine, _SessionLocal
    from .config import get_config

    path = db_path or get_config().db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    event.listen(_engine, "connect", _configure_sqlite)
    Base.metadata.create_all(_engine)
    _migrate_schema(_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    with _SessionLocal() as s:
        _seed_regional(s)
    log.info("Database initialised at %s", path)


def get_session() -> Session:
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()


def get_meta(session: Session, key: str) -> str | None:
    row = session.get(AppMeta, key)
    return row.value if row else None


def set_meta(session: Session, key: str, value: str) -> None:
    row = session.get(AppMeta, key)
    if row is None:
        row = AppMeta(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    session.commit()
