"""Item 29: the Meetup scraper has no built-in location; an unconfigured source refuses."""

from __future__ import annotations

import pytest

from app.config import Config
from app.scrapers import meetup
from app.scrapers.meetup import MeetupScraper


def test_an_unset_location_refuses_before_chromium(monkeypatch):
    def no_browser(self):
        pytest.fail("Chromium started without a location")

    monkeypatch.setattr(MeetupScraper, "_get_page", no_browser)
    monkeypatch.setattr(MeetupScraper, "_run", lambda self: self._scrape_impl())
    with pytest.raises(ValueError, match="Meetup location is not set"):
        MeetupScraper(Config(meetup_location="")).scrape()


def test_a_blank_location_refuses_too(monkeypatch):
    monkeypatch.setattr(MeetupScraper, "_run", lambda self: pytest.fail("ran"))
    with pytest.raises(ValueError, match="Meetup location is not set"):
        MeetupScraper(Config(meetup_location="   ")).scrape()


def test_a_configured_location_is_the_one_searched():
    url = MeetupScraper(Config(meetup_location="us--ny--New+York"))._get_url()
    assert "location=us--ny--New+York" in url


def test_the_refusal_text_names_no_place():
    assert meetup.MISSING_LOCATION == "Meetup location is not set (Settings): nothing to search"
