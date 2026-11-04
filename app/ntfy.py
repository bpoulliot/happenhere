from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from .config import get_config
from .db import get_session
from .models import Event, NotificationLog

log = logging.getLogger(__name__)


def send_digest() -> None:
    cfg = get_config()
    if not cfg.ntfy_url or not cfg.ntfy_topic:
        log.info("ntfy not configured — skipping digest")
        return

    with get_session() as session:
        events = _get_unnotified_events(session)
        if not events:
            log.info("No new events to notify about")
            return

        title, body = _build_message(events)
        status, err = _post_to_ntfy(cfg, title, body)

        log_row = NotificationLog(
            sent_at=datetime.utcnow(),
            event_count=len(events),
            event_ids=json.dumps([e.id for e in events]),
            ntfy_topic=cfg.ntfy_topic,
            status=status,
            error_msg=err,
        )
        session.add(log_row)

        if status == "ok":
            for ev in events:
                ev.notified = True

        session.commit()
        log.info("ntfy digest sent: %d events, status=%s", len(events), status)


def _get_unnotified_events(session: Session) -> list[Event]:
    return (
        session.query(Event)
        .filter(Event.notified == False)  # noqa: E712
        .filter(Event.start_dt >= datetime.utcnow())
        .order_by(Event.start_dt)
        .all()
    )


def _build_message(events: list[Event]) -> tuple[str, str]:
    title = f"Colorado Springs Events — {len(events)} new event{'s' if len(events) != 1 else ''}"
    lines = []
    for ev in events[:20]:  # cap at 20 lines
        dt = ev.start_dt.strftime("%a %b %-d")
        time_str = ev.start_dt.strftime("%-I:%M %p") if ev.start_dt.hour else ""
        venue = ev.venue_name or ""
        line = f"• {ev.title} — {dt}"
        if time_str:
            line += f" at {time_str}"
        if venue:
            line += f" @ {venue}"
        lines.append(line)

    if len(events) > 20:
        lines.append(f"  … and {len(events) - 20} more")

    return title, "\n".join(lines)


def _post_to_ntfy(cfg, title: str, body: str) -> tuple[str, str | None]:
    url = cfg.ntfy_url.rstrip("/") + "/" + cfg.ntfy_topic
    headers = {
        "Title": title,
        "Priority": "default",
        "Tags": "calendar,colorado-springs",
        "Content-Type": "text/plain",
    }
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, content=body.encode(), headers=headers)
            resp.raise_for_status()
        return "ok", None
    except Exception as exc:
        log.exception("ntfy POST failed: %s", exc)
        return "error", str(exc)
