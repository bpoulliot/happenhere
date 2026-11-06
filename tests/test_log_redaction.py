"""Item 21: no API key reaches a log line or a source's stored error."""

from __future__ import annotations

import io
import logging

import httpx
import pytest

from app import config, db, main
from app.config import Config
from app.jobs import run_all_scrapers
from app.logredact import redact
from app.models import Source
from app.scrapers import ticketmaster

KEY = "SyntheticKey0123456789abcdef"  # gitleaks:allow (synthetic test value)


@pytest.fixture
def logs():
    """Logging as the app configures it at start-up, writing to a buffer."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_factory = logging.getLogRecordFactory()
    root.handlers = []
    buf = io.StringIO()
    main.configure_logging(Config(log_level="DEBUG"))
    # The app's own handler (stderr) plus one the test reads; both must be clean.
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    yield buf
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    logging.setLogRecordFactory(saved_factory)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture
def tm_run(monkeypatch, tmp_path):
    db.init_db(tmp_path / "logs.db")
    cfg = Config(ticketmaster_api_key=KEY, city_zip="00000")
    monkeypatch.setattr(config, "_cfg", cfg)
    monkeypatch.setattr("app.jobs.get_config", lambda: cfg)
    with db.get_session() as s:
        s.add(Source(name="tm", display_name="TM", type="ticketmaster", enabled=True))
        s.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"fault": "invalid key"})

    real_client = httpx.Client

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kw)

    monkeypatch.setattr(ticketmaster.httpx, "Client", client)


def test_a_failed_request_leaves_no_key_in_the_log_or_the_stored_error(logs, tm_run):
    run_all_scrapers()

    out = logs.getvalue()
    with db.get_session() as s:
        err = s.query(Source).filter_by(name="tm").one().last_error
    print(f"\nlog output:\n{out}\nlast_error: {err}")
    assert "401" in (err or ""), "the scrape did not fail as staged"
    assert KEY not in out, "the API key was written to the log"
    assert KEY not in err, "the API key was stored in sources.last_error"
    assert "apikey=REDACTED" in err


def test_httpx_request_lines_are_off_at_info(logs):
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


@pytest.mark.parametrize(
    "name", ["apikey", "api_key", "key", "token", "access_token", "client_secret", "APIKEY"]
)
def test_each_secret_parameter_is_redacted(name):
    url = f"https://api.example.com/v1/events?venueId=7&{name}={KEY}&size=20"
    assert redact(url) == f"https://api.example.com/v1/events?venueId=7&{name}=REDACTED&size=20"


def test_other_parameters_and_plain_text_are_left_alone():
    text = "GET https://example.com/e?venueId=7&monkey=1&tokens=2 failed: key = value"
    assert redact(text) == text


def test_any_logger_any_argument_type_is_redacted(logs):
    url = httpx.URL(f"https://api.example.com/v1/events?apikey={KEY}&venueId=7")
    logging.getLogger("some.plugin").warning("GET %s failed", url)
    try:
        raise RuntimeError(f"Client error for url '{url}'")
    except RuntimeError:
        logging.getLogger("some.plugin").exception("scrape failed")
    out = logs.getvalue()
    assert KEY not in out
    assert out.count("apikey=REDACTED") == 2  # the warning line, the traceback's last line


def test_access_log_args_keep_their_shape(logs):
    """uvicorn's access formatter unpacks record.args: a redacted record must still unpack."""
    from uvicorn.logging import AccessFormatter

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(AccessFormatter('%(client_addr)s "%(request_line)s" %(status_code)s'))
    access = logging.getLogger("test.access")
    access.addHandler(h)
    access.info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.1:5", "GET", f"/feed/x.ics?token={KEY}", "1.1", 200
    )
    access.removeHandler(h)
    assert KEY not in buf.getvalue()
    assert "/feed/x.ics?token=REDACTED" in buf.getvalue()
