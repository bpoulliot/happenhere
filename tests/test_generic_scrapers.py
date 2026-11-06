"""Item 19: the generic URL-driven `ical` and `spothopper` scrapers return their events."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app.config import Config
from app.models import Source
from app.scrapers import _ical_base, _spothopper_base
from app.scrapers.registry import get_scraper

FUTURE = (datetime.now() + timedelta(days=20)).replace(hour=20, minute=0, second=0, microsecond=0)

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//example//EN\r\nBEGIN:VEVENT\r\n"
    f"UID:evt-1@example.com\r\nSUMMARY:Synthetic Quartet\r\nDTSTART:{FUTURE:%Y%m%dT%H%M%S}\r\n"
    "URL:https://hall.example.com/events/1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)

SPOTHOPPER = f"""<html><body>
<div class="calendar-day-event">
  <a href="https://brewery.example.com/events/trivia"></a>
  <div class="ev-title">Synthetic Trivia</div>
  <div class="ev-date">{FUTURE:%A}, {FUTURE:%B} {FUTURE.day}th, {FUTURE.year}</div>
  <div class="ev-time">8:00 pm</div>
</div>
</body></html>"""


def _mock(monkeypatch, module, body: str, content_type: str) -> None:
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kw)

    monkeypatch.setattr(module.httpx, "Client", client)


@pytest.mark.parametrize(
    "stype, module, url, body, ctype, title",
    [
        (
            "ical",
            _ical_base,
            "https://hall.example.com/events/?ical=1",
            ICS,
            "text/calendar",
            "Synthetic Quartet",
        ),
        (
            "spothopper",
            _spothopper_base,
            "https://brewery.example.com/events",
            SPOTHOPPER,
            "text/html",
            "Synthetic Trivia",
        ),
    ],
)
def test_generic_scraper_returns_its_event(monkeypatch, stype, module, url, body, ctype, title):
    _mock(monkeypatch, module, body, ctype)
    src = Source(name=f"syn-{stype}", display_name="Synthetic Venue", type=stype, url=url)
    scraper = get_scraper(src, Config())

    events = scraper.scrape()

    assert [e.title for e in events] == [title]
    assert events[0].start_dt == FUTURE
    assert events[0].venue_name == "Synthetic Venue"
    assert scraper.SOURCE_NAME == "Synthetic Venue"


def test_every_scraper_class_has_a_source_name():
    from app.scrapers.registry import _TYPE_MAP

    for key, cls in _TYPE_MAP.items():
        assert isinstance(getattr(cls, "SOURCE_NAME", None), str), key
