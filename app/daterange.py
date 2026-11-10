"""Item 33: the events listing's date range (``?from=YYYY-MM-DD&to=YYYY-MM-DD``).

Dates are whole days in the configured zone (``app.tz.tz_of``), both ends inclusive: ``to``
reaches 23:59:59.999999 of that day. Stored event times are naive local wall time, so the
window is returned naive too.

* no ``from`` and no ``to``: now until now + ``days`` (the listing as it always was);
* ``from`` alone: ``days`` whole days starting on ``from``;
* ``to`` alone: now until the end of ``to``;
* both: ``from`` until the end of ``to``.

Nothing before now is ever shown (the listing never showed past events): a ``from`` before today
counts as today and the window starts at now. A ``to`` before today, ``to`` before ``from`` or a
value that is not a ``YYYY-MM-DD`` date raises ``DateRangeInvalid``, whose ``code`` names the
reason in ``REASONS``: a response says it in that fixed text, never the exception's text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .tz import tz_of

REASONS = {
    "bad_date": "dates must be YYYY-MM-DD",
    "reversed": "the end date is before the start date",
    "past": "the end date is before today",
}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class DateRangeInvalid(ValueError):
    """The range cannot be used. ``code`` is a key of ``REASONS``."""

    def __init__(self, code: str):
        super().__init__(REASONS[code])
        self.code = code


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


def local_now() -> datetime:
    """Now in the configured zone, naive (how the database stores times)."""
    return datetime.now(tz_of()).replace(tzinfo=None)


def _parse(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    if not _ISO_DATE.fullmatch(value):
        raise DateRangeInvalid("bad_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DateRangeInvalid("bad_date") from None


def window(
    days: int, date_from: str = "", date_to: str = "", now: datetime | None = None
) -> Window:
    """The [start, end] the listing shows. Raises ``DateRangeInvalid``."""
    now = now or local_now()
    first, last = _parse(date_from), _parse(date_to)
    if first is None and last is None:
        return Window(now, now + timedelta(days=days))
    if first is not None and last is not None and last < first:
        raise DateRangeInvalid("reversed")
    today = now.date()
    if last is not None and last < today:
        raise DateRangeInvalid("past")
    if first is None or first < today:
        first = today
    if last is None:
        last = first + timedelta(days=max(days, 1) - 1)
    start = max(datetime.combine(first, time.min), now)
    return Window(start, datetime.combine(last, time.max))
