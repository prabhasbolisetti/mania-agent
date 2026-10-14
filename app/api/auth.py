"""Admin authentication and restart-safe signed sessions."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

from fastapi import HTTPException, Request, status

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
SESSION_COOKIE_NAME = "mania_admin_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 8


def _signing_key() -> bytes:
    if SESSION_SECRET:
        return SESSION_SECRET.encode("utf-8")
    if os.getenv("RENDER"):
        raise RuntimeError(
            "SESSION_SECRET must be configured on the production hosting platform."
        )
    # Backward-compatible local development fallback only.
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD.encode("utf-8")
    raise RuntimeError("Configure SESSION_SECRET before using admin sessions.")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def authenticate_admin(username: str, password: str) -> bool:
    if not ADMIN_PASSWORD:
        raise RuntimeError("ADMIN_PASSWORD is not configured.")
    return (
        hmac.compare_digest(username, ADMIN_USERNAME)
        and hmac.compare_digest(password, ADMIN_PASSWORD)
    )


def create_session(username: str) -> str:
    """Create an HMAC-signed session that works across restarts/instances."""
    payload = {
        "username": username,
        "expires_at": int(time.time()) + SESSION_MAX_AGE_SECONDS,
        "nonce": secrets.token_urlsafe(16),
    }
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        _signing_key(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def delete_session(session_id: str) -> None:
    """Stateless sessions are invalidated client-side by deleting the cookie.

    A copied token remains valid until expiry. Use a shared server-side session
    store if immediate token revocation is a hard requirement.
    """
    return None


def get_session(session_id: Optional[str]) -> Optional[dict]:
    if not session_id or "." not in session_id:
        return None
    try:
        encoded_payload, encoded_signature = session_id.rsplit(".", 1)
        received_signature = _b64decode(encoded_signature)
        expected_signature = hmac.new(
            _signing_key(),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(received_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
        username = payload.get("username")
        expires_at = int(payload.get("expires_at", 0))
        if not isinstance(username, str) or not username or expires_at <= int(time.time()):
            return None
        return {
            "username": username,
            "expires_at": expires_at,
        }
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
        return None


def get_current_admin(request: Request) -> dict:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return {
        "session_id": session_id,
        "username": session.get("username"),
    }


def get_optional_current_admin(request: Request) -> Optional[dict]:
    """Return the admin session when present, otherwise None."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(session_id)
    if not session:
        return None
    return {
        "session_id": session_id,
        "username": session.get("username"),
    }
