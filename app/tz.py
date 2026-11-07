"""The one definition of the deployment's timezone.

Stored event times are naive local wall-clock times. Anything that converts an offset-carrying
time into them, or labels them for a calendar app, uses ``tz_of()`` — never a fixed zone.
Local scraper plugins import it as ``from app.tz import tz_of``.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from .config import Config, get_config

log = logging.getLogger(__name__)


def tz_of(cfg: Config | None = None) -> ZoneInfo:
    """The configured zone (``cfg.tz``, default: the loaded config); ``UTC`` if it is unknown."""
    cfg = cfg or get_config()
    try:
        return ZoneInfo((cfg.tz or "").strip() or "UTC")
    except Exception:
        log.warning("Unknown timezone %r — using UTC", cfg.tz)
        return ZoneInfo("UTC")
