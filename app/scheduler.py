from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_SCRAPE_JOB = "scrape_all"
_DIGEST_JOB = "daily_digest"


def start(scrape_fn, digest_fn, scrape_interval_hours: int, digest_time: str) -> None:
    global _scheduler
    tz = os.environ.get("TZ", "America/Denver") or "America/Denver"
    _scheduler = BackgroundScheduler(timezone=tz)

    _scheduler.add_job(
        scrape_fn,
        IntervalTrigger(hours=scrape_interval_hours),
        id=_SCRAPE_JOB,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    log.info("Scheduled scrape every %d hours", scrape_interval_hours)

    try:
        h, m = (int(x) for x in digest_time.split(":"))
    except Exception:
        h, m = 9, 0
    _scheduler.add_job(
        digest_fn,
        CronTrigger(hour=h, minute=m, timezone=tz),
        id=_DIGEST_JOB,
        replace_existing=True,
        max_instances=1,
    )
    log.info("Scheduled daily digest at %02d:%02d %s", h, m, tz)

    _scheduler.start()


def trigger_scrape_now() -> None:
    from datetime import datetime

    if _scheduler:
        job = _scheduler.get_job(_SCRAPE_JOB)
        if job:
            job.modify(next_run_time=datetime.now())


def next_scrape_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_SCRAPE_JOB)
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def reschedule_scraper(interval_hours: int) -> None:
    if _scheduler:
        job = _scheduler.get_job(_SCRAPE_JOB)
        if job:
            from apscheduler.triggers.interval import IntervalTrigger

            job.reschedule(IntervalTrigger(hours=interval_hours))
            log.info("Rescheduled scrape to every %d hours", interval_hours)
