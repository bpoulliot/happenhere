"""Shared Playwright context manager for JS-heavy scrapers."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .base import BaseScraper, EventData

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Common desktop resolutions — vary per scraper run to reduce fingerprint match
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

# Injected on every page: hides the webdriver flag that bot-detection checks.
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
"""


class PlaywrightScraper(BaseScraper):
    """Base for JS-rendered scrapers.

    Playwright's sync API is bound to the thread that started it, so the browser is
    launched AND shut down inside ``scrape()``, on the same thread, every run. Every
    context ``_get_page()`` opens is tracked and closed with it. A failed shutdown is
    logged with its traceback, never swallowed: a silent close failure leaked 1,077
    Chromium processes in peakevents over 11 days.
    """

    SOURCE_TYPE = "playwright"

    def __init__(self, config):
        super().__init__(config)
        self._pw = None
        self._browser: Browser | None = None
        self._contexts: list[BrowserContext] = []
        self._owner_thread: int | None = None

    def _get_page(self) -> Page:
        if self._browser is None:
            pw = sync_playwright().start()
            try:
                self._browser = pw.chromium.launch(
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            except Exception:
                pw.stop()
                raise
            self._pw = pw
            self._owner_thread = threading.get_ident()
        ctx: BrowserContext = self._browser.new_context(
            user_agent=_UA,
            viewport=random.choice(_VIEWPORTS),
            locale="en-US",
            timezone_id=self.config.tz,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
        )
        self._contexts.append(ctx)
        ctx.add_init_script(_STEALTH_SCRIPT)
        return ctx.new_page()

    def scrape(self) -> list[EventData]:
        # sync_playwright cannot run inside an asyncio event loop.
        # Detect and delegate to a clean thread when needed; _run() launches and
        # shuts down the browser on whichever thread it runs on.
        try:
            asyncio.get_running_loop()
            in_async = True
        except RuntimeError:
            in_async = False

        if in_async:
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(self._run).result()
        return self._run()

    def _run(self) -> list[EventData]:
        try:
            return self._scrape_impl()
        finally:
            self._shutdown()

    def _scrape_impl(self) -> list[EventData]:
        raise NotImplementedError

    def _shutdown(self) -> None:
        """Close contexts, browser and driver. Must run on the launching thread."""
        name = type(self).__name__
        for ctx in self._contexts:
            try:
                ctx.close()
            except Exception:
                log.error("%s: closing a browser context failed", name, exc_info=True)
        self._contexts = []
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                log.error("%s: closing Chromium failed", name, exc_info=True)
            self._browser = None
        if self._pw is not None:
            try:
                # Stopping the driver also terminates any browser it still owns.
                self._pw.stop()
            except Exception:
                log.error("%s: stopping Playwright failed", name, exc_info=True)
            self._pw = None
        self._owner_thread = None

    def close(self) -> None:
        # scrape() always shuts down, so nothing is open here unless _get_page()
        # was called outside scrape(). Only the launching thread may close it.
        if self._browser is None and self._pw is None:
            return
        if self._owner_thread == threading.get_ident():
            self._shutdown()
            return
        log.error(
            "%s: browser opened outside scrape() on another thread; cannot close it here",
            type(self).__name__,
        )
