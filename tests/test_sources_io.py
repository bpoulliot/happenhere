"""Source import/export: idempotent, dry-run by default, all-or-nothing, never deletes."""

from __future__ import annotations

import json
import logging
import socket

import pytest
import yaml

from app import db as dbmod
from app import netguard
from app import sources as sio
from app.models import Source

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Every host is public (the seeded regional sources have real hostnames), except
    `lan.example.com` (a LAN address) and `gone.example.com` (unresolvable)."""

    def fake(host, port, *a, **kw):
        if host.startswith("gone."):
            raise socket.gaierror("unknown host")
        ip = "192.168.1.3" if host.startswith("lan.") else PUBLIC
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)


@pytest.fixture
def session(tmp_path):
    dbmod.init_db(tmp_path / "t.db")
    with dbmod.get_session() as s:
        yield s


def _entry(name="alpha", **kw):
    e = {
        "name": name,
        "display_name": name.title(),
        "type": "ical",
        "url": f"https://{name}.example.com/events.ics",
        "categories": ["Music"],
        "enabled": True,
    }
    e.update(kw)
    return e


def _doc(*entries):
    return yaml.safe_dump({"sources": list(entries)})


def _snapshot(session):
    session.expire_all()
    return sorted(
        (s.name, s.display_name, s.type, s.url, s.categories, s.enabled)
        for s in session.query(Source).all()
    )


def _apply(session, text, **kw):
    return sio.run_import(session, text, apply=True, **kw)


def test_export_import_roundtrip_is_noop(session):
    _apply(session, _doc(_entry("alpha"), _entry("beta", enabled=False, categories=[])))
    before = _snapshot(session)
    plan = sio.run_import(session, sio.export_yaml(session), apply=True)
    assert plan.summary().startswith("0 added, 0 updated, ")
    assert not plan.added and not plan.updated and not plan.missing
    assert _snapshot(session) == before


def test_dry_run_changes_nothing_and_reports_plan(session):
    _apply(session, _doc(_entry("alpha"), _entry("beta")))
    before = _snapshot(session)
    text = _doc(
        _entry("alpha", display_name="Renamed"),  # updated
        _entry("beta"),  # unchanged
        _entry("gamma"),  # added
    )
    plan = sio.run_import(session, text)  # dry run is the default
    assert [e["name"] for e in plan.added] == ["gamma"]
    assert [e["name"] for e in plan.updated] == ["alpha"]
    assert [e["name"] for e in plan.unchanged] == ["beta"]
    assert "ticketmaster" in plan.missing  # seeded source the file does not mention
    assert not plan.applied
    assert _snapshot(session) == before
    assert "gamma" in plan.render() and "alpha" in plan.render()


def test_apply_adds_and_updates_by_name(session):
    _apply(session, _doc(_entry("alpha")))
    _apply(
        session,
        _doc(
            _entry("alpha", url="https://alpha2.example.com/x.ics", enabled=False), _entry("gamma")
        ),
    )
    rows = {s.name: s for s in session.query(Source).all()}
    assert rows["alpha"].url == "https://alpha2.example.com/x.ics"
    assert rows["alpha"].enabled is False
    assert rows["gamma"].display_name == "Gamma"
    assert json.loads(rows["gamma"].categories) == ["Music"]


def test_missing_is_reported_and_left_alone(session):
    _apply(session, _doc(_entry("alpha"), _entry("beta")))
    plan = _apply(session, _doc(_entry("alpha")))
    assert "beta" in plan.missing and not plan.disabled
    assert session.query(Source).filter_by(name="beta").one().enabled is True


def test_disable_missing_disables_but_never_deletes(session):
    _apply(session, _doc(_entry("alpha"), _entry("beta")))
    n_before = session.query(Source).count()
    plan = _apply(session, _doc(_entry("alpha")), disable_missing=True)
    assert plan.disabled == ["beta"]  # ticketmaster etc. were already disabled
    assert session.query(Source).count() == n_before
    assert session.query(Source).filter_by(name="beta").one().enabled is False
    assert session.query(Source).filter_by(name="alpha").one().enabled is True


def test_dry_run_with_disable_missing_changes_nothing(session):
    _apply(session, _doc(_entry("alpha"), _entry("beta")))
    before = _snapshot(session)
    plan = sio.run_import(session, _doc(_entry("alpha")), disable_missing=True)
    assert plan.would_disable == ["beta"]
    assert _snapshot(session) == before


@pytest.mark.parametrize(
    "bad, fragment",
    [
        (_entry("x", type="nonesuch"), "unknown type"),
        ({k: v for k, v in _entry("x").items() if k != "name"}, "name"),
        (_entry("x", url="https://lan.example.com/e.ics"), "non-public"),
        (_entry("x", url="ftp://x.example.com/"), "http"),
        (_entry("x", url="https://gone.example.com/e.ics"), "cannot resolve"),
        (_entry("x", enabled="yes"), "enabled"),
        (_entry("x", categories="Music"), "categories"),
        (_entry("x", colour="red"), "unknown field"),
        (_entry("bad name/with slash"), "name"),
    ],
)
def test_bad_entry_fails_whole_import_before_any_write(session, bad, fragment):
    before = _snapshot(session)
    text = _doc(_entry("good"), bad, _entry("good2", type="nonesuch"))
    with pytest.raises(sio.ImportRefused) as exc:
        _apply(session, text)
    msg = str(exc.value)
    assert fragment in msg
    assert "good2" in msg  # every bad entry is named, not just the first
    assert _snapshot(session) == before


def test_duplicate_names_in_file_refused(session):
    with pytest.raises(sio.ImportRefused, match="duplicate"):
        _apply(session, _doc(_entry("alpha"), _entry("alpha")))


def test_bad_document_shapes_refused(session):
    for text in ("", "- just\n- a list\n", "sources: nope\n", "sources: [1]\n", "a: [unclosed"):
        with pytest.raises(sio.ImportRefused):
            sio.run_import(session, text)


def test_write_failure_rolls_back_everything(session, monkeypatch):
    """One transaction: a failure part-way through leaves no partial import."""
    before = _snapshot(session)
    real_commit = session.commit

    def boom():
        session.flush()
        raise RuntimeError("disk gone")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        _apply(session, _doc(_entry("alpha"), _entry("beta")))
    monkeypatch.setattr(session, "commit", real_commit)
    assert _snapshot(session) == before


def test_tags_ignored_with_one_warning_without_model_field(session, caplog):
    assert not hasattr(Source, "tags")  # item 03 adds it; then this test is replaced
    text = _doc(_entry("alpha", tags=["north"]), _entry("beta", tags=["south"]))
    with caplog.at_level(logging.WARNING):
        _apply(session, text)
    warnings = [r for r in caplog.records if "tags" in r.getMessage()]
    assert len(warnings) == 1
    assert session.query(Source).filter_by(name="alpha").one() is not None


def test_tags_must_be_slug_list(session):
    with pytest.raises(sio.ImportRefused, match="tags"):
        _apply(session, _doc(_entry("alpha", tags=["Not A Slug"])))


def test_optional_fields_default(session):
    plan = _apply(session, "sources:\n  - {name: bare, type: ical}\n")
    row = session.query(Source).filter_by(name="bare").one()
    assert (row.display_name, row.url, row.enabled) == ("bare", "", True)
    assert plan.added


def test_cli_dry_run_then_apply_and_exit_codes(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    f = tmp_path / "s.yml"
    f.write_text(_doc(_entry("alpha")))
    assert sio.main(["--db", db, "import", str(f)]) == 0
    out = capsys.readouterr().out
    assert "1 added" in out and "dry run" in out.lower()
    assert sio.main(["--db", db, "import", str(f), "--apply"]) == 0
    capsys.readouterr()
    assert sio.main(["--db", db, "export"]) == 0
    exported = yaml.safe_load(capsys.readouterr().out)
    assert "alpha" in {s["name"] for s in exported["sources"]}
    f.write_text(_doc(_entry("alpha", type="nonesuch")))
    assert sio.main(["--db", db, "import", str(f), "--apply"]) != 0
    assert "unknown type" in capsys.readouterr().err
    assert sio.main(["--db", db, "import", str(tmp_path / "missing.yml")]) != 0


def test_routes_use_shared_url_and_category_rules():
    """The admin form and the importer share one implementation, not two copies."""
    from app.web import routes

    assert routes.check_source_url is sio.check_source_url
    assert routes.split_categories is sio.split_categories
