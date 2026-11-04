"""Admin authentication: local password (default) or OpenID Connect.

``auth_mode: local`` keeps the single built-in admin account (bootstrap creates it).
``auth_mode: oidc`` signs admins in through any OpenID Connect provider and disables
the local login entirely: no password is created and POST /login is refused. An
OIDC user is an admin only when the configured group is in the ID token's groups
claim; anyone else is refused and gets no session.

Sessions are the same stateless HMAC cookies in both modes. OIDC sessions are
short (12 h) so that removing someone from the admin group takes effect within a
day without a session store.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth as _auth
from .config import Config, get_config

log = logging.getLogger(__name__)

SESSION_COOKIE = "happenhere_session"
LOCAL_SESSION_SECS = 30 * 86400
OIDC_SESSION_SECS = 12 * 3600
AUTH_MODES = ("local", "oidc")

router = APIRouter()
_oauth: OAuth | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def client_secret(cfg: Config) -> str:
    """The client secret, from the config/env value or a Docker secret file."""
    if cfg.oidc_client_secret:
        return cfg.oidc_client_secret
    path = os.environ.get("OIDC_CLIENT_SECRET_FILE", "")
    if path:
        return Path(path).read_text().strip()
    return ""


def validate_auth_config(cfg: Config) -> None:
    """Refuse to start with an unusable auth setup rather than run open or locked out."""
    if cfg.auth_mode not in AUTH_MODES:
        raise ValueError(f"auth_mode must be one of {AUTH_MODES}, got {cfg.auth_mode!r}")
    if cfg.auth_mode != "oidc":
        return
    missing = [
        name
        for name, value in (
            ("oidc_discovery_url", cfg.oidc_discovery_url),
            ("oidc_client_id", cfg.oidc_client_id),
            ("oidc_client_secret (or OIDC_CLIENT_SECRET_FILE)", client_secret(cfg)),
            ("oidc_admin_group", cfg.oidc_admin_group),
        )
        if not value
    ]
    if missing:
        raise ValueError("auth_mode is oidc but these are not set: " + ", ".join(missing))
    if not cfg.oidc_discovery_url.startswith("https://"):
        raise ValueError("oidc_discovery_url must be https")


def _key_material(cfg: Config) -> str:
    # Distinct per mode, so a local-mode cookie never validates in oidc mode or back.
    if cfg.auth_mode == "oidc":
        return f"oidc:{cfg.oidc_client_id}"
    return cfg.password_hash


def configure(cfg: Config) -> None:
    global _oauth
    _oauth = None
    if cfg.auth_mode != "oidc":
        return
    oauth = OAuth()
    oauth.register(
        "oidc",
        client_id=cfg.oidc_client_id,
        client_secret=client_secret(cfg),
        server_metadata_url=cfg.oidc_discovery_url,
        client_kwargs={"scope": cfg.oidc_scopes, "code_challenge_method": "S256"},
    )
    _oauth = oauth


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def current_admin(request: Request) -> str | None:
    """The signed-in admin's name, or None. The one check every admin path uses."""
    cfg = get_config()
    material = _key_material(cfg)
    if not material:
        return None  # local mode with no password: nobody is signed in
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    return _auth.get_session_user(token, cfg.secret_key, material)


def set_session(response, request: Request, username: str) -> None:
    cfg = get_config()
    secs = OIDC_SESSION_SECS if cfg.auth_mode == "oidc" else LOCAL_SESSION_SECS
    token = _auth.create_session(username, cfg.secret_key, _key_material(cfg), expiry_secs=secs)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=secs,
    )


def safe_next(target: str | None) -> str:
    """Only same-site relative paths; anything else (//host, scheme:, \\) goes to /admin."""
    target = (target or "").replace("\\", "")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return "/admin"
    return target


def is_admin(claims: dict, cfg: Config) -> bool:
    groups = claims.get(cfg.oidc_groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    return cfg.oidc_admin_group in groups


def _username(claims: dict, cfg: Config) -> str:
    return str(
        claims.get(cfg.oidc_username_claim) or claims.get("email") or claims.get("sub") or ""
    )


# ---------------------------------------------------------------------------
# OIDC routes
# ---------------------------------------------------------------------------


def _redirect_uri(request: Request) -> str:
    cfg = get_config()
    if cfg.public_url:
        return cfg.public_url.rstrip("/") + "/auth/oidc/callback"
    return str(request.url_for("oidc_callback"))


@router.get("/auth/oidc/start")
async def oidc_start(request: Request, next: str | None = None):
    if _oauth is None:
        return RedirectResponse("/login", status_code=302)
    if next is not None or "oidc_next" not in request.session:
        request.session["oidc_next"] = safe_next(next)
    return await _oauth.oidc.authorize_redirect(request, _redirect_uri(request))


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request):
    cfg = get_config()
    if _oauth is None:
        return RedirectResponse("/login", status_code=302)
    try:
        # Checks state, exchanges the code (PKCE), verifies the ID token and nonce.
        token = await _oauth.oidc.authorize_access_token(request)
    except OAuthError as exc:
        log.warning("OIDC callback refused: %s", exc.error)
        return HTMLResponse(_page("Sign-in failed. Try again."), status_code=400)
    claims = dict(token.get("userinfo") or {})
    username = _username(claims, cfg)
    if not username or not is_admin(claims, cfg):
        if cfg.oidc_groups_claim not in claims:
            log.warning(
                "OIDC sign-in refused for %r: the ID token has no %r claim at all "
                "(check oidc_scopes and the provider's scope mappings)",
                username,
                cfg.oidc_groups_claim,
            )
        else:
            log.warning(
                "OIDC sign-in refused for %r: not in group %r", username, cfg.oidc_admin_group
            )
        return HTMLResponse(
            _page("Your account is not allowed to administer this site."), status_code=403
        )
    target = safe_next(request.session.pop("oidc_next", None))
    resp = RedirectResponse(target, status_code=302)
    set_session(resp, request, username)
    log.info("OIDC sign-in: %s", username)
    return resp


def _page(message: str) -> str:
    from html import escape

    return (
        "<!doctype html><meta charset=utf-8><title>Sign in</title>"
        f"<p style='font:16px system-ui;margin:3em auto;max-width:30em'>{escape(message)} "
        "<a href='/'>Back to events</a></p>"
    )
