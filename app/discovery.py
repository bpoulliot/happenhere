"""Auto-detect the best scraper for a given URL.

Before any request, the URL's host is checked (item 12): a ticketing platform that cannot be
a source gets a note saying so, and a Ticketmaster venue URL is a ``ticketmaster_venue``
source (a venue page is resolved through the Discovery API, never fetched).

Detection pipeline (stops at first successful strategy):
  1. httpx fetch → try JSON-LD, iCal probe, SpotHopper, Squarespace
  2. cloudscraper fetch (Cloudflare bypass) → same checks
  3. Playwright render → retry JSON-LD

Returns a DetectionResult describing the scraper type and a preview of events.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .netguard import NonPublicURL, httpx_request_hook, require_public_url
from .scrapers import ticketmaster_venue
from .scrapers._html_base import (
    DEFAULT_HEADERS,
    extract_jsonld_events,
    parse_jsonld_event,
)

_MAX_REDIRECTS = 5

log = logging.getLogger(__name__)

# Common iCal endpoint suffixes to probe
_ICAL_PROBES = [
    "?ical=1",
    "?feed=ical",
    "?feed=tribe_ical",
    "/calendar/?ical=1",
    "/events/?ical=1",
    "/events/feed/ical/",
    "/events.ics",
]

# SpotHopper selectors (Variant A and B)
_SPOTHOPPER_SELECTORS = [".calendar-day-event", ".event-text-holder"]
# Squarespace stacked events
_SQUARESPACE_SELECTOR = ".eventlist-event"


# Ticketing platforms with no path a self-hosted deployment may use: partner-only or
# artist-only APIs, terms that forbid automated reading, or a bot wall
# (docs/measurements/12-ticketing-platforms.md). Host suffix -> platform name.
_NOT_A_SOURCE: dict[str, str] = {
    "dice.fm": "DICE",
    "bandsintown.com": "Bandsintown",
    "axs.com": "AXS",
    "seetickets.com": "See Tickets",
    "seetickets.us": "See Tickets",
    "eventim.us": "See Tickets",
    "etix.com": "Etix",
}
# Eventbrite runs one site per country (eventbrite.com, .co.uk, .com.au, ...): item 16 decides.
_EVENTBRITE_RE = re.compile(r"(?:^|\.)eventbrite\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$")


def platform_note(url: str) -> str | None:
    """The note for a URL on a ticketing platform that cannot be a source, else ``None``.

    Decided from the host alone: no request is made.
    """
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    name = None
    for suffix, platform in _NOT_A_SOURCE.items():
        if host == suffix or host.endswith("." + suffix):
            name = platform
            break
    if name is None and _EVENTBRITE_RE.search(host):
        name = "Eventbrite"
    if name is None:
        return None
    return (
        f"{name}: this platform's terms (or bot wall) do not allow automated reading, so it "
        "cannot be a source. Add the venue's own website instead."
    )


@dataclass
class DetectionResult:
    scraper_type: (
        str  # "jsonld" | "ical" | "spothopper" | "squarespace" | "playwright_jsonld" | None
    )
    detected_url: str  # canonical URL that worked (may differ from input if probed)
    ical_url: str | None  # set when scraper_type == "ical"
    preview: list[dict]  # up to 5 sample events from the detected source
    notes: list[str]  # human-readable steps taken (for onboarding guidance)
    success: bool


def detect(url: str, config=None) -> DetectionResult:
    """Try to auto-detect the right scraper for `url`.

    Pass `config` to enable the Playwright fallback.
    """
    notes: list[str] = []

    # Recognised by host, before any request (item 12).
    note = platform_note(url)
    if note:
        return DetectionResult(None, url, None, [], [note], False)
    if ticketmaster_venue.is_venue_url(url):
        return _detect_ticketmaster_venue(url, config, notes)

    try:
        require_public_url(url)
    except NonPublicURL as exc:
        notes.append(f"Refused: {exc}")
        return DetectionResult(None, url, None, [], notes, False)

    # --- Tier 1: plain httpx ---
    soup, final_url = _fetch_soup(url, use_cloudscraper=False)
    if soup:
        notes.append(f"Fetched {final_url} with httpx")
        result = _probe_soup(soup, final_url, notes)
        if result:
            return result

        # iCal probe on same domain
        ical = _probe_ical(url, notes)
        if ical:
            return ical

    # --- Tier 2: cloudscraper (Cloudflare bypass) ---
    if soup is None:
        notes.append("httpx blocked — retrying with cloudscraper")
        soup, final_url = _fetch_soup(url, use_cloudscraper=True)
        if soup:
            notes.append(f"cloudscraper succeeded for {final_url}")
            result = _probe_soup(soup, final_url, notes)
            if result:
                return result
            ical = _probe_ical(url, notes)
            if ical:
                return ical

    # --- Tier 3: Playwright (JS render) ---
    if config is not None:
        notes.append("Trying Playwright (JavaScript render)")
        playwright_result = _probe_playwright(url, notes)
        if playwright_result:
            return playwright_result

    notes.append("Could not auto-detect a scraper for this URL")
    return DetectionResult(
        scraper_type=None,
        detected_url=url,
        ical_url=None,
        preview=[],
        notes=notes,
        success=False,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_ticketmaster_venue(url: str, config, notes: list[str]) -> DetectionResult:
    """A Ticketmaster venue URL: the Discovery URL to store, without fetching ticketmaster.com."""
    key = getattr(config, "ticketmaster_api_key", "") if config is not None else ""
    try:
        stored = ticketmaster_venue.resolve_venue_url(url, key)
    except ticketmaster_venue.VenueNotFound as exc:
        notes.append(f"Ticketmaster venue: {exc}")
        return DetectionResult(None, url, None, [], notes, False)
    except httpx.HTTPError as exc:
        log.warning("Ticketmaster venue lookup failed: %s", exc)
        notes.append(
            "Ticketmaster venue lookup failed: check TICKETMASTER_API_KEY, or paste the "
            "Discovery URL with the venueId."
        )
        return DetectionResult(None, url, None, [], notes, False)
    notes.append(f"Ticketmaster venue: events come from the Discovery API ({stored})")
    return DetectionResult("ticketmaster_venue", stored, None, [], notes, True)


def _fetch_soup(url: str, use_cloudscraper: bool) -> tuple[BeautifulSoup | None, str]:
    try:
        if use_cloudscraper:
            import cloudscraper

            s = cloudscraper.create_scraper()
            # Follow redirects by hand so each hop is checked BEFORE it is requested.
            target = url
            for _ in range(_MAX_REDIRECTS + 1):
                require_public_url(target)
                resp = s.get(target, timeout=15, allow_redirects=False)
                if not resp.is_redirect:
                    break
                target = urljoin(target, resp.headers["location"])
            else:
                raise NonPublicURL(f"more than {_MAX_REDIRECTS} redirects")
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml"), resp.url if hasattr(resp, "url") else url
        else:
            with httpx.Client(
                headers=DEFAULT_HEADERS,
                timeout=15,
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                event_hooks={"request": [httpx_request_hook]},
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml"), str(resp.url)
    except NonPublicURL as exc:
        log.warning("_fetch_soup(%s): refused: %s", url, exc)
        return None, url
    except Exception as exc:
        log.debug("_fetch_soup(%s, cloudscraper=%s): %s", url, use_cloudscraper, exc)
        return None, url


def _probe_soup(soup: BeautifulSoup, url: str, notes: list[str]) -> DetectionResult | None:
    # JSON-LD
    items = extract_jsonld_events(soup)
    if items:
        notes.append(f"Found {len(items)} JSON-LD Event item(s)")
        preview = _jsonld_preview(items, url)
        return DetectionResult("jsonld", url, None, preview, notes, True)

    notes.append("No JSON-LD events found")

    # SpotHopper
    for sel in _SPOTHOPPER_SELECTORS:
        if soup.select(sel):
            notes.append(f"Detected SpotHopper widget ({sel})")
            return DetectionResult("spothopper", url, None, [], notes, True)

    # Squarespace stacked events
    if soup.select(_SQUARESPACE_SELECTOR):
        notes.append("Detected Squarespace stacked events layout")
        return DetectionResult("squarespace", url, None, [], notes, True)

    return None


def _probe_ical(base_url: str, notes: list[str]) -> DetectionResult | None:
    domain = urlparse(base_url)
    root = f"{domain.scheme}://{domain.netloc}"

    for suffix in _ICAL_PROBES:
        candidate = (
            urljoin(root, suffix) if suffix.startswith("/") else base_url.rstrip("/") + suffix
        )
        try:
            with httpx.Client(
                headers=DEFAULT_HEADERS,
                timeout=8,
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                event_hooks={"request": [httpx_request_hook]},
            ) as client:
                resp = client.get(candidate)
            ct = resp.headers.get("content-type", "")
            body = resp.text[:200]
            if resp.status_code == 200 and (
                "calendar" in ct or body.strip().startswith("BEGIN:VCALENDAR")
            ):
                notes.append(f"Found iCal feed at {candidate}")
                # Count events as preview
                try:
                    from icalendar import Calendar

                    cal = Calendar.from_ical(resp.content)
                    events = [c for c in cal.walk() if c.name == "VEVENT"]
                    preview = [
                        {
                            "title": str(e.get("SUMMARY", "?")),
                            "start": str(e.get("DTSTART", {}).dt if e.get("DTSTART") else ""),
                        }
                        for e in events[:5]
                    ]
                except Exception:
                    preview = []
                return DetectionResult("ical", base_url, candidate, preview, notes, True)
        except Exception:
            pass

    notes.append("No iCal feed found at common endpoints")
    return None


def _jsonld_preview(items: list[dict], url: str) -> list[dict]:
    preview = []
    for item in items[:5]:
        ev = parse_jsonld_event(item, "")
        if ev:
            preview.append(
                {"title": ev.title, "start": ev.start_dt.isoformat() if ev.start_dt else ""}
            )
    return preview


def _probe_playwright(url: str, notes: list[str]) -> DetectionResult | None:
    try:
        import time as _time

        from playwright.sync_api import sync_playwright

        _UA = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        # The context manager stops the driver (and any browser it owns) even
        # when launch() or the page load raises.
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            try:
                page = browser.new_context(user_agent=_UA).new_page()
                page.route("**/*", _guard_navigation)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                _time.sleep(3)
                soup = BeautifulSoup(page.content(), "lxml")
            finally:
                browser.close()

        items = extract_jsonld_events(soup)
        if items:
            notes.append(f"Playwright: found {len(items)} JSON-LD event(s) after JS render")
            return DetectionResult(
                "playwright_jsonld", url, None, _jsonld_preview(items, url), notes, True
            )

        notes.append("Playwright: page rendered but no JSON-LD events found")
    except Exception as exc:
        notes.append(f"Playwright unavailable: {exc}")

    return None


def _guard_navigation(route) -> None:
    """Playwright route handler: abort a navigation (incl. redirects) to a non-public host."""
    req = route.request
    if req.is_navigation_request():
        try:
            require_public_url(req.url)
        except NonPublicURL as exc:
            log.warning("discovery: blocked navigation to %s (%s)", req.url, exc)
            route.abort("blockedbyclient")
            return
    route.continue_()
