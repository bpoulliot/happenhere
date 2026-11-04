from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import joinedload

from .. import auth as _auth
from .. import oidc as _oidc
from ..config import get_config, save_config_fields
from ..db import get_session
from ..ical_export import generate_ics
from ..jobs import run_single_scraper
from ..models import Event, Source
from ..netguard import NonPublicURL
from ..plugins import get_report as get_plugin_report
from ..scrapers.registry import TYPE_LABELS, scraper_types
from ..sources import check_source_url, split_categories

router = APIRouter()
log = logging.getLogger(__name__)
_tpl_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_tpl_dir))

_APP_VERSION = "dev"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_user(request: Request) -> str | None:
    return _oidc.current_admin(request)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None):
    # Redirect targets here are fixed paths; the requested page travels in the
    # session, never in a redirect built from the query string.
    if _get_user(request):
        return RedirectResponse("/admin", status_code=302)
    if get_config().auth_mode == "oidc":
        request.session["oidc_next"] = _oidc.safe_next(next)
        return RedirectResponse("/auth/oidc/start", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None, "v": _APP_VERSION})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    cfg = get_config()
    if cfg.auth_mode != "local":
        return PlainTextResponse("Local sign-in is disabled.", status_code=403)
    if _auth.verify_password(password, cfg.password_hash) and username == cfg.username:
        resp = RedirectResponse("/", status_code=302)
        _oidc.set_session(resp, request, cfg.username)
        return resp
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid username or password", "v": _APP_VERSION},
        status_code=401,
    )


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(_oidc.SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def _favicon_url(source_url: str = "") -> str:
    domain = urlparse(source_url).netloc if source_url else ""
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=32"


def _logo_url(source_url: str = "") -> str:
    domain = urlparse(source_url).netloc if source_url else ""
    if not domain:
        return ""
    return f"https://logo.clearbit.com/{domain}"


_PEXELS = "https://images.pexels.com/photos/{id}/pexels-photo-{id}.jpeg?auto=compress&cs=tinysrgb&w=600&h=400&fit=crop"
_CATEGORY_PHOTOS: dict[str, str] = {
    "brewery": _PEXELS.format(id="1552630"),
    "beer": _PEXELS.format(id="1552630"),
    "live music": _PEXELS.format(id="1763075"),
    "concert": _PEXELS.format(id="1763075"),
    "music": _PEXELS.format(id="1699388"),
    "comedy": _PEXELS.format(id="713149"),
    "theater": _PEXELS.format(id="713149"),
    "arts": _PEXELS.format(id="1839919"),
    "movies": _PEXELS.format(id="7991579"),
    "fathom events": _PEXELS.format(id="7991579"),
    "gaming": _PEXELS.format(id="3165335"),
    "community": _PEXELS.format(id="1190298"),
    "festival": _PEXELS.format(id="1190298"),
    "education": _PEXELS.format(id="256431"),
    "library": _PEXELS.format(id="256431"),
    "live entertainment": _PEXELS.format(id="1763075"),
    "sports": _PEXELS.format(id="46798"),
    "trivia": _PEXELS.format(id="3405457"),
    "food": _PEXELS.format(id="1640777"),
    "outdoor": _PEXELS.format(id="417074"),
}
_DEFAULT_PHOTO = _PEXELS.format(id="1367192")  # generic city/event crowd


def _stock_photo(categories: list[str], title: str) -> str:
    combined = " ".join(categories + [title]).lower()
    for keyword, url in _CATEGORY_PHOTOS.items():
        if keyword in combined:
            return url
    return _DEFAULT_PHOTO


def _event_to_dict(ev: Event) -> dict:
    cats = []
    try:
        cats = json.loads(ev.categories or "[]")
    except Exception:
        pass
    src_url = ev.source.url if ev.source else ""
    return {
        "id": ev.id,
        "title": ev.title,
        "start_dt": ev.start_dt.isoformat() if ev.start_dt else None,
        "start_display": _fmt_dt(ev.start_dt),
        "end_dt": ev.end_dt.isoformat() if ev.end_dt else None,
        "venue_name": ev.venue_name or "",
        "venue_addr": ev.venue_addr or "",
        "url": ev.url or "",
        "image_url": ev.image_url or "",
        "description": (ev.description or "")[:300],
        "categories": cats,
        "source": ev.source.display_name if ev.source else "",
        "source_name": ev.source.name if ev.source else "",
        "source_type": ev.source.type if ev.source else "",
        "source_favicon": _favicon_url(src_url),
        "source_logo": _logo_url(src_url),
        "stock_photo": _stock_photo(cats, ev.title or ""),
        "first_seen": ev.first_seen.isoformat() if ev.first_seen else None,
    }


# Source-type priority for deduplication (lower = preferred)
_TYPE_PRIORITY: dict[str, int] = {
    "ticketmaster": 10,
    "jsonld": 20,
    "ical": 20,
    "squarespace": 30,
    "spothopper": 30,
    "playwright_jsonld": 40,
    "eventbrite": 50,
    "meetup": 50,
}
_STOP_WORDS = frozenset(
    {"the", "a", "an", "and", "or", "at", "in", "on", "of", "for", "with", "to", "by"}
)


def _dedup_key(title: str, dt_iso: str | None) -> str:
    words = re.sub(r"[^\w\s]", "", title.lower()).split()
    sig = [w for w in words if w not in _STOP_WORDS][:4]
    return f"{'_'.join(sig)}|{(dt_iso or '')[:10]}"


def _dedup_events(events: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for ev in events:
        key = _dedup_key(ev["title"], ev.get("start_dt"))
        if key not in seen:
            seen[key] = ev
        else:
            existing = seen[key]
            ev_pri = _TYPE_PRIORITY.get(ev["source_type"], 99)
            ex_pri = _TYPE_PRIORITY.get(existing["source_type"], 99)
            if ev_pri < ex_pri:
                seen[key] = ev
            elif ev_pri == ex_pri:
                if bool(ev.get("image_url")) + bool(ev.get("description")) > bool(
                    existing.get("image_url")
                ) + bool(existing.get("description")):
                    seen[key] = ev
    return list(seen.values())


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.strftime("%a, %b %-d %Y · %-I:%M %p") if dt.hour else dt.strftime("%a, %b %-d %Y")


def _query_events(session, days=30, category="", source="", search="") -> dict:
    now = datetime.now()
    cutoff = now + timedelta(days=days)
    q = (
        session.query(Event)
        .options(joinedload(Event.source))
        .filter(Event.start_dt >= now)
        .filter(Event.start_dt <= cutoff)
        .order_by(Event.start_dt)
    )
    rows = q.all()
    events = [_event_to_dict(e) for e in rows]
    events = _dedup_events(events)
    if category:
        events = [e for e in events if category.lower() in " ".join(e["categories"]).lower()]
    if source:
        events = [e for e in events if e["source_name"] == source]
    if search:
        sl = search.lower()
        events = [e for e in events if sl in e["title"].lower() or sl in e["venue_name"].lower()]
    events.sort(key=lambda e: e.get("start_dt") or "")
    return {"events": events, "total": len(events)}


def _all_categories(session) -> list[str]:
    rows = session.query(Event.categories).filter(Event.categories.isnot(None)).all()
    cats: set[str] = set()
    for (raw,) in rows:
        try:
            for c in json.loads(raw or "[]"):
                if c:
                    cats.add(c)
        except Exception:
            pass
    return sorted(cats)


# ---------------------------------------------------------------------------
# Public event routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return RedirectResponse("/events", status_code=302)


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, days: int = 30, category: str = "", search: str = ""):
    cfg = get_config()
    with get_session() as session:
        data = _query_events(session, days=days, category=category, search=search)
        categories = _all_categories(session)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "events": data["events"],
            "total": data["total"],
            "days": days,
            "category": category,
            "search": search,
            "categories": categories,
            "city_name": cfg.city_name or "your city",
        },
    )


@router.get("/partials/event-list", response_class=HTMLResponse)
async def event_list_partial(
    request: Request,
    days: int = 30,
    category: str = "",
    source: str = "",
    search: str = "",
    limit: int = 100,
):
    with get_session() as session:
        data = _query_events(session, days=days, category=category, source=source, search=search)
    events = data["events"][:limit]
    return templates.TemplateResponse(
        request, "partials/event_list.html", {"request": request, "events": events}
    )


@router.get("/events/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, month: str = ""):
    import calendar as cal_mod

    try:
        first_day = datetime.strptime(month, "%Y-%m")
    except ValueError:
        first_day = datetime.now().replace(day=1)
    if first_day.month == 12:
        last_day = first_day.replace(year=first_day.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last_day = first_day.replace(month=first_day.month + 1, day=1) - timedelta(days=1)

    with get_session() as session:
        rows = (
            session.query(Event)
            .options(joinedload(Event.source))
            .filter(Event.start_dt >= first_day)
            .filter(Event.start_dt <= last_day.replace(hour=23, minute=59))
            .order_by(Event.start_dt)
            .all()
        )
        events_by_day: dict[int, list[dict]] = {}
        for ev in rows:
            events_by_day.setdefault(ev.start_dt.day, []).append(_event_to_dict(ev))

    prev_month = (first_day - timedelta(days=1)).strftime("%Y-%m")
    next_month = (last_day + timedelta(days=1)).strftime("%Y-%m")
    cal = cal_mod.monthcalendar(first_day.year, first_day.month)
    today = datetime.now().day if datetime.now().month == first_day.month else -1

    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "request": request,
            "month_label": first_day.strftime("%B %Y"),
            "month_str": first_day.strftime("%Y-%m"),
            "prev_month": prev_month,
            "next_month": next_month,
            "cal": cal,
            "events_by_day": events_by_day,
            "today": today,
        },
    )


@router.get("/feed/events.ics")
async def ical_feed():
    with get_session() as session:
        ics = generate_ics(session)
    cfg = get_config()
    city_slug = re.sub(r"[^\w]", "-", cfg.city_name or "events").lower()
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={city_slug}-events.ics",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get("/api/events")
async def api_events(
    days: int = Query(30),
    category: str = Query(""),
    source: str = Query(""),
    search: str = Query(""),
):
    with get_session() as session:
        return _query_events(session, days=days, category=category, source=source, search=search)


# ---------------------------------------------------------------------------
# Admin — source management
# ---------------------------------------------------------------------------


def _src_to_dict(s: Source) -> dict:
    return {
        "name": s.name,
        "display_name": s.display_name or "",
        "type": s.type or "",
        "type_label": TYPE_LABELS.get(s.type or "", s.type or ""),
        "url": s.url or "",
        "categories": json.loads(s.categories or "[]"),
        "enabled": s.enabled,
        "last_scraped": s.last_scraped.strftime("%Y-%m-%d %H:%M") if s.last_scraped else "Never",
        "error_count": s.error_count or 0,
        "last_error": (s.last_error or "")[:120],
    }


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    cfg = get_config()
    with get_session() as session:
        sources = session.query(Source).order_by(Source.display_name).all()
        total_events = session.query(Event).count()
        src_list = [_src_to_dict(s) for s in sources]
    from ..scheduler import next_scrape_time

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "sources": src_list,
            "total_events": total_events,
            "next_scrape": next_scrape_time(),
            "city_name": cfg.city_name or "(not configured)",
            "scraper_types": scraper_types(),
            "plugins": get_plugin_report(),
        },
    )


@router.get("/admin/sources/new", response_class=HTMLResponse)
async def source_new_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin_source.html",
        {"request": request, "scraper_types": scraper_types(), "source": None, "detection": None},
    )


@router.get("/admin/sources/{source_name}/edit", response_class=HTMLResponse)
async def source_edit_page(request: Request, source_name: str):
    with get_session() as session:
        src = session.query(Source).filter_by(name=source_name).first()
        if not src:
            return RedirectResponse("/admin", status_code=302)
        src_dict = _src_to_dict(src)
    return templates.TemplateResponse(
        request,
        "admin_source.html",
        {
            "request": request,
            "scraper_types": scraper_types(),
            "source": src_dict,
            "detection": None,
        },
    )


@router.post("/admin/sources/detect")
async def detect_source(url: str = Form(...)):
    """Auto-detect the right scraper type for a given URL. Returns JSON."""
    from ..discovery import detect

    cfg = get_config()
    result = detect(url, config=cfg)
    return {
        "success": result.success,
        "scraper_type": result.scraper_type,
        "detected_url": result.detected_url,
        "ical_url": result.ical_url,
        "preview": result.preview,
        "notes": result.notes,
    }


@router.post("/admin/sources")
async def source_create(
    display_name: str = Form(...),
    scraper_type: str = Form(...),
    url: str = Form(""),
    categories: str = Form(""),
    enabled: str = Form("off"),
):
    name = _slugify(display_name)
    try:
        check_source_url(url)
    except NonPublicURL:
        log.warning("source URL refused (non-public or unresolvable): %s", url, exc_info=True)
        return PlainTextResponse(
            "Source URL refused: it must be http(s) and resolve to a public address.",
            status_code=400,
        )
    cats = split_categories(categories)
    with get_session() as session:
        # Ensure unique name
        base = name
        i = 1
        while session.query(Source).filter_by(name=name).first():
            name = f"{base}_{i}"
            i += 1
        src = Source(
            name=name,
            display_name=display_name,
            type=scraper_type,
            url=url,
            categories=json.dumps(cats),
            enabled=(enabled == "on"),
        )
        session.add(src)
        session.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/sources/{source_name}/update")
async def source_update(
    source_name: str,
    display_name: str = Form(...),
    scraper_type: str = Form(...),
    url: str = Form(""),
    categories: str = Form(""),
    enabled: str = Form("off"),
):
    try:
        check_source_url(url)
    except NonPublicURL:
        log.warning("source URL refused (non-public or unresolvable): %s", url, exc_info=True)
        return PlainTextResponse(
            "Source URL refused: it must be http(s) and resolve to a public address.",
            status_code=400,
        )
    cats = split_categories(categories)
    with get_session() as session:
        src = session.query(Source).filter_by(name=source_name).first()
        if src:
            src.display_name = display_name
            src.type = scraper_type
            src.url = url
            src.categories = json.dumps(cats)
            src.enabled = enabled == "on"
            session.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/sources/{source_name}/delete")
async def source_delete(source_name: str):
    with get_session() as session:
        src = session.query(Source).filter_by(name=source_name).first()
        if src:
            # Remove associated events first
            session.query(Event).filter_by(source_id=src.id).delete()
            session.delete(src)
            session.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/sources/{source_name}/toggle")
async def toggle_source(source_name: str):
    with get_session() as session:
        src = session.query(Source).filter_by(name=source_name).first()
        if src:
            src.enabled = not src.enabled
            session.commit()
            return {"enabled": src.enabled}
    return {"error": "not found"}


@router.post("/admin/scrape/{source_name}", response_class=HTMLResponse)
async def trigger_scrape(request: Request, source_name: str):
    import anyio

    result = await anyio.to_thread.run_sync(lambda: run_single_scraper(source_name))
    with get_session() as session:
        sources = session.query(Source).order_by(Source.display_name).all()
        total_events = session.query(Event).count()
        src_list = [_src_to_dict(s) for s in sources]
    cfg = get_config()
    from ..scheduler import next_scrape_time

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "sources": src_list,
            "total_events": total_events,
            "next_scrape": next_scrape_time(),
            "city_name": cfg.city_name or "(not configured)",
            "scraper_types": scraper_types(),
            "plugins": get_plugin_report(),
            "scrape_result": result,
            "scrape_source": source_name,
        },
    )


@router.post("/admin/scrape-all")
async def scrape_all():
    from ..scheduler import trigger_scrape_now

    trigger_scrape_now()
    return {"status": "triggered"}


# ---------------------------------------------------------------------------
# Admin — settings
# ---------------------------------------------------------------------------


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = get_config()
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "request": request,
            "cfg": {
                "city_name": cfg.city_name,
                "city_zip": cfg.city_zip,
                "city_radius_miles": cfg.city_radius_miles,
                "eventbrite_region": cfg.eventbrite_region,
                "meetup_location": cfg.meetup_location,
                "scrape_interval_hours": cfg.scrape_interval_hours,
                "digest_time": cfg.digest_time,
                "tz": cfg.tz,
                "ntfy_url": cfg.ntfy_url,
                "ntfy_topic": cfg.ntfy_topic,
                "ticketmaster_api_key": cfg.ticketmaster_api_key,
                "pexels_api_key": cfg.pexels_api_key,
                "require_login_to_view": cfg.require_login_to_view,
            },
        },
    )


@router.post("/admin/settings")
async def settings_save(
    city_name: str = Form(""),
    city_zip: str = Form(""),
    city_radius_miles: int = Form(25),
    eventbrite_region: str = Form(""),
    meetup_location: str = Form(""),
    scrape_interval_hours: int = Form(6),
    digest_time: str = Form("09:00"),
    tz: str = Form("America/Chicago"),
    ntfy_url: str = Form(""),
    ntfy_topic: str = Form("happenhere"),
    ticketmaster_api_key: str = Form(""),
    pexels_api_key: str = Form(""),
    require_login_to_view: str = Form("off"),
):
    updates = {
        "city_name": city_name,
        "city_zip": city_zip,
        "city_radius_miles": city_radius_miles,
        "eventbrite_region": eventbrite_region,
        "meetup_location": meetup_location,
        "scrape_interval_hours": scrape_interval_hours,
        "digest_time": digest_time,
        "tz": tz,
        "ntfy_url": ntfy_url,
        "ntfy_topic": ntfy_topic,
        "require_login_to_view": (require_login_to_view == "on"),
    }
    # Only update API keys if non-empty (avoid wiping existing keys)
    if ticketmaster_api_key:
        updates["ticketmaster_api_key"] = ticketmaster_api_key
    if pexels_api_key:
        updates["pexels_api_key"] = pexels_api_key

    save_config_fields(updates)

    # Reschedule scraper if interval changed
    from ..scheduler import reschedule_scraper

    try:
        reschedule_scraper(scrape_interval_hours)
    except Exception:
        pass

    return RedirectResponse("/admin/settings?saved=1", status_code=302)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers and feed readers ask for /favicon.ico directly, whatever the page links.
    return FileResponse(
        Path(__file__).parent / "static" / "brand" / "favicon.ico", media_type="image/x-icon"
    )


@router.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "_", text).strip("_")[:50]
