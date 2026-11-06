"""Keep credentials that travel in URL query strings out of logs and stored errors (item 21).

Some APIs take their key as a query parameter (Ticketmaster's ``?apikey=``). The full URL then
appears in httpx's request log line, in ``httpx.HTTPStatusError``'s text and in any traceback.
``install()`` redacts every log record at creation, whichever logger made it and whichever
handler writes it; ``redact()`` is for text stored elsewhere (``sources.last_error``).
"""

from __future__ import annotations

import logging
import re
import traceback

SECRET_PARAMS = ("apikey", "api_key", "key", "token", "access_token", "client_secret")
REDACTED = "REDACTED"

_PARAM_RE = re.compile(
    r"(?P<pre>[?&;](?:" + "|".join(SECRET_PARAMS) + r")=)(?P<val>[^&;#\s'\"<>]*)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """The text with every secret query-parameter value replaced by ``REDACTED``."""
    if not text or "=" not in text:
        return text
    return _PARAM_RE.sub(lambda m: m.group("pre") + REDACTED, text)


def install() -> None:
    """Redact every log record from now on (idempotent)."""
    base = logging.getLogRecordFactory()
    if getattr(base, "redacts", False):
        return

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = base(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # a bad format string: leave it for the handler to report
            return record
        if redact(message) != message:
            _redact_record(record)
        if record.exc_info and record.exc_info[1] is not None:
            # Formatters use exc_text when it is set instead of rendering exc_info again.
            text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = redact(text)
        return record

    factory.redacts = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def _redact_arg(value):
    text = str(value)
    clean = redact(text)
    return value if clean == text else clean


def _redact_record(record: logging.LogRecord) -> None:
    """Redact the message and each argument in place, keeping the args' shape.

    Some formatters unpack ``record.args`` (uvicorn's access log), so the args stay a tuple or
    mapping of the same length; only an argument whose text holds a secret becomes a string.
    """
    if isinstance(record.msg, str):
        record.msg = redact(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(_redact_arg(a) for a in record.args)
    elif isinstance(record.args, dict):
        record.args = {k: _redact_arg(v) for k, v in record.args.items()}
    try:
        message = record.getMessage()
    except Exception:
        message = None
    if message is None or redact(message) != message:
        # A secret split across message and args: fall back to the rendered, redacted text.
        record.msg, record.args = redact(str(message or record.msg)), None
