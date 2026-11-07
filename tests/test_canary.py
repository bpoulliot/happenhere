"""Item 06: the per-source canary.

A source with no upcoming events for two consecutive runs, or failing for three, raises ONE
notification per run naming it (every newly tripped source in the same message). It stays quiet
while tripped and re-arms when the source is healthy again. Nothing here touches the network: the
ntfy POST is faked, the clock is passed in, and the database is a temp file.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, canary, config, db, digest, ntfy, oidc
from app.config import Config
from app.jobs import run_all_scrapers
from app.main import app
from app.models import Event, NotificationLog, Source
from app.scrapers import registry
from app.scrapers.base import BaseScraper, EventData

NOW = datetime(2026, 11, 2, 9, 0)
SOON = NOW + timedelta(days=3)
PAST = NOW - timedelta(days=3)


class FakeNtfy:
    """Stands in for the ntfy POST; ``outcomes`` is consumed one per call, then success."""

    def __init__(self):
        self.calls: list[dict] = []
        self.outcomes: list[tuple[bool, str | None]] = []

    def __call__(self, cfg, title, body, click=None, tags=None):
        self.calls.append({"title": title, "body": body, "click": click, "tags": tags})
        return self.outcomes.pop(0) if self.outcomes else (True, None)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yml"))
    db.init_db(tmp_path / "t.db")
    c = Config(
        ntfy_url="https://ntfy.example.com",
        ntfy_topic="events",
        tz="UTC",
        city_name="Exampleton",
        public_url="https://events.example.com",
        secret_key="k" * 64,
    )
    monkeypatch.setattr(config, "_cfg", c)
    monkeypatch.setattr(config, "load_config", lambda: c)
    fake = FakeNtfy()
    monkeypatch.setattr(ntfy, "post_to_ntfy", fake)
    c.fake = fake  # type: ignore[attr-defined]
    return c


def add_source(name="venue", enabled=True, error_count=0, last_error=None) -> None:
    with db.get_session() as s:
        s.add(
            Source(
                name=name,
                display_name=name.title(),
                type="ical",
                url=f"https://{name}.example.com/events",
                enabled=enabled,
                error_count=error_count,
                last_error=last_error,
            )
        )
        s.commit()


def add_event(name: str, key: str, start: datetime, retired: bool = False) -> int:
    with db.get_session() as s:
        src = s.query(Source).filter_by(name=name).one()
        ev = Event(
            source_id=src.id,
            dedup_hash=hashlib.sha256(f"{name}{key}".encode()).hexdigest(),
            title=f"Event {key}",
            start_dt=start,
            retired_at=NOW if retired else None,
        )
        s.add(ev)
        s.commit()
        return ev.id


def set_errors(name: str, count: int, last_error: str | None = None) -> None:
    with db.get_session() as s:
        src = s.query(Source).filter_by(name=name).one()
        src.error_count = count
        src.last_error = last_error
        s.commit()


def source(name="venue") -> Source:
    with db.get_session() as s:
        row = s.query(Source).filter_by(name=name).one()
        s.expunge(row)
        return row


def run(n: int = 1, at: datetime = NOW) -> None:
    for _ in range(n):
        canary.check(at)


# --- tripping ---------------------------------------------------------------------------------


def test_two_zero_event_runs_trip_one_does_not(env):
    add_source()
    run(1)
    assert env.fake.calls == []
    run(1)
    assert len(env.fake.calls) == 1
    assert "Venue" in env.fake.calls[0]["body"]
    assert "no upcoming events" in env.fake.calls[0]["body"].lower()


def test_three_error_runs_trip_two_do_not(env):
    add_source(error_count=1, last_error="HTTP 500 from the venue")
    add_event("venue", "a", SOON)  # has events, so only the error condition can trip
    run(2)
    assert env.fake.calls == []
    run(1)
    assert len(env.fake.calls) == 1
    body = env.fake.calls[0]["body"]
    assert "Venue" in body and "HTTP 500 from the venue" in body
    assert "fail" in body.lower()


def test_last_error_is_truncated_and_one_line(env):
    add_source(error_count=1, last_error="boom\n" + "x" * 500)
    add_event("venue", "a", SOON)
    run(3)
    body = env.fake.calls[0]["body"]
    assert "x" * 200 not in body
    assert "boom\n" not in body


def test_past_and_retired_events_are_not_upcoming(env):
    add_source()
    add_event("venue", "old", PAST)
    add_event("venue", "gone", SOON, retired=True)
    run(2)
    assert len(env.fake.calls) == 1


def test_an_upcoming_event_keeps_the_source_quiet(env):
    add_source()
    add_event("venue", "a", SOON)
    run(5)
    assert env.fake.calls == []


# --- silence and re-arm -----------------------------------------------------------------------


def test_a_tripped_source_does_not_alert_again(env):
    add_source()
    run(2)
    assert len(env.fake.calls) == 1
    run(6)
    assert len(env.fake.calls) == 1


def test_recovery_rearms_and_a_later_trip_alerts_again(env):
    add_source()
    run(2)
    assert len(env.fake.calls) == 1
    eid = add_event("venue", "back", SOON)
    run(1)  # healthy: events and no errors
    s = source()
    assert (s.empty_runs, s.error_runs, s.canary_alerted_at) == (0, 0, None)
    with db.get_session() as ses:
        ses.delete(ses.get(Event, eid))
        ses.commit()
    run(1)
    assert len(env.fake.calls) == 1  # one empty run is not enough
    run(1)
    assert len(env.fake.calls) == 2


def test_error_recovery_rearms(env):
    add_source(error_count=1, last_error="down")
    add_event("venue", "a", SOON)
    run(3)
    assert len(env.fake.calls) == 1
    set_errors("venue", 0)
    run(1)
    set_errors("venue", 2, "down again")
    run(2)
    assert len(env.fake.calls) == 1
    run(1)
    assert len(env.fake.calls) == 2


def test_a_healthy_run_in_between_resets_the_streak(env):
    add_source()
    run(1)  # empty
    eid = add_event("venue", "a", SOON)
    run(1)  # healthy
    with db.get_session() as ses:
        ses.delete(ses.get(Event, eid))
        ses.commit()
    run(1)  # empty again, but the streak restarted
    assert env.fake.calls == []


# --- disabled, unconfigured, failed POST --------------------------------------------------------


def test_a_disabled_source_never_trips(env):
    add_source(enabled=False, error_count=9, last_error="dead")
    run(10)
    assert env.fake.calls == []
    s = source()
    assert (s.empty_runs or 0, s.error_runs or 0) == (0, 0)


def test_disabling_a_counting_source_clears_its_streak(env):
    add_source()
    run(1)
    with db.get_session() as ses:
        ses.query(Source).filter_by(name="venue").one().enabled = False
        ses.commit()
    run(3)
    with db.get_session() as ses:
        ses.query(Source).filter_by(name="venue").one().enabled = True
        ses.commit()
    run(1)  # first empty run since it came back
    assert env.fake.calls == []


def test_a_failed_post_does_not_mark_it_alerted_and_the_next_run_retries(env):
    add_source()
    env.fake.outcomes = [(False, "connection refused")]
    run(2)
    assert len(env.fake.calls) == 1
    assert source().canary_alerted_at is None
    run(1)
    assert len(env.fake.calls) == 2
    assert source().canary_alerted_at is not None
    run(2)
    assert len(env.fake.calls) == 2


def test_no_ntfy_configured_sends_nothing_and_raises_nothing(env):
    env.ntfy_url = ""
    add_source()
    run(4)
    assert env.fake.calls == []
    assert source().canary_alerted_at is None
    env.ntfy_url = "https://ntfy.example.com"
    run(1)  # configured later: the waiting alert goes out
    assert len(env.fake.calls) == 1


def test_a_post_that_raises_is_swallowed_and_retried(env, monkeypatch):
    add_source()

    def boom(*a, **k):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(ntfy, "post_to_ntfy", boom)
    run(2)
    assert source().canary_alerted_at is None
    monkeypatch.setattr(ntfy, "post_to_ntfy", env.fake)
    run(1)
    assert len(env.fake.calls) == 1


# --- one message per run ----------------------------------------------------------------------------


def test_two_sources_tripping_in_one_run_send_one_message(env):
    add_source("alpha")
    add_source("beta")
    run(2)
    assert len(env.fake.calls) == 1
    body = env.fake.calls[0]["body"]
    assert "Alpha" in body and "Beta" in body
    assert env.fake.calls[0]["title"].count("2") >= 1


def test_a_later_trip_sends_only_the_newly_tripped(env):
    add_source("alpha")
    run(2)
    add_source("beta")
    run(2)
    assert len(env.fake.calls) == 2
    second = env.fake.calls[1]["body"]
    assert "Beta" in second and "Alpha" not in second


def test_click_points_at_the_admin(env):
    add_source("alpha")
    run(2)
    assert env.fake.calls[0]["click"] == "https://events.example.com/admin/sources/alpha/edit"
    add_source("beta")
    add_source("gamma")
    run(2)
    assert env.fake.calls[1]["click"] == "https://events.example.com/admin"


def test_title_is_not_the_digests(env):
    add_source()
    run(2)
    title = env.fake.calls[0]["title"]
    assert "canary" in title.lower()
    assert "event" not in title.lower().replace("no upcoming events", "")


# --- the digest is unaffected ------------------------------------------------------------------------


def test_the_canary_writes_no_notification_log_row(env):
    add_source()
    run(3)
    with db.get_session() as s:
        assert s.query(NotificationLog).count() == 0


def test_the_digest_gate_still_fires_and_skips_as_before(env):
    add_source("tripped")  # no events: trips and alerts
    add_source("fine")
    add_event("fine", "a", SOON)
    run(2)
    assert len(env.fake.calls) == 1  # the canary
    env.fake.calls.clear()
    assert digest.run_digest(now=NOW).status == "sent"  # first digest: not gated by the canary
    assert digest.run_digest(now=NOW).status == "skipped-unchanged"
    run(3)  # more canary runs, nothing new
    assert digest.run_digest(now=NOW).status == "skipped-unchanged"
    add_event("fine", "b", SOON + timedelta(days=1))
    assert digest.run_digest(now=NOW).status == "sent"
    with db.get_session() as s:
        assert [r.status for r in s.query(NotificationLog).order_by(NotificationLog.id)] == [
            "sent",
            "skipped-unchanged",
            "skipped-unchanged",
            "sent",
        ]


# --- hooked into the scrape run ----------------------------------------------------------------------

BATCH: dict[str, list[EventData] | Exception] = {}


class _Fake(BaseScraper):
    SOURCE_TYPE = "fake_item06"

    def __init__(self, config, source_url: str = "", venue_name: str = ""):
        super().__init__(config)
        self.url = source_url

    def scrape(self):
        result = BATCH[self.url]
        if isinstance(result, Exception):
            raise result
        return list(result)


@pytest.fixture
def scrape_env(env, monkeypatch):
    monkeypatch.setattr("app.jobs.get_config", lambda: env)
    monkeypatch.setattr("app.jobs._backfill_pexels_images", lambda: None)
    registry.register_type("fake_item06", _Fake, "Fake")
    yield env
    registry.unregister_type("fake_item06")
    BATCH.clear()


def test_a_scrape_run_checks_the_canary_after_the_sources(scrape_env):
    with db.get_session() as s:
        s.add(
            Source(
                name="flaky",
                display_name="Flaky",
                type="fake_item06",
                url="https://flaky.example.com/x",
                enabled=True,
            )
        )
        s.commit()
    BATCH["https://flaky.example.com/x"] = RuntimeError("HTTP 503")
    run_all_scrapers()
    run_all_scrapers()
    # the source stores no events, so two runs are two empty runs: it trips on the second
    assert len(scrape_env.fake.calls) == 1
    body = scrape_env.fake.calls[0]["body"]
    assert "Flaky" in body
    run_all_scrapers()
    assert len(scrape_env.fake.calls) == 1  # silent while tripped; the error streak reached 3


def test_a_canary_failure_never_breaks_the_scrape_run(scrape_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("canary exploded")

    monkeypatch.setattr(canary, "check", boom)
    with db.get_session() as s:
        s.add(Source(name="ok", display_name="Ok", type="fake_item06", url="u", enabled=True))
        s.commit()
    BATCH["u"] = [EventData(title="T", start_dt=datetime.now() + timedelta(days=2), venue_name="V")]
    run_all_scrapers()  # must not raise
    with db.get_session() as s:
        assert s.query(Event).count() == 1


# --- migration --------------------------------------------------------------------------------------


def test_an_old_database_gains_the_columns(tmp_path):
    path = tmp_path / "old.db"
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR NOT NULL UNIQUE,"
        " display_name VARCHAR, type VARCHAR, url VARCHAR, categories TEXT, tags TEXT,"
        " last_scraped DATETIME, enabled BOOLEAN NOT NULL DEFAULT 1, error_count INTEGER DEFAULT 0,"
        " last_error TEXT)"
    )
    con.execute("INSERT INTO sources (name, display_name, type, url) VALUES ('a','A','ical','u')")
    con.commit()
    con.close()
    db.init_db(path)
    with db.get_session() as s:
        cols = {r[1] for r in s.execute(text("PRAGMA table_info(sources)"))}
        assert {"empty_runs", "error_runs", "canary_alerted_at"} <= cols
        row = s.query(Source).filter_by(name="a").one()
        assert (row.empty_runs or 0, row.error_runs or 0, row.canary_alerted_at) == (0, 0, None)


# --- the admin badge --------------------------------------------------------------------------------


@pytest.fixture
def admin(env, monkeypatch):
    env.auth_mode = "oidc"
    env.oidc_discovery_url = "https://idp.example/.well-known/openid-configuration"
    env.oidc_client_id = "happenhere"
    env.oidc_client_secret = "s3cret"
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: env)
    token = auth.create_session("someone", env.secret_key, "oidc:happenhere", expiry_secs=60)
    http = TestClient(app, base_url="https://testserver", follow_redirects=False)
    http.cookies.set(oidc.SESSION_COOKIE, token)
    return nullcontext(http)


def test_the_admin_list_shows_the_canary_state(env, admin):
    add_source("quiet")
    add_event("quiet", "a", SOON)
    add_source("empty")
    add_source("broken", error_count=1, last_error="HTTP 500")
    add_event("broken", "b", SOON)
    add_source("counting")
    run(2, at=NOW)
    # "counting" tripped too (no events); make it count only once by resetting it
    with db.get_session() as s:
        row = s.query(Source).filter_by(name="counting").one()
        row.empty_runs, row.canary_alerted_at = 1, None
        s.commit()
    with admin as http:
        r = http.get("/admin")
    assert r.status_code == 200
    html = r.text
    assert 'data-canary="empty"' in html
    assert html.count('data-canary="empty"') == 1  # only "empty" is tripped
    assert "canary: no events" in html
    assert 'data-canary="failing"' not in html  # "broken" is at 2 error runs, not 3


def test_the_admin_badge_shows_failing_and_alert_pending(env, admin):
    add_source("broken", error_count=1, last_error="HTTP 500")
    add_event("broken", "b", SOON)
    env.ntfy_url = ""  # nothing sent: the alert stays pending
    run(3)
    with admin as http:
        r = http.get("/admin")
    assert r.status_code == 200
    assert 'data-canary="failing"' in r.text
    assert "alert pending" in r.text


def test_a_disabled_source_shows_no_badge(env, admin):
    add_source("off", enabled=False)
    run(3)
    with admin as http:
        r = http.get("/admin")
    assert r.status_code == 200
    assert "data-canary" not in r.text
