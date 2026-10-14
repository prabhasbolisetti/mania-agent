"""Regression tests for restart-safe admin session cookies."""

import os

os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-for-tests-only")
os.environ.setdefault("SESSION_SECRET", "test-session-signing-secret")

from app.api.auth import create_session, get_session  # noqa: E402


def test_signed_session_round_trip():
    token = create_session("test-admin")
    session = get_session(token)

    assert session is not None
    assert session["username"] == "test-admin"
    assert session["expires_at"] > 0


def test_signed_session_rejects_tampering():
    token = create_session("test-admin")
    encoded_payload, encoded_signature = token.split(".", 1)
    tampered = f"{encoded_payload}x.{encoded_signature}"

    assert get_session(tampered) is None


def test_signed_session_rejects_missing_cookie():
    assert get_session(None) is None
    assert get_session("") is None
