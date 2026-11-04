from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import logging
import os
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN_EXPIRY_SECS = 30 * 86400  # 30 days


def _session_key(secret: str, password_hash: str) -> str:
    # Fold the first 16 chars of the bcrypt hash into the signing key so that
    # changing the password automatically invalidates all existing sessions.
    return f"{secret}:{password_hash[:16]}" if password_hash else secret


# ---------------------------------------------------------------------------
# Password hashing — bcrypt only (a pinned dependency; there is no weaker fallback)
# ---------------------------------------------------------------------------
import bcrypt as _bcrypt  # noqa: E402


def hash_password(pw: str) -> str:
    return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    # A legacy "sha256:" hash (from a build without bcrypt) never verifies:
    # the operator sets a new password via HAPPENHERE_PASSWORD.
    try:
        return _bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Stateless HMAC session tokens — survive container restarts
# ---------------------------------------------------------------------------


def _sign(payload: str, secret: str) -> str:
    return _hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session(
    username: str, secret: str, password_hash: str = "", expiry_secs: int = _TOKEN_EXPIRY_SECS
) -> str:
    """Return a signed, time-limited session token."""
    key = _session_key(secret, password_hash)
    expires = int(time.time()) + expiry_secs
    payload = f"{username}:{expires}"
    sig = _sign(payload, key)
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def get_session_user(token: str, secret: str, password_hash: str = "") -> str | None:
    """Verify token signature and expiry; return username or None."""
    key = _session_key(secret, password_hash)
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        # Format: <username>:<expires>:<sig> — split from right to handle colons in usernames
        last = decoded.rfind(":")
        if last < 0:
            return None
        sig = decoded[last + 1 :]
        rest = decoded[:last]
        sep = rest.rfind(":")
        if sep < 0:
            return None
        username = rest[:sep]
        expires_str = rest[sep + 1 :]
        if time.time() > int(expires_str):
            return None
        expected = _sign(f"{username}:{expires_str}", key)
        if not secrets.compare_digest(sig, expected):
            return None
        return username
    except Exception:
        return None


def delete_session(token: str) -> None:
    """No-op — stateless tokens are invalidated by deleting the cookie."""


# ---------------------------------------------------------------------------
# First-run bootstrap
# ---------------------------------------------------------------------------


def bootstrap(cfg_auth, save_fn, data_dir=None, create_password: bool = True) -> None:
    """
    Ensure a signing secret and admin account exist.
    Priority: existing values in config > HAPPENHERE_USERNAME/PASSWORD env vars > auto-generated.

    An auto-generated password is written to ``<data_dir>/initial-password`` (mode 0600),
    never to the log: container logs are read by log shippers and other tools.
    """
    changed = False

    if not cfg_auth.secret_key:
        cfg_auth.secret_key = secrets.token_hex(32)
        changed = True

    if create_password and not cfg_auth.password_hash:
        username = os.environ.get("HAPPENHERE_USERNAME", "") or cfg_auth.username or "admin"
        password = os.environ.get("HAPPENHERE_PASSWORD", "")

        if not password:
            password = secrets.token_urlsafe(16)
            pw_path = Path(data_dir or ".") / "initial-password"
            _write_private(pw_path, password)
            log.warning(
                "First run: generated a password for admin user %r; it is in %s. "
                "Set HAPPENHERE_USERNAME / HAPPENHERE_PASSWORD to choose your own.",
                username,
                pw_path,
            )

        cfg_auth.username = username
        cfg_auth.password_hash = hash_password(password)
        changed = True

    if changed:
        save_fn(cfg_auth)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
