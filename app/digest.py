"""The ntfy digest: upcoming events on a schedule, and never the same list twice.

Three ideas hold it together:

* **Schedule.** One run every ``digest_interval_days`` at ``digest_time`` (wall-clock, in the
  configured timezone). The slots are anchored on the last *scheduled* run, which is stored
  whether or not that run sent anything, so a skipped run does not move the next one.
* **Gate.** Every entry has a fingerprint. A run sends only if some entry's fingerprint was not in
  the last digest that was actually SENT. Removals (events that passed or were cancelled) never
  send. A failed post stores nothing, so the next run tries again. The comparison is against the
  last sent digest, never against the last run: an event that drops out of one run and returns
  in the next is not news, and a new event whose post failed still is.
* **Content.** The events starting inside the window, by start time, capped at 20 lines.

The functions take ``now`` so the tests (and the admin Preview) own the clock. ``now`` is a
datetime in the configured timezone; a naive one is taken as local time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import ntfy
from .config import Config, get_config
from .db import get_meta, get_session, set_meta
from .models import Event, NotificationLog, shown_events
from .tz import tz_of

log = logging.getLogger(__name__)

META_ANCHOR = "digest_last_scheduled"  # ISO date of the last scheduled run
META_SENT = "digest_last_sent_fingerprints"  # JSON list, the last digest actually sent
META_SENT_AT = "digest_last_sent_at"  # ISO local time of that send

MAX_LINES = 20
DEFAULT_NAME = "HappenHere"


# ---------------------------------------------------------------------------
# Identity and fingerprints
# ---------------------------------------------------------------------------


def event_identity(ev: Event) -> str:
    """What makes two rows the same event: the feed UID (item 03). A row from before item 03
    has none and keeps its dedup hash, so its fingerprint does not change on an upgrade."""
    return ev.uid or ev.dedup_hash


def fingerprint(ev: Event) -> str:
    parts = (event_identity(ev), ev.title or "", ev.start_dt.isoformat(), ev.venue_name or "")
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def local_now(cfg: Config) -> datetime:
    return datetime.now(tz_of(cfg))


def _naive_local(now: datetime | None, cfg: Config) -> datetime:
    """``now`` as the naive local time that scraped ``Event.start_dt`` values are in."""
    if now is None:
        now = local_now(cfg)
    if now.tzinfo is not None:
        now = now.astimezone(tz_of(cfg)).replace(tzinfo=None)
    return now


def parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        h, m = (int(x) for x in str(value).strip().split(":"))
    except Exception:
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def next_digest_slot(
    anchor: date | None, interval_days: int, hhmm: str, now: datetime, tz: ZoneInfo
) -> datetime:
    """The first slot after ``now``: ``anchor`` plus whole intervals, at ``hhmm`` wall-clock.

    With no anchor (nothing has run yet) it is the next ``hhmm``, today or tomorrow. A slot
    that has already passed is skipped, not run late, and the phase is kept.
    """
    h, m = parse_hhmm(hhmm) or (9, 0)
    step = timedelta(days=max(1, int(interval_days)))

    def at(d: date) -> datetime:
        return datetime.combine(d, time(h, m), tzinfo=tz)

    now = now.astimezone(tz)
    if anchor is None:
        slot = at(now.date())
        return slot if slot > now else at(now.date() + timedelta(days=1))
    d = anchor + step
    while at(d) <= now:
        d += step
    return at(d)


def next_scheduled_slot(now: datetime | None = None, cfg: Config | None = None) -> datetime:
    """The next slot, from the stored anchor and the current settings."""
    cfg = cfg or get_config()
    tz = tz_of(cfg)
    now = now or local_now(cfg)
    with get_session() as s:
        raw = get_meta(s, META_ANCHOR)
    anchor = date.fromisoformat(raw) if raw else None
    return next_digest_slot(anchor, cfg.digest_interval_days, cfg.digest_time, now, tz)


# ---------------------------------------------------------------------------
# Building and deciding
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    event_id: int
    fingerprint: str
    line: str


@dataclass
class Decision:
    status: str  # send | skipped-unchanged | skipped-empty | not-configured | disabled
    reason: str
    title: str
    body: str
    click: str | None
    entries: list[Entry] = field(default_factory=list)
    new_count: int = 0
    last_sent_at: str | None = None

    @property
    def would_send(self) -> bool:
        return self.status == "send"


@dataclass
class Result:
    status: str  # sent | failed | skipped-unchanged | skipped-empty | not-configured | disabled
    reason: str
    count: int = 0
    new_count: int = 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _line(ev: Event) -> str:
    dt = ev.start_dt
    line = f"• {ev.title} — {dt:%a %b} {dt.day}"
    if dt.hour or dt.minute:
        line += f" at {dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
    if ev.venue_name:
        line += f" @ {ev.venue_name}"
    return line


def _events_link(cfg: Config) -> str | None:
    return cfg.public_url.rstrip("/") + "/events" if cfg.public_url else None


def decide(session, cfg: Config, now: datetime | None = None) -> Decision:
    """Build the digest for ``now`` and decide whether it would go out, and why."""
    local = _naive_local(now, cfg)
    window = max(1, int(cfg.digest_window_days))
    events = (
        shown_events(session, local, local + timedelta(days=window))
        .order_by(Event.start_dt, Event.title, Event.id)
        .all()
    )
    entries = [Entry(ev.id, fingerprint(ev), _line(ev)) for ev in events]

    name = (cfg.city_name or "").strip() or DEFAULT_NAME
    title = f"{name} — {_plural(len(entries), 'event')} in the next {_plural(window, 'day')}"
    lines = [e.line for e in entries[:MAX_LINES]]
    if len(entries) > MAX_LINES:
        lines.append(f"… and {len(entries) - MAX_LINES} more")
    click = _events_link(cfg)
    if click:
        lines += ["", click]
    body = "\n".join(lines)

    last_sent_at = get_meta(session, META_SENT_AT)
    d = Decision("send", "", title, body, click, entries, 0, last_sent_at)

    if not cfg.digest_enabled:
        d.status, d.reason = "disabled", "The digest is turned off."
        return d
    if not ntfy.configured(cfg):
        d.status, d.reason = "not-configured", "ntfy is not configured (server URL and topic)."
        return d
    if not entries:
        d.status = "skipped-empty"
        d.reason = f"No events start in the next {_plural(window, 'day')}."
        return d

    raw = get_meta(session, META_SENT)
    if raw is None:
        d.new_count = len(entries)
        d.reason = "This would be the first digest: nothing has been sent yet."
        return d
    seen = set(json.loads(raw))
    d.new_count = sum(1 for e in entries if e.fingerprint not in seen)
    if d.new_count == 0:
        d.status = "skipped-unchanged"
        d.reason = f"Unchanged since the digest sent {last_sent_at or 'earlier'}."
    else:
        d.reason = (
            f"{_plural(d.new_count, 'event')} new or changed since the digest sent "
            f"{last_sent_at or 'earlier'}."
        )
    return d


def preview(now: datetime | None = None) -> Decision:
    with get_session() as session:
        return decide(session, get_config(), now)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_digest(now: datetime | None = None) -> Result:
    cfg = get_config()
    local = _naive_local(now, cfg)
    with get_session() as session:
        d = decide(session, cfg, local)
        if d.status in ("disabled", "not-configured"):
            log.info("digest not sent: %s", d.reason)
            return Result(d.status, d.reason, len(d.entries))

        status, err = d.status, None
        if d.would_send:
            ok, err = ntfy.post_to_ntfy(cfg, d.title, d.body, click=d.click, tags=["calendar"])
            status = "sent" if ok else "failed"

        session.add(
            NotificationLog(
                sent_at=local,
                event_count=len(d.entries),
                event_ids=json.dumps([e.event_id for e in d.entries]),
                ntfy_topic=cfg.ntfy_topic,
                status=status,
                error_msg=err,
            )
        )
        if status == "sent":
            set_meta(session, META_SENT, json.dumps(sorted(e.fingerprint for e in d.entries)))
            set_meta(session, META_SENT_AT, local.strftime("%Y-%m-%d %H:%M"))
        session.commit()
        log.info("digest %s: %d entries, %d new. %s", status, len(d.entries), d.new_count, d.reason)
        return Result(status, err or d.reason, len(d.entries), d.new_count)


def run_scheduled(slot: datetime, now: datetime | None = None) -> str:
    """The scheduled run for ``slot``: record it as the anchor, then run. Returns the status."""
    with get_session() as session:
        set_meta(session, META_ANCHOR, slot.date().isoformat())
    try:
        return run_digest(now=now if now is not None else slot).status
    except Exception:
        log.exception("digest run failed")
        return "error"


def send_test(now: datetime | None = None) -> tuple[bool, str | None, Decision]:
    """Post the next digest marked TEST. Records nothing, so it never uses up a real send."""
    cfg = get_config()
    with get_session() as session:
        d = decide(session, cfg, now)
    if not ntfy.configured(cfg):
        return False, "ntfy is not configured (server URL and topic).", d
    body = "This is a test. Nothing was recorded.\n\n" + (d.body or "No upcoming events.")
    ok, err = ntfy.post_to_ntfy(cfg, f"TEST — {d.title}", body, click=d.click, tags=["test_tube"])
    return ok, err, d
