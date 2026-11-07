"""Item 07: behaviour pinned before the deprecated calls were replaced, and the gate itself.

The warning gate lives in ``pyproject.toml`` (``filterwarnings``); ``test_the_gate_*`` prove it bites.
Everything here is synthetic: example.com hosts, invented titles.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, ical_export
from app.config import Config
from app.dedup import upsert_events
from app.main import app
from app.models import NotificationLog, Source
from app.scrapers.base import EventData

ROOT = Path(__file__).resolve().parent.parent

EVENT_KEYS = [
    "id",
    "title",
    "start_dt",
    "start_display",
    "end_dt",
    "venue_name",
    "venue_addr",
    "url",
    "image_url",
    "description",
    "categories",
    "source",
    "source_name",
    "source_type",
    "source_favicon",
    "source_logo",
    "stock_photo",
    "first_seen",
]


def _naive_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yml"))
    db.init_db(tmp_path / "app.db")
    cfg = Config(secret_key="k" * 64, password_hash=auth.hash_password("pw"))
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    when = (datetime.now() + timedelta(days=3)).replace(hour=20, minute=0, second=0, microsecond=0)
    with db.get_session() as s:
        src = Source(name="hall", display_name="Hall", type="ical", url="https://hall.example.com")
        s.add(src)
        s.commit()
        upsert_events(
            s,
            [
                EventData("Café Nöel – “quoted”", when, "Hall", url="https://hall.example.com/e/1"),
                EventData("Second Show", when + timedelta(hours=2), "Hall"),
            ],
            src.id,
        )


def test_api_events_shape_and_encoding_are_pinned(api_env):
    with nullcontext(TestClient(app, base_url="https://testserver")) as c:
        r = c.get("/api/events")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert list(body) == ["events", "total"]
    assert body["total"] == 2 == len(body["events"])
    for ev in body["events"]:
        assert list(ev) == EVENT_KEYS
        assert isinstance(ev["id"], int)
        assert isinstance(ev["categories"], list)
        assert isinstance(ev["start_dt"], str) and "T" in ev["start_dt"]
    assert [e["title"] for e in body["events"]] == ["Café Nöel – “quoted”", "Second Show"]
    # Compact separators, UTF-8 (not \u escapes), key order kept: the bytes of the old encoder.
    assert r.content == json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def test_feed_default_now_is_naive_utc(monkeypatch):
    """``feed_rows`` keeps comparing a NAIVE UTC clock with the stored column (same value as before)."""
    seen: dict[str, datetime] = {}

    def fake_shown(session, start, end):
        seen["start"], seen["end"] = start, end

        class _Q:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def all(self):
                return []

        return _Q()

    monkeypatch.setattr(ical_export, "shown_events", fake_shown)
    before = _naive_utc()
    ical_export.feed_rows(None, lookahead_days=10)
    after = _naive_utc()
    assert seen["start"].tzinfo is None
    assert before <= seen["start"] <= after
    assert seen["end"] - seen["start"] == timedelta(days=10)
    assert abs(_naive_utc() - seen["start"]) < timedelta(seconds=5)


def test_stored_timestamps_round_trip_naive(tmp_path):
    db.init_db(tmp_path / "ts.db")
    stamp = datetime(2030, 1, 2, 3, 4, 5)
    with db.get_session() as s:
        src = Source(name="x", display_name="X", type="ical", url="https://x.example.com")
        src.last_scraped = stamp
        s.add(src)
        s.add(NotificationLog(event_count=0, status="skipped-empty"))
        s.commit()
    with db.get_session() as s:
        got = s.query(Source).filter_by(name="x").one().last_scraped
        assert got == stamp
        assert got.tzinfo is None
        sent = s.query(NotificationLog).one().sent_at
        assert sent.tzinfo is None
        assert abs(sent - datetime.now()) < timedelta(minutes=1)


# --- the gate ---------------------------------------------------------------------------------


def _run_pytest_on(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    """Run one throwaway test file with this repo's pytest configuration."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent(source))
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, test-only
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
         "--rootdir", str(ROOT), str(probe)],
        capture_output=True, text=True, cwd=ROOT, check=False,
    )  # fmt: skip


def test_the_gate_fails_a_deprecation_raised_from_app(tmp_path):
    """A mutant: ``datetime.utcnow()`` called from a module named ``app.*`` must fail the run."""
    res = _run_pytest_on(
        tmp_path,
        """
        import warnings

        def test_mutant():
            code = compile("from datetime import datetime\\nx = datetime.utcnow()", "m.py", "exec")
            g = {"__name__": "app.mutant"}
            exec(code, g)
        """,
    )
    assert res.returncode != 0, res.stdout
    assert "DeprecationWarning" in res.stdout + res.stderr


def test_the_gate_leaves_a_clean_app_module_alone(tmp_path):
    """The other direction: no warning, no failure (a gate that always fails proves nothing)."""
    res = _run_pytest_on(tmp_path, "def test_ok():\n    assert 1 + 1 == 2\n")
    assert res.returncode == 0, res.stdout + res.stderr
