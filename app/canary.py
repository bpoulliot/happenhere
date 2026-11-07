"""The per-source canary (item 06): one alert when a source goes quiet or keeps failing.

After each scrape run, every ENABLED source is counted:

* **empty**: it has no upcoming event (shown, starting from now) for ``EMPTY_RUNS`` consecutive
  runs, or
* **failing**: its ``error_count`` is non-zero for ``ERROR_RUNS`` consecutive runs.

A source that trips is announced once, in ONE message per run that lists every source newly
tripped (a bad night must not flood a phone). It stays quiet while it stays tripped, and re-arms
when a run finds it healthy. The alert is only recorded once the POST succeeded, so a failed post
(or ntfy not being configured yet) is retried at the next run.

The streak counters and the alert time live on ``Source``. Nothing is written to
``NotificationLog``: the digest's change gate reads only its own ``app_meta`` keys, and a canary
send must never look like a digest.
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import ntfy
from .config import Config, get_config
from .db import get_session
from .models import Source, shown_events
from .tz import tz_of

log = logging.getLogger(__name__)

EMPTY_RUNS = 2  # consecutive runs with no upcoming event
ERROR_RUNS = 3  # consecutive runs with a non-zero error_count
ERROR_CHARS = 120

EMPTY = "empty"
FAILING = "failing"


def _local_now(cfg: Config, now: datetime | None) -> datetime:
    """``now`` as the naive local time that scraped ``Event.start_dt`` values are in."""
    if now is None:
        now = datetime.now(tz_of(cfg))
    if now.tzinfo is not None:
        now = now.astimezone(tz_of(cfg)).replace(tzinfo=None)
    return now


def conditions(src: Source) -> list[str]:
    """The conditions ``src`` is tripped on (``empty``, ``failing``); none while it is counting."""
    if not src.enabled:
        return []
    out = []
    if (src.empty_runs or 0) >= EMPTY_RUNS:
        out.append(EMPTY)
    if (src.error_runs or 0) >= ERROR_RUNS:
        out.append(FAILING)
    return out


def _count_run(session, src: Source, now: datetime) -> None:
    """Fold one finished run into ``src``'s streaks and re-arm it if it is no longer tripped."""
    if not src.enabled:
        # Never counted, and a stale streak must not carry over to when it is enabled again.
        src.empty_runs = src.error_runs = 0
        src.canary_alerted_at = None
        return
    upcoming = shown_events(session, now).filter_by(source_id=src.id).count()
    src.empty_runs = 0 if upcoming else (src.empty_runs or 0) + 1
    src.error_runs = (src.error_runs or 0) + 1 if (src.error_count or 0) else 0
    if not conditions(src):
        src.canary_alerted_at = None


def _line(src: Source) -> str:
    name = src.display_name or src.name
    parts = []
    if EMPTY in conditions(src):
        parts.append(f"no upcoming events for {src.empty_runs} runs")
    if FAILING in conditions(src):
        err = " ".join((src.last_error or "").split())[:ERROR_CHARS]
        parts.append(f"failing for {src.error_runs} runs" + (f": {err}" if err else ""))
    return f"• {name} — " + "; ".join(parts)


def _message(cfg: Config, pending: list[Source]) -> tuple[str, str, str | None]:
    name = (cfg.city_name or "").strip() or "HappenHere"
    n = len(pending)
    title = f"{name} — canary: {n} source{'' if n == 1 else 's'} quiet or failing"
    base = cfg.public_url.rstrip("/") if cfg.public_url else None
    click = None
    if base:
        click = f"{base}/admin/sources/{pending[0].name}/edit" if n == 1 else f"{base}/admin"
    return title, "\n".join(_line(s) for s in pending), click


def check(now: datetime | None = None) -> int:
    """Count one finished scrape run; announce what newly tripped. Returns the sources announced."""
    cfg = get_config()
    local = _local_now(cfg, now)
    with get_session() as session:
        sources = session.query(Source).order_by(Source.display_name, Source.name).all()
        for src in sources:
            _count_run(session, src, local)
        session.commit()
        pending = [s for s in sources if conditions(s) and s.canary_alerted_at is None]
        if not pending or not ntfy.configured(cfg):
            return 0
        title, body, click = _message(cfg, pending)
        try:
            ok, err = ntfy.post_to_ntfy(cfg, title, body, click=click, tags=["warning"])
        except Exception as exc:  # post_to_ntfy never raises; a replacement might
            ok, err = False, str(exc)
        if not ok:
            log.warning("canary alert not sent (will retry next run): %s", err)
            return 0
        for src in pending:
            src.canary_alerted_at = local
        session.commit()
        log.info("canary alerted for %d source(s)", len(pending))
        return len(pending)
