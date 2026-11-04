from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class Config:
    # Location — set by the user in the admin settings UI
    city_zip: str = ""
    city_name: str = ""  # e.g. "Portland, OR" — venue fallback, and the name in digest titles
    city_radius_miles: int = 25

    # Regional scraper hints (auto-built from city_name / city_zip if blank)
    eventbrite_region: str = ""  # e.g. "co--colorado-springs"
    meetup_location: str = ""  # e.g. "us--co--Colorado+Springs"

    # Scheduling
    scrape_interval_hours: int = 6

    # Digest (item 14). Admin-UI settings: deliberately not in the env mapping below, except the
    # older DIGEST_TIME, which still overrides digest_time at every start.
    digest_enabled: bool = True  # does nothing until ntfy is configured
    digest_interval_days: int = 3
    digest_time: str = "09:00"  # HH:MM in `tz`
    digest_window_days: int = 14

    # Notifications
    ntfy_url: str = ""
    ntfy_topic: str = "happenhere"
    ntfy_token: str = ""

    # APIs
    ticketmaster_api_key: str = ""
    pexels_api_key: str = ""

    # Misc
    db_path: str = "/app/data/happenhere.db"
    tz: str = "America/Chicago"  # neutral default; user changes in settings
    log_level: str = "INFO"

    # Auth — managed by bootstrap()
    username: str = "admin"
    secret_key: str = ""
    password_hash: str = ""

    # Admin sign-in: "local" (built-in admin account) or "oidc" (local login disabled)
    auth_mode: str = "local"
    oidc_discovery_url: str = ""  # https://<provider>/.well-known/openid-configuration
    oidc_client_id: str = ""
    oidc_client_secret: str = ""  # or OIDC_CLIENT_SECRET_FILE (Docker secret)
    oidc_admin_group: str = "admins"
    oidc_groups_claim: str = "groups"
    # Authentik and Authelia only send the groups claim when the "groups" scope is asked for.
    oidc_scopes: str = "openid email profile groups"
    oidc_username_claim: str = "preferred_username"
    public_url: str = ""  # e.g. https://events.example.org, for the OIDC redirect URI

    # Directory of local scraper plugins (extra source types); empty = none. See CLAUDE.md.
    plugins_dir: str = ""

    # Feature flags
    require_login_to_view: bool = False  # if True, even event browsing needs login


_cfg: Config | None = None


def load_config() -> Config:
    global _cfg
    cfg = Config()

    yaml_path = Path(os.environ.get("CONFIG_PATH", "/app/config.yml"))
    if yaml_path.exists():
        with yaml_path.open() as f:
            data = yaml.safe_load(f) or {}
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    # Load auto-generated auth from data dir
    import json as _json

    auth_path = Path(cfg.db_path).parent / "auth.json"
    if auth_path.exists():
        try:
            auth_data = _json.loads(auth_path.read_text())
            for k in ("secret_key", "password_hash", "username"):
                if auth_data.get(k):
                    setattr(cfg, k, auth_data[k])
        except Exception:
            pass

    _apply_env(cfg)
    _cfg = cfg
    return cfg


def get_config() -> Config:
    if _cfg is None:
        return load_config()
    return _cfg


def save_config_fields(updates: dict) -> Config:
    """Persist select config fields to config.yml (used by settings UI)."""
    cfg = get_config()
    yaml_path = Path(os.environ.get("CONFIG_PATH", "/app/config.yml"))

    if yaml_path.exists():
        with yaml_path.open() as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    for k, v in updates.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
            data[k] = v

    try:
        yaml_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    except Exception as exc:
        log.warning("Could not save config: %s", exc)

    return cfg


def save_auth(cfg: Config) -> None:
    import json as _json

    data_dir = Path(cfg.db_path).parent
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_path = data_dir / "auth.json"
        # 0600: the session signing key lives here (it must persist across restarts).
        fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # an existing file keeps its old mode through O_CREAT
        with os.fdopen(fd, "w") as f:
            f.write(
                _json.dumps(
                    {
                        "secret_key": cfg.secret_key,
                        "password_hash": cfg.password_hash,
                        "username": cfg.username,
                    }
                )
            )
    except Exception as exc:
        log.warning("Could not persist auth credentials: %s", exc)


def _apply_env(cfg: Config) -> None:
    mapping = {
        "CITY_ZIP": "city_zip",
        "CITY_NAME": "city_name",
        "CITY_RADIUS_MILES": ("city_radius_miles", int),
        "SCRAPE_INTERVAL_HOURS": ("scrape_interval_hours", int),
        "DIGEST_TIME": "digest_time",
        "NTFY_URL": "ntfy_url",
        "NTFY_TOPIC": "ntfy_topic",
        "NTFY_TOKEN": "ntfy_token",
        "TICKETMASTER_API_KEY": "ticketmaster_api_key",
        "PEXELS_API_KEY": "pexels_api_key",
        "DB_PATH": "db_path",
        "TZ": "tz",
        "LOG_LEVEL": "log_level",
        "HAPPENHERE_USERNAME": "username",
        "AUTH_MODE": "auth_mode",
        "OIDC_DISCOVERY_URL": "oidc_discovery_url",
        "OIDC_CLIENT_ID": "oidc_client_id",
        "OIDC_CLIENT_SECRET": "oidc_client_secret",
        "OIDC_ADMIN_GROUP": "oidc_admin_group",
        "OIDC_GROUPS_CLAIM": "oidc_groups_claim",
        "OIDC_SCOPES": "oidc_scopes",
        "OIDC_USERNAME_CLAIM": "oidc_username_claim",
        "PUBLIC_URL": "public_url",
        "PLUGINS_DIR": "plugins_dir",
        "REQUIRE_LOGIN_TO_VIEW": (
            "require_login_to_view",
            lambda v: v.lower() in ("1", "true", "yes"),
        ),
    }
    for env_key, attr in mapping.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if isinstance(attr, tuple):
            attr_name, cast = attr
            setattr(cfg, attr_name, cast(val))
        else:
            setattr(cfg, attr, val)
