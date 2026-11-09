"""Test-run setup shared by every test module."""

# Authlib calls ``warnings.simplefilter("always", AuthlibDeprecationWarning)`` when
# ``authlib.deprecate`` is first imported, which lands in front of every filter in
# ``pyproject.toml``. Import it here, before pytest applies those filters, so the scoped
# ``ignore`` entry there for Authlib's httpx notice (ROADMAP 07) takes effect.
import authlib.deprecate  # noqa: F401

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

GAZETTEER_FIXTURE = Path(__file__).parent / "fixtures" / "gazetteer"


@pytest.fixture(autouse=True)
def _synthetic_gazetteer(monkeypatch):
    """Item 30: every test resolves locations from the small synthetic Gazetteer, never the
    bundled Census files (a test that reached them would fail on the missing directory)."""
    try:
        from app import gazetteer
    except ImportError:  # a checkout from before item 30 (the failing-first run)
        yield
        return
    monkeypatch.setattr(gazetteer, "DEFAULT_DIR", Path("/nonexistent/bundled-gazetteer"))
    monkeypatch.setenv("GAZETTEER_DIR", str(GAZETTEER_FIXTURE))
    gazetteer._load.cache_clear()
    yield
    gazetteer._load.cache_clear()
