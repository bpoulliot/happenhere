"""Pexels image search — fetch a representative photo for events with no image."""

from __future__ import annotations

import logging
import random
import re

import httpx

log = logging.getLogger(__name__)

_API = "https://api.pexels.com/v1/search"

_STRIP = re.compile(
    r"\b("
    r"brewery|brewing|company|co|llc|inc|"
    r"presents?|featuring?|ft|vs|with|the|and|an?|of|at|in|on|for|by|"
    r"presented|hosted|night|day|event|series|live|show|performance|annual|"
    r"th|st|nd|rd|"
    r"mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|"
    r"mons?|tues?|weds?|thurs?|fris?|sats?|suns?"
    r")\b",
    re.I,
)


def fetch_photo_url(title: str, categories: list[str], api_key: str) -> str | None:
    """Return a random Pexels photo URL for the event, or None on failure."""
    if not api_key:
        return None

    query = _build_query(title, categories)
    if not query:
        return None

    try:
        resp = httpx.get(
            _API,
            params={"query": query, "per_page": 5, "orientation": "landscape"},
            headers={"Authorization": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if photos:
            photo = random.choice(photos)
            src = photo.get("src", {})
            url = src.get("large") or src.get("medium") or src.get("original")
            log.debug("pexels: %r -> %s", query, url)
            return url
    except Exception as exc:
        log.warning("pexels fetch failed for %r: %s", query, exc)
    return None


def _build_query(title: str, categories: list[str]) -> str:
    cleaned = _STRIP.sub(" ", title)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    hint = ""
    for cat in categories:
        cat_clean = cat.lower().strip()
        if cat_clean and cat_clean not in cleaned.lower():
            hint = cat_clean
            break

    query = f"{cleaned} {hint}".strip()
    words = query.split()
    if not words:
        return ""
    # Ensure at least one content word; add 'event' when the query is just a category hint
    if len(words) == 1 and not cleaned:
        words.append("event")
    return " ".join(words[:5])
