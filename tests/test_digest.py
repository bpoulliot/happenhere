"""The notification digest: configurable, anchored, and never repeats an unchanged list.

Nothing here touches the network: the ntfy POST is faked (or goes through an
``httpx.MockTransport``), the clock is passed in, and the database is a temp file.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, digest, ntfy, oidc
from app import scheduler as sched
from app.config import Config
from app.main import app
from app.models import Event, NotificationLog, Source

UTC = ZoneInfo("UTC")
MON = datetime(2026, 11, 2, 9, 0)  # a Monday
THU = MON + timedelta(days=3)
SUN = MON + timedelta(days=6)


def test_the_fixture_dates_are_the_weekdays_the_tests_name():
    assert (MON.weekday(), THU.weekday(), SUN.weekday()) == (0, 3, 6)


# --- fixtures ------------------------------------------------------------------------------------


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
    """A configured deployment: temp DB, ntfy set, UTC, a fake ntfy POST."""
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yml"))
    db.init_db(tmp_path / "t.db")
    c = Config(
        ntfy_url="https://ntfy.example.com",
        ntfy_topic="events",
        tz="UTC",
        site_name="Exampleton",  # the digest title's name (item 30: no longer a city setting)
        public_url="https://events.example.com",
        secret_key="k" * 64,
    )
    monkeypatch.setattr(config, "_cfg", c)
    monkeypatch.setattr(config, "load_config", lambda: c)
    fake = FakeNtfy()
    monkeypatch.setattr(ntfy, "post_to_ntfy", fake)
    c.fake = fake  # type: ignore[attr-defined]
    return c


def add_event(key: str, start: datetime, title: str | None = None, venue="Hall", **kw) -> int:
    with db.get_session() as s:
        src = s.query(Source).filter_by(name="src").first()
        if src is None:
            src = Source(name="src", display_name="Src", type="ical", url="https://e.example.com")
            s.add(src)
            s.commit()
        ev = Event(
            source_id=src.id,
            dedup_hash=hashlib.sha256(key.encode()).hexdigest(),
            title=title or f"Event {key}",
            start_dt=start,
            venue_name=venue,
            **kw,
        )
        s.add(ev)
        s.commit()
        return ev.id


def edit_event(event_id: int, **fields) -> None:
    with db.get_session() as s:
        ev = s.get(Event, event_id)
        for k, v in fields.items():
            setattr(ev, k, v)
        s.commit()


def drop_event(event_id: int) -> None:
    with db.get_session() as s:
        s.delete(s.get(Event, event_id))
        s.commit()


def statuses() -> list[str]:
    with db.get_session() as s:
        return [r.status for r in s.query(NotificationLog).order_by(NotificationLog.id)]


def run(now: datetime) -> str:
    return digest.run_digest(now=now).status


# --- the owner's example -----------------------------------------------------------------------


def test_monday_thursday_sunday(env):
    """Sent Monday; Thursday's list is unchanged so nothing goes out; Sunday has a new
    event so it goes out as scheduled. A skipped run does not move the schedule."""
    add_event("a", datetime(2026, 11, 6, 19, 0))
    add_event("b", datetime(2026, 11, 10, 19, 0))
    tz = UTC

    first = digest.next_digest_slot(None, 3, "09:00", MON.replace(hour=8, tzinfo=tz), tz)
    assert first.replace(tzinfo=None) == MON

    assert digest.run_scheduled(first, now=MON) == "sent"
    assert len(env.fake.calls) == 1

    second = digest.next_scheduled_slot(MON.replace(hour=9, minute=1, tzinfo=tz))
    assert second.replace(tzinfo=None) == THU
    assert digest.run_scheduled(second, now=THU) == "skipped-unchanged"
    assert len(env.fake.calls) == 1  # nothing posted

    # the skipped run still counts as the last scheduled run: the next slot is Sunday
    third = digest.next_scheduled_slot(THU.replace(hour=9, minute=1, tzinfo=tz))
    assert third.replace(tzinfo=None) == SUN

    add_event("c", datetime(2026, 11, 12, 20, 0), title="Brand New Show")
    assert digest.run_scheduled(third, now=SUN) == "sent"
    assert len(env.fake.calls) == 2
    assert "Brand New Show" in env.fake.calls[1]["body"]
    assert statuses() == ["sent", "skipped-unchanged", "sent"]


def test_a_failed_new_event_on_thursday_is_retried_on_sunday(env):
    """The gate looks at the last SENT digest. A gate that looks at the last RUN has seen
    Thursday's new event already, and stays quiet on Sunday."""
    add_event("a", datetime(2026, 11, 12, 19, 0))
    assert run(MON) == "sent"
    add_event("c", datetime(2026, 11, 13, 19, 0))
    env.fake.outcomes.append((False, "boom"))
    assert run(THU) == "failed"
    assert run(SUN) == "sent"  # no further change, but C was never sent
    assert statuses() == ["sent", "failed", "sent"]
    assert "Event c" in env.fake.calls[-1]["body"]


def test_an_event_that_drops_out_and_comes_back_is_not_new(env):
    """Same cause: against the last run, the returning event looks new."""
    add_event("a", datetime(2026, 11, 12, 19, 0))
    b = add_event("b", datetime(2026, 11, 13, 19, 0))
    assert run(MON) == "sent"
    edit_event(b, start_dt=datetime(2026, 11, 1, 19, 0))  # off the list: it has passed
    assert run(THU) == "skipped-unchanged"
    edit_event(b, start_dt=datetime(2026, 11, 13, 19, 0))  # back, as sent on Monday
    assert run(SUN) == "skipped-unchanged"
    assert len(env.fake.calls) == 1


# --- the gate -------------------------------------------------------------------------------------


def test_removal_only_does_not_send(env):
    a = add_event("a", datetime(2026, 11, 6, 19, 0))
    add_event("b", datetime(2026, 11, 7, 19, 0))
    assert run(MON) == "sent"
    drop_event(a)  # cancelled
    assert run(THU) == "skipped-unchanged"
    assert len(env.fake.calls) == 1


def test_passed_events_alone_do_not_send(env):
    add_event("a", datetime(2026, 11, 3, 19, 0))  # passes before Thursday
    add_event("b", datetime(2026, 11, 7, 19, 0))
    assert run(MON) == "sent"
    assert run(THU) == "skipped-unchanged"


@pytest.mark.parametrize(
    "change",
    [
        {"start_dt": datetime(2026, 11, 7, 21, 0)},
        {"title": "A Different Title"},
        {"venue_name": "Another Hall"},
    ],
    ids=["start-time", "title", "venue"],
)
def test_a_changed_time_title_or_venue_sends(env, change):
    a = add_event("a", datetime(2026, 11, 7, 19, 0))
    assert run(MON) == "sent"
    edit_event(a, **change)
    assert run(THU) == "sent"
    assert len(env.fake.calls) == 2


def test_the_first_run_sends(env):
    add_event("a", datetime(2026, 11, 6, 19, 0))
    assert statuses() == []
    assert run(MON) == "sent"
    assert len(env.fake.calls) == 1


def test_a_failed_post_is_not_recorded_as_sent_and_the_next_run_retries(env):
    add_event("a", datetime(2026, 11, 6, 19, 0))
    env.fake.outcomes.append((False, "connection refused"))
    assert run(MON) == "failed"
    with db.get_session() as s:
        row = s.query(NotificationLog).one()
        assert row.status == "failed"
        assert "connection refused" in row.error_msg
        assert db.get_meta(s, digest.META_SENT) is None
    assert run(THU) == "sent"
    assert len(env.fake.calls) == 2


def test_a_run_with_nothing_upcoming_is_logged_as_skipped_empty(env):
    add_event("a", datetime(2026, 10, 1, 19, 0))  # long past
    add_event("b", datetime(2027, 3, 1, 19, 0))  # beyond the window
    assert run(MON) == "skipped-empty"
    assert env.fake.calls == []
    assert statuses() == ["skipped-empty"]


def test_no_ntfy_settings_means_no_send_and_no_error(env):
    env.ntfy_url = ""
    add_event("a", datetime(2026, 11, 6, 19, 0))
    result = digest.run_digest(now=MON)
    assert result.status == "not-configured"
    assert env.fake.calls == []
    assert statuses() == []  # nothing recorded, so enabling ntfy later still sends

    env.ntfy_url, env.ntfy_topic = "https://ntfy.example.com", ""
    assert digest.run_digest(now=MON).status == "not-configured"


def test_a_disabled_digest_sends_nothing(env):
    env.digest_enabled = False
    add_event("a", datetime(2026, 11, 6, 19, 0))
    assert digest.run_digest(now=MON).status == "disabled"
    assert env.fake.calls == []


# --- the content ----------------------------------------------------------------------------------


def test_the_window_and_the_order(env):
    env.digest_window_days = 5
    add_event("late", datetime(2026, 11, 6, 21, 0), title="Late")
    add_event("early", datetime(2026, 11, 3, 10, 0), title="Early")
    add_event("far", datetime(2026, 11, 20, 10, 0), title="Far")
    add_event("past", datetime(2026, 11, 1, 10, 0), title="Past")
    assert run(MON) == "sent"
    call = env.fake.calls[0]
    assert call["body"].index("Early") < call["body"].index("Late")
    assert "Far" not in call["body"] and "Past" not in call["body"]
    assert call["title"] == "Exampleton — 2 events in the next 5 days"


def test_twenty_lines_then_and_n_more_and_a_link(env):
    for i in range(23):
        add_event(f"e{i:02d}", datetime(2026, 11, 3, 10, 0) + timedelta(hours=i), title=f"T{i:02d}")
    assert run(MON) == "sent"
    call = env.fake.calls[0]
    lines = call["body"].splitlines()
    assert sum(1 for ln in lines if ln.startswith("•")) == 20
    assert "… and 3 more" in call["body"]
    assert "T19" in call["body"] and "T20" not in call["body"]
    assert call["click"] == "https://events.example.com/events"
    assert "https://events.example.com/events" in call["body"]
    assert call["title"] == "Exampleton — 23 events in the next 14 days"


def test_a_new_event_past_the_line_cap_still_counts_as_new(env):
    for i in range(20):
        add_event(f"e{i:02d}", datetime(2026, 11, 8, 10, 0) + timedelta(hours=i))
    assert run(MON) == "sent"
    add_event("tail", datetime(2026, 11, 12, 10, 0), title="Hidden Beyond The Cap")
    assert run(THU) == "sent"
    assert "… and 1 more" in env.fake.calls[1]["body"]


def test_the_title_is_singular_for_one_event_and_has_a_fallback_name(env):
    env.site_name = ""
    add_event("a", datetime(2026, 11, 6, 19, 0))
    assert run(MON) == "sent"
    assert env.fake.calls[0]["title"] == "HappenHere — 1 event in the next 14 days"


def test_the_digest_modules_name_no_place():
    banned = "".join(["colo", "rado"])  # the old hard-coded city, kept out of the repo's text
    for mod in ("ntfy.py", "digest.py"):
        text = (Path(__file__).parent.parent / "app" / mod).read_text().lower()
        assert banned not in text, mod


def test_the_fingerprint_is_built_on_the_one_identity_function(env, monkeypatch):
    add_event("a", datetime(2026, 11, 6, 19, 0))
    with db.get_session() as s:
        ev = s.query(Event).one()
        before = digest.fingerprint(ev)
        monkeypatch.setattr(digest, "event_identity", lambda e: "uid-from-item-03")
        assert digest.fingerprint(ev) != before


# --- the schedule ---------------------------------------------------------------------------------


def test_slots_keep_the_phase_and_wall_clock_time():
    chi = ZoneInfo("America/Chicago")
    now = datetime(2026, 10, 31, 12, 0, tzinfo=chi)
    anchor = date(2026, 10, 31)
    nxt = digest.next_digest_slot(anchor, 3, "09:00", now, chi)
    assert (nxt.date(), nxt.hour) == (date(2026, 11, 3), 9)  # 09:00 across the DST change
    # a slot already past is skipped, not run late, and the phase is kept
    later = datetime(2026, 11, 20, 12, 0, tzinfo=chi)
    nxt = digest.next_digest_slot(anchor, 3, "09:00", later, chi)
    assert nxt.date() == date(2026, 11, 21)
    assert (nxt.date() - anchor).days % 3 == 0


def test_first_slot_is_today_if_still_ahead_else_tomorrow():
    now = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert digest.next_digest_slot(None, 3, "09:00", now, UTC).date() == date(2026, 11, 2)
    now = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    assert digest.next_digest_slot(None, 3, "09:00", now, UTC).date() == date(2026, 11, 3)


def test_a_bad_time_falls_back_to_nine():
    now = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
    assert digest.next_digest_slot(None, 3, "not a time", now, UTC).hour == 9


# --- rescheduling without a restart --------------------------------------------------------------


@pytest.fixture
def paused_scheduler(env):
    from apscheduler.schedulers.background import BackgroundScheduler

    s = BackgroundScheduler(timezone="UTC")
    s.start(paused=True)
    saved = sched._scheduler
    sched._scheduler = s
    yield s
    s.shutdown(wait=False)
    sched._scheduler = saved


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


SETTINGS = {
    "scrape_interval_hours": "6",
    "tz": "UTC",
    "ntfy_url": "https://ntfy.example.com",
    "ntfy_topic": "events",
    "digest_enabled": "on",
    "digest_interval_days": "3",
    "digest_time": "09:00",
    "digest_window_days": "14",
}


def test_saving_a_new_interval_reschedules_the_job_without_a_restart(
    env, admin, paused_scheduler, monkeypatch
):
    monkeypatch.setattr(digest, "local_now", lambda cfg: datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    with db.get_session() as s:
        db.set_meta(s, digest.META_ANCHOR, "2026-11-02")  # last scheduled run: Monday
    sched.schedule_digest()
    job = sched.digest_job()
    assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 11, 5, 9, 0)

    with admin as http:
        r = http.post("/admin/settings", data={**SETTINGS, "digest_interval_days": "7"})
    assert r.status_code == 302
    job = sched.digest_job()
    assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 11, 9, 9, 0)
    assert env.digest_interval_days == 7

    with admin as http:
        http.post("/admin/settings", data={**SETTINGS, "digest_time": "18:30"})
    assert sched.digest_job().next_run_time.hour == 18

    env_off = {k: v for k, v in SETTINGS.items() if k != "digest_enabled"}
    with admin as http:
        http.post("/admin/settings", data=env_off)
    assert env.digest_enabled is False
    assert sched.digest_job() is None


def test_settings_are_validated(env, admin, paused_scheduler):
    with admin as http:
        http.post(
            "/admin/settings",
            data={
                **SETTINGS,
                "digest_interval_days": "0",
                "digest_window_days": "9999",
                "digest_time": "25:99",
            },
        )
    assert env.digest_interval_days == 1
    assert env.digest_window_days == 365
    assert env.digest_time == "09:00"  # the bad value is refused, the old one kept


def test_a_scheduled_run_records_the_anchor_and_rearms(env, paused_scheduler, monkeypatch):
    add_event("a", datetime(2026, 11, 6, 19, 0))
    slot = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(digest, "local_now", lambda cfg: slot + timedelta(seconds=5))
    sched.run_digest_slot(slot)
    with db.get_session() as s:
        assert db.get_meta(s, digest.META_ANCHOR) == "2026-11-02"
    assert sched.digest_job().next_run_time.date() == date(2026, 11, 5)
    assert statuses() == ["sent"]


# --- the transport --------------------------------------------------------------------------------


def _mock_httpx(monkeypatch, handler):
    real = httpx.Client

    def factory(*a, **kw):
        return real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items()})

    monkeypatch.setattr(ntfy.httpx, "Client", factory)


def test_post_to_ntfy_publishes_json_so_a_dash_in_the_title_works(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200)

    _mock_httpx(monkeypatch, handler)
    cfg = Config(ntfy_url="https://ntfy.example.com/", ntfy_topic="events", ntfy_token="tok")
    ok, err = ntfy.post_to_ntfy(
        cfg, "Site — 2 events", "body", click="https://e.example.com/events"
    )
    assert (ok, err) == (True, None)
    assert seen["url"] == "https://ntfy.example.com/"
    assert seen["auth"] == "Bearer tok"
    assert seen["json"]["topic"] == "events"
    assert seen["json"]["title"] == "Site — 2 events"
    assert seen["json"]["click"] == "https://e.example.com/events"
    assert seen["json"]["message"] == "body"


def test_post_to_ntfy_reports_a_server_error(monkeypatch):
    _mock_httpx(monkeypatch, lambda request: httpx.Response(500))
    cfg = Config(ntfy_url="https://ntfy.example.com", ntfy_topic="events")
    ok, err = ntfy.post_to_ntfy(cfg, "t", "b")
    assert ok is False and "500" in err


# --- the admin pages --------------------------------------------------------------------------------


def test_settings_page_renders_the_digest_card(env, admin):
    with admin as http:
        r = http.get("/admin/settings")
    assert r.status_code == 200
    for name in ("digest_enabled", "digest_interval_days", "digest_time", "digest_window_days"):
        assert f'name="{name}"' in r.text
    assert "/admin/digest" in r.text  # the Preview link


def test_preview_says_what_would_happen_and_why(env, admin, monkeypatch):
    monkeypatch.setattr(digest, "local_now", lambda cfg: MON.replace(tzinfo=UTC))
    add_event("a", datetime(2026, 11, 6, 19, 0), title="Preview Me")
    with admin as http:
        r = http.get("/admin/digest")
        assert r.status_code == 200
        assert "Preview Me" in r.text
        assert "would send" in r.text.lower()
        assert "first digest" in r.text.lower()

        assert run(MON) == "sent"
        r = http.get("/admin/digest")
    assert r.status_code == 200
    assert "would not send" in r.text.lower()
    assert "unchanged" in r.text.lower()


def test_preview_renders_with_ntfy_unset_and_with_nothing_upcoming(env, admin, monkeypatch):
    monkeypatch.setattr(digest, "local_now", lambda cfg: MON.replace(tzinfo=UTC))
    env.ntfy_url = ""
    with admin as http:
        r = http.get("/admin/digest")
    assert r.status_code == 200
    assert "not configured" in r.text.lower()


def test_send_test_posts_marked_test_and_records_nothing(env, admin, monkeypatch):
    monkeypatch.setattr(digest, "local_now", lambda cfg: MON.replace(tzinfo=UTC))
    add_event("a", datetime(2026, 11, 6, 19, 0))
    with admin as http:
        r = http.post("/admin/digest/test")
    assert r.status_code == 200
    assert len(env.fake.calls) == 1
    assert env.fake.calls[0]["title"].startswith("TEST")
    assert statuses() == []
    with db.get_session() as s:
        assert db.get_meta(s, digest.META_SENT) is None
    assert run(MON) == "sent"  # the test did not use up the first real digest


def test_send_test_reports_a_failure_on_the_page(env, admin, monkeypatch):
    monkeypatch.setattr(digest, "local_now", lambda cfg: MON.replace(tzinfo=UTC))
    add_event("a", datetime(2026, 11, 6, 19, 0))
    env.fake.outcomes.append((False, "no route to host"))
    with admin as http:
        r = http.post("/admin/digest/test")
    assert r.status_code == 200
    assert "no route to host" in r.text
    assert statuses() == []


def test_the_digest_pages_need_an_admin(env):
    http = TestClient(app, base_url="https://testserver", follow_redirects=False)
    with nullcontext(http) as h:
        assert h.get("/admin/digest").status_code == 302
        assert h.post("/admin/digest/test").status_code == 302
