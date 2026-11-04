"""Admin sign-in: local and OIDC modes, group gating, fail-closed config."""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from app import auth, config, oidc
from app.config import Config
from app.main import app


def _cfg(**kw) -> Config:
    c = Config()
    c.secret_key = "k" * 64
    for k, v in kw.items():
        setattr(c, k, v)
    return c


OIDC = dict(
    auth_mode="oidc",
    oidc_discovery_url="https://idp.example/.well-known/openid-configuration",
    oidc_client_id="happenhere",
    oidc_client_secret="s3cret",
    public_url="https://events.example",
)


@pytest.fixture
def use(monkeypatch, tmp_path):
    from app.db import init_db

    init_db(tmp_path / "test.db")

    def _use(c: Config):
        monkeypatch.setattr(config, "_cfg", c)
        monkeypatch.setattr(config, "load_config", lambda: c)
        import app.main as main

        monkeypatch.setattr(main, "load_config", lambda: c)
        return c

    return _use


class _FakeIdP:
    """Stands in for authlib's registered client."""

    def __init__(self, claims=None, error=None):
        self.claims = claims or {}
        self.error = error
        self.redirect_uri = None

    async def authorize_redirect(self, request, redirect_uri):
        from fastapi.responses import RedirectResponse

        self.redirect_uri = redirect_uri
        return RedirectResponse("https://idp.example/authorize?state=x", status_code=302)

    async def authorize_access_token(self, request):
        if self.error:
            raise oidc.OAuthError(error=self.error)
        return {"userinfo": self.claims}


@pytest.fixture
def idp(monkeypatch):
    def _idp(**kw):
        fake = _FakeIdP(**kw)
        monkeypatch.setattr(oidc, "_oauth", type("O", (), {"oidc": fake})())
        return fake

    return _idp


def client():
    # nullcontext, not TestClient's own context manager: that would run the app's
    # lifespan (scheduler, bootstrap, the real data path).
    return nullcontext(TestClient(app, base_url="https://testserver", follow_redirects=False))


# --- config validation ---------------------------------------------------------


def test_unknown_mode_refused():
    with pytest.raises(ValueError, match="auth_mode"):
        oidc.validate_auth_config(_cfg(auth_mode="none"))


@pytest.mark.parametrize("missing", ["oidc_discovery_url", "oidc_client_id", "oidc_client_secret"])
def test_oidc_mode_missing_setting_refused(missing, monkeypatch):
    monkeypatch.delenv("OIDC_CLIENT_SECRET_FILE", raising=False)
    with pytest.raises(ValueError, match=missing.split("_", 1)[1][:8]):
        oidc.validate_auth_config(_cfg(**{**OIDC, missing: ""}))


def test_oidc_secret_from_file(tmp_path, monkeypatch):
    f = tmp_path / "secret"
    f.write_text("from-file\n")
    monkeypatch.setenv("OIDC_CLIENT_SECRET_FILE", str(f))
    c = _cfg(**{**OIDC, "oidc_client_secret": ""})
    oidc.validate_auth_config(c)
    assert oidc.client_secret(c) == "from-file"


def test_http_discovery_refused():
    with pytest.raises(ValueError, match="https"):
        oidc.validate_auth_config(_cfg(**{**OIDC, "oidc_discovery_url": "http://idp/x"}))


def test_oidc_mode_creates_no_password(tmp_path):
    c = _cfg(**OIDC)
    c.secret_key = ""
    auth.bootstrap(c, lambda _c: None, data_dir=tmp_path, create_password=False)
    assert c.secret_key and not c.password_hash
    assert not (tmp_path / "initial-password").exists()


# --- group gate and redirects ---------------------------------------------------


def test_is_admin_requires_the_group():
    c = _cfg(**OIDC)
    assert oidc.is_admin({"groups": ["users", "admins"]}, c)
    assert not oidc.is_admin({"groups": ["users"]}, c)
    assert not oidc.is_admin({}, c)
    assert oidc.is_admin({"groups": "admins"}, c)


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/admin/sources", "/admin/sources"),
        ("//evil.example/x", "/admin"),
        ("https://evil.example", "/admin"),
        ("/\\evil.example", "/evil.example"),  # backslash stripped: a same-site path
        ("/\\/evil.example", "/admin"),  # becomes //evil.example
        ("javascript:alert(1)", "/admin"),
        ("", "/admin"),
        (None, "/admin"),
    ],
)
def test_safe_next(target, expected):
    assert oidc.safe_next(target) == expected


# --- OIDC mode end to end (fake IdP) --------------------------------------------


def test_admin_redirects_to_login_then_idp(use, idp):
    use(_cfg(**OIDC))
    fake = idp()
    with client() as c:
        r = c.get("/admin")
        assert r.status_code == 302 and r.headers["location"].startswith("/login")
        r = c.get(r.headers["location"])
        assert r.headers["location"].startswith("/auth/oidc/start")
        r = c.get(r.headers["location"])
        assert r.headers["location"].startswith("https://idp.example/authorize")
    assert fake.redirect_uri == "https://events.example/auth/oidc/callback"


def test_callback_admin_gets_session(use, idp):
    use(_cfg(**OIDC))
    idp(claims={"preferred_username": "bitmap", "groups": ["admins"]})
    with client() as c:
        c.get("/auth/oidc/start?next=/admin/settings")
        r = c.get("/auth/oidc/callback")
        assert r.status_code == 302 and r.headers["location"] == "/admin/settings"
        cookie = r.cookies.get(oidc.SESSION_COOKIE)
        assert cookie
        assert "Secure" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
        c.cookies.set(oidc.SESSION_COOKIE, cookie)
        assert c.get("/admin").status_code == 200


def test_callback_non_admin_refused_without_session(use, idp):
    use(_cfg(**OIDC))
    idp(claims={"preferred_username": "guest", "groups": ["users"]})
    with client() as c:
        r = c.get("/auth/oidc/callback")
        assert r.status_code == 403
        assert oidc.SESSION_COOKIE not in r.cookies


def test_callback_provider_error_refused(use, idp):
    use(_cfg(**OIDC))
    idp(error="access_denied")
    with client() as c:
        r = c.get("/auth/oidc/callback")
        assert r.status_code == 400
        assert oidc.SESSION_COOKIE not in r.cookies


def test_oidc_mode_refuses_local_login(use):
    c = use(_cfg(**OIDC))
    c.password_hash = auth.hash_password("pw")  # even a leftover hash must not work
    with client() as cl:
        r = cl.post("/login", data={"username": "admin", "password": "pw"})
        assert r.status_code == 403
        assert oidc.SESSION_COOKIE not in r.cookies


def test_local_cookie_does_not_work_in_oidc_mode(use):
    local = use(_cfg(password_hash=auth.hash_password("pw")))
    token = auth.create_session("admin", local.secret_key, local.password_hash)
    use(_cfg(**OIDC))
    with client() as c:
        c.cookies.set(oidc.SESSION_COOKIE, token)
        assert c.get("/admin").status_code == 302


def test_public_pages_stay_public_in_oidc_mode(use):
    use(_cfg(**OIDC))
    with client() as c:
        assert c.get("/health").status_code == 200
        assert c.get("/feed/events.ics").status_code == 200


# --- local mode still works ------------------------------------------------------


def test_local_login_round_trip(use):
    use(_cfg(username="admin", password_hash=auth.hash_password("pw")))
    with client() as c:
        assert c.post("/login", data={"username": "admin", "password": "bad"}).status_code == 401
        r = c.post("/login", data={"username": "admin", "password": "pw"})
        assert r.status_code == 302
        c.cookies.set(oidc.SESSION_COOKIE, r.cookies[oidc.SESSION_COOKIE])
        assert c.get("/admin").status_code == 200


# --- every HTML page renders (Starlette's TemplateResponse(request, name, ctx)) ---


@pytest.mark.parametrize("path", ["/events", "/events/calendar", "/partials/event-list"])
def test_public_pages_render(use, path):
    use(_cfg(**OIDC))
    with client() as c:
        r = c.get(path)
        assert r.status_code == 200, r.text[:300]
        assert "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("path", ["/admin", "/admin/sources/new", "/admin/settings"])
def test_admin_pages_render(use, path):
    c_ = use(_cfg(**OIDC))
    token = auth.create_session("bitmap", c_.secret_key, "oidc:happenhere", expiry_secs=60)
    with client() as c:
        c.cookies.set(oidc.SESSION_COOKIE, token)
        r = c.get(path)
        assert r.status_code == 200, r.text[:300]


def test_root_redirects_to_events(use):
    use(_cfg(**OIDC))
    with client() as c:
        r = c.get("/")
        assert r.status_code == 302 and r.headers["location"] == "/events"


def test_groups_scope_requested_by_default(use):
    c = use(_cfg(**OIDC))
    oidc.configure(c)
    try:
        assert "groups" in oidc._oauth.oidc.client_kwargs["scope"].split()
        assert oidc._oauth.oidc.client_kwargs["code_challenge_method"] == "S256"
    finally:
        oidc._oauth = None


def test_missing_groups_claim_is_named_in_the_log(use, idp, caplog):
    use(_cfg(**OIDC))
    idp(claims={"preferred_username": "bitmap"})
    with client() as c, caplog.at_level("WARNING", logger="app.oidc"):
        assert c.get("/auth/oidc/callback").status_code == 403
    assert any("no 'groups' claim" in r.getMessage() for r in caplog.records)


def test_login_carries_next_through_the_session(use, idp):
    use(_cfg(**OIDC))
    idp(claims={"preferred_username": "bitmap", "groups": ["admins"]})
    with client() as c:
        r = c.get("/login?next=/admin/settings")
        assert r.headers["location"] == "/auth/oidc/start"  # fixed path, no query
        c.get("/auth/oidc/start")
        r = c.get("/auth/oidc/callback")
        assert r.headers["location"] == "/admin/settings"
