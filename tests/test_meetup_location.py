"""Item 29: the Meetup scraper has no built-in location; an unconfigured source refuses.

Since item 30 the location is a search location (``SearchArea``) passed per search: the place
it resolved to, or the location's own Meetup value. Every place here is invented.
"""

from __future__ import annotations

import pytest

from app.config import Config
from app.scrapers import meetup
from app.scrapers.base import SearchArea
from app.scrapers.meetup import MeetupScraper


def _area(**kw) -> SearchArea:
    base = {"id": 1, "label": "Area", "lat": 11.0, "lon": -21.0, "radius": 25, "unit": "miles"}
    return SearchArea(**{**base, **kw})


def test_no_location_refuses_before_chromium(monkeypatch):
    def no_browser(self):
        pytest.fail("Chromium started without a location")

    monkeypatch.setattr(MeetupScraper, "_get_page", no_browser)
    monkeypatch.setattr(MeetupScraper, "_run", lambda self: self._scrape_impl())
    with pytest.raises(ValueError, match="Meetup needs a search location"):
        MeetupScraper(Config()).scrape()


def test_a_location_with_no_us_place_and_a_blank_override_refuses_too(monkeypatch):
    monkeypatch.setattr(MeetupScraper, "_run", lambda self: pytest.fail("ran"))
    with pytest.raises(ValueError, match="Meetup needs a search location"):
        MeetupScraper(Config(), location=_area(meetup_location="   ")).scrape()


def test_the_locations_own_value_is_the_one_searched():
    area = _area(city="Sample City", state="CA", meetup_location="us--ny--New+York")
    assert "location=us--ny--New+York" in MeetupScraper(Config(), location=area)._get_url()


def test_the_refusal_text_names_no_place():
    assert meetup.MISSING_LOCATION == (
        "Meetup needs a search location in the US (a ZIP code or a city), "
        "or the location's own Meetup location: nothing to search"
    )
