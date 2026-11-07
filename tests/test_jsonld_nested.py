"""Items 12 (G1, G2) and 23: Event subtypes, list-valued ``@type``, and events nested under a
top-level or ``@graph`` item's ``event`` / ``events`` / ``Events`` key, de-duplicated."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from bs4 import BeautifulSoup

from app import discovery
from app.scrapers._html_base import extract_jsonld_events

FUTURE = (datetime.now() + timedelta(days=30)).replace(microsecond=0)


def _ev(n: int, typ="MusicEvent", **kw) -> dict:
    d = {
        "@type": typ,
        "name": f"Synthetic Show {n}",
        "startDate": (FUTURE + timedelta(days=n)).strftime("%Y-%m-%dT20:00:00"),
        "url": f"https://hall.example.com/events/{n}",
    }
    d.update(kw)
    return d


def _page(*blocks) -> BeautifulSoup:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return BeautifulSoup(f"<html><head>{scripts}</head><body></body></html>", "lxml")


def _names(items: list[dict]) -> list[str]:
    return sorted(i["name"] for i in items)


# --- G1 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typ", ["EducationEvent", "ComedyEvent", "SportsEvent", "Festival", "Event", "TheaterEvent"]
)
def test_g1_event_subtypes_are_read(typ):
    assert _names(extract_jsonld_events(_page(_ev(1, typ)))) == ["Synthetic Show 1"]


def test_g1_list_valued_type():
    assert len(extract_jsonld_events(_page(_ev(1, ["Event", "Thing"])))) == 1
    assert len(extract_jsonld_events(_page(_ev(1, ["Thing", "MusicEvent"])))) == 1


@pytest.mark.parametrize("typ", ["Place", "Organization", "EventVenue", "Thing", ["Place"]])
def test_g1_non_events_are_not_read(typ):
    item = {"@type": typ, "name": "Synthetic Hall"}
    assert extract_jsonld_events(_page(item)) == []


# --- G2 / item 23 -----------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["event", "events", "Events"])
def test_nested_list_under_a_place(key):
    place = {"@type": "Place", "name": "Synthetic Hall", key: [_ev(1), _ev(2), _ev(3)]}
    assert _names(extract_jsonld_events(_page(place))) == [
        "Synthetic Show 1",
        "Synthetic Show 2",
        "Synthetic Show 3",
    ]


@pytest.mark.parametrize("key", ["event", "events", "Events"])
def test_nested_single_object_under_a_place(key):
    place = {"@type": "Place", "name": "Synthetic Hall", key: _ev(7)}
    assert _names(extract_jsonld_events(_page(place))) == ["Synthetic Show 7"]


@pytest.mark.parametrize("key", ["event", "events", "Events"])
def test_nested_under_a_graph_item(key):
    graph = {
        "@context": "https://schema.org",
        "@graph": [{"@type": "Organization", "name": "Synthetic Org", key: [_ev(1, "Event")]}],
    }
    assert _names(extract_jsonld_events(_page(graph))) == ["Synthetic Show 1"]


@pytest.mark.parametrize(
    "container", ["MusicVenue", "EventVenue", "LocalBusiness", ["Place", "Thing"]]
)
def test_nested_under_a_place_subtype(container):
    """Item 23: the container is any top-level item, so a ``Place`` subtype (or a list-valued
    ``@type``) lists its events like a plain ``Place`` does, and is not itself an event."""
    place = {"@type": container, "name": "Synthetic Hall", "Events": [_ev(5, "EducationEvent")]}
    assert _names(extract_jsonld_events(_page(place))) == ["Synthetic Show 5"]


def test_nested_under_a_top_level_list_item():
    data = [{"@type": "Place", "name": "Synthetic Hall", "Events": [_ev(4)]}]
    assert _names(extract_jsonld_events(_page(data))) == ["Synthetic Show 4"]


def test_nested_non_events_are_dropped():
    place = {
        "@type": "Place",
        "Events": [_ev(1), {"@type": "Offer", "name": "Ticket"}, "a string", 3],
        "event": {"@type": "Place", "name": "Not an event"},
    }
    assert _names(extract_jsonld_events(_page(place))) == ["Synthetic Show 1"]


def test_top_level_and_nested_copy_of_one_event_is_read_once():
    place = {"@type": "Place", "name": "Synthetic Hall", "event": [_ev(1), _ev(2)]}
    assert _names(extract_jsonld_events(_page(_ev(1), place))) == [
        "Synthetic Show 1",
        "Synthetic Show 2",
    ]


def test_duplicates_without_url_fall_back_to_name_and_start():
    a = _ev(1)
    del a["url"]
    place = {"@type": "Place", "Events": [dict(a)]}
    assert len(extract_jsonld_events(_page(a, place))) == 1
    # The same name at another start is another event.
    b = dict(a, startDate=(FUTURE + timedelta(days=9)).strftime("%Y-%m-%dT20:00:00"))
    assert len(extract_jsonld_events(_page(a, {"@type": "Place", "Events": [b]}))) == 2


def test_two_events_sharing_a_listing_url_are_not_merged_when_starts_differ():
    """A url shared by distinct events (a series page) is not an identity on its own here:
    only ``url`` + the same start, or the same (name, start), is one event."""
    a = _ev(1, url="https://hall.example.com/series")
    b = _ev(2, url="https://hall.example.com/series")
    assert len(extract_jsonld_events(_page({"@type": "Place", "event": [a, b]}))) == 2


@pytest.mark.parametrize("key", ["event", "events", "Events"])
def test_detection_reports_jsonld_with_the_nested_count(key):
    place = {"@type": "Place", "name": "Synthetic Hall", key: [_ev(1), _ev(2)]}
    notes: list[str] = []
    res = discovery._probe_soup(_page(place), "https://hall.example.com/events", notes)
    assert res is not None and res.scraper_type == "jsonld"
    assert "Found 2 JSON-LD Event item(s)" in notes
    assert [p["title"] for p in res.preview] == ["Synthetic Show 1", "Synthetic Show 2"]
