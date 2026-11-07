from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .tz import tz_of

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_SCRAPE_JOB = "scrape_all"
_DIGEST_PREFIX = "digest@"


def _timezone():
    """The configured zone (``TZ`` in the environment lands in ``Config.tz``)."""
    return tz_of()


def start(scrape_fn, scrape_interval_hours: int) -> None:
    global _scheduler
    _scheduler = BackgroundScheduler(timezone=_timezone())

    _scheduler.add_job(
        scrape_fn,
        IntervalTrigger(hours=scrape_interval_hours),
        id=_SCRAPE_JOB,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    log.info("Scheduled scrape every %d hours", scrape_interval_hours)

    _scheduler.start()
    schedule_digest()


def _digest_jobs() -> list:
    return (
        [j for j in _scheduler.get_jobs() if j.id.startswith(_DIGEST_PREFIX)] if _scheduler else []
    )


def digest_job():
    jobs = _digest_jobs()
    return jobs[0] if jobs else None


def schedule_digest(now: datetime | None = None) -> datetime | None:
    """(Re)arm the one digest job from the stored anchor and the current settings.

    Called at start, after the settings are saved, and after every run. One date job at a time,
    each under its own id: a job re-armed from inside its own run must not share the id the
    scheduler is about to remove for the run that just fired.
    """
    from . import digest
    from .config import get_config

    if _scheduler is None:
        return None
    for job in _digest_jobs():
        try:
            job.remove()
        except JobLookupError:
            pass  # already gone: it is the run that just fired
    if not get_config().digest_enabled:
        log.info("Digest is disabled — not scheduled")
        return None
    slot = digest.next_scheduled_slot(now)
    _scheduler.add_job(
        run_digest_slot,
        DateTrigger(run_date=slot),
        args=[slot],
        id=f"{_DIGEST_PREFIX}{slot.isoformat()}",
        max_instances=1,
        misfire_grace_time=3600,
    )
    log.info("Next digest at %s", slot.isoformat())
    return slot


def run_digest_slot(slot: datetime) -> None:
    from . import digest

    try:
        digest.run_scheduled(slot)
    finally:
        schedule_digest()


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
