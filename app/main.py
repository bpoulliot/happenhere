from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from . import auth as _auth
from . import oidc as _oidc
from .config import load_config, save_auth
from .db import init_db
from .plugins import load_plugins
from .scheduler import start, stop
from .web.routes import router

_version_file = Path(__file__).parent.parent / "VERSION"
_version = _version_file.read_text().strip() if _version_file.exists() else "dev"

_PUBLIC_PREFIXES = (
    "/static",
    "/favicon.ico",
    "/login",
    "/logout",
    "/auth",
    "/health",
    "/feed",
    "/api",
)
_ADMIN_PREFIXES = ("/admin",)


class _AuthGuard(BaseHTTPMiddleware):
    """
    - /admin/* always requires login.
    - Everything else is public unless require_login_to_view is set.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        cfg = load_config()

        # Always public
        if any(
            path == p or path.startswith(p + "/") or path.startswith(p + "?")
            for p in _PUBLIC_PREFIXES
        ):
            return await call_next(request)

        is_authed = bool(_oidc.current_admin(request))

        # Admin paths always require auth
        if any(path == p or path.startswith(p) for p in _ADMIN_PREFIXES):
            if not is_authed:
                from fastapi.responses import RedirectResponse

                return RedirectResponse(f"/login?next={quote(path)}", status_code=302)
            return await call_next(request)

        # Non-admin paths: gated only if require_login_to_view
        if cfg.require_login_to_view and not is_authed:
            from fastapi.responses import RedirectResponse

            return RedirectResponse(f"/login?next={path}", status_code=302)

        return await call_next(request)


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "SAMEORIGIN"
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"
        h["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: *; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    _oidc.validate_auth_config(cfg)
    _auth.bootstrap(
        cfg,
        save_auth,
        data_dir=Path(cfg.db_path).parent,
        create_password=cfg.auth_mode == "local",
    )
    _oidc.configure(cfg)
    load_plugins(cfg.plugins_dir)
    init_db()
    from .jobs import run_all_scrapers, send_daily_digest

    start(
        scrape_fn=run_all_scrapers,
        digest_fn=send_daily_digest,
        scrape_interval_hours=cfg.scrape_interval_hours,
        digest_time=cfg.digest_time,
    )
    yield
    stop()


app = FastAPI(
    title="HappenHere",
    version=_version,
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

app.add_middleware(_SecurityHeaders)
app.add_middleware(_AuthGuard)
# Holds only the OIDC state/nonce/next during a sign-in (signed cookie, 10 min).
app.add_middleware(
    SessionMiddleware,
    # Per-process random key: this cookie lives only for one sign-in, and the app runs
    # one worker. A restart mid-sign-in just fails that sign-in.
    secret_key=secrets.token_hex(32),
    session_cookie="happenhere_oidc",
    max_age=600,
    same_site="lax",
    https_only=False,
)

_static = Path(__file__).parent / "web" / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

app.include_router(router)
app.include_router(_oidc.router)
