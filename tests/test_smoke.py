"""Smoke tests — verify core modules import and key objects instantiate without errors."""
import json

import pytest


def test_config_loads():
    from app.config import Config
    cfg = Config()
    assert cfg.scrape_interval_hours == 6
    assert cfg.city_zip == ""


def test_registry_has_generic_types():
    from app.scrapers.registry import _TYPE_MAP, scraper_types
    assert "jsonld" in _TYPE_MAP
    assert "ical" in _TYPE_MAP
    assert "spothopper" in _TYPE_MAP
    assert "squarespace" in _TYPE_MAP
    assert len(scraper_types()) >= 5


def test_generic_scraper_instantiates():
    from app.config import Config
    from app.scrapers.generic import GenericICalScraper, GenericJsonLdScraper
    cfg = Config()
    scraper = GenericJsonLdScraper(cfg, source_url="https://example.com/events", venue_name="Test Venue")
    assert scraper.source_url == "https://example.com/events"
    assert scraper.venue_name == "Test Venue"
    ical = GenericICalScraper(cfg, source_url="https://example.com/events/?ical=1", venue_name="Test")
    assert ical.ICAL_URL == "https://example.com/events/?ical=1"


def test_detection_result_structure():
    from app.discovery import DetectionResult
    r = DetectionResult(
        scraper_type=None,
        detected_url="https://example.com",
        ical_url=None,
        preview=[],
        notes=["test"],
        success=False,
    )
    assert not r.success
    assert r.notes == ["test"]


def test_dedup_key_consistent():
    from app.web.routes import _dedup_key
    k1 = _dedup_key("Trivia Night at the Pub", "2026-07-01T19:00:00")
    k2 = _dedup_key("Trivia Night at the Pub", "2026-07-01T20:00:00")
    assert k1 == k2  # same title+date regardless of time


def test_stock_photo_fallback():
    from app.web.routes import _stock_photo
    url = _stock_photo(["Brewery"], "Geeks Who Drink")
    from urllib.parse import urlparse

    assert urlparse(url).hostname == "images.pexels.com"
    default_url = _stock_photo([], "Something Obscure XYZ 123")
    assert urlparse(default_url).hostname == "images.pexels.com"


def test_slugify():
    from app.web.routes import _slugify
    assert _slugify("Bristol Brewing Company") == "bristol_brewing_company"
    assert _slugify("Café Rösti!") == "cafe_rosti"
    assert len(_slugify("a" * 100)) <= 50


def test_models_import():
    from app.models import AppMeta, Event, NotificationLog, Source
    # Verify all columns exist on Source
    assert hasattr(Source, "name")
    assert hasattr(Source, "type")
    assert hasattr(Source, "url")
    assert hasattr(Source, "categories")
