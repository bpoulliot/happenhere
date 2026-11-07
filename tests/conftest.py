"""Test-run setup shared by every test module."""

# Authlib calls ``warnings.simplefilter("always", AuthlibDeprecationWarning)`` when
# ``authlib.deprecate`` is first imported, which lands in front of every filter in
# ``pyproject.toml``. Import it here, before pytest applies those filters, so the scoped
# ``ignore`` entry there for Authlib's httpx notice (ROADMAP 07) takes effect.
import authlib.deprecate  # noqa: F401
