"""The admin sources table: every row's cells stay table cells (operator report 2026-11-10).

A ``<td>`` given ``display:flex`` stops being a table cell: it no longer stretches to the row's
height, so its bottom border ends above the row's and its controls sit at the top instead of
being vertically centred. The Actions cell had that inline style; its controls now sit in a flex
wrapper INSIDE the cell.
"""

from __future__ import annotations

import re
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, oidc
from app.config import Config
from app.main import app
from app.models import Source


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


def _seed() -> None:
    with db.get_session() as s:
        s.add(
            Source(
                name="venue",
                display_name="Venue",
                type="ical",
                url="https://www.example.com/events.ics",
                enabled=True,
                categories='["theater", "arts"]',
            )
        )
        s.commit()


def _admin_html(client) -> str:
    _seed()
    with client as c:
        r = c.get("/admin")
    assert r.status_code == 200
    return r.text


def test_no_table_cell_is_a_flex_box(client):
    html = _admin_html(client)
    cells = re.findall(r"<td\b[^>]*>", html)
    assert cells, "the sources table rendered no cells"
    flex = [c for c in cells if re.search(r"display\s*:\s*(inline-)?flex", c)]
    assert flex == []


def test_the_actions_sit_in_a_flex_wrapper_inside_their_cell(client):
    html = _admin_html(client)
    cells = re.findall(
        r'<td class="actions-cell">\s*<div class="row-actions">(.*?)</div>\s*</td>', html, re.S
    )
    # every row's Actions cell (the app seeds regional sources too), the seeded one among them
    assert len(cells) == html.count('class="actions-cell"') > 0, "an Actions cell lost its wrapper"
    (body,) = [c for c in cells if "/admin/sources/venue/edit" in c]
    for control in ("toggle-btn", "scrape-btn", "/admin/sources/venue/edit", "/delete"):
        assert control in body
