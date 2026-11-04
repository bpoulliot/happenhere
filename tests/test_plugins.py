"""Local scraper plugins: loaded from a directory outside the repo, never fatal.

Every plugin here is synthetic code written into a ``tmp_path`` directory by the test, and
every fixture is on an ``example.com`` host. Nothing touches the network.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import nullcontext

import pytest
import yaml
from fastapi.testclient import TestClient

from app import auth, config, netguard, oidc, plugins
from app import sources as sio
from app.config import Config
from app.main import app
from app.scrapers import registry
from app.scrapers.generic import GenericICalScraper

GOOD = '''
from datetime import datetime

from bs4 import BeautifulSoup

from app.scrapers._html_base import extract_jsonld_events, parse_jsonld_event
from app.scrapers.base import BaseScraper

HTML = """<html><head><script type="application/ld+json">
{"@type": "Event", "name": "Synthetic Night", "startDate": "2099-01-02T19:00:00",
 "url": "https://example.com/e/1"}
</script></head></html>"""


class ExampleHallScraper(BaseScraper):
    SOURCE_NAME = "Example Hall"
    SOURCE_TYPE = "example_hall"
    TYPE_LABEL = "Example Hall (plugin)"

    def __init__(self, config, source_url="", venue_name=""):
        super().__init__(config)
        self.source_url = source_url
        self.venue_name = venue_name

    def scrape(self):
        soup = BeautifulSoup(HTML, "lxml")
        evs = [parse_jsonld_event(i, self.venue_name) for i in extract_jsonld_events(soup)]
        return [e for e in evs if e and e.start_dt >= datetime.now()]
'''


def _scraper(type_key: str, *, label: str | None = None, extra: str = "") -> str:
    label_line = f'    TYPE_LABEL = "{label}"\n' if label else ""
    return (
        "from app.scrapers.base import BaseScraper\n\n\n"
        f"class {type_key.title().replace('_', '')}Scraper(BaseScraper):\n"
        f'    SOURCE_TYPE = "{type_key}"\n{label_line}{extra}\n'
        "    def __init__(self, config, source_url='', venue_name=''):\n"
        "        super().__init__(config)\n\n"
        "    def scrape(self):\n"
        "        return []\n"
    )


def _put(d, name: str, code: str) -> None:
    (d / name).write_text(code)


@pytest.fixture(autouse=True)
def _clean():
    saved = config._cfg
    plugins.unload()
    yield
    plugins.unload()
    config._cfg = saved


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    return d


def _src(type_key="example_hall", **kw):
    from app.models import Source

    return Source(
        name="x",
        display_name=kw.get("display_name", "Example Hall"),
        type=type_key,
        url="https://example.com/events",
    )


# --- the loader ------------------------------------------------------------------------------


def test_good_plugin_registers_and_scrapes_a_saved_fixture(pdir):
    _put(pdir, "example_hall.py", GOOD)
    report = plugins.load_plugins(str(pdir))

    assert [(p.module, p.source_type, p.label) for p in report.loaded] == [
        ("happenhere_plugins.example_hall", "example_hall", "Example Hall (plugin)")
    ]
    assert report.errors == []
    assert ("example_hall", "Example Hall (plugin)") in registry.scraper_types()
    assert registry.TYPE_LABELS["example_hall"] == "Example Hall (plugin)"

    scraper = registry.get_scraper(_src(), Config())
    events = scraper.scrape()
    assert [e.title for e in events] == ["Synthetic Night"]
    assert events[0].venue_name == "Example Hall"  # the (config, source_url, venue_name) contract


def test_label_defaults_to_the_class_name(pdir):
    _put(pdir, "nolabel.py", _scraper("nolabel"))
    plugins.load_plugins(str(pdir))
    assert registry.TYPE_LABELS["nolabel"] == "NolabelScraper"


def test_syntax_error_module_is_skipped_logged_and_siblings_still_load(pdir, caplog):
    _put(pdir, "a_broken.py", "def broken(:\n    pass\n")
    _put(pdir, "b_good.py", _scraper("good_one"))
    with caplog.at_level(logging.ERROR, logger="app.plugins"):
        report = plugins.load_plugins(str(pdir))

    assert [p.source_type for p in report.loaded] == ["good_one"]
    assert [e.module for e in report.errors] == ["happenhere_plugins.a_broken"]
    assert "SyntaxError" in report.errors[0].message
    rec = [r for r in caplog.records if r.levelno == logging.ERROR and "a_broken" in r.getMessage()]
    assert rec and rec[0].exc_info, "the traceback is logged"
    assert "happenhere_plugins.a_broken" not in __import__("sys").modules


def test_a_plugin_that_raises_or_exits_at_import_is_skipped(pdir):
    _put(pdir, "boom.py", "raise RuntimeError('boom')\n")
    _put(pdir, "quit.py", "import sys\nsys.exit(3)\n")
    _put(pdir, "ok.py", _scraper("ok_type"))
    report = plugins.load_plugins(str(pdir))
    assert [p.source_type for p in report.loaded] == ["ok_type"]
    assert sorted(e.module for e in report.errors) == [
        "happenhere_plugins.boom",
        "happenhere_plugins.quit",
    ]


def test_type_colliding_with_a_builtin_is_refused_and_logged(pdir, caplog):
    _put(pdir, "evil.py", _scraper("ical", label="Hijacked"))
    with caplog.at_level(logging.ERROR, logger="app.plugins"):
        report = plugins.load_plugins(str(pdir))

    assert report.loaded == []
    assert len(report.errors) == 1 and "ical" in report.errors[0].message
    assert registry._TYPE_MAP["ical"] is GenericICalScraper  # the built-in is untouched
    assert registry.TYPE_LABELS["ical"] != "Hijacked"
    assert any(r.levelno == logging.ERROR and "collides" in r.getMessage() for r in caplog.records)


def test_type_colliding_with_another_plugin_is_refused_first_wins(pdir):
    _put(pdir, "a_first.py", _scraper("dup_type", label="First"))
    _put(pdir, "b_second.py", _scraper("dup_type", label="Second"))
    report = plugins.load_plugins(str(pdir))
    assert [p.label for p in report.loaded] == ["First"]
    assert [e.module for e in report.errors] == ["happenhere_plugins.b_second"]
    assert registry.TYPE_LABELS["dup_type"] == "First"


def test_only_classes_defined_in_the_module_with_their_own_type_register(pdir):
    _put(
        pdir,
        "bases.py",
        "from app.scrapers._ical_base import ICalScraper\n"
        "from app.scrapers.base import BaseScraper\n\n\n"
        "class Inherits(ICalScraper):\n"  # inherits SOURCE_TYPE = 'ical': not its own
        "    pass\n\n\n"
        "class NoType(BaseScraper):\n"
        "    def scrape(self):\n"
        "        return []\n\n\n"
        "class StillAbstract(BaseScraper):\n"
        "    SOURCE_TYPE = 'abstract_one'\n",
    )
    report = plugins.load_plugins(str(pdir))
    assert report.loaded == [] and report.errors == []
    assert "abstract_one" not in registry._TYPE_MAP
    assert registry._TYPE_MAP["ical"] is GenericICalScraper


@pytest.mark.parametrize("bad", ["", "Has Space", "UPPER", "semi;colon", "-lead", 5])
def test_a_malformed_type_key_is_refused(pdir, bad):
    _put(pdir, "badkey.py", _scraper("placeholder").replace('"placeholder"', repr(bad)))
    report = plugins.load_plugins(str(pdir))
    assert report.loaded == [] and len(report.errors) == 1


def test_only_py_files_directly_in_the_directory_load(pdir):
    (pdir / "sub").mkdir()
    _put(pdir / "sub", "nested.py", _scraper("nested_type"))
    (pdir / "notes.txt").write_text("x")
    _put(pdir, "top.py", _scraper("top_type"))
    report = plugins.load_plugins(str(pdir))
    assert [p.source_type for p in report.loaded] == ["top_type"]


def test_unset_plugins_dir_loads_nothing(pdir):
    _put(pdir, "example_hall.py", GOOD)
    before = list(registry.scraper_types())
    for unset in ("", None):
        report = plugins.load_plugins(unset)
        assert report.directory == "" and report.loaded == [] and report.errors == []
        assert registry.scraper_types() == before


def test_a_missing_directory_is_reported_not_fatal(tmp_path):
    report = plugins.load_plugins(str(tmp_path / "nope"))
    assert report.loaded == [] and len(report.errors) == 1


def test_reload_replaces_the_previous_plugins(pdir, tmp_path):
    _put(pdir, "one.py", _scraper("one_type"))
    plugins.load_plugins(str(pdir))
    assert "one_type" in registry._TYPE_MAP
    other = tmp_path / "other"
    other.mkdir()
    _put(other, "two.py", _scraper("two_type"))
    plugins.load_plugins(str(other))
    assert "one_type" not in registry._TYPE_MAP and "two_type" in registry._TYPE_MAP
    plugins.load_plugins(None)
    assert "two_type" not in registry._TYPE_MAP and "two_type" not in registry.TYPE_LABELS


def test_plugins_dir_comes_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "none.yml"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "h.db"))
    monkeypatch.delenv("PLUGINS_DIR", raising=False)
    assert config.load_config().plugins_dir == ""
    monkeypatch.setenv("PLUGINS_DIR", str(tmp_path / "plugins"))
    assert config.load_config().plugins_dir == str(tmp_path / "plugins")


# --- start-up ----------------------------------------------------------------------------------


def test_app_starts_with_a_broken_plugin_directory(pdir, monkeypatch, tmp_path):
    import app.main as main

    _put(pdir, "broken.py", "def broken(:\n")
    _put(pdir, "good.py", _scraper("good_type"))
    cfg = Config(plugins_dir=str(pdir), db_path=str(tmp_path / "d" / "h.db"))
    cfg.secret_key = "k" * 64
    called = []
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main._auth, "bootstrap", lambda *a, **k: None)
    monkeypatch.setattr(main, "init_db", lambda *a, **k: called.append("init_db"))
    monkeypatch.setattr(main, "start", lambda **k: called.append("scheduler"))
    monkeypatch.setattr(main, "stop", lambda: called.append("stop"))

    async def run():
        async with main.lifespan(main.app):
            called.append("serving")

    asyncio.run(run())
    assert called == ["init_db", "scheduler", "serving", "stop"]
    assert "good_type" in registry._TYPE_MAP
    assert len(plugins.get_report().errors) == 1


# --- sources import knows plugin types -------------------------------------------------------


def test_source_import_cli_loads_plugins(pdir, tmp_path, monkeypatch):
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)
    _put(pdir, "example_hall.py", GOOD)
    monkeypatch.setattr(config, "_cfg", Config(plugins_dir=str(pdir)))
    f = tmp_path / "sources.yml"
    f.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "name": "hall",
                        "display_name": "Hall",
                        "type": "example_hall",
                        "url": "https://example.com/events",
                    }
                ]
            }
        )
    )
    rc = sio.main(["--db", str(tmp_path / "t.db"), "import", str(f), "--apply"])
    assert rc == 0


# --- the admin page ------------------------------------------------------------------------------


def _cfg(**kw) -> Config:
    c = Config()
    c.secret_key = "k" * 64
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def admin(monkeypatch, tmp_path):
    from app.db import init_db

    init_db(tmp_path / "test.db")
    c = _cfg(
        auth_mode="oidc",
        oidc_discovery_url="https://idp.example/.well-known/openid-configuration",
        oidc_client_id="happenhere",
        oidc_client_secret="s3cret",
        public_url="https://events.example",
    )
    monkeypatch.setattr(config, "_cfg", c)
    monkeypatch.setattr(config, "load_config", lambda: c)
    import app.main as main

    monkeypatch.setattr(main, "load_config", lambda: c)
    token = auth.create_session("bitmap", c.secret_key, "oidc:happenhere", expiry_secs=60)
    http = TestClient(app, base_url="https://testserver", follow_redirects=False)
    http.cookies.set(oidc.SESSION_COOKIE, token)
    return c, nullcontext(http)


def test_admin_page_lists_plugins_and_load_errors(admin, pdir):
    c, client = admin
    c.plugins_dir = str(pdir)
    _put(pdir, "example_hall.py", GOOD)
    _put(pdir, "broken.py", "def broken(:\n")
    plugins.load_plugins(str(pdir))
    with client as http:
        r = http.get("/admin")
    assert r.status_code == 200, r.text[:300]
    assert "Plugins" in r.text
    assert "happenhere_plugins.example_hall" in r.text
    assert "example_hall" in r.text and "Example Hall (plugin)" in r.text
    assert "happenhere_plugins.broken" in r.text and "SyntaxError" in r.text


def test_admin_page_without_a_plugins_dir_has_no_plugin_section(admin):
    _, client = admin
    with client as http:
        r = http.get("/admin")
    assert r.status_code == 200 and "Plugins" not in r.text


def test_new_source_form_offers_the_plugin_type(admin, pdir):
    _, client = admin
    _put(pdir, "example_hall.py", GOOD)
    plugins.load_plugins(str(pdir))
    with client as http:
        r = http.get("/admin/sources/new")
    assert r.status_code == 200 and "Example Hall (plugin)" in r.text


def test_admin_page_after_a_scrape_trigger_renders_the_plugin_list(admin, pdir, monkeypatch):
    from app.web import routes

    c, client = admin
    c.plugins_dir = str(pdir)
    _put(pdir, "example_hall.py", GOOD)
    plugins.load_plugins(str(pdir))
    monkeypatch.setattr(
        routes, "run_single_scraper", lambda name: {"new": 0, "updated": 0, "total": 0}
    )
    with client as http:
        r = http.post("/admin/scrape/anything")
    assert r.status_code == 200, r.text[:300]
    assert "happenhere_plugins.example_hall" in r.text
