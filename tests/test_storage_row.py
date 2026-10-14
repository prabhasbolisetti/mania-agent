"""Regression tests for the lead row builder (no Supabase network calls)."""

import os
import sys
import types

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-only-not-a-real-key")

# The row builder does not need Supabase. Allow this focused test to run in a
# clean environment before the production dependencies have been installed.
try:
    import supabase  # noqa: F401
except ModuleNotFoundError:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = object
    supabase_stub.create_client = lambda *args, **kwargs: object()
    sys.modules["supabase"] = supabase_stub

from app.database.storage import _build_lead_row  # noqa: E402


def test_build_lead_row_preserves_abandoned_session_status():
    row = _build_lead_row(
        customer={"phone_number": "919000000000", "product_interest": "iPhone 15"},
        qualification={"score": 55, "status": "medium", "reasons": ["budget stated"]},
        intent={"score": 55, "status": "medium", "signals": ["budget stated"]},
        status="abandoned",
    )

    assert row["phone_number"] == "919000000000"
    assert row["intent_score"] == 55
    assert row["intent"]["session_status"] == "abandoned"


def test_build_lead_row_defaults_to_completed_status():
    row = _build_lead_row(
        customer={"phone_number": "919000000000"},
        qualification={"score": 0, "status": "low", "reasons": []},
        intent=None,
    )

    assert row["intent"]["session_status"] == "completed"
    assert row["intent"]["score"] == 0
