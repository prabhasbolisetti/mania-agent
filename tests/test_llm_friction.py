"""Customer-friction handling: grounded LLM fallback, scope gate, repeat detection.

The model is always stubbed here. These tests prove the wiring and the safety
validator; they do not prove real provider quality (verify that on Render).
"""

import json
import os
import sys
import types

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-only-not-a-real-key")

try:
    import supabase  # noqa: F401
except ModuleNotFoundError:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = object
    supabase_stub.create_client = lambda *args, **kwargs: object()
    sys.modules["supabase"] = supabase_stub

from app.agent import conversation, llm  # noqa: E402
from app.catalog import product_lookup  # noqa: E402

STORE_URL = conversation.MANIA_STORE_MAPS_URL
CATALOG = [
    {
        "product_name": "Apple iPhone 15 128GB Black",
        "product_url": "https://example.test/iphone-15-black",
        "price_inr": 42500,
        "price_needs_verification": False,
        "stock_status": "in_stock",
        "categories": ["Mobiles"],
        "attributes": {"storage": "128GB", "color": "Black", "battery_health": 90, "condition": "Used"},
        "specifications": {"storage": "128GB", "color": "Black", "battery_health": 90, "condition": "Used"},
    },
    {
        "product_name": "Apple iPhone 15 128GB Blue",
        "product_url": "https://example.test/iphone-15-blue",
        "price_inr": 44000,
        "price_needs_verification": False,
        "stock_status": "in_stock",
        "categories": ["Mobiles"],
        "attributes": {"storage": "128GB", "color": "Blue", "battery_health": 85, "condition": "Used"},
        "specifications": {"storage": "128GB", "color": "Blue", "battery_health": 85, "condition": "Used"},
    },
]


# ---------------------------------------------------------------- llm layer

def _enable_model(monkeypatch, reply):
    """Pretend a provider is configured and the model returns `reply`."""
    calls = []
    monkeypatch.setattr(llm, "_llm_provider", lambda: "openai")

    def fake_call(system_prompt, user_prompt, **kwargs):
        calls.append(user_prompt)
        return reply

    monkeypatch.setattr(llm, "_call_text_model", fake_call)
    return calls


def _ask(language="roman_telugu", message="Yem verify chesthav?", catalog=None, customer=None):
    return llm.answer_unhandled_message(
        customer_message=message,
        language=language,
        customer=customer or {"product_interest": "iPhone 15"},
        conversation_history=[],
        catalog_context=catalog if catalog is not None else [
            {"product_name": "Apple iPhone 15 128GB Black", "price_inr": 42500,
             "battery_health": 90, "product_url": "https://example.test/iphone-15-black"}
        ],
        store_facts={"store_address": "Beside MGB Mall, Nellore", "google_maps": STORE_URL},
    )


def test_provider_none_never_calls_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*args, **kwargs):
        raise AssertionError("model must not be called when LLM_PROVIDER=none")

    monkeypatch.setattr(llm, "_call_text_model", boom)
    assert _ask() is None


def test_valid_grounded_roman_telugu_reply_is_returned(monkeypatch):
    _enable_model(monkeypatch, "Bro, catalog lo iPhone 15 128GB Black ₹42,500 ki undhi, battery health 90%. Inka em telusukovali?")
    assert "42,500" in _ask()


def test_english_reply_is_rejected(monkeypatch):
    _enable_model(monkeypatch, "The iPhone 15 is available for you today, please visit the store.")
    assert _ask() is None


def test_invented_price_is_rejected(monkeypatch):
    _enable_model(monkeypatch, "Bro, iPhone 15 ₹39,999 ki undhi, meeku kavala?")
    assert _ask() is None


def test_invented_battery_health_is_rejected(monkeypatch):
    _enable_model(monkeypatch, "Bro, aa phone battery health 97% undhi, meeku kavala?")
    assert _ask() is None


def test_invented_url_is_rejected_but_known_url_is_allowed(monkeypatch):
    _enable_model(monkeypatch, "Bro, ikkada chudandi https://evil.example/pay meeku kavala?")
    assert _ask() is None
    _enable_model(monkeypatch, f"Bro, maa store ikkada undhi {STORE_URL} meeku kavala?")
    assert _ask(message="Meeru ekkada untaru?") is not None


def test_fake_action_claims_are_rejected(monkeypatch):
    _enable_model(monkeypatch, "Sare bro, mee phone reserve chesam, team ki notify chesam.")
    assert _ask() is None


def test_internal_scoring_language_is_rejected(monkeypatch):
    _enable_model(monkeypatch, "Bro mee lead score high undhi, meeku kavala?")
    assert _ask() is None


def test_language_lock_is_enforced_both_ways(monkeypatch):
    _enable_model(monkeypatch, "Bro, meeku ye phone kavali cheppandi?")
    assert _ask(language="telugu") is None
    _enable_model(monkeypatch, "మీకు ఏ ఫోన్ కావాలి చెప్పండి?")
    assert _ask(language="telugu") is not None
    assert _ask(language="roman_telugu") is None


def test_model_failure_and_empty_reply_return_none(monkeypatch):
    _enable_model(monkeypatch, None)
    assert _ask() is None
    _enable_model(monkeypatch, "   ")
    assert _ask() is None


def test_oversized_reply_is_rejected(monkeypatch):
    _enable_model(monkeypatch, "meeku " * 400)
    assert _ask() is None


def test_prompt_does_not_leak_contact_details(monkeypatch):
    calls = _enable_model(monkeypatch, "Bro, meeku ye phone kavali cheppandi?")
    _ask(customer={"product_interest": "iPhone 15", "phone_number": "919999900000"})
    assert calls and "919999900000" not in calls[0]


def test_has_concrete_signal():
    assert llm.has_concrete_signal({"budget_max": 45000})
    assert llm.has_concrete_signal({"purchase_timeline": "this weekend"})
    assert llm.has_concrete_signal({"_confirmed": True})
    assert not llm.has_concrete_signal({})
    assert not llm.has_concrete_signal({"budget": None, "storage": ""})
    assert not llm.has_concrete_signal(None)


# ------------------------------------------------------- conversation layer

def _setup_conversation(monkeypatch, tmp_path, model_reply=None):
    catalog_path = tmp_path / "mobiles.json"
    catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
    monkeypatch.setattr(product_lookup, "CATALOG_PATH", catalog_path)
    monkeypatch.setattr(conversation, "save_lead", lambda **kw: {"lead_id": "lead-test"})
    monkeypatch.setattr(conversation, "update_lead", lambda **kw: {"lead_id": kw.get("lead_id")})
    monkeypatch.setattr(conversation, "get_latest_lead_by_phone", lambda phone: None)
    monkeypatch.setenv("LLM_PROVIDER", "none")
    seen = {"calls": 0, "languages": []}

    def fake_llm(**kwargs):
        seen["calls"] += 1
        seen["languages"].append(kwargs.get("language"))
        return model_reply

    monkeypatch.setattr(conversation, "answer_unhandled_message", fake_llm)
    return seen


def _say(customer_id, *messages):
    reply = None
    for message in messages:
        reply = conversation.process_message(customer_id, message)
    return reply


def test_followup_budget_is_stored_instead_of_rejected(monkeypatch, tmp_path):
    _setup_conversation(monkeypatch, tmp_path, model_reply=None)
    result = _say("wa_919000000101", "iPhone 15 price entha bro?", "45k varaku.")
    assert "maatrame help cheyyagalanu" not in result["message"]
    assert result["customer"].get("budget_amount") == 45000


def test_explicit_off_topic_never_calls_llm(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="Bro, meeku ye phone kavali?")
    result = _say("wa_919000000102", "iPhone 15 price entha bro?", "write a poem for me")
    assert "maatrame help cheyyagalanu" in result["message"]
    assert seen["calls"] == 0


def test_unclassified_message_uses_llm_reply(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="Bro, catalog price tho match chesthanu. Inka em telusukovali?")
    result = _say("wa_919000000103", "iPhone 15 price entha bro?", "Yem verify chesthav?")
    assert result["message"] == "Bro, catalog price tho match chesthanu. Inka em telusukovali?"
    assert seen["calls"] == 1


def test_unclassified_message_falls_back_to_clarification_not_deflection(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply=None)
    result = _say("wa_919000000104", "iPhone 15 price entha bro?", "Yem verify chesthav?")
    assert "maatrame help cheyyagalanu" not in result["message"]
    assert "iPhone 15" in result["message"]
    assert seen["calls"] == 1


def test_store_location_is_answered_without_llm(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="should not be used")
    result = _say("wa_919000000105", "iPhone 15 price entha bro?", "Meeru ekkada untaru?")
    assert conversation.MANIA_STORE_ADDRESS in result["message"]
    assert seen["calls"] == 0


def test_telugu_session_passes_locked_language_to_llm(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="మీకు ఏ ఫోన్ కావాలి చెప్పండి?")
    _say("wa_919000000106", "ఐఫోన్ 15 ధర ఎంత?", "మీరు ఏం verify చేస్తారు?")
    assert seen["calls"] >= 1
    assert set(seen["languages"]) == {"telugu"}


def test_session_cap_limits_model_calls(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="Bro, meeku ye phone kavali cheppandi?")
    monkeypatch.setattr(conversation, "FRICTION_LLM_MAX_PER_SESSION", 1)
    result = _say(
        "wa_919000000107",
        "iPhone 15 price entha bro?",
        "Yem verify chesthav?",
        "Nenu tarvatha cheptha.",
    )
    assert seen["calls"] == 1
    assert "maatrame help cheyyagalanu" not in result["message"]


def _force_same_flow_reply(monkeypatch, text):
    """Make the rule-based flow answer every message with the same text."""

    def fake_impl(customer_id, message):
        session = conversation._get_session(customer_id)
        session.setdefault("conversation_history", []).append(
            {"turn": len(session["conversation_history"]) + 1, "message": message}
        )
        return {
            "customer_id": customer_id, "message": text,
            "customer": session.get("customer", {}), "qualification": None,
            "intent": None, "intent_history": [], "lead_id": None, "lead_saved": False,
        }

    monkeypatch.setattr(conversation, "_process_message_impl", fake_impl)


REPEATED = "iPhone 15 ki catalog lo dorikina options: Apple iPhone 15 128GB Black ₹42,500. Ye option kavali bro?"


def test_repeated_reply_to_new_message_triggers_llm(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="Bro, battery health 90% catalog lo undhi. Inka em telusukovali?")
    _force_same_flow_reply(monkeypatch, REPEATED)
    first = _say("wa_919000000108", "iPhone 15 price entha bro?")
    assert first["message"] == REPEATED and seen["calls"] == 0
    second = _say("wa_919000000108", "Battery health ante enti bro?")
    assert seen["calls"] == 1
    assert second["message"].startswith("Bro, battery health 90%")


def test_repeated_reply_keeps_rule_reply_when_llm_unavailable(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply=None)
    _force_same_flow_reply(monkeypatch, REPEATED)
    _say("wa_919000000110", "iPhone 15 price entha bro?")
    second = _say("wa_919000000110", "Battery health ante enti bro?")
    assert seen["calls"] == 1
    assert second["message"] == REPEATED


def test_resent_identical_message_is_not_treated_as_friction(monkeypatch, tmp_path):
    seen = _setup_conversation(monkeypatch, tmp_path, model_reply="should not be used")
    _force_same_flow_reply(monkeypatch, REPEATED)
    _say("wa_919000000109", "iPhone 15 price entha bro?")
    second = _say("wa_919000000109", "iPhone 15 price entha bro?")
    assert seen["calls"] == 0
    assert second["message"] == REPEATED