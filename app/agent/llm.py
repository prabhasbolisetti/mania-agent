import json
import os
import re
import time
import threading
from collections import Counter
from typing import Any, Optional

import requests
from pydantic import BaseModel, ConfigDict

try:
    from ollama import chat as ollama_chat
except ImportError:  # Ollama is optional; API providers are preferred.
    ollama_chat = None

from app.agent import english, roman_telugu, telugu
from app.config import PROJECT_ROOT

from app.agent.prompts import (
    BUSINESS_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    build_customer_response_prompt,
    build_extraction_prompt,
)

MODEL_NAME = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
_MODEL_FAILURE_WARNED = False
_MODEL_RETRY_AFTER = 0.0

# Process-local LLM usage counters. Render logs are the durable audit trail;
# these counters reset whenever this worker restarts.
_LLM_USAGE_LOCK = threading.Lock()
_LLM_USAGE = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "by_provider": Counter(),
    "by_model": Counter(),
}
_LLM_CALL_SEQUENCE = 0


def get_llm_usage_stats() -> dict:
    """Return a thread-safe snapshot of this worker's LLM request counters."""
    with _LLM_USAGE_LOCK:
        return {
            "total_requests": _LLM_USAGE["total_requests"],
            "successful_requests": _LLM_USAGE["successful_requests"],
            "failed_requests": _LLM_USAGE["failed_requests"],
            "input_tokens": _LLM_USAGE["input_tokens"],
            "output_tokens": _LLM_USAGE["output_tokens"],
            "by_provider": dict(_LLM_USAGE["by_provider"]),
            "by_model": dict(_LLM_USAGE["by_model"]),
        }


def _llm_request_started(provider: str, model: str) -> tuple[str, float]:
    """Count one actual outbound model request, including requests that fail."""
    global _LLM_CALL_SEQUENCE
    started = time.monotonic()
    with _LLM_USAGE_LOCK:
        _LLM_CALL_SEQUENCE += 1
        call_id = f"llm-{_LLM_CALL_SEQUENCE}"
        _LLM_USAGE["total_requests"] += 1
        _LLM_USAGE["by_provider"][provider] += 1
        _LLM_USAGE["by_model"][model] += 1
        total = _LLM_USAGE["total_requests"]
    print(
        f"LLM_USAGE event=request_started call_id={call_id} "
        f"provider={provider} model={model} worker_total={total}"
    )
    return call_id, started


def _llm_request_finished(
    call_id: str,
    provider: str,
    model: str,
    started: float,
    *,
    success: bool,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    """Record request outcome and token counts when the provider returns them."""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    with _LLM_USAGE_LOCK:
        key = "successful_requests" if success else "failed_requests"
        _LLM_USAGE[key] += 1
        if input_tokens is not None:
            _LLM_USAGE["input_tokens"] += max(0, int(input_tokens))
        if output_tokens is not None:
            _LLM_USAGE["output_tokens"] += max(0, int(output_tokens))
    print(
        f"LLM_USAGE event=request_finished call_id={call_id} "
        f"provider={provider} model={model} "
        f"status={'success' if success else 'failed'} duration_ms={elapsed_ms} "
        f"input_tokens={input_tokens if input_tokens is not None else 'unknown'} "
        f"output_tokens={output_tokens if output_tokens is not None else 'unknown'}"
    )


def _llm_provider() -> str:
    """Return the explicitly selected provider when its credentials are available."""
    requested = os.getenv("LLM_PROVIDER", "").strip().lower()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    ollama_enabled = os.getenv("OLLAMA_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }

    if requested == "gemini":
        return "gemini" if gemini_key else "none"

    if requested == "openai":
        return "openai" if openai_key else "none"

    if requested == "ollama":
        return (
            "ollama"
            if ollama_enabled and ollama_chat is not None
            else "none"
        )

    if requested in {"none", "disabled", "off"}:
        return "none"

    # Do not automatically activate a paid provider.
    if ollama_enabled and ollama_chat is not None:
        return "ollama"

    return "none"



def _call_text_model(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> Optional[str]:
    """Call the configured Gemini, OpenAI-compatible API, or enabled Ollama model."""
    global _MODEL_FAILURE_WARNED, _MODEL_RETRY_AFTER

    provider = _llm_provider()
    if provider == "none":
        if not _MODEL_FAILURE_WARNED:
            print(
                "No language model is active. Check LLM_PROVIDER, "
                "the selected provider's API key, OLLAMA_ENABLED, and .env settings."
            )
            _MODEL_FAILURE_WARNED = True
        return None

    if time.monotonic() < _MODEL_RETRY_AFTER:
        return None

    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
    active_call = None

    try:
        if provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

            if not api_key or not model:
                print("Gemini is selected, but GEMINI_API_KEY or GEMINI_MODEL is missing.")
                return None

            payload = {
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user_prompt}],
                    }
                ],
                "generationConfig": {
                    "temperature": temperature,
                },
            }

            if json_mode:
                payload["generationConfig"]["responseMimeType"] = "application/json"

            call_id, call_started = _llm_request_started(provider, model)
            active_call = (call_id, provider, model, call_started)
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usageMetadata") or {}
            _llm_request_finished(
                call_id, provider, model, call_started, success=True,
                input_tokens=usage.get("promptTokenCount"),
                output_tokens=usage.get("candidatesTokenCount"),
            )
            active_call = None

            candidates = data.get("candidates") or []
            if not candidates:
                # Some blocked/empty responses include useful feedback metadata.
                feedback = data.get("promptFeedback", {})
                block_reason = feedback.get("blockReason")
                if block_reason:
                    print("Gemini returned no candidate; prompt block reason:", block_reason)
                return None

            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ).strip()
            return text or None

        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            base_url = os.getenv(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/")
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            payload = {
                "model": model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            call_id, call_started = _llm_request_started(provider, model)
            active_call = (call_id, provider, model, call_started)
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            _llm_request_finished(
                call_id, provider, model, call_started, success=True,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
            active_call = None
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return str(text).strip() or None

        if provider == "ollama" and ollama_chat is not None:
            kwargs = {
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {"temperature": temperature},
            }
            if json_mode:
                kwargs["format"] = "json"

            call_id, call_started = _llm_request_started(provider, MODEL_NAME)
            active_call = (call_id, provider, MODEL_NAME, call_started)
            result = ollama_chat(**kwargs)
            input_tokens = getattr(result, "prompt_eval_count", None)
            output_tokens = getattr(result, "eval_count", None)
            _llm_request_finished(
                call_id, provider, MODEL_NAME, call_started, success=True,
                input_tokens=input_tokens, output_tokens=output_tokens,
            )
            active_call = None
            return str(result.message.content or "").strip() or None

    except requests.HTTPError as exc:
        if active_call is not None:
            _llm_request_finished(*active_call[:3], active_call[3], success=False)
            active_call = None
        _MODEL_RETRY_AFTER = time.monotonic() + 30
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        # Do not log request headers or API keys. The response body is intentionally
        # omitted because provider error payloads may contain sensitive request details.
        print(f"Language model HTTP request failed (status {status_code}).")
        if not _MODEL_FAILURE_WARNED:
            _MODEL_FAILURE_WARNED = True
    except Exception as exc:
        if active_call is not None:
            _llm_request_finished(*active_call[:3], active_call[3], success=False)
            active_call = None
        _MODEL_RETRY_AFTER = time.monotonic() + 30
        if not _MODEL_FAILURE_WARNED:
            print("Language model unavailable; using safe fallback:", type(exc).__name__)
            _MODEL_FAILURE_WARNED = True

    return None


class LeadExtraction(BaseModel):

    model_config = ConfigDict(extra="ignore")

    product_interest: Optional[str] = None

    storage: Optional[str] = None

    budget_amount: Optional[int] = None

    budget_min: Optional[int] = None

    budget_max: Optional[int] = None

    budget_status: Optional[str] = None

    purchase_timeline: Optional[str] = None

    condition: Optional[str] = None

    payment_method: Optional[str] = None

    location: Optional[str] = None

    battery_health_min: Optional[int] = None

    preferences: Optional[list[str]] = None

    use_case: Optional[list[str]] = None

    comparison_brands: Optional[list[str]] = None

    product_question: Optional[str] = None

FIELDS = list(LeadExtraction.model_fields.keys())

# ---------------------------------------------------------

# GENERIC PRODUCT VALUES

# ---------------------------------------------------------

GENERIC_PRODUCTS = {

    "phone",

    "mobile",

    "smartphone",

    "iphone",

    "android phone",

    "google phone",

    "samsung phone",

    "oneplus phone",

    "realme phone",

    "oppo phone",

    "vivo phone",

    "a phone",

    "a mobile",

    "any phone",

}

# ---------------------------------------------------------

# CORRECTION CUES

# ---------------------------------------------------------

CORRECTION_CUES = (
    # Only genuine revision language belongs here. Statements such as
    # "I want", "I need", and "my budget is" provide new information.
    "actually", "i mean", "i meant", "correction", "instead",
    "not that", "rather", "change it to", "change it", "change that",
    "make it", "update it", "correct that", "no i mean",
    "change cheyyi", "change cheyandi", "marchu", "marchandi", "marcheyi",
    "అంటే కాదు", "సరిచేయండి", "మార్చండి", "మార్చు",
)

# ---------------------------------------------------------

# LANGUAGE DETECTION

# ---------------------------------------------------------

def detect_language(message: str) -> str:

    """

    Detect the language/style of the CURRENT customer message.

    Priority:

        1\\. Telugu Unicode -> Telugu

        2\\. Strong Romanized Telugu evidence -> Roman Telugu

        3\\. Otherwise -> English

    Roman Telugu is intentionally detected using a combination of

    vocabulary, suffixes and common WhatsApp phrases.

    """

    if not message or not message.strip():

        return "english"

    text = message.strip()

    lower = text.lower()

    # Telugu Unicode always wins.

    if re.search(r"[\u0C00-\u0C7F]", text):

        return "telugu"

    roman_words = {
        # Genuine Roman-Telugu conversational vocabulary.
        "avunu", "avvunu", "avunandi", "sare", "saree", "aithe", "ayithe",
        "mari", "mariy", "inka", "inkaenti", "inkenti", "enti", "entandi",
        "emiti", "emundi", "emundhi", "undha", "unda", "undaa", "undi",
        "unnava", "unnaya", "unnai", "unnayi", "levu", "ledu", "ledhu",
        "parledu", "parledhu", "oddu", "vaddu", "vaddhu", "kaadu", "kadu",
        "enduku", "endhukku", "ela", "elaa", "elanti", "yela",
        "eppudu", "epudu", "eppatiki", "ekkada", "ekkad", "ikkada", "akkada",
        "ikkad", "akkad", "yevaru", "evaru", "evadu", "evarki", "evariki",
        "naaku", "naku", "naa", "na", "mana", "mee", "mi", "meeku", "miku",
        "naadi", "naadhi", "meedi", "meedhi", "kosam", "kosame", "daggara",
        "daggarlo", "deggara", "deggarlo", "nunchi", "nundi", "nundhi", "varaku",
        "lopu", "lopala", "daaka", "daka", "tho", "to", "ki", "ku", "lo",
        "la", "ga", "ani", "anta", "antha", "ayina", "aina",
        "kuda", "kooda", "matrame", "maatrame", "koncham", "konchem", "chala",
        "chaala", "bagundi", "bagundhi", "baagundi", "baagundhi",
        "manchi", "kotha", "kotta", "kottha", "paatha", "pata", "paata",
        "konali", "konalanundi", "konalani", "teesukovali", "teeskovali",
        "kavali", "kavala", "ivvali", "ivvandi", "cheppu", "cheppandi",
        "chupinchu", "chupinchandi", "telusu", "teliyadu", "telidu",
        "telusukovali", "chustunna", "chusthunna", "choostunna", "choosthunna",
        "chesukovali", "cheskuntanu", "konukovali", "avvaledu",
        "anukuntunna", "anukuntunnanu", "rendu", "oka", "okati", "moodu",
        "nalugu", "aidu", "aaru", "edu", "enimidi", "tommidi", "padi",
        "repu", "repatiki", "ivala", "ivvala", "eeroju", "eeroje", "ippudu",
        "ippude", "ventane", "varam", "vaaram", "varallo", "vaarallo",
        "rojullo", "vela", "velu", "nundi", "nundhi",
        "ante", "em", "emi", "evaru", "mee", "meeku", "meeeku",
        "akka", "ra", "randi", "avuna",
    }
    tokens = re.findall(r"[a-zA-Z]+", lower)
    token_set = set(tokens)

    strong_hits = len(token_set.intersection(roman_words))

    roman_suffixes = (
        "kavali", "kavala", "undha", "unda", "undaa", "undi",
        "ledu", "ledhu", "lopu", "lopala", "varaku", "varake",
        "nunchi", "nundi", "nundhi", "kosam", "chesanu",
        "chestunna", "chustunna", "chusthunna", "konali",
        "konukovali", "konalanukuntunna", "cheppu", "cheppandi",
        "chupinchu", "chudali", "teliyadu", "parledu", "vaddu",
        "kaadu", "kadu", "avvaledu", "anukuntunna", "anukuntunnanu",
    )

    suffix_hit = any(
        token.endswith(suffix) and len(token) > len(suffix) + 1
        for token in tokens
        for suffix in roman_suffixes
    )

    phrase_hit = any(
        phrase in lower
        for phrase in (
            "na budget", "naa budget", "mee budget",
            "naaku kavali", "naku kavali", "phone kavali",
            "mobile kavali", "entha bro", "entha undi",
            "entha untundi", "undha bro", "unda bro",
            "emaina unda", "emaina undha", "ee week",
            "ee weekend", "rendu weeks", "oka week",
            "oka varam", "oka nela", "lopu kavali",
            "konali ani", "konalanundi", "konalani undi",
            "chustunna", "compare chestunna",
            "research chestunna", "decide avvaledu",
        )
    )

    english_strong_phrases = (
        "in a week", "within a week", "in one week", "next week", "this week",
        "in a month", "within a month", "next month", "this month", "this weekend",
        "by tomorrow", "tomorrow", "today", "right now", "brand new", "new phone",
        "used phone", "second hand", "refurbished", "how much", "how many", "how long",
        "where can", "when can", "i want", "i need", "i would like", "can you",
        "could you", "please tell", "please show", "do you have", "is there",
        "which one", "tell me", "show me", "i am looking", "i'm looking",
        "i want to buy", "i need to buy", "looking for", "interested in",
    )
    if any(p in lower for p in english_strong_phrases):
        return "english"
    if strong_hits >= 1 or phrase_hit or suffix_hit:
        return "roman_telugu"
    return "english"

def _explicit_condition(message: str) -> bool:

    text = (message or "").lower()

    cues = (

        "brand new",

        "brand-new",

        "sealed",

        "sealed piece",

        "sealed pack",

        "new phone",
        "కొత్తది",
        "కొత్త పీస్",
        "కొత్త ఫోన్",
        "కొత్త మొబైల్",
        "న్యూ ఫోన్",

        "used",

        "second hand",

        "second-hand",

        "refurbished",

        "pre-owned",

        "kotha phone",

        "kotta phone",

        "kothaga",

        "pata phone",

        "paatha phone",

        "కొత్త ఫోన్",

        "కొత్త మొబైల్",

        "సీల్డ్ పీస్",

        "సీల్డ్ ప్యాక్",

        "బ్రాండ్ న్యూ",

        "పాత ఫోన్",

        "పాతది",

        "యూజ్డ్",

        "రిఫర్బిష్డ్",

        "సెకండ్ హ్యాండ్",

    )

    return any(cue in text for cue in cues)

# ---------------------------------------------------------

# EXPLICIT PAYMENT

# ---------------------------------------------------------

def _explicit_payment(message: str) -> bool:

    text = (message or "").lower()

    cues = (

        "cash",
        "క్యాష్",
        "ఫోన్‌పే",
        "ఫోన్ పే",
        "క్రెడిట్ కార్డ్",
        "డెబిట్ కార్డ్",

        "upi",

        "gpay",

        "google pay",

        "phonepe",

        "paytm",

        "bhim",

        "emi",

        "installment",

        "installments",

        "card",

        "credit card",

        "debit card",

        "bank transfer",

        "net banking",

        "నగదు",

        "యూపీఐ",

        "గూగుల్ పే",

        "ఫోన్ పే",

        "పేటీఎం",

        "ఈఎంఐ",

        "వాయిదాలు",

        "కార్డు",

        "కార్డ్",

        "బ్యాంక్ ట్రాన్స్‌ఫర్",

        "బ్యాంక్ ట్రాన్స్ఫర్",

        "నెట్ బ్యాంకింగ్",

    )

    return any(cue in text for cue in cues)

# ---------------------------------------------------------

# CORRECTION

# ---------------------------------------------------------

def _has_correction_cue(message: str) -> bool:
    text = re.sub(r"\s+", " ", (message or "").lower()).strip()
    return any(_phrase_matches(text, cue) for cue in CORRECTION_CUES)

# ---------------------------------------------------------

# PRODUCT HELPERS

# ---------------------------------------------------------

def _is_weak_product(value: Any) -> bool:

    """

    Return True when the extracted product is generic/ambiguous.

    """

    if not isinstance(value, str):

        return True

    normalized = re.sub(

        r"\s+",

        " ",

        value.strip().lower(),

    )

    if not normalized:

        return True

    return normalized in GENERIC_PRODUCTS

def _clean_product_name(value: Any) -> Any:

    """

    Normalize whitespace without guessing the intended model.

    """

    if not isinstance(value, str):

        return value

    value = re.sub(

        r"\s+",

        " ",

        value,

    ).strip()

    return value or None

# ---------------------------------------------------------

# BUDGET HELPERS

# ---------------------------------------------------------

def _valid_budget_number(value: Any) -> Optional[int]:

    """

    Accept only positive, plausible INR budget values.

    """

    if value is None or isinstance(value, bool):

        return None

    try:

        number = int(value)

    except (TypeError, ValueError, OverflowError):

        return None

    if number <= 0 or number > 10_000_000:

        return None

    return number

def _normalize_budget(result: dict) -> None:

    """

    Keep budget_amount, budget_min, budget_max and budget

    consistent.

    """

    amount = _valid_budget_number(

        result.get("budget_amount")

    )

    minimum = _valid_budget_number(

        result.get("budget_min")

    )

    maximum = _valid_budget_number(

        result.get("budget_max")

    )

    # Proper range.

    if minimum is not None and maximum is not None:

        if minimum <= maximum:

            result["budget_min"] = minimum

            result["budget_max"] = maximum

            result["budget_amount"] = None

            result["budget"] = {

                "min": minimum,

                "max": maximum,

            }

            return

    # Lower bound.

    if minimum is not None and maximum is None:

        result["budget_min"] = minimum

        result["budget_max"] = None

        result["budget_amount"] = amount

        result["budget"] = (

            amount

            if amount is not None

            else {"min": minimum}

        )

        return

    # Upper bound.

    if maximum is not None and minimum is None:

        result["budget_min"] = None

        result["budget_max"] = maximum

        result["budget_amount"] = (

            amount

            if amount is not None

            else maximum

        )

        result["budget"] = result["budget_amount"]

        return

    # Single amount.

    result["budget_min"] = None

    result["budget_max"] = None

    result["budget_amount"] = amount

    result["budget"] = amount

# ---------------------------------------------------------

# CONDITION NORMALIZATION

# ---------------------------------------------------------

def _normalize_condition(

    value: Any,

) -> Optional[str]:

    if not isinstance(value, str):

        return None

    text = value.strip().lower()

    if text in {

        "brand new",

        "brand-new",

        "new",

        "sealed",

    }:

        return "brand new"

    if text in {

        "used",

        "second hand",

        "second-hand",

        "refurbished",

        "pre-owned",

        "used/refurbished",

    }:

        return "used"

    return None

# ---------------------------------------------------------

# PAYMENT NORMALIZATION

# ---------------------------------------------------------

def _normalize_payment(

    value: Any,

) -> Optional[str]:

    if not isinstance(value, str):

        return None

    text = value.strip().lower()

    aliases = {

        "cash": "cash",

        "cash payment": "cash",

        "card": "card",

        "credit card": "card",

        "debit card": "card",

        "upi": "UPI",

        "gpay": "UPI",

        "google pay": "UPI",

        "phonepe": "UPI",

        "paytm": "UPI",

        "bhim": "UPI",

        "emi": "EMI",

        "installment": "EMI",

        "installments": "EMI",

        "bank transfer": "bank transfer",

        "net banking": "bank transfer",

    }

    return aliases.get(text)

# ---------------------------------------------------------

# FAST SHORT-REPLY EXTRACTION

# ---------------------------------------------------------

def _extract_fast_short_reply(

    message: str,

    result: dict,

) -> None:

    """

    Handle very common short WhatsApp replies without Ollama.

    Examples:

        25000

        45,000

        55k

        1.2l

    These messages are structured enough to understand

    deterministically.

    """

    text = (message or "").strip()

    if not text:

        return

    compact = text.replace(",", "").strip()

    # -----------------------------------------------------

    # Plain numeric reply

    # -----------------------------------------------------

    #

    # Example:

    #

    # Agent: Mee budget entha?

    # Customer: 25000

    #

    # This is a budget amount and does not require Ollama.

    # -----------------------------------------------------

    if re.fullmatch(

        r"\d+(?:\.\d+)?",

        compact,

    ):

        number = int(float(compact))

        if 0 < number <= 10_000_000:

            result["budget_amount"] = number

            result["budget_min"] = None

            result["budget_max"] = None

            result["budget"] = number

            return

    # -----------------------------------------------------

    # Budget shorthand

    # -----------------------------------------------------

    #

    # 45k

    # 55k

    # 1.2l

    # 2 lakh

    # -----------------------------------------------------

    match = re.fullmatch(

        r"(\d+(?:\.\d+)?)\s*"

        r"(k|l|lac|lakh|lakhs)",

        text.lower(),

    )

    if match:

        number = float(match.group(1))

        unit = match.group(2)

        if unit == "k":

            amount = int(number * 1000)

        else:

            amount = int(number * 100000)

        if 0 < amount <= 10_000_000:

            result["budget_amount"] = amount

            result["budget_min"] = None

            result["budget_max"] = None

            result["budget"] = amount

# ---------------------------------------------------------

# LLM DECISION

# ---------------------------------------------------------

def _needs_llm(

    field: str,

    value: Any,

    message: str,

) -> bool:

    """

    Use the configured language model only when deterministic extraction

    genuinely needs semantic interpretation.

    IMPORTANT:

    We do NOT call the language model merely because a field is missing.

    Normal WhatsApp messages such as:

        60000

        Hyderabad

        Cash

        Card

        128GB

        3-5 days lo

        Second hand aina parledhu

    are handled by deterministic extractors.

    The language model is reserved for:

        \\- weak/ambiguous product names

        \\- explicit corrections

    """

    if field == "product_interest" and _is_weak_product(value):

        return True

    # Only genuine revision phrases should mark every extracted field
    # as potentially ambiguous. Phrases like "I want" and "budget is"
    # describe normal new information, not necessarily a correction.
    explicit_revision_cues = (
        "actually", "i mean", "i meant", "correction", "instead",
        "not that", "rather", "change it to", "change that",
        "update it", "correct that", "no i mean",
        "ante kaadu", "kaadu", "kadu", "marchu", "marchandi",
        "change cheyyi", "change cheyandi",
        "అంటే కాదు", "కాదు", "మార్చండి", "మార్చు", "సరిచేయండి",
    )
    lower_message = re.sub(r"\s+", " ", (message or "").lower()).strip()
    if any(_phrase_matches(lower_message, cue) for cue in explicit_revision_cues):
        return True

    return False

# ---------------------------------------------------------

# DETERMINISTIC EXTRACTION

# ---------------------------------------------------------

# ---------------------------------------------------------
# BROAD WHATSAPP / ROMAN-TELUGU NORMALIZATION
# ---------------------------------------------------------

# These are intentionally broad colloquial forms. They are not meant to
# understand every possible Telugu sentence; they cover common WhatsApp
# spellings, slang, shortened words, and regional variations that appear
# in mobile-shopping conversations.
ROMAN_TELUGU_NUMBER_WORDS = {
    "oka": 1, "okati": 1, "one": 1,
    "rendu": 2, "two": 2,
    "moodu": 3, "three": 3,
    "nalugu": 4, "four": 4,
    "aidu": 5, "ayidu": 5, "five": 5,
    "aaru": 6, "aru": 6, "six": 6,
    "edu": 7, "yedu": 7, "seven": 7,
    "enimidi": 8, "enimidhi": 8, "eight": 8,
    "tommidi": 9, "tommidhi": 9, "nine": 9,
    "padi": 10, "ten": 10,
}

# Common Roman-Telugu variants. Keep these as token fragments rather than
# requiring one exact spelling.
TIMELINE_IMMEDIATE_TERMS = (
    "eeroju", "ee roju", "eeroje", "ee roje", "ivala", "ivale",
    "ivalae", "ivale", "ippude", "ippudu", "ventane", "taksana",
    "taksaname", "immediate", "asap", "right now", "today",
    "ఈరోజు", "ఈరోజే", "ఈ రోజు", "ఇప్పుడే", "వెంటనే",
)
TIMELINE_TOMORROW_TERMS = (
    "repu", "repatiki", "repu lopu", "repu varaku", "tomorrow",
    "రేపు", "రేపటికి", "రేపులోపు",
)
TIMELINE_WEEK_TERMS = (
    "ee week", "ee week lo", "ee varam", "ee varam lo", "ee vaaram",
    "ee vaaram lo", "oka week", "oka varam", "oka vaaram",
    "one week", "in a week", "within a week", "next week",
    "vache week", "vacche week", "week lopu", "week lo",
    "వారం", "ఒక వారం", "ఈ వారం", "వచ్చే వారం",
)
TIMELINE_MONTH_TERMS = (
    "oka month", "one month", "in a month", "within a month",
    "oka nela", "oka nela lo", "oka nela lopu", "nelalo",
    "next month", "vache nela", "vacche nela",
    "నెలలో", "ఒక నెలలో", "నెలలోపు", "వచ్చే నెల",
)
BROWSE_TERMS = (
    "just browsing", "just checking", "research", "researching",
    "chustunna", "chusthunna", "chustunnanu", "chusthunanu",
    "telusukuntunna", "telusukuntunnanu", "ippatiki chustunna",
    "inka decide avvaledu", "decide avvaledu", "decision ledu",
    "fix avvaledu", "konali ani ledu", "ippudu konanu",
    "buy ready kaadu", "buy ready kadu",
)

def _contains_any_phrase(text: str, phrases) -> bool:
    compact = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return any(_phrase_matches(compact, phrase) for phrase in phrases)


def _phrase_matches(text: str, phrase: str) -> bool:
    """Match a phrase without accidental ASCII substring matches.

    English/Roman-Telugu cues use alphanumeric boundaries, so "y" does not
    match the y in "buying", "no" does not match inside another word, and
    "phone" does not match inside "iPhone". Telugu-script phrases use
    normalized substring matching because Indic-script word boundaries are
    not reliably represented by Python's ASCII-oriented ``\b`` semantics.
    """
    haystack = re.sub(r"\s+", " ", (text or "").lower()).strip()
    needle = re.sub(r"\s+", " ", (phrase or "").lower()).strip()
    if not haystack or not needle:
        return False
    if re.search(r"[\u0C00-\u0C7F]", needle):
        return needle in haystack
    pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
    return re.search(pattern, haystack, re.IGNORECASE) is not None

def _repair_broad_timeline(text: str, result: dict) -> None:
    """Recognize colloquial Telugu/Roman-Telugu time expressions."""

    lower = re.sub(r"\s+", " ", (text or "").lower()).strip()

    if not lower or result.get("purchase_timeline"):
        return

    # "I don't have a budget right now" is a budget objection, not a
    # statement that the customer intends to purchase immediately.
    if result.get("budget_status") in {"unavailable_now", "undecided", "prefer_not_to_say"}:
        lower = re.sub(r"\b(?:right now|now|ippudu|ippude)\b", " ", lower)
        lower = re.sub(r"\s+", " ", lower).strip()

    if _contains_any_phrase(lower, BROWSE_TERMS):
        result["purchase_timeline"] = "just browsing"
        return

    if _contains_any_phrase(lower, TIMELINE_IMMEDIATE_TERMS):
        result["purchase_timeline"] = "immediately"
        return

    if _contains_any_phrase(lower, TIMELINE_TOMORROW_TERMS):
        result["purchase_timeline"] = "tomorrow"
        return

    if _contains_any_phrase(lower, TIMELINE_WEEK_TERMS):
        result["purchase_timeline"] = "within a week"
        return

    if _contains_any_phrase(lower, TIMELINE_MONTH_TERMS):
        result["purchase_timeline"] = "within a month"
        return

    # Numeric day/week/month expressions:
    # 3 days, 3 days lo, 3 rojullo, 3-5 days, 2 weeks lo, 1 month lo.
    numeric = re.search(
        r"\b(\d+)\s*(?:-\s*(\d+)\s*)?"
        r"(?:days?|day|rojulu|rojullo|rojulalo|rojullo|"
        r"weeks?|week|varalu|varallo|vaaralu|vaarallo|"
        r"months?|month|nelalu|nelalo)"
        r"(?:\s*(?:lo|lopu|varaku|within|లో|లోపు|వరకు))?\b",
        lower,
        re.I,
    )
    if numeric:
        first = int(numeric.group(1))
        second = numeric.group(2)
        # Determine the unit from the matched phrase.
        unit_text = numeric.group(0).lower()
        if "week" in unit_text or "var" in unit_text:
            unit = "week"
        elif "month" in unit_text or "nel" in unit_text:
            unit = "month"
        else:
            unit = "day"

        if second:
            result["purchase_timeline"] = f"{first}-{int(second)} {unit}s"
        else:
            result["purchase_timeline"] = (
                f"within {first} {unit}{'' if first == 1 else 's'}"
            )
        return

    # Roman-Telugu number words:
    # oka moodu rojullo, rendu rojullo, oka rendu weeks lo, etc.
    words = "|".join(
        sorted(ROMAN_TELUGU_NUMBER_WORDS, key=len, reverse=True)
    )
    word_match = re.search(
        rf"\b(?:oka\s+)?({words})\s*"
        r"(?:days?|day|rojulu|rojullo|rojulalo|"
        r"weeks?|week|varalu|varallo|vaaralu|vaarallo|"
        r"months?|month|nelalu|nelalo)"
        r"(?:\s*(?:lo|lopu|varaku|within))?\b",
        lower,
        re.I,
    )
    if word_match:
        n = ROMAN_TELUGU_NUMBER_WORDS[word_match.group(1).lower()]
        phrase = word_match.group(0).lower()
        if "week" in phrase or "var" in phrase or "vaar" in phrase:
            unit = "week"
        elif "month" in phrase or "nel" in phrase:
            unit = "month"
        else:
            unit = "day"
        result["purchase_timeline"] = (
            f"within {n} {unit}{'' if n == 1 else 's'}"
        )
        return

    # Pure number-word colloquialisms where "days" is omitted:
    # "moodu rojullo" is handled above; this catches "moodu days".
    # Also accept "3 lo" only when a time-context word is present.
    short = re.search(
        rf"\b({words})\s+(?:lo|lopu|rojullo|rojulu|varallo|nelalo)\b",
        lower,
        re.I,
    )
    if short:
        n = ROMAN_TELUGU_NUMBER_WORDS[short.group(1).lower()]
        result["purchase_timeline"] = f"within {n} days"

def _repair_budget_status(text: str, result: dict) -> None:
    """Extract non-numeric budget states so the agent won't repeat a budget question."""
    lower = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not lower:
        return

    undecided = (
        "budget not decided", "haven't decided my budget", "have not decided my budget",
        "not decided budget", "budget is not fixed", "no fixed budget",
        "budget decide cheyyaledu", "budget inka decide cheyyaledu", "inka budget decide cheyyaledu", "budget fix cheyyaledu", "budget teliyadu",
        "budget telidu", "inka budget decide cheyyaledu",
        "బడ్జెట్ నిర్ణయించలేదు", "బడ్జెట్ ఇంకా నిర్ణయించలేదు",
        "ఇంకా బడ్జెట్ ఫిక్స్ కాలేదు",
    )
    flexible = (
        "flexible budget", "budget is flexible", "no fixed limit",
        "budget flexible ga undi", "budget flexible", "ఎంతైనా పర్లేదు",
        "బడ్జెట్ ఫ్లెక్సిబుల్",
    )
    prefer_not = (
        "prefer not to say", "don't want to share my budget",
        "do not want to share my budget", "budget cheppalenu",
        "budget cheppadam ishtam ledu", "బడ్జెట్ చెప్పదలుచుకోలేదు",
    )
    price_concern = (
        "too expensive", "price is too high", "way too expensive",
        "too costly", "very costly", "can't afford that", "cannot afford that",
        "price ekkuva", "rate ekkuva", "chala costly", "chaala costly",
        "chaala ekkuva", "చాలా ఖరీదు", "ధర ఎక్కువ", "రేటు ఎక్కువ",
    )
    unavailable = (
        "no budget", "don't have a budget", "do not have a budget",
        "i have no budget", "no money", "don't have money", "do not have money",
        "can't afford", "cannot afford", "not enough money", "not enough cash",
        "not in a position to buy", "can't spend that much", "cannot spend that much",
        "na daggara budget ledu", "naa daggara budget ledu",
        "na daggara budget ledhu", "naa daggara budget ledhu",
        "naaku budget ledu", "naku budget ledu", "budget ledu", "budget ledhu",
        "ippudu budget ledu", "ippudu budget ledhu", "na daggara dabbulu levu",
        "naa daggara dabbulu levu", "dabbulu levu", "dabbu ledu", "dabbu ledhu",
        "konadaniki dabbulu levu", "konadaniki budget ledu", "money ledu",
        "money ledhu", "afford cheyalenu", "afford cheyyalenu", "ippudu konalenu",
        "నా దగ్గర బడ్జెట్ లేదు", "బడ్జెట్ లేదు", "నా దగ్గర డబ్బులు లేవు",
        "డబ్బులు లేవు", "ఇప్పుడు కొనలేను", "కొనడానికి డబ్బులు లేవు",
    )

    status = None
    if any(phrase in lower for phrase in undecided):
        status = "undecided"
    elif any(phrase in lower for phrase in flexible):
        status = "flexible"
    elif any(phrase in lower for phrase in prefer_not):
        status = "prefer_not_to_say"
    elif any(phrase in lower for phrase in price_concern):
        status = "price_concern"
    elif any(phrase in lower for phrase in unavailable):
        status = "unavailable_now"

    if not status:
        return

    # Do not erase a clear, affirmative budget amount. But if a number was
    # accidentally interpreted from a phone model or quoted price, don't store
    # it as the customer's budget.
    explicit_budget_amount = bool(re.search(
        r"(?:my budget|budget is|budget of|budget around|budget\s*(?:₹|rs\.?)?\s*\d|"
        r"\b(?:under|below|upto|up to|less than|lopu|lopala|varaku|max(?:imum)?)\s*"
        r"(?:₹|rs\.?)?\s*\d)",
        lower,
        re.I,
    ))
    has_amount = any(
        result.get(key) is not None
        for key in ("budget_amount", "budget_min", "budget_max", "budget")
    )
    if has_amount and explicit_budget_amount and status == "price_concern":
        result["budget_status"] = status
        return
    if has_amount and not explicit_budget_amount:
        result["budget"] = None
        result["budget_amount"] = None
        result["budget_min"] = None
        result["budget_max"] = None
    if status in {"unavailable_now", "undecided", "prefer_not_to_say"}:
        result["budget"] = None
        result["budget_amount"] = None
        result["budget_min"] = None
        result["budget_max"] = None
    result["budget_status"] = status

    # A phrase like "I don't have a budget right now" does not mean
    # the customer intends to purchase immediately. Some language-specific
    # extractors over-read "right now" as a timeline.
    has_purchase_timing_context = bool(re.search(
        r"\b(?:buy|buying|purchase|purchasing|need it|want it|konali|konukovali|"
        r"konadaniki|teesukovali|purchase cheyyali)\b",
        lower,
        re.I,
    ))
    if (
        status in {"unavailable_now", "undecided", "prefer_not_to_say"}
        and not has_purchase_timing_context
        and re.search(r"\b(?:right now|now|ippudu|ippude)\b", lower)
    ):
        result["purchase_timeline"] = None


def _repair_broad_budget(text: str, result: dict) -> None:
    """Parse budget amounts/ranges without misreading product model numbers.

    Supports shorthand such as "40 to 45k" by inheriting the multiplier
    across a range when the unmarked number is clearly a shorthand amount.
    It avoids treating arbitrary ranges like "iPhone 15 to 16" as budgets.
    """
    lower = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not lower:
        return
    if result.get("budget_status") in {"unavailable_now", "undecided", "prefer_not_to_say"}:
        return

    budget_context = bool(re.search(
        r"(?:\bbudget\b|\bamount\b|\blimit\b|\bprice\b|"
        r"\bunder\b|\bbelow\b|\bupto\b|\bup to\b|"
        r"\bmaximum\b|\bmax\b|\bvaraku\b|\blopu\b|"
        r"\blopala\b|\banukuntunnam(?:u)?\b|\banukuntunna(?:nu)?\b|"
        r"₹|\brs\.?\s*|\brupees?\b|"
        r"\b(?:k|thousand|lakh|lakhs|lac|vela|velu)\b)",
        lower,
        re.I,
    ))

    # Corrections such as "Actually, my budget is 35k, not 45k" must use
    # the new/current amount (35k), not the rejected old amount (45k).
    # Do this before generic number selection, which otherwise tends to pick
    # the last amount in the sentence.
    correction_amount = re.search(
        r"(?:\bactually\b[\s,:-]*)?"
        r"(?:my\s+)?(?:budget|limit|amount)\s*(?:is|:|=|of)?\s*"
        r"(?:₹|rs\.?\s*|rupees?\s*)?"
        r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>k|thousand|lakh|lakhs|lac|l|vela|velu)?"
        r"[^.!?]{0,35}\b(?:not|instead of|rather than|wrong|old amount)\b",
        lower,
        re.I,
    )
    if correction_amount and budget_context:
        raw = correction_amount.group("number").replace(",", "")
        amount = float(raw)
        unit = (correction_amount.group("unit") or "").lower()
        if unit in {"k", "thousand", "vela", "velu"}:
            amount *= 1000
        elif unit in {"lakh", "lakhs", "lac", "l"}:
            amount *= 100000
        amount = int(amount)
        if 1000 <= amount <= 10_000_000:
            result["budget_amount"] = amount
            result["budget"] = amount
            result["budget_min"] = None
            result["budget_max"] = None
            return

    # A budget range always wins. Keep both unit groups so that "40 to 45k"
    # can be normalized to 40,000–45,000, but don't scale already-expanded
    # values such as "40000 to 45k" a second time.
    range_match = re.search(
        r"(?:₹|rs\.?\s*|rupees?\s*)?(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac|l|vela|velu)?"
        r"\s*(?:-|to|nunchi|nundi|nundhi|from)\s*"
        r"(?:₹|rs\.?\s*|rupees?\s*)?(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac|l|vela|velu)?",
        lower,
        re.I,
    )
    if range_match:
        raw_a, unit_a, raw_b, unit_b = range_match.groups()
        a_num = float(raw_a.replace(",", ""))
        b_num = float(raw_b.replace(",", ""))
        unit_a = (unit_a or "").lower()
        unit_b = (unit_b or "").lower()

        def multiplier(unit: str) -> int:
            if unit in {"k", "thousand", "vela", "velu"}:
                return 1000
            if unit in {"lakh", "lakhs", "lac", "l"}:
                return 100000
            return 1

        factor_a = multiplier(unit_a)
        factor_b = multiplier(unit_b)

        # A unit on one side may qualify the other side too: "40 to 45k",
        # "40k to 45", "1 to 1.5 lakh". Only infer for shorthand values <1000.
        if not unit_a and unit_b and a_num < 1000:
            factor_a = factor_b
        if not unit_b and unit_a and b_num < 1000:
            factor_b = factor_a

        a = int(a_num * factor_a)
        b = int(b_num * factor_b)
        has_range_unit = bool(unit_a or unit_b)
        if budget_context or has_range_unit:
            if 0 < a <= 10_000_000 and 0 < b <= 10_000_000:
                low, high = sorted((a, b))
                result["budget_min"] = low
                result["budget_max"] = high
                result["budget_amount"] = None
                result["budget"] = {"min": low, "max": high}
                return

    # "25k", "25k lopu", "25000 anukuntunna", "25 vela varaku",
    # "25000 budget", "budget 25000", "na budget 25k".
    number_pattern = re.compile(
        r"(?P<currency>₹|rs\.?\s*|rupees?\s*)?"
        r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>k|thousand|lakh|lakhs|lac|l|vela|velu)?",
        re.I,
    )
    bare_amount = re.fullmatch(
        r"\s*(?:₹|rs\.?\s*|rupees?\s*)?\d[\d,]*(?:\.\d+)?\s*"
        r"(?:k|thousand|lakh|lakhs|lac|l|vela|velu)?\s*",
        lower,
        re.I,
    )

    candidates = []
    for candidate in number_pattern.finditer(lower):
        raw_candidate = candidate.group("number").replace(",", "")
        unit_candidate = (candidate.group("unit") or "").lower()
        # Ignore phone storage/capacity numbers such as 128GB and 1TB.
        suffix = lower[candidate.end():candidate.end() + 5]
        if re.match(r"\s*(?:gb|tb|gigabytes?|mb)\b", suffix, re.I):
            continue
        numeric_value = float(raw_candidate)
        if unit_candidate in {"k", "thousand", "vela", "velu"}:
            numeric_value *= 1000
        elif unit_candidate in {"lakh", "lakhs", "lac", "l"}:
            numeric_value *= 100000
        has_currency = bool(candidate.group("currency"))
        has_unit = bool(unit_candidate)
        before = lower[max(0, candidate.start() - 55):candidate.start()]
        after = lower[candidate.end():candidate.end() + 55]
        near_budget_cue = bool(re.search(
            r"\b(budget|amount|limit|price|under|below|upto|up to|max|maximum|"
            r"varaku|lopu|lopala|anukuntunnam(?:u)?|anukuntunna(?:nu)?)\b",
            before + " " + after,
            re.I,
        ))
        # Prefer explicit currency/unit and numbers close to budget language.
        score = (4 if has_currency else 0) + (3 if has_unit else 0) + (2 if near_budget_cue else 0)
        if numeric_value >= 1000:
            score += 1
        if score:
            candidates.append((score, candidate.start(), int(numeric_value)))

    if bare_amount:
        match = number_pattern.search(lower)
        if match:
            raw = match.group("number").replace(",", "")
            unit = (match.group("unit") or "").lower()
            amount = float(raw)
            if unit in {"k", "thousand", "vela", "velu"}:
                amount *= 1000
            elif unit in {"lakh", "lakhs", "lac", "l"}:
                amount *= 100000
            amount = int(amount)
        else:
            return
    else:
        # Do not infer a budget from unrelated model/storage numbers.
        if not budget_context and not candidates:
            return
        if not candidates:
            return
        # If an explicit budget cue exists, prefer the last candidate before
        # any unrelated trailing text; score breaks ties toward currency/units.
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        amount = candidates[0][2]

    if 1000 <= amount <= 10_000_000:
        result["budget_amount"] = amount
        result["budget"] = amount
        if re.search(
            r"\b(under|below|lopu|lopala|varaku|maximum|max|upto|up to)\b",
            lower,
            re.I,
        ):
            result["budget_max"] = amount

def _repair_broad_condition_payment_storage(text: str, result: dict) -> None:
    lower = re.sub(r"\s+", " ", (text or "").lower()).strip()

    if not result.get("condition"):
        if re.search(
            r"\b(brand new|brandnew|new piece|new phone|fresh piece|fresh phone|"
            r"sealed|sealed piece|sealed pack|sealed box|factory seal|"
            r"kotha|kotta|kottha|kotthadi|kotha piece|kotha phone|"
            r"కొత్త|సీల్డ్|కొత్త ఫోన్)\b",
            lower, re.I,
        ):
            result["condition"] = "brand new"
        elif re.search(
            r"\b(used|second hand|secondhand|second-hand|pre owned|pre-owned|"
            r"preowned|refurb|refurbished|old piece|old phone|paata|paatha|"
            r"pata phone|పాత|సెకండ్ హ్యాండ్)\b",
            lower, re.I,
        ):
            result["condition"] = "Used"

    if not result.get("payment_method"):
        if re.search(
            r"\b(upi|gpay|google pay|googlepay|phonepe|phone pe|paytm|"
            r"upi lo|gpay lo|phonepe lo)\b", lower, re.I
        ):
            result["payment_method"] = "UPI"
        elif re.search(
            r"\b(credit card|debit card|card payment|card lo|card tho|"
            r"card|కార్డ్)\b", lower, re.I
        ):
            result["payment_method"] = "Card"
        elif re.search(
            r"\b(emi|installment|installments|instalment|monthly payment|"
            r"monthly payments|ఈఎంఐ)\b", lower, re.I
        ):
            result["payment_method"] = "EMI"
        elif re.search(
            r"\b(cash|cash lo|cash tho|cash payment|nagadu|నగదు)\b",
            lower, re.I
        ):
            result["payment_method"] = "Cash"
        elif re.search(
            r"\b(bank transfer|net banking|bank payment|account transfer)\b",
            lower, re.I
        ):
            result["payment_method"] = "Bank transfer"

    if not result.get("storage"):
        storage = re.search(
            r"\b(32|64|128|256|512|1024)\s*(gb|g|gig|gigs)\b",
            lower, re.I
        )
        if storage:
            result["storage"] = storage.group(1) + "GB"

def _repair_invalid_product(text: str, result: dict) -> None:
    """Prevent filler words such as 'kavali' becoming product names."""

    value = result.get("product_interest")
    if not isinstance(value, str):
        return

    cleaned = re.sub(r"\s+", " ", value).strip()
    lower = cleaned.lower()

    filler_only = {
        "kavali", "kavala", "kavali bro", "phone kavali",
        "mobile kavali", "konali", "konali ani", "want",
        "phone", "mobile", "smartphone",
    }

    # Generic values are allowed when they carry a brand, e.g. "Vivo phone".
    if lower in filler_only:
        # Preserve generic product intent only for actual phone/mobile.
        if lower in {"phone", "mobile", "smartphone"}:
            result["product_interest"] = None
        else:
            result["product_interest"] = None

    # "Brand new kavali" must not become product_interest="kavali".
    if re.fullmatch(
        r"(brand[\s-]*new|new|sealed|sealed piece|sealed pack|"
        r"kotha|kotta|kottha)(?:\s+kavali)?",
        lower,
        re.I,
    ):
        result["product_interest"] = None

# ---------------------------------------------------------
# BROAD ENGLISH + TELUGU + ROMAN-TELUGU INTENT LEXICON V4
# ---------------------------------------------------------
# The goal is variation coverage, not a giant list of unrelated words.
# WhatsApp users misspell, shorten, repeat vowels, mix English/Telugu,
# and use regional colloquial forms. These families normalize those forms
# into the fields already used by the agent.

ENGLISH_PURCHASE_TERMS = (
    "i want", "i need", "i'd like", "id like", "looking for", "searching for",
    "interested in", "planning to buy", "want to buy", "need to buy",
    "want one", "need one", "get me", "give me", "send me", "show me",
    "i am buying", "im buying", "i'm buying", "ready to buy", "i'll buy",
    "ill buy", "will buy", "going to buy", "purchase", "purchasing",
    "order", "place an order", "book one", "reserve one",
)
ROMAN_PURCHASE_TERMS = (
    "naku", "naaku", "naaku kavali", "naku kavali", "kavali", "kavala",
    "kavalandi", "kavali bro", "kavali anna", "kavali sir", "kavali madam",
    "konali", "konalanundi", "konalani undi", "konali ani undi", "kondam",
    "kontanu", "kontaa", "kontam", "teesukovali", "teeskovali", "tesukovali",
    "teesukunta", "teeskunta", "kavali anukuntunna", "konadaniki", "buy cheyali",
    "konadaniki chustunna", "konali ani chustunna", "konali ani anukuntunna",
    "phone kavali", "mobile kavali", "oka phone kavali", "oka mobile kavali",
)
TELUGU_PURCHASE_TERMS = (
    "నాకు కావాలి", "నాకు ఫోన్ కావాలి", "నాకు మొబైల్ కావాలి", "కొనాలి",
    "కొనాలనుంది", "కొనాలని ఉంది", "కొనాలి అని ఉంది", "తీసుకోవాలి",
    "తీసుకుంటాను", "ఫోన్ కావాలి", "మొబైల్ కావాలి", "ఒక ఫోన్ కావాలి",
    "కొనడానికి చూస్తున్నాను", "కొనాలి అనుకుంటున్నాను",
)

PRICE_QUESTION_TERMS = (
    "price", "prices", "cost", "costs", "rate", "rates", "how much",
    "worth", "selling price", "current price", "final price", "best price",
    "deal price", "offer price", "price please", "price pls", "mrp",
    "ధర", "ఎంత", "ఎంత ధర", "రేటు", "ధర ఎంత", "ఎంతకి", "ఎంతకు",
    "entha", "enthaa", "entha price", "entha cost", "rate entha",
    "price entha", "entha ki", "entha ku", "entha bro", "entha anna",
)
STOCK_QUESTION_TERMS = (
    "stock", "in stock", "available", "availability", "available now",
    "available aa", "available ah", "got stock", "have stock", "do you have",
    "is it there", "can i get", "ready stock", "ready piece", "stock unda",
    "stock undha", "stock lo unda", "stock lo undha", "available unda",
    "dorukutunda", "dorukuthunda", "dorukutundaa", "undha", "unda",
    "స్టాక్", "అందుబాటులో", "ఉందా", "దొరుకుతుందా", "అందుబాటులో ఉందా",
)
BATTERY_QUESTION_TERMS = (
    "battery", "battery health", "battery backup", "battery condition",
    "battery percentage", "battery percent", "mah", "backup entha",
    "battery ela undi", "battery health entha", "battery bagunda",
    "బ్యాటరీ", "బ్యాటరీ హెల్త్", "బ్యాటరీ బ్యాకప్", "బ్యాటరీ ఎంత",
)
EMI_QUESTION_TERMS = (
    "emi", "em i", "installment", "installments", "monthly payment",
    "monthly emi", "emi option", "emi available", "no cost emi",
    "card emi", "finance", "financing", "వాయిదా", "ఈఎంఐ", "ఎమై",
    "emi unda", "emi undha", "monthly ga pay", "monthly pay",
)
DELIVERY_QUESTION_TERMS = (
    "delivery", "deliver", "shipping", "ship", "delivery time", "how long",
    "when will it arrive", "arrival", "dispatch", "courier", "home delivery",
    "delivery chestara", "deliver chestara", "delivery eppudu", "eppudu vastundi",
    "eppudu vasthundi", "eppudu cherutundi", "intiki pampistara", "home ki pampistara",
    "డెలివరీ", "పంపిస్తారా", "ఎప్పుడు వస్తుంది", "ఎప్పుడు చేరుతుంది",
)
WARRANTY_QUESTION_TERMS = (
    "warranty", "guarantee", "warranty period", "warranty unda", "warranty undha",
    "warranty entha", "guarantee unda", "guarantee undha", "గ్యారంటీ", "వారంటీ",
    "వారంటీ ఉందా", "గ్యారంటీ ఉందా",
)
DISCOUNT_QUESTION_TERMS = (
    "discount", "offer", "offers", "deal", "best deal", "best offer",
    "negotiable", "negotiate", "discount unda", "discount undha", "offer unda",
    "offer undha", "last price", "final rate", "best rate", "తగ్గింపు", "ఆఫర్",
    "డిస్కౌంట్", "చివరి ధర", "బెస్ట్ ప్రైస్",
)
EXCHANGE_QUESTION_TERMS = (
    "exchange", "exchange offer", "old phone exchange", "trade in", "trade-in",
    "exchange unda", "exchange undha", "old phone isthe", "old mobile isthe",
    "పాత ఫోన్ ఎక్స్చేంజ్", "ఎక్స్చేంజ్", "మార్చుకోవచ్చా",
)
SPEC_QUESTION_TERMS = (
    "specs", "specifications", "features", "details", "configuration", "variant",
    "ram", "rom", "processor", "camera", "display", "screen", "refresh rate",
    "color", "colour", "storage", "memory", "specs cheppu",
    "details cheppu", "features cheppu", "specifications cheppu", "ఫీచర్స్", "స్పెసిఫికేషన్స్",
    "వేరియంట్", "స్టోరేజ్", "కెమెరా", "డిస్ప్లే", "రామ్", "మెమరీ",
)

USE_CASE_FAMILIES = {
    "gaming": (
        "gaming", "game", "games", "gamer", "for gaming", "gaming kosam",
        "games kosam", "gaming ki", "game aadadaniki", "ఆటలు", "గేమింగ్", "గేమ్స్",
    ),
    "photography": (
        "photography", "photos", "photo", "camera", "pictures", "portrait",
        "photography kosam", "photos kosam", "camera kosam", "ఫోటోలు", "ఫోటోగ్రఫీ", "కెమెరా",
    ),
    "video": (
        "video", "videos", "video recording", "reels", "youtube", "content creation",
        "video kosam", "reels kosam", "youtube kosam", "వీడియో", "వీడియోలు", "యూట్యూబ్",
    ),
    "work": (
        "work", "office", "office work", "business", "professional", "work kosam",
        "office kosam", "business kosam", "job kosam", "పని", "ఆఫీస్", "బిజినెస్",
    ),
    "study": (
        "study", "studies", "student", "college", "classes", "online classes",
        "study kosam", "college kosam", "classes kosam", "చదువు", "స్టడీస్", "కాలేజ్",
    ),
    "daily use": (
        "daily use", "everyday use", "normal use", "regular use", "basic use",
        "daily ga", "daily use kosam", "రోజూ వాడటానికి", "రోజువారీ ఉపయోగం",
    ),
}

BROAD_BROWSE_TERMS = (
    "just browsing", "just checking", "just looking", "checking options", "exploring",
    "researching", "doing research", "not sure yet", "not decided", "haven't decided",
    "have not decided", "still deciding", "just comparing", "comparing options",
    "chustunna", "chusthunna", "chustunnanu", "choostunna", "choosthunna",
    "inka chustunna", "inka decide avvaledu", "decision ledu", "fix avvaledu",
    "ippudu konanu", "ippatiki konanu", "just chustunna", "compare chestunna",
    "research chestunna", "telusukuntunna", "telusukuntunnanu",
    "ఇంకా నిర్ణయించుకోలేదు", "చూస్తున్నాను", "ఇంకా చూస్తున్నాను", "ఇప్పుడు కొనను",
)

BROAD_CONFIRM_TERMS = (
    "yes", "yeah", "yep", "yup", "y", "sure", "okay", "ok", "alright", "correct",
    "right", "exactly", "that's right", "thats right", "confirmed", "confirm",
    "avunu", "avunu bro", "avunu anna", "sare", "sare bro", "sare anna", "aithe sare",
    "correct eh", "ade", "ade kavali", "ade correct", "ha", "haa", "hmm okay",
    "అవును", "అవును బ్రో", "సరే", "అదే", "అదే కావాలి", "కరెక్ట్", "అవును కావాలి",
)

BROAD_NEGATIVE_TERMS = (
    "no", "nope", "nah", "not interested", "don't want", "do not want", "not now",
    "cancel", "stop", "never mind", "nevermind", "vaddhu", "vaddu", "oddu",
    "avsarama ledu", "ippudu vaddu", "vaddu bro", "kaadu", "kadu", "kadu bro",
    "వద్దు", "ఒద్దు", "కాదు", "ఇప్పుడు వద్దు", "అవసరం లేదు",
)

CORRECTION_VARIANTS = (
    "actually", "i mean", "i meant", "correction", "instead", "not that",
    "rather", "change it", "change that", "make it", "update it", "correct that",
    "no i mean", "change cheyyi", "change cheyandi", "marchu", "marchandi",
    "marcheyi", "actually naku", "ante kaadu", "అంటే కాదు", "సరిచేయండి",
    "మార్చండి", "మార్చు",
)

def _first_intent_phrase(text: str, families) -> Optional[str]:
    lower = re.sub(r"\s+", " ", (text or "").lower()).strip()
    for name, phrases in families.items():
        if any(p in lower for p in phrases):
            return name
    return None

TELUGU_BROAD_TIMELINE_TERMS = (
    "ఈరోజు", "ఈరోజే", "ఇవాళ", "ఇవాళే", "ఇప్పుడే", "వెంటనే", "రేపు", "రేపటికి",
    "ఈ వారం", "ఈ వారంలో", "ఈ వీకెండ్", "ఈ నెల", "ఈ నెలలో", "వచ్చే వారం", "వచ్చే నెల",
    "ఒక వారం", "ఒక వారంలో", "రెండు వారాలు", "రెండు వారాల్లో", "మూడు రోజులు", "మూడు రోజుల్లో",
    "ఐదు రోజులు", "ఐదు రోజుల్లో", "ఒక నెల", "ఒక నెలలో", "కొన్ని రోజుల్లో", "కొన్ని వారాల్లో",
)
TELUGU_BROAD_BUDGET_TERMS = (
    "బడ్జెట్", "నా బడ్జెట్", "నా బడ్జెట్", "లోపు", "వరకు", "వరకూ", "కంటే తక్కువ", "లోపల",
    "వేల లోపు", "వేలు లోపు", "లక్ష లోపు", "లక్ష వరకు", "నుండి", "నుంచి",
)
TELUGU_BROAD_CONDITION_TERMS = (
    "కొత్త", "కొత్తది", "కొత్త ఫోన్", "కొత్త మొబైల్", "సీల్డ్", "సీల్డ్ పీస్", "సీల్డ్ ప్యాక్",
    "బ్రాండ్ న్యూ", "ఫ్యాక్టరీ సీల్", "యూజ్డ్", "వాడిన", "పాత", "పాత ఫోన్", "సెకండ్ హ్యాండ్",
    "రిఫర్బిష్డ్", "రీఫర్బిష్డ్", "ప్రీ ఓన్డ్", "పాతది",
)
TELUGU_BROAD_PAYMENT_TERMS = (
    "నగదు", "క్యాష్", "కార్డ్", "కార్డు", "క్రెడిట్ కార్డ్", "డెబిట్ కార్డ్", "యూపీఐ",
    "గూగుల్ పే", "ఫోన్ పే", "ఫోన్‌పే", "పేటీఎం", "ఈఎంఐ", "ఎమై", "వాయిదా", "వాయిదాలు",
    "బ్యాంక్ ట్రాన్స్‌ఫర్", "బ్యాంక్ ట్రాన్స్ఫర్", "నెట్ బ్యాంకింగ్",
)
TELUGU_BROAD_QUESTION_TERMS = (
    "ధర", "ధర ఎంత", "ఎంత ధర", "రేటు", "ఎంతకి", "ఎంతకు", "స్టాక్", "స్టాక్ ఉందా",
    "అందుబాటులో", "ఉందా", "ఉన్నదా", "దొరుకుతుందా", "బ్యాటరీ", "బ్యాటరీ హెల్త్",
    "బ్యాటరీ బ్యాకప్", "ఈఎంఐ", "వాయిదా", "డెలివరీ", "ఎప్పుడు వస్తుంది", "ఎప్పుడు చేరుతుంది",
    "వారంటీ", "గ్యారంటీ", "డిస్కౌంట్", "ఆఫర్", "ఎక్స్చేంజ్", "మార్చుకోవచ్చా",
    "ఫీచర్లు", "స్పెసిఫికేషన్స్", "వేరియంట్", "స్టోరేజ్", "కెమెరా", "డిస్ప్లే", "రామ్", "మెమరీ",
)
TELUGU_BROAD_PURCHASE_TERMS = (
    "నాకు కావాలి", "నాకు ఫోన్ కావాలి", "నాకు మొబైల్ కావాలి", "కొనాలి", "కొనాలనుంది",
    "కొనాలని ఉంది", "కొనాలి అనుకుంటున్నాను", "కొనడానికి చూస్తున్నాను", "తీసుకోవాలి",
    "తీసుకుంటాను", "కొనుక్కోవాలి", "ఆర్డర్ చేయాలి", "బుక్ చేయాలి", "చూడాలి", "చూస్తున్నాను",
)
TELUGU_BROAD_USE_CASES = {
    "gaming": ("గేమింగ్", "గేమ్స్", "ఆటలు", "గేమ్ ఆడటానికి"),
    "photography": ("ఫోటోగ్రఫీ", "ఫోటోలు", "కెమెరా కోసం", "ఫోటోలు తీయడానికి"),
    "video": ("వీడియో", "వీడియోలు", "రీల్స్", "యూట్యూబ్", "వీడియో చేయడానికి"),
    "work": ("పని", "ఆఫీస్", "బిజినెస్", "ప్రొఫెషనల్"),
    "study": ("చదువు", "కాలేజ్", "క్లాసులు", "స్టడీ"),
    "daily use": ("రోజూ వాడటానికి", "రోజువారీ ఉపయోగం", "సాధారణ వాడకం"),
}

def _repair_telugu_script_broad(text: str, result: dict) -> None:
    lower = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not lower:
        return
    if not result.get("purchase_timeline"):
        if any(x in lower for x in ("ఈరోజు", "ఈరోజే", "ఇవాళ", "ఇవాళే", "ఇప్పుడే", "వెంటనే")):
            result["purchase_timeline"] = "immediately"
        elif any(x in lower for x in ("రేపు", "రేపటికి")):
            result["purchase_timeline"] = "tomorrow"
        elif any(x in lower for x in ("ఈ వారం", "ఈ వారంలో", "ఈ వీకెండ్", "వచ్చే వారం", "ఒక వారం", "ఒక వారంలో")):
            result["purchase_timeline"] = "within a week"
        elif any(x in lower for x in ("ఈ నెల", "ఈ నెలలో", "వచ్చే నెల", "ఒక నెల", "ఒక నెలలో")):
            result["purchase_timeline"] = "within a month"
        else:
            m = re.search(r"(\d+)\s*(రోజు|రోజులు|రోజుల్లో|రోజులలో)", lower)
            if m:
                n = int(m.group(1))
                result["purchase_timeline"] = f"within {n} days"
            else:
                m = re.search(r"(\d+)\s*(వారం|వారాలు|వారాల్లో|వారాలలో)", lower)
                if m:
                    result["purchase_timeline"] = f"within {int(m.group(1))} weeks"
    if not result.get("condition"):
        if any(x in lower for x in TELUGU_BROAD_CONDITION_TERMS):
            if any(x in lower for x in ("యూజ్డ్", "వాడిన", "పాత", "సెకండ్ హ్యాండ్", "రిఫర్బిష్డ్", "రీఫర్బిష్డ్", "ప్రీ ఓన్డ్")):
                result["condition"] = "used/refurbished"
            else:
                result["condition"] = "brand new"
    if any(x in lower for x in TELUGU_BROAD_PAYMENT_TERMS):
        if any(x in lower for x in ("యూపీఐ", "గూగుల్ పే", "ఫోన్ పే", "ఫోన్‌పే", "పేటీఎం")):
            result["payment_method"] = "UPI"
        elif any(x in lower for x in ("కార్డ్", "కార్డు", "క్రెడిట్ కార్డ్", "డెబిట్ కార్డ్")):
            result["payment_method"] = "Card"
        elif any(x in lower for x in ("ఈఎంఐ", "ఎమై", "వాయిదా", "వాయిదాలు")):
            result["payment_method"] = "EMI"
        elif any(x in lower for x in ("నగదు", "క్యాష్")):
            result["payment_method"] = "Cash"
    if not result.get("product_question"):
        qgroups = (
            ("battery", ("బ్యాటరీ", "బ్యాటరీ హెల్త్", "బ్యాటరీ బ్యాకప్")),
            ("EMI", ("ఈఎంఐ", "ఎమై", "వాయిదా")),
            ("warranty", ("వారంటీ", "గ్యారంటీ")),
            ("exchange", ("ఎక్స్చేంజ్", "మార్చుకోవచ్చా")),
            ("delivery", ("డెలివరీ", "ఎప్పుడు వస్తుంది", "ఎప్పుడు చేరుతుంది")),
            ("discount", ("డిస్కౌంట్", "ఆఫర్")),
            ("specifications", ("ఫీచర్లు", "స్పెసిఫికేషన్స్", "వేరియంట్", "స్టోరేజ్", "కెమెరా", "డిస్ప్లే", "రామ్", "మెమరీ")),
            ("stock", ("స్టాక్", "అందుబాటులో", "దొరుకుతుందా", "ఉందా")),
            ("price", ("ధర", "ధర ఎంత", "ఎంత ధర", "రేటు", "ఎంతకి", "ఎంతకు")),
        )
        for name, phrases in qgroups:
            if any(x in lower for x in phrases):
                result["product_question"] = name
                break
    if not result.get("preferences") and any(x in lower for x in TELUGU_BROAD_PURCHASE_TERMS):
        result["preferences"] = ["purchase intent"]
    if not result.get("use_case"):
        cases = [name for name, phrases in TELUGU_BROAD_USE_CASES.items() if any(x in lower for x in phrases)]
        if cases:
            result["use_case"] = cases
    if any(x in lower for x in ("ఇంకా నిర్ణయించుకోలేదు", "ఇంకా చూస్తున్నాను", "ఇప్పుడు కొనను", "జస్ట్ చూస్తున్నాను")):
        result["purchase_timeline"] = "just browsing"

def _repair_broad_intents_v4(text: str, result: dict) -> None:
    """Large but controlled synonym layer for English/Telugu/Roman-Telugu."""
    lower = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not lower:
        return

    # Telugu-script and mixed-script variants are matched by substring because
    # Python \b boundaries are unreliable around some Indic-script forms.
    if not result.get("purchase_timeline"):
        if any(x in lower for x in ("3 రోజుల్లో", "3 రోజులలో", "మూడు రోజుల్లో")):
            result["purchase_timeline"] = "within 3 days"
        elif any(x in lower for x in ("రెండు వారాల్లో", "రెండు వారాలలో")):
            result["purchase_timeline"] = "within 2 weeks"

    # Explicit condition/payment phrases should beat weak extractor output.
    if any(x in lower for x in ("sealed", "sealed piece", "sealed pack", "factory seal", "కొత్త", "సీల్డ్", "brand new", "new piece", "kotha", "kotta", "kottha")):
        result["condition"] = "brand new"
    elif any(x in lower for x in ("used", "second hand", "secondhand", "pre owned", "preowned", "refurb", "refurbished", "old phone", "old piece", "పాత", "సెకండ్ హ్యాండ్", "paata", "paatha", "pata phone")):
        result["condition"] = "Used"

    if any(x in lower for x in ("upi", "gpay", "google pay", "phonepe", "phone pe", "paytm", "ఫోన్‌పే", "యూపీఐ")):
        result["payment_method"] = "UPI"
    elif any(x in lower for x in ("credit card", "debit card", "card payment", "card lo", "card tho", "కార్డ్", "క్రెడిట్ కార్డ్", "డెబిట్ కార్డ్")):
        result["payment_method"] = "Card"
    elif any(x in lower for x in ("emi", "installment", "installments", "monthly payment", "no cost emi", "ఈఎంఐ", "ఎమై")):
        result["payment_method"] = "EMI"
    elif any(x in lower for x in ("cash", "cash lo", "cash tho", "నగదు")):
        result["payment_method"] = "Cash"

    # Purchase intent is useful for qualification, but never invent a model.
    if not result.get("preferences") and any(
        _phrase_matches(lower, p)
        for p in ENGLISH_PURCHASE_TERMS + ROMAN_PURCHASE_TERMS + TELUGU_PURCHASE_TERMS
    ):
        result["preferences"] = ["purchase intent"]

    if True:
        groups = (
            # Specific phrases first; generic words like "entha", "offer" or
            # "unda" must not steal a battery/EMI/warranty question.
            ("battery", BATTERY_QUESTION_TERMS),
            ("EMI", EMI_QUESTION_TERMS),
            ("warranty", WARRANTY_QUESTION_TERMS),
            ("exchange", EXCHANGE_QUESTION_TERMS),
            ("delivery", DELIVERY_QUESTION_TERMS),
            ("discount", DISCOUNT_QUESTION_TERMS),
            ("specifications", SPEC_QUESTION_TERMS),
            ("stock", STOCK_QUESTION_TERMS),
            ("price", PRICE_QUESTION_TERMS),
        )
        for question, phrases in groups:
            if any(_phrase_matches(lower, p) for p in phrases):
                result["product_question"] = question
                break

    if not result.get("use_case"):
        use_cases = []
        for name, phrases in USE_CASE_FAMILIES.items():
            if any(_phrase_matches(lower, p) for p in phrases):
                use_cases.append(name)
        if use_cases:
            result["use_case"] = use_cases

    if not result.get("purchase_timeline") and any(
        _phrase_matches(lower, p) for p in BROAD_BROWSE_TERMS
    ):
        result["purchase_timeline"] = "just browsing"

    # Keep these as lightweight intent flags only; conversation.py/scoring can
    # decide how much weight to give them.
    if any(_phrase_matches(lower, p) for p in CORRECTION_VARIANTS):
        result["_has_correction"] = True
    if any(_phrase_matches(lower, p) for p in BROAD_CONFIRM_TERMS):
        result["_confirmed"] = True
    if any(_phrase_matches(lower, p) for p in BROAD_NEGATIVE_TERMS):
        result["_negative"] = True

def _repair_common_message_extractions(message: str, result: dict, language: str) -> dict:
    """
    Final deterministic safety net for short WhatsApp replies.

    This runs after language-specific extraction and before the result is
    merged into the conversation state. It prevents the agent from asking
    the same question again when the customer answered with a compact
    phrase such as:
        Oka moodu rojullo
        Oka 3 days lo
        3 days
        25000 anukuntunnamu
    """

    text = (message or "").strip()
    lower = text.lower().strip()

    if not lower:
        return result

    # Broad colloquial safety-net repairs run before the narrower rules.
    _repair_telugu_script_broad(text, result)
    _repair_budget_status(text, result)
    _repair_broad_timeline(text, result)
    _repair_broad_budget(text, result)
    _repair_broad_condition_payment_storage(text, result)
    _repair_broad_intents_v4(text, result)
    _repair_invalid_product(text, result)

    # ---------------------------------------------------------
    # PURCHASE TIMELINE
    # ---------------------------------------------------------

    # "Oka moodu rojullo" / "moodu rojullo" / "3 days lo"
    # "Oka 3 days lo" / "3 rojullo"
    number_words = {
        "oka": 1,
        "okati": 1,
        "rendu": 2,
        "moodu": 3,
        "nalugu": 4,
        "aidu": 5,
        "aaru": 6,
        "edu": 7,
        "enimidi": 8,
        "tommidi": 9,
        "padi": 10,
    }

    # Keep an already extracted timeline. Otherwise repair compact
    # day/week/month expressions.
    if not result.get("purchase_timeline"):
        if re.search(
            r"\b(today|right now|immediately|asap|ivale|ivala|eeroju|ee roju|"
            r"ఈరోజు|ఈరోజే|ఈ రోజు|ఈరోజే)\b",
            lower,
            re.I,
        ):
            result["purchase_timeline"] = "immediately"

        elif re.search(
            r"\b(tomorrow|repu|repatiki|రేపు|రేపటికి)\b",
            lower,
            re.I,
        ):
            result["purchase_timeline"] = "tomorrow"

        elif re.search(
            r"\b(this weekend|ee weekend|weekend|ఈ వీకెండ్|ఈ వారాంతం)\b",
            lower,
            re.I,
        ):
            result["purchase_timeline"] = "this weekend"

        elif re.search(
            r"\b(this week|ee week|ee week lo|ee varam|ee varam lo|"
            r"one week|in a week|within a week|oka week|oka varam|oka vaaram|"
            r"ఒక వారంలో|ఒక వారం లోపు|ఈ వారంలో)\b",
            lower,
            re.I,
        ):
            result["purchase_timeline"] = "within a week"

        elif re.search(
            r"\b(two weeks|2 weeks|rendu weeks|rendu vaarallo|rendu varallo|"
            r"రెండు వారాల్లో|రెండు వారాలలో)\b",
            lower,
            re.I,
        ):
            result["purchase_timeline"] = "within two weeks"

        elif re.search(
            r"\b(one month|in a month|within a month|oka month|oka nela|"
            r"oka nela lo|oka nela lopu|నెలలోపు|ఒక నెలలో)\b",
            lower,
            re.I,
        ):
            result["purchase_timeline"] = "within a month"

        else:
            # Numeric days: 3 days, 3 days lo, 3 rojullo, 3-5 days lo.
            day_match = re.search(
                r"\b(\d+)\s*(?:-\s*(\d+)\s*)?"
                r"(?:days?|rojulu|rojullo|rojulalo)"
                r"(?:\s*(?:lo|lopu|within|లో|లోపు))?\b",
                lower,
                re.I,
            )

            if day_match:
                first = int(day_match.group(1))
                second = day_match.group(2)
                if second:
                    result["purchase_timeline"] = (
                        f"{first}-{int(second)} days"
                    )
                else:
                    result["purchase_timeline"] = (
                        f"within {first} days"
                    )
            else:
                # Roman-Telugu number words + days.
                word_pattern = (
                    r"\b(?:oka\s+)?("
                    + "|".join(sorted(number_words, key=len, reverse=True))
                    + r")\s*(?:days?|rojulu|rojullo|rojulalo)"
                    r"(?:\s*(?:lo|lopu|within))?\b"
                )
                word_match = re.search(word_pattern, lower, re.I)
                if word_match:
                    days = number_words[word_match.group(1).lower()]
                    result["purchase_timeline"] = f"within {days} days"

    # ---------------------------------------------------------
    # COMPACT BUDGET REPLIES
    # ---------------------------------------------------------

    if not result.get("budget") and not result.get("budget_amount"):
        # A bare number or "25000 anukuntunnamu / budget 25000".
        # Only do this when the message is short and budget-like, so
        # product model numbers such as "iPhone 15" are not budgets.
        budget_context = re.search(
            r"\b(budget|anukuntunnam|anukuntunnamu|anukuntunna|"
            r"budgetga|budget lo|amount|lopu|varaku)\b",
            lower,
            re.I,
        )

        bare_number = re.fullmatch(
            r"\s*(?:₹|rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)\s*",
            lower,
            re.I,
        )

        contextual_number = re.search(
            r"(?:₹|rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)\s*"
            r"(?:anukuntunnam|anukuntunnamu|anukuntunna|budgetga|budget lo|"
            r"varaku|lopu)\b",
            lower,
            re.I,
        )

        match = bare_number or contextual_number if (
            bare_number or budget_context
        ) else None

        if match:
            raw = match.group(1).replace(",", "")
            try:
                amount = int(float(raw))
            except ValueError:
                amount = 0

            if 0 < amount <= 10_000_000:
                result["budget_amount"] = amount
                result["budget"] = amount

                if re.search(
                    r"\b(under|below|lopu|lopala|varaku|maximum|max|upto|"
                    r"up to)\b",
                    lower,
                    re.I,
                ):
                    result["budget_max"] = amount

    # Final guard: "no budget right now" is not a purchase timeline.
    if result.get("budget_status") in {
        "unavailable_now", "undecided", "prefer_not_to_say", "price_concern"
    }:
        has_purchase_timing_context = bool(re.search(
            r"\b(?:buy|buying|purchase|purchasing|need it|want it|konali|konukovali|"
            r"konadaniki|teesukovali|purchase cheyyali)\b",
            lower,
            re.I,
        ))
        if (
            not has_purchase_timing_context
            and re.search(r"\b(?:right now|now|ippudu|ippude)\b", lower)
        ):
            result["purchase_timeline"] = None

    return result

def _extract_deterministically(

    message: str,

    language: str,

) -> dict:

    if language == "telugu":

        result = telugu.extract(message)

    elif language == "roman_telugu":

        result = roman_telugu.extract(message)

    else:

        result = english.extract(message)

    if not isinstance(result, dict):

        return {}

    result = dict(result)

    # Final repair pass for compact WhatsApp answers that the
    # language-specific extractors may miss.
    repaired = _repair_common_message_extractions(
        message,
        result,
        language,
    )
    _repair_invalid_product(message, repaired)
    return repaired

# ---------------------------------------------------------

# CUSTOMER DETAIL EXTRACTION

# ---------------------------------------------------------

def extract_customer_details(

    message: str,

    existing_customer: dict,

) -> dict:

    """

    Extract details from the NEW customer message.

    Existing customer data is reference context only.

    Fast path:

        deterministic extraction

        -> no Ollama

    Slow path:

        Ollama only when:

            \\- product is ambiguous/generic

            \\- correction is detected

            \\- deterministic extraction found nothing useful

    """

    message = (message or "").strip()

    if not message:

        return {

            field: None

            for field in FIELDS + ["budget"]

        }

    language = detect_language(message)

    # -----------------------------------------------------

    # STEP 1

    # Deterministic extraction

    # -----------------------------------------------------

    try:

        result = _extract_deterministically(

            message,

            language,

        )

    except Exception as exc:

        print(

            "Deterministic extraction failed: "

            f"{type(exc).__name__}"

        )

        result = {}

    if not isinstance(result, dict):

        result = {}

    # -----------------------------------------------------

    # STEP 2

    # Ensure consistent shape

    # -----------------------------------------------------

    for field in FIELDS:

        result.setdefault(field, None)

    result["product_interest"] = _clean_product_name(

        result.get("product_interest")

    )

    # -----------------------------------------------------

    # FAST SHORT REPLIES

    # -----------------------------------------------------

    #

    # Do this BEFORE deciding whether Ollama is needed.

    #

    # This fixes the important case:

    #

    # Agent: Mee budget entha?

    # Customer: 25000

    #

    # No LLM call.

    # -----------------------------------------------------

    _extract_fast_short_reply(

        message,

        result,

    )

    # -----------------------------------------------------

    # STEP 3

    # Determine whether Ollama is actually needed

    # -----------------------------------------------------

    meaningful_fields = [

        field

        for field in FIELDS

        if result.get(field) is not None

    ]

    fields_for_llm = [

        field

        for field in FIELDS

        if _needs_llm(

            field,

            result.get(field),

            message,

        )

    ]

    # If deterministic extraction found absolutely nothing,

    # the message may be genuinely unknown/ambiguous.

    #

    # In that case allow one Ollama extraction call.

    if not fields_for_llm and not meaningful_fields:

        fields_for_llm = list(FIELDS)

    # -----------------------------------------------------

    # IMPORTANT FAST-PATH PROTECTION

    # -----------------------------------------------------

    #

    # If the message is a simple structured reply, do NOT

    # call Ollama even though unrelated fields are missing.

    #

    # Examples:

    #

    #   25000

    #   45000

    #   55k

    #   Hyderabad

    #   Cash

    #   Card

    #   128GB

    #

    # -----------------------------------------------------

    simple_structured_message = (

        bool(re.fullmatch(

            r"\d+(?:[.,]\d+)?",

            message.strip(),

        ))

        or bool(re.fullmatch(

            r"\d+(?:\.\d+)?\s*(?:k|l|lac|lakh|lakhs)",

            message.strip().lower(),

        ))

        or bool(re.fullmatch(

            r"\d+\s*(?:gb|జీబీ)",

            message.strip().lower(),

        ))

        or message.strip().lower()

        in {

            "cash",

            "card",

            "upi",

            "gpay",

            "google pay",

            "phonepe",

            "paytm",

            "emi",

            "installment",

            "installments",

            "bank transfer",

            "net banking",

        }

    )

    if simple_structured_message:

        fields_for_llm = []

    # -----------------------------------------------------
    # SEMANTIC EXTRACTION FOR SUBSTANTIVE CUSTOMER MESSAGES
    # -----------------------------------------------------
    # When a model provider is enabled, interpret substantive multi-word
    # customer messages even if deterministic rules already found a field.
    # Short answers such as "Cash", "128GB", "45000", and "55k" remain fast.
    provider = _llm_provider()
    normalized_message = re.sub(
        r"\s+", " ", message.strip().lower()
    )
    social_only = normalized_message in {
        "hi", "hii", "hiii", "hello", "hey", "heyy",
        "good morning", "good afternoon", "good evening",
        "thanks", "thank you", "ok", "okay",
        "హాయ్", "హలో", "సరే", "ధన్యవాదాలు",
    }

    if (
        provider in {"gemini", "openai", "ollama"}
        and not simple_structured_message
        and not social_only
        and len(message.split()) >= 3
    ):
        fields_for_llm = list(FIELDS)

    # -----------------------------------------------------

    # STEP 4

    # Optional model-assisted extraction

    # -----------------------------------------------------

    if fields_for_llm and _llm_provider() != "none":
        prompt = build_extraction_prompt(
            message=message,
            language=language,
            fields=fields_for_llm,
            deterministic_result=result,
            existing_customer=existing_customer,
        )

        try:

            response_text = _call_text_model(
                EXTRACTION_SYSTEM_PROMPT,
                prompt,
                temperature=0,
                json_mode=True,
            )
            if not response_text:
                raise RuntimeError("No configured language-model provider is available")
            parsed = json.loads(response_text)

            llm_result = LeadExtraction.model_validate(

                parsed

            ).model_dump()

            # Only use LLM output for fields that were

            # explicitly selected for reconsideration.

            for field in fields_for_llm:
                candidate = llm_result.get(field)

                if candidate is None:
                    continue

                if isinstance(candidate, str) and not candidate.strip():
                    continue

                current = result.get(field)

                # Keep reliable deterministic values unless missing or
                # marked ambiguous by the existing decision rule.
                if current is None or _needs_llm(field, current, message):
                    result[field] = candidate

        except Exception as exc:

            # Deterministic extraction remains usable even

            # if the configured language model is unavailable.

            print(
                "Model-assisted extraction unavailable; keeping deterministic extraction: "
                f"{type(exc).__name__}"
            )

    # -----------------------------------------------------
    # FINAL MESSAGE-LEVEL VALIDATION
    # -----------------------------------------------------
    # Model/language-specific extractors can return plausible-looking but
    # incorrect values. Re-run high-confidence deterministic parsing AFTER
    # optional LLM extraction so explicit ranges and short intent flags cannot
    # be overwritten by a weaker candidate.
    _repair_broad_budget(message, result)

    # Flags are turn-level signals, not facts to inherit from an extractor.
    # Clear false positives such as treating "I want ... buying in a week"
    # as a correction or a standalone confirmation.
    if _has_correction_cue(message):
        result["_has_correction"] = True
    else:
        result.pop("_has_correction", None)

    if any(_phrase_matches(normalized_message, cue) for cue in BROAD_CONFIRM_TERMS):
        result["_confirmed"] = True
    else:
        result.pop("_confirmed", None)

    if any(_phrase_matches(normalized_message, cue) for cue in BROAD_NEGATIVE_TERMS):
        result["_negative"] = True
    else:
        result.pop("_negative", None)

    # A storage/model mention is not itself a specification question. Clear a
    # false specifications label unless the current message actually contains
    # a recognizable specification cue.
    if result.get("product_question") == "specifications":
        explicit_spec_cues = (
            "specs", "specifications", "features", "details", "configuration",
            "variant", "ram", "rom", "processor", "camera", "display",
            "screen", "refresh rate", "color", "colour", "storage", "memory",
            "ఫీచర్లు", "స్పెసిఫికేషన్స్", "వేరియంట్", "స్టోరేజ్", "కెమెరా",
            "డిస్ప్లే", "రామ్", "మెమరీ",
        )
        if not any(_phrase_matches(normalized_message, cue) for cue in explicit_spec_cues):
            result["product_question"] = None

    # -----------------------------------------------------
    # STEP 5
    # Explicit condition validation
    # -----------------------------------------------------

    if _explicit_condition(message):

        result["condition"] = _normalize_condition(

            result.get("condition")

        )

    else:

        result["condition"] = None

    # -----------------------------------------------------

    # STEP 6

    # Explicit payment validation

    # -----------------------------------------------------

    if _explicit_payment(message):

        result["payment_method"] = _normalize_payment(

            result.get("payment_method")

        )

    else:

        result["payment_method"] = None

    # -----------------------------------------------------

    # STEP 7

    # Normalize budget

    # -----------------------------------------------------

    _normalize_budget(result)

    # Explicit upper-limit language means the amount

    # represents a maximum.

    if (

        result.get("budget_amount") is not None

        and result.get("budget_max") is None

    ):

        limit_cues = (

            "limit",

            "maximum",

            "max",

            "under",

            "below",

            "upto",

            "up to",

            "lopu",

            "lopala",

            "varake",

            "varaku",

            "only",

            "మాత్రమే",

            "పరిమితి",

            "లోపు",

            "వరకే",

        )

        if any(

            cue in message.lower()

            for cue in limit_cues

        ):

            result["budget_max"] = (

                result["budget_amount"]

            )

    # -----------------------------------------------------

    # STEP 8

    # Final output shape

    # -----------------------------------------------------

    for field in FIELDS:

        result.setdefault(field, None)

    return result

# ---------------------------------------------------------

# DETERMINISTIC LOCALIZATION

# ---------------------------------------------------------

def _localized_fallback(

    draft_response: str,

    language: str,

    customer: Optional[dict] = None,

) -> str:

    """

    Fast deterministic customer-facing localization.

    IMPORTANT:

    This function does NOT call Ollama.

    Dynamic catalog information is preserved instead of being

    replaced by a generic translation.

    """

    draft = (draft_response or "").strip()

    if not draft:

        return ""

    if language == "english":

        return draft

    lower = draft.lower()

    # -----------------------------------------------------

    # ROMAN TELUGU

    # -----------------------------------------------------

    if language == "roman_telugu":

        replacements = {

            "If the price and availability work for you, would you like to proceed with the purchase?": (
                "Price mariyu availability meeku okay unte, phone purchase proceed cheyyalanukuntunnara?"
            ),

            "I couldn't check the product catalog right now. "

            "The store can confirm the model, price, and availability.": (

                "ప్రస్తుతం ఉత్పత్తుల జాబితాను పరిశీలించలేకపోయాను. "

                "నమూనా, ధర మరియు లభ్యతను దుకాణంతో నిర్ధారించుకోండి."

            ),

            "Sorry, I couldn't process that message properly. "

            "Could you please rephrase it?": (

                "క్షమించండి, మీ సందేశాన్ని సరిగ్గా అర్థం చేసుకోలేకపోయాను. "

                "దయచేసి మరోసారి చెప్పండి."

            ),

            "Could you please rephrase it?": (

                "దయచేసి మరో విధంగా చెప్పగలరా?"

            ),

            "I couldn't check the product catalog right now. "

            "The store can confirm the model, price, and availability.": (

                "Ippudu product catalog check cheyyalekapoyanu. "

                "Model, price mariyu availability store tho confirm chesukondi."

            ),

            "Sorry, I couldn't process that message properly. "

            "Could you please rephrase it?": (

                "Sorry bro, mee message ni sarigga process cheyyalekapoyanu. "

                "Koncham malli cheppandi."

            ),

            "Could you please rephrase it?": (

                "Koncham vere vidhamga cheppagalara?"

            ),

            "Which mobile are you interested in? "

            "Please tell me the brand and model.": (

                "Meeku ye mobile kavali? Brand mariyu model peru cheppandi."

            ),

            "What's your budget for the mobile?": (

                "Mee budget entha?"

            ),

            "When are you planning to buy it?": (

                "Eppudu konalanukuntunnaru?"

            ),

            "Are you looking for a brand-new or a "

            "second-hand/refurbished phone?": (

                "Kotha phone kavala, leka vaadina/refurbished phone aina parledu?"

            ),

            "How would you prefer to pay—cash, card, UPI, or EMI?": (

                "Payment ela cheyyalanukuntunnaru — "

                "cash, card, UPI leka EMI?"

            ),

            "Which city or town are you purchasing from?": (

                "Meeru ye nagaram leda pattnam nunchi konugolu chestunnaru?"

            ),

            "Got it — I've updated your requirements. "

            "What would you like to know about the phone?": (

                "Sare bro, mee requirements update chesanu. "

                "Phone gurinchi inka em telusukovali?"

            ),

            "I've retained your requirements. "

            "What would you like to know about the phone?": (

                "Sare bro, mee requirements note chesanu. "

                "Phone gurinchi inka em telusukovali?"

            ),

            "Thank you! I've recorded your requirements for Mania Mobiles. "

            "Our team can follow up with you about the phone and confirm "

            "the latest price and availability.": (

                "Sare bro, mee requirements note chesanu. "

                "Phone latest price mariyu availability store tho confirm chesukondi."

            ),

            "I couldn't find an exact match in the demo catalog. "

            "The store can confirm the model, price, and availability.": (

                "Demo catalog lo exact match dorakaledu. "

                "Model, price mariyu availability store tho confirm chesukondi."

            ),

            "Please confirm the latest price, exact variant, "

            "and availability with the store.": (

                "Latest price, exact variant mariyu availability "

                "store tho confirm chesukondi."

            ),

            "The latest price should be confirmed with the Mania Mobiles "

            "store, as prices and offers may change.": (

                "Latest price store tho confirm chesukondi, "

                "endukante prices mariyu offers change avvachu."

            ),

            "I can't confirm live stock from this chat. Please ask the "

            "Mania Mobiles store to verify the exact model and variant.": (

                "Live stock ni ee chat nunchi confirm cheyyalenu. "

                "Exact model mariyu variant store tho verify chesukondi."

            ),

            "Delivery availability, charges, and timing need to be "

            "confirmed by the Mania Mobiles store for your location.": (

                "Delivery availability, charges mariyu timing "

                "mee location kosam store tho confirm chesukondi."

            ),

            "Battery health or backup depends on the exact model and unit. "

            "I can't confirm a specific battery condition unless it is "

            "verified by the store.": (

                "Battery health/backup exact model mariyu unit meeda "

                "depend avutundi. Specific battery condition store verify "

                "chesina tarvata matrame confirm cheyyagalanu."

            ),

            "EMI eligibility, bank offers, and discounts can change. "

            "Please confirm the current offers with the Mania Mobiles "

            "store. Don't share your card number, CVV, or OTP in this chat.": (

                "EMI eligibility, bank offers mariyu discounts change avvachu. "

                "Current offers store tho confirm chesukondi. "

                "Card number, CVV leka OTP ni ee chat lo share cheyyakandi."

            ),

        }

        for source, target in replacements.items():

            if source.lower() == lower:

                return target

        # Catalog-miss/qualification replies can contain a known canned
        # sentence followed by a dynamic question. Exact matching above
        # misses those combined replies, so replace known fragments too.
        localized_draft = draft
        replaced = False

        for source, target in replacements.items():

            if source.lower() in lower:

                localized_draft = localized_draft.replace(source, target)
                replaced = True

        if replaced:
            for source, target in {
                "Product:": "Product:",
                "Price:": "Dharа:",
                "Original price:": "Asalu dhara:",
                "Current price:": "Prastuta dhara:",
                "Availability:": "Labhyata:",
                "Stock:": "Stock paristhiti:",
                "Out of stock": "Stock lo ledu",
                "In stock": "Stock lo undi",
            }.items():
                localized_draft = localized_draft.replace(source, target)
            return localized_draft

        # Catalog heading.

        if (

            "here are the closest matches in our catalog:"

            in lower

        ):

            draft = draft.replace(

                "Here are the closest matches in our catalog:",

                "Mana catalog lo closest matches ivi:",

            )

        if (

            "please confirm the latest price, exact variant, "

            "and availability with the store."

            in lower

        ):

            draft = draft.replace(

                "Please confirm the latest price, exact variant, "

                "and availability with the store.",

                "Latest price, exact variant mariyu availability "

                "store tho confirm chesukondi.",

            )

        return draft

    # -----------------------------------------------------

    # TELUGU

    # -----------------------------------------------------

    if language == "telugu":

        replacements = {
            "If the price and availability work for you, would you like to proceed with the purchase?": "ధర మరియు లభ్యత మీకు అనుకూలంగా ఉంటే, ఫోన్ కొనుగోలు చేయాలనుకుంటున్నారా?",
            "I couldn't find an exact match in the demo catalog.": "డెమో ఉత్పత్తుల జాబితాలో సరిపోలే నమూనా దొరకలేదు.",
            "The store can confirm the model, price, and availability.": "నమూనా, ధర మరియు లభ్యతను దుకాణంతో నిర్ధారించుకోండి.",
            "What's your budget for the mobile?": "మీ బడ్జెట్ ఎంత?",
            "When are you planning to buy it?": "ఎప్పుడు కొనాలనుకుంటున్నారు?",
            "Are you looking for a brand-new or a second-hand/refurbished phone?": "కొత్త ఫోన్ కావాలా, లేక వాడిన లేదా పునరుద్ధరించిన ఫోన్ అయినా సరేనా?",
            "How would you prefer to pay—cash, card, UPI, or EMI?": "చెల్లింపు నగదుగా, కార్డు ద్వారా, UPI ద్వారా లేదా EMI ద్వారా చేయాలనుకుంటున్నారు?",
            "Which city or town are you purchasing from?": "మీరు ఏ నగరం లేదా పట్టణం నుంచి కొనుగోలు చేస్తున్నారు?",
            "Which mobile are you interested in? Please tell me the brand and model.": "మీకు కావాల్సిన మొబైల్ బ్రాండ్ మరియు మోడల్ చెప్పండి.",
            "Sorry, I couldn't process that message properly.": "క్షమించండి, మీ మెసేజ్‌ను సరిగ్గా ప్రాసెస్ చేయలేకపోయాను.",
            "Could you please rephrase it?": "దయచేసి మరోసారి చెప్పండి.",
            "Please confirm the latest price, exact variant, and availability with the store.": "తాజా ధర, సరైన రూపాంతరం మరియు లభ్యతను దుకాణంతో నిర్ధారించుకోండి.",


            "Which mobile are you interested in? "

            "Please tell me the brand and model.": (

                "మీకు కావాల్సిన మొబైల్ బ్రాండ్ మరియు మోడల్ చెప్పండి."

            ),

            "What's your budget for the mobile?": (

                "మీ బడ్జెట్ ఎంత?"

            ),

            "When are you planning to buy it?": (

                "ఎప్పుడు కొనాలనుకుంటున్నారు?"

            ),

            "Are you looking for a brand-new or a "

            "second-hand/refurbished phone?": (

                "కొత్త ఫోన్ కావాలా, లేక "

                "సెకండ్ హ్యాండ్/రిఫర్బిష్డ్ అయినా పర్లేదా?"

            ),

            "How would you prefer to pay—cash, card, UPI, or EMI?": (

                "పేమెంట్ ఎలా చేయాలనుకుంటున్నారు — "

                "క్యాష్, కార్డ్, UPI లేదా EMI?"

            ),

            "Which city or town are you purchasing from?": (

                "మీరు ఏ సిటీ/టౌన్ నుంచి కొనుగోలు చేస్తున్నారు?"

            ),

            "Got it — I've updated your requirements. "

            "What would you like to know about the phone?": (

                "సరే 👍 మీ అవసరాలను అప్‌డేట్ చేసుకున్నాను. "

                "ఫోన్ గురించి ఇంకా ఏమి తెలుసుకోవాలి?"

            ),

            "I've retained your requirements. "

            "What would you like to know about the phone?": (

                "సరే 👍 మీ అవసరాలను నోట్ చేసుకున్నాను. "

                "ఫోన్ గురించి ఇంకా ఏమి తెలుసుకోవాలి?"

            ),

            "Thank you! I've recorded your requirements for Mania Mobiles. "

            "Our team can follow up with you about the phone and confirm "

            "the latest price and availability.": (

                "సరే 👍 మీ అవసరాలను నోట్ చేసుకున్నాను. "

                "ఫోన్ తాజా ధర మరియు లభ్యతను స్టోర్‌తో నిర్ధారించుకోండి."

            ),

            "I couldn't find an exact match in the demo catalog. "

            "The store can confirm the model, price, and availability.": (

                "డెమో క్యాటలాగ్‌లో exact match దొరకలేదు. "

                "నమూనా, ధర మరియు లభ్యతను దుకాణంతో నిర్ధారించుకోండి."

            ),

            "Please confirm the latest price, exact variant, "

            "and availability with the store.": (

                "తాజా ధర, exact variant మరియు లభ్యతను "

                "స్టోర్‌తో నిర్ధారించుకోండి."

            ),

            "The latest price should be confirmed with the Mania Mobiles "

            "store, as prices and offers may change.": (

                "తాజా ధరను Mania Mobiles దుకాణంతో నిర్ధారించుకోండి. "

                "ధరలు మరియు రాయితీలు మారవచ్చు."

            ),

            "I can't confirm live stock from this chat. Please ask the "

            "Mania Mobiles store to verify the exact model and variant.": (

                "ఈ సంభాషణ ద్వారా ప్రస్తుత నిల్వను నిర్ధారించలేను. "

                "సరైన నమూనా, రూపాంతరాన్ని దుకాణంతో నిర్ధారించుకోండి."

            ),

            "Delivery availability, charges, and timing need to be "

            "confirmed by the Mania Mobiles store for your location.": (

                "సరుకు చేరవేత లభ్యత, రుసుములు మరియు సమయాన్ని "

                "మీ ప్రాంతానికి దుకాణంతో నిర్ధారించుకోండి."

            ),

            "Battery health or backup depends on the exact model and unit. "

            "I can't confirm a specific battery condition unless it is "

            "verified by the store.": (

                "బ్యాటరీ ఆరోగ్యం లేదా పనిచేసే సమయం నిర్దిష్ట నమూనా, పరికరంపై "

                "ఆధారపడి ఉంటుంది. బ్యాటరీ పరిస్థితిని "

                "దుకాణం పరిశీలించిన తర్వాతే నిర్ధారించగలను."

            ),

            "EMI eligibility, bank offers, and discounts can change. "

            "Please confirm the current offers with the Mania Mobiles "

            "store. Don't share your card number, CVV, or OTP in this chat.": (

                "EMI అర్హత, బ్యాంకు రాయితీలు మరియు తగ్గింపులు మారవచ్చు. "

                "ప్రస్తుత రాయితీలను దుకాణంతో నిర్ధారించుకోండి. "

                "కార్డు సంఖ్య, CVV లేదా OTP ను ఈ సంభాషణలో పంచుకోకండి."

            ),

        }

        for source, target in replacements.items():
            if source.lower() == lower:
                return target

        # Also translate known English fragments inside combined drafts,
        # such as catalog fallback + the next required question.
        for source, target in sorted(replacements.items(), key=lambda x: len(x[0]), reverse=True):
            if source.lower() in draft.lower():
                draft = re.sub(re.escape(source), target, draft, flags=re.IGNORECASE)

        for source, target in {
            "Product:": "ఉత్పత్తి:",
            "Price:": "ధర:",
            "Original price:": "అసలు ధర:",
            "Current price:": "ప్రస్తుత ధర:",
            "Availability:": "లభ్యత:",
            "Stock:": "నిల్వ స్థితి:",
            "Out of stock": "స్టాక్‌లో లేదు",
            "In stock": "స్టాక్‌లో ఉంది",
        }.items():
            draft = draft.replace(source, target)
        return draft

    return draft

# ---------------------------------------------------------

# FINAL RESPONSE GENERATION

# ---------------------------------------------------------

def generate_response(
    draft_response: str,
    customer_message: str,
    language: Optional[str] = None,
    customer: Optional[dict] = None,
    conversation_history: Optional[list] = None,
    catalog_context: Optional[list] = None,
    force_llm: bool = False,
) -> str:
    """Generate a grounded, context-aware WhatsApp reply when an API is configured.

    If no provider/key is configured or the provider fails, use the existing
    deterministic localization. The conversation and catalogue code remain the
    source of truth; the model only writes the customer-facing wording.
    """
    draft_response = (draft_response or "").strip()
    customer_message = (customer_message or "").strip()
    if not draft_response:
        return ""
    if language is None:
        language = detect_language(customer_message)
    if language not in {"english", "telugu", "roman_telugu"}:
        language = detect_language(customer_message)
    if language not in {"english", "telugu", "roman_telugu"}:
        language = "roman_telugu"

    # Response rewriting is optional because the conversation layer already
    # constructs the business decision and draft. By default, keep that draft
    # and use deterministic localization to avoid an extra paid model request
    # for every customer message. Set LLM_RESPONSE_REWRITE_ENABLED=true to
    # restore model-assisted wording without changing the public API.
    rewrite_enabled = os.getenv(
        "LLM_RESPONSE_REWRITE_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # Even when rewriting every normal response is disabled, invoke the model
    # for a business-related fallback that indicates the deterministic handler
    # did not understand the customer. The model receives the locked language,
    # known customer fields, history and catalogue context; it must not invent
    # prices/stock/policies that are absent from that context.
    draft_lower = draft_response.casefold()
    fallback_markers = (
        "didn't understand", "did not understand", "couldn't understand",
        "could not understand", "sarigga connect cheyyaledu",
        "mee message ni context tho", "mee message ni sarigga",
        "what would you like to know about the phone", "which mobile model are we discussing",
        "ye mobile model gurinchi matladuthunnamo", "ye mobile model gurinchi matladutunnamo",
        "meeku ye mobile kavali? brand mariyu model peru cheppandi",
        "మీ మాటను సరిగా అర్థం చేసుకోలేదు",
    )
    fallback_needs_llm = any(marker in draft_lower for marker in fallback_markers)
    if not rewrite_enabled and not fallback_needs_llm and not force_llm:
        return _localized_fallback(
            draft_response=draft_response,
            language=language,
            customer=customer,
        )

    prompt = build_customer_response_prompt(
        customer_message=customer_message,
        draft_response=draft_response,
        response_language=language,
        customer=customer,
        conversation_history=conversation_history,
        catalog_context=catalog_context,
    )
    generated = _call_text_model(
        BUSINESS_SYSTEM_PROMPT,
        prompt,
        temperature=0.35,
        json_mode=False,
    )
    if generated:
        generated = generated.strip().strip('"')
        # Guard against language-script drift. Keep product names and model
        # acronyms, but don't return a full response in the wrong script.
        has_telugu_script = bool(re.search(r"[\u0C00-\u0C7F]", generated))
        roman_markers = re.search(
            r"\b(meeku|mee|kavali|undi|undhi|ledu|cheppandi|chesi|chestha|"
            r"parledhu|sare|konali|entha|ela|inka|kosam|chesukondi|"
            r"avuthundi|avutundi|chudandi|help chestha)\b",
            generated.lower(),
        )
        if language == "telugu" and has_telugu_script:
            return generated[:1800]
        if language == "roman_telugu" and not has_telugu_script and roman_markers:
            return generated[:1800]
        if language == "english" and not has_telugu_script and not roman_markers:
            return generated[:1800]
        # If the provider returns the wrong language/script, use the safe fallback.

    return _localized_fallback(
        draft_response=draft_response,
        language=language,
        customer=customer,
    )
