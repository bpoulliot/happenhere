from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def configured(cfg) -> bool:
    return bool(cfg.ntfy_url and cfg.ntfy_topic)


def post_to_ntfy(
    cfg, title: str, body: str, click: str | None = None, tags: list[str] | None = None
) -> tuple[bool, str | None]:
    """Publish one message. Returns (ok, error). Never raises.

    It publishes as JSON to the server root rather than through ``Title:`` headers: an
    HTTP header value is ASCII, and a title with a dash in it would raise before sending.
    """
    payload: dict = {"topic": cfg.ntfy_topic, "title": title, "message": body, "priority": 3}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    headers = {}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(cfg.ntfy_url.rstrip("/") + "/", json=payload, headers=headers)
            resp.raise_for_status()
        return True, None
    except Exception as exc:
        log.warning("ntfy POST failed: %s", exc)
        return False, str(exc)
