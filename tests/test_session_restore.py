"""Regression tests for recovering persisted agent sessions after a restart."""

import os
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-only-not-a-real-key")

try:
    import supabase  # noqa: F401
except ModuleNotFoundError:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = object
    supabase_stub.create_client = lambda *args, **kwargs: object()
    sys.modules["supabase"] = supabase_stub

from app.agent import conversation  # noqa: E402


def test_restore_session_recovers_customer_and_conversation(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        conversation,
        "get_latest_lead_by_phone",
        lambda phone: {
            "lead_id": "lead-123",
            "customer": {
                "phone_number": phone,
                "product_interest": "iPhone 15",
            },
            "qualification": {
                "score": 28,
                "status": "low",
                "reasons": ["product interest stated"],
            },
            "intent": {
                "score": 28,
                "status": "low",
                "signals": ["product interest stated"],
                "session_status": "active",
            },
            "conversation_status": "active",
            "last_activity": now,
            "conversation_history": [
                {"turn": 1, "message": "iPhone 15 kavali", "sender_type": "customer"}
            ],
        },
    )
    conversation.sessions.pop("wa_919000000000", None)

    session = conversation._restore_session_from_storage("wa_919000000000")

    assert session is not None
    assert session["record_id"] == "lead-123"
    assert session["customer"]["phone_number"] == "919000000000"
    assert session["customer"]["product_interest"] == "iPhone 15"
    assert session["asked_field"] == "budget"
    assert session["lead_saved"] is False
    assert session["conversation_history"][0]["message"] == "iPhone 15 kavali"

    conversation.sessions.pop("wa_919000000000", None)


def test_restore_session_does_not_resume_abandoned_conversation(monkeypatch):
    monkeypatch.setattr(
        conversation,
        "get_latest_lead_by_phone",
        lambda phone: {
            "lead_id": "lead-old",
            "customer": {"phone_number": phone},
            "qualification": {"score": 12, "status": "low", "reasons": []},
            "intent": {"score": 12, "status": "low", "signals": [], "session_status": "abandoned"},
            "conversation_status": "abandoned",
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "conversation_history": [],
        },
    )
    conversation.sessions.pop("wa_919000000001", None)

    assert conversation._restore_session_from_storage("wa_919000000001") is None
    assert "wa_919000000001" not in conversation.sessions
