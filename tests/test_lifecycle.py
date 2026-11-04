"""Browser lifecycle and auth fail-closed tests.

The Playwright tests use a fake driver that records the thread of every launch and
close, so they run without Chromium. They pin the peakevents leak: the browser was
launched in a worker thread and closed from the caller's, the close raised, and a
bare ``except: pass`` hid it while 1,077 Chromium processes piled up.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from app.config import Config
from app.scrapers import _playwright_base as pwb


class _Recorder:
    def __init__(self):
        self.events: list[tuple[str, int]] = []

    def add(self, what: str) -> None:
        self.events.append((what, threading.get_ident()))

    def threads(self, what: str) -> list[int]:
        return [t for w, t in self.events if w == what]

    def count(self, what: str) -> int:
        return len(self.threads(what))


class _Affine:
    """Raises like Playwright's sync API when used off its owning thread."""

    def __init__(self, rec: _Recorder, owner: int):
        self._rec = rec
        self._owner = owner

    def _check(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("cannot switch to a different thread")


class _Page(_Affine):
    def close(self):
        self._check()
        self._rec.add("page.close")


class _Context(_Affine):
    def add_init_script(self, _script):
        self._check()

    def new_page(self):
        self._check()
        return _Page(self._rec, self._owner)

    def close(self):
        self._check()
        self._rec.add("context.close")


class _Browser(_Affine):
    def new_context(self, **_kw):
        self._check()
        self._rec.add("context.open")
        return _Context(self._rec, self._owner)

    def close(self):
        self._check()
        self._rec.add("browser.close")


class _Chromium(_Affine):
    def launch(self, **_kw):
        self._check()
        self._rec.add("launch")
        return _Browser(self._rec, self._owner)


class _Driver(_Affine):
    def __init__(self, rec, owner):
        super().__init__(rec, owner)
        self.chromium = _Chromium(rec, owner)

    def stop(self):
        self._check()
        self._rec.add("driver.stop")


class _Starter:
    def __init__(self, rec):
        self._rec = rec

    def start(self):
        self._rec.add("driver.start")
        return _Driver(self._rec, threading.get_ident())


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(pwb, "sync_playwright", lambda: _Starter(r))
    return r


class _TwoPages(pwb.PlaywrightScraper):
    SOURCE_NAME = "test"

    def __init__(self, config, fail=False):
        super().__init__(config)
        self.fail = fail

    def _scrape_impl(self):
        for _ in range(2):
            page = self._get_page()
            page.close()  # callers close only the page, as the real scrapers do
        if self.fail:
            raise ValueError("scrape failed")
        return []


def _assert_clean(rec: _Recorder):
    assert rec.count("launch") == 1
    assert rec.count("browser.close") == 1, "Chromium was never closed"
    assert rec.count("driver.stop") == 1
    assert rec.count("context.close") == rec.count("context.open") == 2, "a context leaked"
    launch_thread = rec.threads("launch")[0]
    for what in ("browser.close", "driver.stop", "context.close"):
        assert set(rec.threads(what)) == {launch_thread}, f"{what} ran off the launch thread"


def test_sync_scrape_closes_everything(rec):
    s = _TwoPages(Config())
    s.scrape()
    s.close()
    _assert_clean(rec)


def test_scrape_inside_event_loop_closes_on_worker_thread(rec):
    """The job runner's path: scrape() inside a loop, close() from the caller."""
    s = _TwoPages(Config())

    async def run():
        s.scrape()
        s.close()

    asyncio.run(run())
    _assert_clean(rec)
    assert rec.threads("launch")[0] != threading.get_ident()


def test_failed_scrape_still_closes(rec):
    s = _TwoPages(Config(), fail=True)
    with pytest.raises(ValueError):
        s.scrape()
    s.close()
    _assert_clean(rec)


def test_close_failure_is_logged_not_swallowed(rec, monkeypatch, caplog):
    def boom(self):
        raise RuntimeError("close failed")

    monkeypatch.setattr(_Browser, "close", boom)
    s = _TwoPages(Config())
    with caplog.at_level(logging.ERROR, logger=pwb.log.name):
        s.scrape()
    assert any("closing Chromium failed" in r.message for r in caplog.records)
    assert rec.count("driver.stop") == 1, "driver must still stop after a browser close error"


def test_failed_launch_stops_driver(rec, monkeypatch):
    def boom(self, **_kw):
        raise RuntimeError("no chromium")

    monkeypatch.setattr(_Chromium, "launch", boom)
    s = _TwoPages(Config())
    with pytest.raises(RuntimeError):
        s.scrape()
    assert rec.count("driver.start") == rec.count("driver.stop") == 1


def test_context_uses_configured_timezone(rec, monkeypatch):
    seen = {}
    orig = _Browser.new_context

    def spy(self, **kw):
        seen.update(kw)
        return orig(self, **kw)

    monkeypatch.setattr(_Browser, "new_context", spy)
    cfg = Config()
    cfg.tz = "Europe/Berlin"
    _TwoPages(cfg).scrape()
    assert seen["timezone_id"] == "Europe/Berlin"


# ---------------------------------------------------------------------------
# Auth fails closed
# ---------------------------------------------------------------------------


class _Req:
    cookies: dict = {}


def test_no_password_means_no_user(monkeypatch):
    from app.web import routes

    cfg = Config()
    cfg.password_hash = ""
    monkeypatch.setattr(routes, "get_config", lambda: cfg)
    assert routes._get_user(_Req()) is None


def test_bootstrap_keeps_generated_password_out_of_the_log(tmp_path, caplog, monkeypatch):
    from app import auth

    monkeypatch.delenv("HAPPENHERE_PASSWORD", raising=False)
    cfg = Config()
    saved = []
    with caplog.at_level(logging.DEBUG):
        auth.bootstrap(cfg, saved.append, data_dir=tmp_path)
    pw_file = tmp_path / "initial-password"
    password = pw_file.read_text().strip()
    assert password and auth.verify_password(password, cfg.password_hash)
    assert oct(pw_file.stat().st_mode & 0o777) == "0o600"
    assert all(password not in r.getMessage() for r in caplog.records)
    assert saved == [cfg] and cfg.secret_key


def test_auth_json_is_private_even_if_it_existed(tmp_path):
    from app.config import save_auth

    cfg = Config()
    cfg.db_path = str(tmp_path / "x.db")
    cfg.secret_key = "s"
    path = tmp_path / "auth.json"
    path.write_text("{}")
    path.chmod(0o644)
    save_auth(cfg)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
