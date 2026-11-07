"""Item 24: enabling a source re-runs the URL guard (admin toggle, create and edit forms)."""

from __future__ import annotations

import socket
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, netguard, oidc
from app.config import Config
from app.main import app
from app.models import Source

BLOCKED = "https://sinkholed.example.com/events.ics"
OK = "https://venue.example.com/events.ics"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """`sinkholed.` answers an unspecified address (a DNS filter's usual sinkhole); the rest public."""

    def fake(host, port, *a, **kw):
        ip = "0.0.0.0" if host.startswith("sinkholed.") else "93.184.216.34"  # noqa: S104
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)


@pytest.fixture
def client(monkeypatch, tmp_path):
    db.init_db(tmp_path / "app.db")
    cfg = Config()
    cfg.secret_key = "k" * 64
    cfg.password_hash = auth.hash_password("pw")
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: cfg)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    c.cookies.set(
        oidc.SESSION_COOKIE, auth.create_session("admin", cfg.secret_key, cfg.password_hash)
    )
    return nullcontext(c)


def _seed(name="held", url=BLOCKED, enabled=False, last_error=None) -> None:
    with db.get_session() as s:
        s.add(
            Source(
                name=name,
                display_name=name.title(),
                type="ical",
                url=url,
                enabled=enabled,
                last_error=last_error,
            )
        )
        s.commit()


def _row(name="held") -> Source:
    with db.get_session() as s:
        row = s.query(Source).filter_by(name=name).one()
        s.expunge(row)
        return row


def _form(url=BLOCKED, enabled="off"):
    return {
        "display_name": "Held",
        "scraper_type": "ical",
        "url": url,
        "enabled": enabled,
    }


def test_the_toggle_refuses_to_enable_a_source_whose_url_is_refused(client):
    _seed()
    with client as c:
        r = c.post("/admin/sources/held/toggle")
    assert r.status_code == 400
    assert "Source URL refused" in r.text and "non-public address" in r.text
    assert _row().enabled is False


def test_the_toggle_enables_when_the_url_now_passes_and_clears_the_guard_error(client):
    _seed(url=OK, last_error=f"{'url refused: '}old reason")
    with client as c:
        r = c.post("/admin/sources/held/toggle")
    assert r.status_code == 200 and r.json() == {"enabled": True}
    assert _row().enabled is True and _row().last_error is None


def test_the_toggle_always_disables(client):
    _seed(url=BLOCKED, enabled=True)  # enabled before the guard saw it: turning it off must work
    with client as c:
        r = c.post("/admin/sources/held/toggle")
    assert r.status_code == 200 and r.json() == {"enabled": False}


def test_the_edit_form_refuses_enabling_and_shows_the_reason(client):
    _seed()
    with client as c:
        r = c.post("/admin/sources/held/update", data=_form(enabled="on"))
    assert r.status_code == 400 and "Source URL refused" in r.text
    assert "non-public address" in r.text
    assert _row().enabled is False


def test_the_edit_form_keeps_a_disabled_source_with_a_refused_url_editable(client):
    _seed()
    with client as c:
        r = c.post("/admin/sources/held/update", data={**_form(), "categories": "Music"})
    assert r.status_code == 302
    row = _row()
    assert row.enabled is False and row.categories == '["Music"]'
    assert row.last_error.startswith("url refused: ")


def test_the_edit_form_clears_the_guard_error_when_the_url_passes(client):
    _seed(last_error="url refused: old reason")
    with client as c:
        r = c.post("/admin/sources/held/update", data=_form(url=OK, enabled="on"))
    assert r.status_code == 302
    assert _row().enabled is True and _row().last_error is None


def test_a_scraper_error_survives_an_edit(client):
    _seed(url=OK, last_error="HTTP 500 from the venue")
    with client as c:
        c.post("/admin/sources/held/update", data=_form(url=OK, enabled="on"))
    assert _row().last_error == "HTTP 500 from the venue"


def test_the_create_form_refuses_an_enabled_source_but_stores_a_disabled_one(client):
    with client as c:
        bad = c.post("/admin/sources", data=_form(enabled="on"))
        ok = c.post("/admin/sources", data=_form(enabled="off"))
    assert bad.status_code == 400 and "Source URL refused" in bad.text
    assert ok.status_code == 302
    row = _row()
    assert row.enabled is False and row.last_error.startswith("url refused: ")


def test_the_admin_page_renders_the_guard_reason_of_a_stored_source(client):
    _seed(last_error="url refused: held.example.com resolves to non-public address 0.0.0.0")
    with client as c:
        r = c.get("/admin")
    assert r.status_code == 200
    assert "URL refused" in r.text and "non-public address" in r.text
    assert "response-error" in r.text  # a refused toggle puts the button back


def test_a_refused_url_message_is_fixed_text_not_the_exception(client):
    """CodeQL py/stack-trace-exposure: the 400 body is a fixed sentence per reason code."""
    _seed()
    with client as c:
        r = c.post("/admin/sources/held/toggle")
    assert "sinkholed.example.com" not in r.text and "0.0.0.0" not in r.text
