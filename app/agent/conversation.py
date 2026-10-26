import os

import re

from datetime import datetime, timedelta, timezone

from threading import Lock, RLock

from typing import Any, Dict, Optional

from weakref import WeakValueDictionary

from app.agent.llm import (

    extract_customer_details,

    detect_language,

    generate_response,

    answer_unhandled_message,

    extract_deterministic_details,

    has_concrete_signal,

)

from app.catalog.product_lookup import search_products, format_product

from app.qualification.scoring import calculate_intent

from app.database.storage import save_lead, update_lead, get_latest_lead_by_phone

# In-process session cache. Missing sessions are recovered from Supabase when

# the customer sends another message, so a worker restart does not discard the

# saved lead or conversation history.

sessions: Dict[str, Dict[str, Any]] = {}

_SESSION_LOCKS = WeakValueDictionary()

_SESSION_LOCKS_GUARD = Lock()

def _customer_lock(customer_id: str) -> Any:

    """Serialize turns for one customer within a worker process."""

    with _SESSION_LOCKS_GUARD:

        lock = _SESSION_LOCKS.get(customer_id)

        if lock is None:

            lock = RLock()

            _SESSION_LOCKS[customer_id] = lock

        return lock

QUESTIONS = {

    "product_interest": (

        "Which mobile are you interested in? "

        "Please tell me the brand and model."

    ),

    "budget": "What's your budget for the mobile?",

    "purchase_timeline": "When are you planning to buy it?",

    "condition": (

        "Are you looking for a brand-new or a "

        "second-hand/refurbished phone?"

    ),

    "location": "Which city or town are you purchasing from?",

}

# This is asked after the six required qualification questions.

INTENT_QUESTION = (

    "If the price and availability work for you, would you like to proceed "

    "with the purchase?"

)

FIELD_ORDER = [
    # Ask for the exact model first. Once known, show catalogue variants and ask
    # budget in the same message. Storage/color may be answered before or after
    # budget, but must be resolved before moving to condition.
    "product_interest",
    "budget",
    "storage",
    "purchase_timeline",
    "condition",
    "location",
]

# Preserve the historical restore contract used by existing saved sessions:
# restore the six core qualification fields in their original order. Storage
# is still required during the live flow (FIELD_ORDER), but it must not change
# the expected asked_field when recovering a legacy session.
RESTORE_FIELD_ORDER = [
    "product_interest",
    "budget",
    "storage",
    "purchase_timeline",
    "condition",
    "location",
]

# Additional details that must be preserved in the lead record.

EXTRA_FIELDS = {

    "storage",

    "preferences",

    "battery_health_min",

    "battery_health",

    "use_case",

    "comparison_brands",

    "product_question",

    "color",

    "target_price",

    "budget_amount",

    "budget_min",

    "budget_max",

    "brand",

    "model",

    "shopping_mode",
    # Persist budget/availability follow-up requests in the existing customer
    # JSON so the dashboard can display them without a schema migration.
    "availability_notification_requested",
    "availability_notification_model",
    "availability_notification_budget",
    "availability_notification_status",

}

def _get_session(customer_id: str) -> dict:

    if customer_id not in sessions:

        sessions[customer_id] = {

            "customer": {},

            "asked_field": "product_interest",

            "lead_saved": False,

            "lead_id": None,

            "qualification": None,

            # Language is chosen from the first inbound message and locked.

            # Telugu Unicode on turn one => Telugu script; anything else => Roman Telugu.

            "language": "roman_telugu",

            "language_locked": False,

            "record_id": None,

            "last_activity": None,

            # Intent tracking.

            "intent": None,

            "intent_history": [],

            # Full customer conversation record.

            "conversation_history": [],

            # The final willingness-to-buy question.

            "intent_question_asked": False,

            # Prevent repeated catalog replies for the same product.

            "catalog_message_sent": False,

            "catalog_product": None,
            "compact_sales_flow": False,
            "catalog_variant_selection_required": False,

        }

    return sessions[customer_id]

def _is_specific_model_name(value: Any) -> bool:
    """True when a customer has chosen an actual model, not only a brand/series."""
    text = re.sub(r"\b\d{1,4}\s*(?:gb|tb)\b", "", str(value or ""), flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if not text:
        return False
    generic = {
        "mobile", "mobiles", "phone", "phones", "smartphone", "android phone",
        "iphone", "apple", "samsung", "samsung galaxy", "samsung s series",
        "samsung a series", "samsung m series", "oneplus", "oppo", "vivo",
        "realme", "redmi", "xiaomi", "google pixel", "pixel", "motorola",
        "nothing", "nothing phone", "android", "any phone", "any mobile",
    }
    if text in generic or re.fullmatch(r"(?:samsung )?(?:galaxy )?[samz] series", text):
        return False
    # Most consumer phone models contain a model number (iPhone 15, Galaxy A55,
    # Pixel 9, Redmi Note 13, OnePlus Nord 4, etc.).
    if re.search(r"\d{1,3}", text):
        return True
    # Recognize named product variants which do not always use a simple number.
    return bool(re.search(r"\b(airpods pro|airpods max|iphone se)\b", text))


def _is_brand_only(value: Any) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return text in {
        "samsung", "apple", "iphone", "oneplus", "oppo", "vivo", "realme",
        "redmi", "xiaomi", "google pixel", "pixel", "motorola", "nothing",
        "samsung galaxy", "samsung s series", "samsung a series", "samsung m series",
    }


def _field_is_complete(field: str, customer: dict, session: Optional[dict] = None) -> bool:

    # The production sales flow intentionally collects only the product/variant
    # and one purchase-intent answer. Budget, timeline, condition and location
    # are not mandatory questions; infer an interest band from the viewed item
    # and let the owner follow up. Legacy/restored sessions keep their old rules.
    if session and session.get("compact_sales_flow") and field != "product_interest":
        return True

    value = customer.get(field)
    if field == "storage" and session and session.get("catalog_variant_unresolved"):
        return False

    # A brand by itself is not an exact phone variant. Keep asking for the model
    # until the customer chooses one, unless they explicitly want recommendations.
    if field == "product_interest":
        if customer.get("shopping_mode") == "recommendation":
            return True
        return _is_specific_model_name(customer.get("model") or value)

    if field == "storage":
        if customer.get("shopping_mode") == "recommendation" and not value:
            return True
        # Storage is required. Color is optional: the agent may ask once, but
        # must continue if the customer skips it or answers another field.
        return bool(str(value or "").strip())

    # Payment method is deliberately not part of lead qualification. EMI is
    # answered by the store FAQ handler, never collected as a payment question.
    if field == "payment_method":
        return True

    if field == "budget":

        if isinstance(value, dict):

            try:

                minimum = float(value.get("min", 0))

                maximum = float(value.get("max", 0))

                return (

                    minimum > 0

                    and maximum >= minimum

                )

            except (TypeError, ValueError):

                return False

        try:

            return (

                value is not None

                and float(value) > 0

            )

        except (TypeError, ValueError):

            return False

    return bool(

        str(value or "").strip()

    )

def _format_budget(budget: Any) -> str:

    if isinstance(budget, dict):

        minimum = budget.get("min")

        maximum = budget.get("max")

        try:

            minimum = float(minimum)

            maximum = float(maximum)

            if minimum == maximum:

                return f"₹{minimum:,.0f}"

            return (

                f"₹{minimum:,.0f}–"

                f"₹{maximum:,.0f}"

            )

        except (TypeError, ValueError):

            return str(budget)

    try:

        return f"₹{float(budget):,.0f}"

    except (TypeError, ValueError):

        return str(budget)

def _search_catalog(product_interest: str) -> list:

    """

    Search using the complete product interest first.

    If no match is found, retry without a storage size such as 128GB.

    """

    if not product_interest:

        return []

    matches = search_products(

        query=product_interest,

        limit=3,

    )

    if matches:

        return matches

    simpler_query = re.sub(

        r"\b\d+\s?gb\b",

        "",

        product_interest,

        flags=re.IGNORECASE,

    ).strip()

    if (

        simpler_query

        and simpler_query.lower()

        != product_interest.lower()

    ):

        return search_products(

            query=simpler_query,

            limit=3,

        )

    return []

def _merge_customer_details(

    customer: dict,

    extracted: dict,

) -> None:

    """

    Preserve required and additional fields.

    Ignore None and blank values so existing details

    aren't erased.

    """

    allowed_fields = (

        set(FIELD_ORDER)

        | EXTRA_FIELDS

    )

    for field, value in extracted.items():

        if field not in allowed_fields:

            continue

        if value is None:

            continue

        if (

            isinstance(value, str)

            and not value.strip()

        ):

            continue

        # Do not let a generic follow-up

        # erase a specific model already supplied.

        if (

            field in {"product_interest", "model"}

            and isinstance(value, str)

        ):

            generic = {

                "phone",

                "mobile",

                "smartphone",

                "android phone",

                "iphone",

                "a phone",

                "a mobile",

                "any phone",

                "any mobile",

                "google phone",

            }

            if value.strip().lower() in generic:

                previous = customer.get(field)

                if (

                    isinstance(previous, str)

                    and previous.strip().lower()

                    not in generic

                ):

                    continue

        # Preserve the product-question priority logic.

        if field == "product_question":

            previous_q = customer.get(field)

            if (

                previous_q

                and isinstance(value, str)

            ):

                # A later stock/availability question

                # should not erase an explicit price question.

                if (

                    previous_q == "price"

                    and value == "availability"

                ):

                    continue

                if value == "price":

                    customer[field] = value

                    continue

                if (

                    previous_q == "EMI offer"

                    and value == "availability"

                ):

                    continue

            customer[field] = value

            continue

        customer[field] = value

def _is_specific_product_mentioned(message: str, product_interest: str = "") -> bool:

    """Return True only when the current message contains a specific product cue.

    Generic requests such as "mobile kavali", "phone price", or "show mobiles"

    must not trigger a product card/link. At least one meaningful model/brand

    token from the extracted product must be present in the customer's message.

    """

    raw = (message or "").strip().lower()

    product = (product_interest or "").strip().lower()

    if not raw or not product:

        return False

    generic = {

        "mobile", "mobiles", "phone", "phones", "smartphone", "smartphones",

        "mobile phone", "any phone", "any mobile", "a phone", "a mobile",

        "phone kavali", "mobile kavali", "iphone", "android phone",

    }

    if product in generic:
        # A brand-only request such as "iPhone" is not specific enough to
        # select a catalogue item. Ask which model instead of guessing.
        if product == "iphone":
            return bool(
                re.search(
                    r"\biphone\s*(?:\d{1,2}|se|air|fold)\b"
                    r"(?:\s*(?:pro|max|plus|mini|e))?",
                    raw,
                )
            )
        return False

    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    raw_norm = normalize(raw)

    product_norm = normalize(product)

    if not product_norm:

        return False

    if product_norm in raw_norm:

        return True

    ignored = {

        "the", "a", "an", "and", "or", "with", "for", "want", "need",

        "show", "price", "cost", "how", "much", "is", "do", "you", "have",

        "please", "buy", "purchase", "mobile", "phone", "smartphone", "new",

        "brand", "model", "under", "below", "around", "in", "our", "catalog",

    }

    product_tokens = [t for t in product_norm.split() if t not in ignored and len(t) > 1]

    message_tokens = set(raw_norm.split())

    return bool(product_tokens and any(token in message_tokens for token in product_tokens))

def _catalog_variant_matches(requested_variant: str, catalog_name: str) -> bool:
    """Avoid sending a nearby but wrong model (for example Pro vs base model)."""
    requested = re.sub(r"[^a-z0-9]+", " ", (requested_variant or "").lower()).strip()
    candidate_raw = (catalog_name or "").lower()
    candidate = re.sub(r"[^a-z0-9]+", " ", candidate_raw).strip()
    if not requested or not candidate:
        return False

    requested_storage = re.search(r"\b(\d{1,4})\s*(gb|tb)\b", requested_variant or "", re.I)
    candidate_storage = re.search(r"\b(\d{1,4})\s*(gb|tb)\b", catalog_name or "", re.I)
    if requested_storage and (
        not candidate_storage
        or requested_storage.group(1) != candidate_storage.group(1)
        or requested_storage.group(2).lower() != candidate_storage.group(2).lower()
    ):
        return False

    # Require every meaningful requested model token, but permit a catalogue brand prefix.
    ignored = {"apple", "samsung", "mobile", "phone", "smartphone", "the", "with"}
    requested_tokens = [
        token for token in requested.split()
        if token not in ignored and not re.fullmatch(r"\d{1,4}(?:gb|tb)", token)
    ]
    if not requested_tokens or not all(token in candidate.split() for token in requested_tokens):
        return False

    modifiers = {"pro", "max", "plus", "mini", "ultra", "air", "fold", "flip", "fe"}
    requested_modifiers = modifiers.intersection(requested.split())
    candidate_modifiers = modifiers.intersection(candidate.split())
    if candidate_modifiers - requested_modifiers:
        return False
    return True


def _catalog_color(product: dict) -> str:
    attrs = product.get("attributes") or {}
    color = attrs.get("color") or product.get("color") or ""
    if color:
        return str(color).strip().title()
    name = str(product.get("product_name") or "")
    match = re.search(r"\b(Black|White|Blue|Pink|Green|Teal|Ultramarine|Yellow|Purple|Natural|Titanium|Silver|Gold|Midnight|Starlight|Graphite|Desert|Sierra|Red)\s+Color\b", name, re.I)
    return match.group(1).title() if match else ""


def _catalog_storage(product: dict) -> str:
    attrs = product.get("attributes") or {}
    value = attrs.get("storage") or (product.get("specifications") or {}).get("storage") or ""
    if value:
        return str(value).upper().replace(" ", "")
    name = str(product.get("product_name") or "")
    match = re.search(r"\b(64|128|256|512|1024)\s*(GB|TB)\b", name, re.I)
    return f"{match.group(1)}{match.group(2).upper()}" if match else ""


def _catalog_products_for_model(model: str, storage: str = "", color: str = "") -> list:
    """Return catalog rows for the exact model and optional storage/color filters."""
    model = re.sub(r"\s+", " ", (model or "").strip())
    if not model or not _is_specific_model_name(model):
        return []
    try:
        products = search_products(query=model, limit=200)
    except TypeError:
        products = search_products(query=model, limit=100)
    except Exception as exc:
        print("Catalog variants lookup failed:", type(exc).__name__)
        return []
    wanted_storage = re.sub(r"\s+", "", str(storage or "").upper())
    wanted_color = str(color or "").strip().casefold()
    result = []
    for item in products:
        item_model = str(item.get("model") or "").strip()
        item_name = str(item.get("product_name") or "").strip()
        # Prefer an exact model field match. If absent, require model tokens and
        # reject Pro/Max/Plus/Ultra etc. substitutions.
        # The products table may store model with a brand prefix, storage suffix,
        # or variant descriptor (e.g. "Apple iPhone 16 128GB"). Apply the same
        # strict variant matcher to both model and title fields; it still rejects
        # Pro/Max/Plus substitutions and mismatched storage.
        candidate_model = item_model or item_name
        if not _catalog_variant_matches(model, candidate_model):
            continue
        item_storage = _catalog_storage(item)
        item_color = _catalog_color(item)
        if wanted_storage and item_storage != wanted_storage:
            continue
        if wanted_color and item_color.casefold() != wanted_color:
            continue
        result.append(item)
    # Deduplicate exact URLs, preserve catalog order.
    unique = []
    seen = set()
    for item in result:
        key = item.get("product_url") or item.get("product_name")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _catalog_price_text(product: dict, language: str) -> str:
    price = product.get("price_inr")
    needs_verification = product.get("price_needs_verification") is not False
    if price is None:
        return "ధర స్టోర్‌తో నిర్ధారించాలి" if language == "telugu" else "price store tho confirm cheyyali"
    try:
        price_text = f"₹{float(price):,.0f}"
    except (TypeError, ValueError):
        return "ధర స్టోర్‌తో నిర్ధారించాలి" if language == "telugu" else "price store tho confirm cheyyali"
    if needs_verification:
        return (f"కాటలాగ్‌లో చూపిన ధర {price_text} (తాజా ధర నిర్ధారించాలి)" if language == "telugu"
                else f"catalog lo listed price {price_text} (latest price confirm cheyyali)")
    return (f"ధర {price_text}" if language == "telugu" else f"price {price_text}")


def _catalog_stock_text(product: dict, language: str) -> str:
    status = str(product.get("stock_status") or "unknown").lower()
    if status == "in_stock":
        return "కాటలాగ్‌లో స్టాక్ ఉన్నట్లు ఉంది; మళ్లీ నిర్ధారించాలి" if language == "telugu" else "catalog status in stock; recheck cheyyali"
    if status == "out_of_stock":
        return "కాటలాగ్‌లో ప్రస్తుతం స్టాక్ లేదని ఉంది; స్టోర్‌తో నిర్ధారించాలి" if language == "telugu" else "catalog status out of stock; store tho confirm cheyyali"
    return "స్టాక్‌ను స్టోర్‌తో నిర్ధారించాలి" if language == "telugu" else "stock store tho confirm cheyyali"


def _compact_price(product: dict) -> str:
    """Short price for WhatsApp lists. Unverified prices are flagged, never hidden."""
    try:
        text = f"₹{float(product.get('price_inr')):,.0f}"
    except (TypeError, ValueError):
        return "price store tho confirm"
    return text if product.get("price_needs_verification") is False else text + "*"


def _compact_option_line(product: dict, fallback_name: str = "") -> str:
    """One short line: name, price, colour, battery health. No link, no stock text."""
    name = str(product.get("product_name") or fallback_name).strip()
    parts = [_compact_price(product)]
    color = _catalog_color(product)
    if color and color.casefold() not in name.casefold():
        parts.append(color)
    battery = _catalog_battery_health(product)
    if battery and battery.lower() not in {"unknown", "n/a", "na", "none"}:
        parts.append(f"Battery {battery if '%' in battery else battery + '%'}")
    return f"• {name} — " + " | ".join(parts)


def _compact_list_note(items: list, language: str) -> str:
    unverified = any(item.get("price_needs_verification") is not False for item in items)
    if language == "telugu":
        return ("* ధర స్టోర్‌తో నిర్ధారించాలి." if unverified else "తాజా ధర, స్టాక్‌ను స్టోర్‌తో నిర్ధారించండి.")
    return ("* price store tho confirm cheyyali." if unverified else "Latest price/stock store tho confirm cheyyandi.")


MAX_LIST_OPTIONS = 5


def _purchase_link_line(customer: dict, language: str) -> str:
    """The single product link, sent only once the customer confirms buying.

    Returns "" unless the customer's chosen variant resolves to exactly one
    catalogue product with a real URL; a link is never guessed.
    """
    model = str(customer.get("model") or customer.get("product_interest") or "").strip()
    if not model:
        return ""
    try:
        items = _catalog_products_for_model(
            model,
            storage=str(customer.get("storage") or ""),
            color=str(customer.get("color") or customer.get("colour") or ""),
        )
    except Exception as exc:
        print("Purchase link lookup failed:", type(exc).__name__)
        return ""
    urls = {
        str(item.get("product_url") or "").strip()
        for item in items
        if str(item.get("product_url") or "").strip().startswith(("https://", "http://"))
    }
    if len(urls) != 1:
        return ""
    url = next(iter(urls))
    if language == "telugu":
        return f"మీరు వెబ్‌సైట్ నుండి కూడా ఆర్డర్ చేయవచ్చు: {url}"
    return f"Website nundi kuda order chesukovachu: {url}"


def _catalog_model_options_message(model: str, language: str, storage: str = "") -> str:
    """Summarize only catalog-backed storage/color options and listed prices."""
    items = _catalog_products_for_model(model, storage=storage)
    if not items:
        if language == "telugu":
            return f"{model} మా డెమో కాటలాగ్‌లో ఉంది అని నిర్ధారించలేకపోయాను. సరైన వేరియంట్, ధర, స్టాక్‌ను స్టోర్‌తో నిర్ధారించాలి."
        return f"{model} exact variants maa demo catalog lo dorakaledu. Correct variant, price, stock store tho confirm cheyyali."
    lines, seen = [], set()
    for item in items:
        line = _compact_option_line(item, model)
        if line not in seen:
            seen.add(line)
            lines.append(line)
        if len(lines) >= MAX_LIST_OPTIONS:
            break
    if language == "telugu":
        title = f"{model} కోసం కాటలాగ్‌లో కనిపించిన వేరియంట్లు:"
        ask = (f"మీకు {storage}లో ఏ కలర్ కావాలి?" if storage else "మీకు ఏ స్టోరేజ్, కలర్ కావాలి?")
        budget = "మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
        note = _compact_list_note(items, language)
    else:
        title = f"{model} ki catalog lo kanipinchina variants:"
        ask = (f"{storage} lo ye color kavali?" if storage else "Ye storage, color kavali?")
        budget = "Mee budget entha anukuntunnaru?"
        note = _compact_list_note(items, language)
    return title + "\n" + "\n".join(lines) + "\n\n" + note + "\n\n" + ask + "\n" + budget


def _catalog_condition(product: dict) -> str:
    attrs = product.get("attributes") or {}
    return str(attrs.get("condition") or product.get("condition") or "").strip().lower()


def _catalog_battery_health(product: dict) -> str:
    attrs = product.get("attributes") or {}
    value = attrs.get("battery_health") or product.get("battery_health") or ""
    return str(value).strip()


def _catalog_is_new(product: dict) -> bool:
    condition = _catalog_condition(product)
    return any(token in condition for token in ("brand new", "brand-new", "sealed", "new condition")) or condition == "new"


def _catalog_price_sort_value(product: dict) -> float:
    try:
        value = float(product.get("price_inr"))
        return value if value >= 0 else float("inf")
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def _customer_budget_ceiling(customer: dict) -> Optional[float]:
    """Return a customer-stated budget ceiling; never use inferred interest bands."""
    if not isinstance(customer, dict):
        return None

    budget = customer.get("budget")
    if isinstance(budget, dict):
        for key in ("max", "maximum", "budget_max", "amount"):
            try:
                value = float(budget.get(key))
                if value > 0:
                    return value
            except (TypeError, ValueError, AttributeError):
                continue
        return None

    for key in ("budget_max", "budget_amount", "target_price", "budget"):
        value = customer.get(key)
        if isinstance(value, dict):
            continue
        try:
            numeric = float(value)
            if numeric > 0:
                return numeric
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _remember_inferred_interest_band(customer: dict, product: dict) -> None:
    """Record a price-interest estimate only from an explicitly verified catalog price."""
    if product.get("price_needs_verification") is not False:
        return
    try:
        price = float(product.get("price_inr"))
    except (TypeError, ValueError, OverflowError):
        return
    if price <= 0:
        return
    customer["inferred_budget_min"] = max(0, int(price - 5000))
    customer["inferred_budget_max"] = int(price + 5000)
    customer["budget_source"] = "inferred_from_viewed_catalog_product_not_customer_stated"
    customer["interest_reference_price"] = int(price)


def _catalog_options_only_message(model: str, language: str, storage: str = "", color: str = "") -> str:
    """Show exact catalog-backed options, used/second-hand cheapest first and new last."""
    telugu = language == "telugu"
    items = _catalog_products_for_model(model, storage=storage, color=color)
    if not items:
        if telugu:
            return f"{model}ki saripoye exact variant catalog lo confirm kaaledu. Price, battery health, stock store tho confirm cheyyali."
        return f"{model} ki exact matching variant catalog lo confirm kaaledu. Price, battery health, stock store tho confirm cheyyali."

    # If both used and brand-new variants exist, show used options by ascending
    # price, then brand-new options last. Unknown conditions stay between them.
    def sort_key(item):
        condition = _catalog_condition(item)
        group = 2 if _catalog_is_new(item) else (0 if any(x in condition for x in ("used", "second hand", "second-hand", "refurbished", "pre-owned", "pre owned")) or re.search(r"\d{1,3}\s*%", _catalog_battery_health(item)) else 1)
        return (group, _catalog_price_sort_value(item), str(item.get("product_name") or "").casefold())

    items = sorted(items, key=sort_key)
    lines, seen = [], set()
    for item in items:
        line = _compact_option_line(item, model)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    extra = max(0, len(lines) - MAX_LIST_OPTIONS)
    lines = lines[:MAX_LIST_OPTIONS]
    if extra:
        lines.append(f"+{extra} inka options unnayi" if not telugu else f"+{extra} ఇంకా ఆప్షన్లు ఉన్నాయి")

    if telugu:
        title = f"{model} kosam catalog lo dorikina options (used phones takkuva price nunchi; brand-new last lo):"
        note = _compact_list_note(items, language)
        ask = "Veetilo meeku ye option kavali bro?"
    else:
        title = f"{model} ki catalog lo dorikina options (used phones low-to-high price; brand-new last):"
        note = _compact_list_note(items, language)
        ask = "Veetilo meeku ye option kavali bro?"
    return title + "\n" + "\n".join(lines) + "\n\n" + note + "\n\n" + ask

def _get_product_message(
    product_interest: str,
    customer_message: str = "",
    language: str = "roman_telugu",
    condition: str = "",
) -> str:
    """Return a catalog-grounded exact variant summary and URL when resolved."""
    if not product_interest:
        return ""
    is_telugu = language == "telugu"
    try:
        matches = _search_catalog(product_interest)
        matches = [
            item for item in matches
            if _catalog_variant_matches(product_interest, str(item.get("product_name") or item.get("model") or ""))
        ]
        requested_battery = re.search(r"(?<!\d)(\d{1,3})\s*%", f"{customer_message} {condition}")
        if requested_battery and re.search(r"\b(?:battery\s*health|battery\s*condition|bh)\b", f"{customer_message} {condition}", re.I):
            battery_matches = [
                item for item in matches
                if re.search(
                    rf"(?<!\d){re.escape(requested_battery.group(1))}\s*%",
                    _catalog_battery_health(item),
                )
            ]
            if not battery_matches:
                return (
                    f"మీరు అడిగిన {requested_battery.group(1)}% battery health variant catalog lo confirm kaaledu. Available battery-health options chupisthanu."
                    if is_telugu else
                    f"Meeru adigina {requested_battery.group(1)}% battery health variant catalog lo confirm kaaledu. Available battery-health options chupisthanu."
                )
            matches = battery_matches
        # Use exact model/storage/color catalog filtering first; it avoids a
        # similarly named color or open-box listing being selected by search rank.
        model = re.sub(r"\b\d{1,4}\s*(?:gb|tb)\b", "", product_interest, flags=re.I).strip()
        variant_query = customer_message or product_interest
        storage_match = re.search(r"\b(64|128|256|512|1024)\s*(gb|tb)\b", variant_query, re.I)
        color_match = re.search(r"\b(black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red)\b", variant_query, re.I)
        model = re.sub(r"\b(black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red)\b", "", model, flags=re.I)
        model = re.sub(r"\s+", " ", model).strip()
        exact = _catalog_products_for_model(
            model,
            storage=(f"{storage_match.group(1)}{storage_match.group(2)}" if storage_match else ""),
            color=(color_match.group(1) if color_match else ""),
        )
        if exact:
            matches = exact

        request_text = f"{customer_message} {condition}".lower()
        wants_used = (
            any(term in request_text for term in (
                "second hand", "second-hand", "used", "refurbished", "pre-owned", "pre owned"
            ))
            or bool(
                re.search(r"\b(?:battery\s*health|battery\s*condition|bh)\b", request_text)
                and re.search(r"(?<!\d)\d{1,3}\s*%", request_text)
            )
        )
        wants_new = any(term in request_text for term in (
            "brand new", "brand-new", "sealed", "new piece", "kotha phone", "kottha phone"
        ))

        def condition_matches(item, target):
            attrs = item.get("attributes") or {}
            item_condition = str(attrs.get("condition") or item.get("condition") or "").lower()
            battery_value = str(attrs.get("battery_health") or item.get("battery_health") or "").strip()
            is_used_item = (
                any(term in item_condition for term in (
                    "used", "second hand", "second-hand", "refurbished", "pre-owned", "pre owned"
                ))
                or bool(re.search(r"(?<!\d)\d{1,3}\s*%", battery_value))
            )
            is_new_item = (
                any(term in item_condition for term in ("brand new", "brand-new", "sealed"))
                or item_condition.strip() in {"new", "new condition"}
            )
            return is_used_item if target == "used" else is_new_item

        requested_condition = "used" if wants_used else ("new" if wants_new else "")
        if requested_condition:
            condition_source = exact or matches
            condition_filtered = [
                item for item in condition_source
                if condition_matches(item, requested_condition)
            ]
            if not condition_filtered:
                return (
                    "మీరు అడిగిన కండిషన్‌కు సరిపోయే వేరియంట్ కాటలాగ్‌లో నిర్ధారించబడలేదు. స్టోర్‌తో ధర, బ్యాటరీ హెల్త్, స్టాక్ నిర్ధారించాలి."
                    if is_telugu
                    else "Ee model ki meeru adigina condition match catalog lo confirm kaaledu. Store tho price, battery health, stock confirm cheyyali."
                )
            matches = condition_filtered
            exact = condition_filtered

        # If the customer has not chosen storage and the model has several
        # storage variants, show real catalog options rather than accidentally
        # quoting the first variant's price as if it were the requested one.
        if exact and not storage_match:
            storage_options = []
            seen_storage = set()
            for item in exact:
                storage_value = _catalog_storage(item)
                if storage_value and storage_value not in seen_storage:
                    seen_storage.add(storage_value)
                    storage_options.append((storage_value, item))
            if len(storage_options) > 1:
                title = (
                    f"మీరు అడిగిన మోడల్: {model}"
                    if is_telugu
                    else f"Mee model: {model}"
                )
                option_lines = []
                for storage_value, item in storage_options[:5]:
                    item_name = str(item.get("product_name") or item.get("model") or model).strip()
                    option_lines.append(f"- {storage_value}: {_compact_price(item)}")
                return title + "\\n" + "\\n".join(option_lines)

        if matches:
            product = matches[0]
            name = str(product.get("product_name") or product.get("model") or product_interest).strip()
            url = str(product.get("product_url") or "").strip()
            price = _catalog_price_text(product, language)
            stock = _catalog_stock_text(product, language)
            line = _compact_option_line(product, product_interest)[2:]
            note = _compact_list_note([product], language)
            if is_telugu:
                return f"మీరు అడిగిన ప్రోడక్ట్: {line}\n{note}"
            return f"Mee product: {line}\n{note}"
        if is_telugu:
            return "ఈ ఖచ్చితమైన వేరియంట్ మా డెమో కాటలాగ్‌లో కనిపించలేదు. ధర, స్టాక్‌ను స్టోర్‌తో నిర్ధారించాలి."
        return "Ee exact variant maa demo catalog lo dorakaledu. Price mariyu stock store tho confirm cheyyali."
    except Exception as exc:
        print("Product catalog lookup failed:", type(exc).__name__)
        return ("ప్రస్తుతం కాటలాగ్‌ను చెక్ చేయలేకపోతున్నాను. మళ్లీ ప్రయత్నించండి." if is_telugu
                else "Ippudu catalog check cheyalekapothunnanu. Konchem tarvatha try cheyyandi.")

def _handle_saved_lead_follow_up(

    customer_id: str,

    message: str,

    customer: dict,

    session: dict,

    extracted: dict,

) -> dict:

    """

    Handle follow-up messages after lead creation.

    Do not create a duplicate lead.

    """

    language = session.get("language", "roman_telugu")
    if _is_ai_identity_question(message):
        return {
            "customer_id": customer_id,
            "message": _ai_identity_reply(language),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": True,
        }
    if _is_price_objection(message) or _intent_response_kind(message) in {"positive", "compare"}:
        reply = _saved_purchase_acknowledgement(message, customer, language)
        return {
            "customer_id": customer_id,
            "message": reply,
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": True,
        }

    previous_product = customer.get("product_interest")

    extracted_product = extracted.get("product_interest")

    product_interest = (

        extracted_product

        or previous_product

    )

    product_message = ""

    # Only perform a new lookup when the customer actually

    # supplies a different product.

    new_product_received = (

        bool(extracted_product)

        and (

            not previous_product

            or str(extracted_product).strip().lower()

            != str(previous_product).strip().lower()

        )

    )

    variant_update = bool(
        product_interest
        and (extracted.get("storage") or extracted.get("color"))
    )
    if product_interest and (
        (extracted_product and _is_specific_product_mentioned(message, product_interest))
        or variant_update
    ):

        product_message = _get_product_message(
            product_interest,
            message,
            session.get("language", "roman_telugu"),
            condition=str(customer.get("condition") or ""),
        )

    question = str(

        extracted.get("product_question")

        or message

    ).strip()

    question_lower = question.lower()

    nonbuyer_mode = _nonbuyer_mode(message)

    if nonbuyer_mode:

        reply = _low_pressure_reply(nonbuyer_mode, session.get("language", "roman_telugu"))

        draft_reply = (product_message.strip() + "\n\n" if product_message else "") + reply

        return {

            "customer_id": customer_id,

            "message": _localize_reply(draft_reply, message, customer, session),

            "customer": customer,

            "qualification": session.get("qualification"),

            "intent": session.get("intent"),

            "intent_history": session.get("intent_history", []),

            "lead_id": session.get("lead_id"),

            "lead_saved": True,

        }

    if any(

        word in question_lower

        for word in (

            "reserve",

            "reservation",

            "hold",

            "book",

            "రీసర్వ్",

        )

    ):

        reply = (

            "I can note that you'd like to reserve "

            "the phone, but I can't confirm a reservation "

            "here. Please contact the Mania Mobiles store "

            "to confirm stock and whether they can hold "

            "it for you."

        )

    elif any(

        word in question_lower

        for word in (

            "delivery",

            "deliver",

            "shipping",

            "డెలివరీ",

        )

    ):

        reply = (

            "Delivery availability, charges, and timing "

            "need to be confirmed by the Mania Mobiles "

            "store for your location."

        )

    elif any(

        word in question_lower

        for word in (

            "battery health",

            "battery backup",

            "battery life",

        )

    ):

        reply = (

            "Battery health or backup depends on the exact "

            "model and unit. I can't confirm a specific "

            "battery condition unless it is verified "

            "by the store."

        )

    elif any(

        word in question_lower

        for word in (

            "emi",

            "bank offer",

            "credit card",

            "discount",

        )

    ):

        reply = (

            "EMI eligibility, bank offers, and discounts "

            "can change. Please confirm the current offers "

            "with the Mania Mobiles store. Don't share your "

            "card number, CVV, or OTP in this chat."

        )

    elif any(

        word in question_lower

        for word in (

            "price",

            "cost",

            "rate",

            "ధర",

        )

    ):

        reply = (

            "The latest price should be confirmed with "

            "the Mania Mobiles store, as prices and offers "

            "may change."

        )

    elif any(

        word in question_lower

        for word in (

            "stock",

            "available",

            "availability",

        )

    ):

        reply = (

            "I can't confirm live stock from this chat. "

            "Please ask the Mania Mobiles store to verify "

            "the exact model and variant."

        )

    else:

        # No explicit product question in this turn.

        # Do not invent a fulfilment action.

        if extracted:

            reply = (

                "Got it — I've updated your requirements. "

                "What would you like to know about the phone?"

            )

        else:

            reply = (

                "I've retained your requirements. "

                "What would you like to know about the phone?"

            )

    # If the requested model is not in the demo catalog, give the store-check
    # response only. Do not prepend the generic requirements boilerplate or
    # ask the customer to repeat a model they already specified.
    if product_message:
        session["catalog_message_sent"] = True
        session["catalog_product"] = product_interest
        # Ask only one next-step question. Product/storage details already
        # supplied by the customer must not be asked again.
        condition = str(customer.get("condition") or "").strip().lower()
        storage = str(customer.get("storage") or "").strip()
        budget = customer.get("budget") or customer.get("budget_amount") or customer.get("budget_max")
        if not storage:
            follow_up = (
                "ఏ స్టోరేజ్ వేరియంట్ కావాలి—128GB లేదా 256GB?"
                if session.get("language") == "telugu"
                else "Ye storage variant kavali bro—128GB leka 256GB?"
            )
        elif not condition:
            follow_up = (
                "మీకు కొత్త సీల్డ్ ఫోన్ కావాలా, లేక సెకండ్ హ్యాండ్ ఫోన్ కావాలా?"
                if session.get("language") == "telugu"
                else "Meeku brand-new sealed phone kavala, leka second-hand phone kavala?"
            )
        elif not budget:
            follow_up = (
                "మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
                if session.get("language") == "telugu"
                else "Mee budget entha anukuntunnaru bro?"
            )
        else:
            follow_up = (
                "ఈ ధరకు ఫోన్ అందుబాటులో ఉంటే కొనాలనుకుంటున్నారా?"
                if session.get("language") == "telugu"
                else "Ee price ki phone available ga unte konadaniki interested ga unnara bro?"
            )
        return {
            "customer_id": customer_id,
            "message": product_message + "\n\n" + follow_up,
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": True,
        }

    draft_reply = reply
    # This branch builds English draft text. Do NOT mark it as a final
    # template reply: _localize_reply must convert it to the conversation's
    # locked language (Telugu script or Roman Telugu) before it reaches WhatsApp.
    session.pop("_return_template_reply", None)
    return {
        "customer_id": customer_id,
        "message": _localize_reply(

            draft_reply,

            message,

            customer,

            session,

        ),

        "customer": customer,

        "qualification": session["qualification"],

        "intent": session["intent"],

        "intent_history": session["intent_history"],

        "lead_id": session["lead_id"],

        "lead_saved": True,

    }

def _is_explicit_english_message(

    message: str,

) -> bool:

    """

    Detect a genuine switch back to English.

    Short structured replies such as:

        25000

        45k

        128GB

        Hyderabad

        Cash

        Yes

    must NOT switch a Telugu/Roman-Telugu

    conversation back to English.

    """

    text = (

        message or ""

    ).strip().lower()

    if not text:

        return False

    # Telugu script is definitely not English.

    if re.search(

        r"[\u0C00-\u0C7F]",

        text,

    ):

        return False

    # Numeric answers inherit the current language.

    if re.fullmatch(

        r"\d+(?:[.,]\d+)?"

        r"(?:\s*(?:k|l|lac|lakh|lakhs))?",

        text,

    ):

        return False

    if re.fullmatch(

        r"\d+\s*(?:gb|tb)",

        text,

    ):

        return False

    # These are answer-like values,

    # not language switches.

    structured_replies = {

        "yes",

        "yeah",

        "yep",

        "no",

        "nope",

        "ok",

        "okay",

        "sure",

        "cash",

        "card",

        "upi",

        "emi",

        "hyderabad",

        "vijayawada",

        "bhimavaram",

        "rajahmundry",

        "kakinada",

        "128gb",

        "256gb",

        "512gb",

        "64gb",

    }

    if text in structured_replies:

        return False

    # Clear English sentences are allowed

    # to switch the conversation.

    english_phrases = (

        "in a week",

        "within a week",

        "in one week",

        "next week",

        "this week",

        "in a month",

        "within a month",

        "next month",

        "this month",

        "this weekend",

        "by tomorrow",

        "tomorrow",

        "today",

        "right now",

        "brand new",

        "new phone",

        "used phone",

        "second hand",

        "second-hand",

        "refurbished phone",

        "how much",

        "how long",

        "where can",

        "when can",

        "when should",

        "i want",

        "i need",

        "i would like",

        "i'd like",

        "can you",

        "could you",

        "please tell",

        "please show",

        "do you have",

        "is there",

        "are there",

        "which one",

        "tell me",

        "show me",

        "looking for",

        "interested in",

        "i want to buy",

        "i need to buy",

        "i would like to buy",

        "what is",

        "what's",

        "how many",

        "where is",

    )

    if any(

        phrase in text

        for phrase in english_phrases

    ):

        return True

    english_words = {

        "the",

        "this",

        "that",

        "with",

        "without",

        "for",

        "from",

        "need",

        "want",

        "looking",

        "buy",

        "buying",

        "price",

        "cost",

        "available",

        "availability",

        "stock",

        "budget",

        "phone",

        "mobile",

        "model",

        "variant",

        "storage",

        "location",

        "city",

        "cash",

        "card",

        "payment",

        "new",

        "used",

        "refurbished",

        "today",

        "tomorrow",

        "week",

        "weekend",

        "month",

        "under",

        "below",

        "upto",

        "please",

        "confirm",

        "check",

        "compare",

        "in",

        "a",

        "an",

        "is",

        "are",

        "it",

        "to",

        "of",

        "on",

        "at",

        "and",

        "or",

        "my",

        "your",

        "me",

        "you",

    }

    tokens = re.findall(

        r"[a-zA-Z]+",

        text,

    )

    if len(tokens) == 1:

        return tokens[0] in {

            "tomorrow",

            "today",

            "week",

            "weekend",

            "month",

            "price",

            "cost",

            "available",

            "availability",

            "stock",

            "payment",

            "delivery",

            "warranty",

            "discount",

            "exchange",

            "new",

            "used",

            "refurbished",

        }

    # Require reasonably clear English.

    return sum(

        token in english_words

        for token in tokens

    ) >= 2

def _resolve_response_language(message: str, session: dict) -> str:

    """Lock the conversation language from the first customer message.

    Telugu Unicode in the first message selects Telugu script. Every other

    first message (including "Hi", English, Roman Telugu, or numbers) selects

    Roman Telugu. Later turns never switch the language.

    """

    supported = {"telugu", "roman_telugu"}

    current = session.get("language")

    history = session.get("conversation_history") or []

    if not session.get("language_locked"):

        # Restore language from the earliest saved turn for older sessions.

        for item in history:

            if not isinstance(item, dict):

                continue

            saved_language = item.get("language")

            if saved_language in supported:

                session["language"] = saved_language

                session["language_locked"] = True

                return saved_language

        text = (message or "").strip()

        chosen = "telugu" if re.search(r"[\u0C00-\u0C7F]", text) else "roman_telugu"

        session["language"] = chosen

        session["language_locked"] = True

        return chosen

    if current in supported:

        return current

    chosen = "telugu" if re.search(r"[\u0C00-\u0C7F]", (message or "")) else "roman_telugu"

    session["language"] = chosen

    session["language_locked"] = True

    return chosen

def _localize_reply(

    draft: str,

    customer_message: str,

    customer: dict,

    session: dict = None,

) -> str:

    """

    Generate the reply using the already-resolved

    active language.

    """

    if session is None:

        session = {}

    if session.pop("_return_template_reply", False):

        return draft.strip()

    language = session.get(

        "language"

    )

    if language not in ("roman_telugu", "telugu"):

        language = _resolve_response_language(

            customer_message,

            session,

        )

        session["language"] = language

    print("Response language:", language)

    # Catalogue context is deliberately limited to a product named in THIS

    # customer message. Prior interest, saved brand/model, and comparison

    # preferences must never cause unsolicited product suggestions.

    catalog_context = []

    current_product = customer.get("product_interest") or customer.get("model") or ""

    if current_product and _is_specific_product_mentioned(

        customer_message, str(current_product)

    ):

        try:

            for product in _search_catalog(str(current_product))[:1]:

                product_name = str(

                    product.get("product_name") or product.get("model") or ""

                )

                if not _is_specific_product_mentioned(customer_message, product_name):

                    continue

                catalog_context.append({

                    "product_name": product_name,

                    "brand": product.get("brand"),

                    "model": product.get("model"),

                    "price": (

                        product.get("price_inr")

                        if product.get("price_needs_verification") is False

                        else None

                    ),

                    "price_needs_verification": product.get("price_needs_verification") is not False,

                    "stock_status": product.get("stock_status", "unknown"),

                    "attributes": product.get("attributes") or {},

                    "short_description": product.get("short_description"),

                    "description": str(product.get("description") or "")[:500],

                    "product_url": product.get("product_url"),

                })

        except Exception as exc:

            print(f"Could not prepare catalogue context: {type(exc).__name__}: {exc}")

    response = generate_response(

        draft_response=draft,

        customer_message=customer_message,

        language=language,

        customer=customer,

        conversation_history=session.get("conversation_history", [])[-8:],

        catalog_context=catalog_context[:6],

    )

    # Guard against the model returning its English boilerplate verbatim.
    # The language is locked from the customer's first message, so use a short
    # deterministic greeting instead of sending English to a Telugu customer.
    response_text = str(response or "").strip()
    boilerplate = "got it — i've updated your requirements"
    if boilerplate in response_text.lower() or "what would you like to know about the phone" in response_text.lower():
        if language == "telugu":
            response = "నమస్కారం 😊 మీకు ఏ ఫోన్ గురించి వివరాలు కావాలి? మోడల్ పేరు చెప్పండి, సహాయం చేస్తాను."
        else:
            response = "Hi 😊 Meeku ye phone gurinchi details kavali? Model peru cheppandi, help chesthanu."

    # Preserve the exact product URL generated from the catalogue. The LLM may

    # rephrase the surrounding text, but it must not drop or invent the URL.

    direct_link_lines = []

    if "Direct link:" in draft:

        for line in draft.splitlines():

            if line.strip().startswith(("Mee product:", "Direct link:")):

                direct_link_lines.append(line.strip())

        if direct_link_lines:

            url_line = next((line for line in direct_link_lines if line.startswith("Direct link:")), "")

            if url_line and url_line.split(":", 1)[1].strip() not in str(response):

                response = str(response).rstrip() + "\n\n" + "\n".join(direct_link_lines)

    return response

def _record_customer_turn(

    session: dict,

    message: str,

    language: str,

    customer: dict,

    intent: dict,

) -> None:

    """

    Store the complete customer state after every turn.

    """

    turn = (

        len(session["conversation_history"])

        + 1

    )

    record = {

        "turn": turn,

        "message": message,

        "language": language,

        "created_at": datetime.now(timezone.utc).isoformat(),

        "customer": dict(customer),

        "intent": dict(intent),

    }

    session["conversation_history"].append(

        record

    )

    session["intent_history"].append({

        "turn": turn,

        "message": message,

        "score": intent.get(

            "score",

            0,

        ),

        "status": intent.get(

            "status",

            "low",

        ),

        "signals": intent.get(

            "signals",

            [],

        ),

    })

    session["intent"] = intent

def _nonbuyer_mode(message: str) -> str:

    """Return 'decline', 'browsing', or '' for explicit intent language."""

    text = (message or "").strip().lower()

    hard_no = (

        "not interested", "don't want to buy", "do not want to buy",

        "not buying now", "not planning to buy", "no plan to buy",

        "i won't buy", "i will not buy", "not decided", "haven't decided",
        "have not decided", "not planning to order", "not ordering now",
        "present order cheyali anukovatledhu", "present order cheyyali anukovatledu",
        "inka konalani decide avvatle", "inka konalani decide avvaledu",
        "konalane uddesam ledhu", "konalani uddesam ledu",
        "konanu", "konalani ledu", "konadaniki plan ledu", "ippudu konanu", "interest ledu",

        "ఇప్పుడు కొనను", "ఇప్పుడే కొనను", "కొనాలని లేదు",

        "కొనాలని ప్లాన్ లేదు", "ఆసక్తి లేదు",

    )

    browsing = (

        "just browsing", "just checking", "only checking", "just comparing",

        "only comparing", "researching", "research only", "just looking",

        "just price check", "checking options", "maybe later", "not sure yet",

        "inka chustunna", "inka choostunna", "compare chestunna",

        "just chustunna", "just choostunna", "price check chestunna",

        "ippatiki chustunna", "ఇంకా చూస్తున్నాను", "కేవలం చూస్తున్నాను",

        "ధర మాత్రమే చూస్తున్నాను", "పోల్చి చూస్తున్నాను", "ఇంకా నిర్ణయించుకోలేదు",

    )

    if any(phrase in text for phrase in hard_no):

        return "decline"

    if any(phrase in text for phrase in browsing):

        return "browsing"

    return ""

def _low_pressure_reply(mode: str, language: str) -> str:

    if language == "telugu":

        if mode == "decline":

            return "పర్లేదు 😊 నేను ఒత్తిడి చేయను. తర్వాత మొబైల్ వివరాలు లేదా ధర పోల్చి చూడాలనిపిస్తే చెప్పండి."

        return "పర్లేదు 😊 మీరు ఇప్పుడే కొనాలని నిర్ణయించాల్సిన అవసరం లేదు. మోడళ్లను లేదా ధరలను పోల్చి చూడటంలో సహాయం చేస్తాను."

    if mode == "decline":

        return "Parledhu 😊 Nenu pressure cheyyanu. Tarvatha mobile details leda prices compare cheyyali anipisthe cheppandi."

    return "Parledhu 😊 Ippude konali ani decide avvalsina avasaram ledu. Models leda prices compare cheyyadaniki help chestha."

def _apply_deterministic_customer_updates(message: str, extracted: dict, customer: dict, asked_field: str = "") -> dict:

    """Fill common qualification answers locally so the flow doesn't repeat questions.

    The LLM extractor remains responsible for broad language understanding. These

    high-confidence rules cover frequent Roman-Telugu replies that are easy to miss.

    Existing customer answers are preserved when a turn is only a clarification.

    """

    result = dict(extracted or {})

    text = re.sub(r"\s+", " ", (message or "").strip().lower())

    compact = re.sub(r"[^a-z0-9₹]+", "", text)

    # Detect an explicit request for recommendations separately from a customer
    # who has selected a brand and must still choose an exact model.
    recommendation_patterns = (
        r"\b(best phone|best mobile|suggest|recommend|recommendation|which phone|which mobile|options compare|compare phones|manchi phone suggest|manchi mobile suggest)\b",
        r"ఏ ఫోన్ మంచిది|మంచి ఫోన్ సూచించ|మోడల్స్ పోల్చి",
    )
    if any(re.search(pattern, text, re.I) for pattern in recommendation_patterns):
        result["shopping_mode"] = "recommendation"

    # Deterministically capture common brands and exact models. This prevents an
    # LLM call for simple messages such as "Samsung mobile kavali" or
    # "iPhone 15 128GB kavali". Brand-only choices remain incomplete until the
    # exact model is selected.
    model_patterns = [
        (r"\biphone\s*(\d{1,2})(?:\s*(pro\s*max|pro|max|plus|mini|e))?", "iPhone"),
        (r"ఐఫోన్\s*(\d{1,2})(?:\s*(ప్రో\s*మ్యాక్స్|ప్రో|ప్లస్|మినీ))?", "iPhone"),
        (r"\b(?:samsung\s*)?(?:galaxy\s*)?([samz]\s*\d{1,3})(?:\s*(ultra|plus|fe|classic|edge))?", "Samsung Galaxy"),
        (r"(?:^|\s)(?:సామ్‌సంగ్\s*)?(?:గెలాక్సీ\s*)?([samz]\s*\d{1,3})(?:\s*(ultra|plus|fe))?", "Samsung Galaxy"),
        (r"\b(?:samsung\s+)?galaxy\s+([a-z]\d{1,3})(?:\s*(ultra|plus|fe))?", "Samsung Galaxy"),
        (r"\b(oneplus\s+(?:nord\s+)?\d{1,2}(?:r|t|pro|rce)?(?:\s*(?:pro|plus|ce))?)", "OnePlus"),
        (r"\b(redmi\s+note\s+\d{1,2}(?:\s*(?:pro|plus|pro plus))?)", "Redmi"),
        (r"\b(redmi\s+\d{1,2}(?:\s*(?:a|c|pro))?)", "Redmi"),
        (r"\b(realme\s+\d{1,2}(?:\s*(?:pro|plus|pro plus|x))?)", "realme"),
        (r"\b(oppo\s+(?:reno\s*)?\d{1,2}(?:\s*(?:pro|plus|f|a))?)", "OPPO"),
        (r"\b(vivo\s+(?:v|y|x|t)\s*\d{1,3}(?:\s*(?:pro|plus|e))?)", "vivo"),
        (r"\b(?:google\s*)?pixel\s*(\d{1,2})(?:\s*(pro|xl|a|fold))?", "Google Pixel"),
        (r"\b(motorola\s+edge\s+\d{1,2}(?:\s*(?:ultra|neo|fusion))?)", "Motorola"),
        (r"\b(nothing\s+phone\s*\d{1,2}(?:\s*(?:a|plus))?)", "Nothing Phone"),
    ]
    model_found = False
    for pattern, prefix in model_patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        matched = re.sub(r"\s+", " ", match.group(0)).strip()
        # Normalize common model family names while preserving model suffixes.
        if prefix == "iPhone":
            tail = re.sub(r"^iphone\s*", "", matched, flags=re.I)
            result["product_interest"] = f"iPhone {tail}".strip()
            result["brand"] = "Apple"
        elif prefix == "Samsung Galaxy":
            tail = re.sub(r"^(?:samsung\s*)?(?:galaxy\s*)?", "", matched, flags=re.I).strip()
            code_match = re.match(r"^([a-z])\s*(\d+)(.*)$", tail, re.I)
            if code_match:
                suffix = " ".join(part.title() for part in code_match.group(3).split())
                tail = f"{code_match.group(1).upper()}{code_match.group(2)}" + (f" {suffix}" if suffix else "")
            result["product_interest"] = f"Samsung Galaxy {tail}".strip()
            result["brand"] = "Samsung"
        elif prefix == "Google Pixel":
            tail = re.sub(r"^(?:google\s*)?pixel\s*", "", matched, flags=re.I)
            result["product_interest"] = f"Google Pixel {tail}".strip()
            result["brand"] = "Google"
        else:
            result["product_interest"] = matched[:1].upper() + matched[1:]
            result["brand"] = prefix
        result["model"] = result["product_interest"]
        result["shopping_mode"] = "specific"
        model_found = True
        break

    if not model_found:
        brand_patterns = [
            (r"\b(samsung)(?:\s+(?:mobile|phone|galaxy))?\b|సామ్‌సంగ్", "Samsung"),
            (r"\b(iphone|apple)(?:\s+(?:mobile|phone))?\b|ఐఫోన్|ఆపిల్", "Apple"),
            (r"\b(oneplus|oppo|vivo|realme|redmi|xiaomi|motorola|nothing)(?:\s+(?:mobile|phone))?\b|వన్‌ప్లస్|ఒప్పో|వివో|రియల్‌మి|రెడ్‌మి|మోటరోలా|నథింగ్", None),
            (r"\bgoogle\s+pixel\b|గూగుల్ పిక్సెల్", "Google"),
        ]
        for pattern, fixed_brand in brand_patterns:
            match = re.search(pattern, text, re.I)
            if match:
                captured_brand = match.group(1) if match.lastindex else None
                if fixed_brand:
                    brand = fixed_brand
                elif captured_brand:
                    brand = captured_brand.title()
                elif "వన్‌ప్లస్" in text:
                    brand = "OnePlus"
                elif "ఒప్పో" in text:
                    brand = "OPPO"
                elif "వివో" in text:
                    brand = "Vivo"
                elif "రియల్‌మి" in text:
                    brand = "Realme"
                elif "రెడ్‌మి" in text:
                    brand = "Redmi"
                elif "మోటరోలా" in text:
                    brand = "Motorola"
                else:
                    brand = "Nothing"
                result.setdefault("brand", brand)
                if not result.get("product_interest") or _is_brand_only(result.get("product_interest")):
                    result["product_interest"] = brand if brand != "Apple" else "iPhone"
                if result.get("shopping_mode") != "recommendation":
                    result["shopping_mode"] = "specific"
                break

    # Capture storage variants locally, including short contextual replies such as "128 kavali".
    storage_match = re.search(r"\b(\d{1,4})\s*(gb|tb)\b", text, re.I)
    if storage_match:
        result["storage"] = f"{storage_match.group(1)}{storage_match.group(2).upper()}"
    elif (asked_field == "storage" or (customer.get("product_interest") or result.get("product_interest")) and not customer.get("storage")):
        bare_storage = re.search(r"(?<!\d)(64|128|256|512)\s*(?:gb)?(?:\s*(?:kavali|kavala|kaavali|variant|storage|want|please|saripothundi))?\b", text, re.I)
        if bare_storage:
            result["storage"] = f"{bare_storage.group(1)}GB"

    # Timeline: explicitly map short answers such as "Tarwatha" and
    # "compare chesi confirm chestha" to a completed timeline field.
    timeline_patterns = [

        (r"\b(ivala|eeroju|today|right now|immediately|ippude)\b", "today"),

        (r"\b(ee week|this week|within (a|one) week|one week lo|week lopu|ee vaaram)\b", "within a week"),

        (r"\b(next week|next month|next year|next few weeks|few weeks tarvatha|few weeks later)\b", "later"),

        (r"\b(tarvatha|tarwatha|tarvata|taruvatha|later|not now|after some time|sometime later)\b", "later"),

        (r"\b(compare chesi|compare che(si|si)|compare chestha|compare chesi confirm|decide chesaka|confirm chesaka|think and decide|after comparing|comparing options|compare first|just comparing|only comparing|just browsing|just checking|just looking)\b", "later"),
        (r"ఈరోజు|ఇవాళ|ఇప్పుడే", "today"),
        (r"ఈ వారం|ఈ వారంలో|వారంలోపు", "within a week"),
        (r"తర్వాత|తరువాత|వచ్చే వారం|వచ్చే నెల|కొన్ని వారాల తర్వాత", "later"),

    ]

    for pattern, canonical in timeline_patterns:

        if re.search(pattern, text):

            result["purchase_timeline"] = canonical

            break

    # Comparison language answers the final intent question, not the earlier
    # timeline field. Preserve the timeline already collected unless this turn
    # was explicitly answering the timeline question.
    if _intent_response_kind(message) == "compare" and asked_field != "purchase_timeline":
        result.pop("purchase_timeline", None)

    # If the customer is clarifying their intent after already saying "later",

    # do not let an incomplete extractor erase the stored answer.

    if (

        not result.get("purchase_timeline")

        and customer.get("purchase_timeline")

        and re.search(r"\b(compare|confirm|decide|think|options|compare chesi)\b", text)

    ):

        result["purchase_timeline"] = customer["purchase_timeline"]

    # A short answer like "Bhimavaram" is a valid location when the agent

    # explicitly asked for the customer's city/town.

    if (
        asked_field == "location"
        and not result.get("location")
        and not customer.get("location")
        and (
            re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,49}", (message or "").strip())
            or re.fullmatch(r"[\u0c00-\u0c7f\s]{2,40}", (message or "").strip())
        )
        and not re.search(r"\b(cash|card|upi|emi|yes|no|okay|fine|later|today|tomorrow)\b|అవును|కాదు|తర్వాత|ఈరోజు", text)
    ):
        result["location"] = message.strip()

    # Capture a catalog color when the customer names one.
    color_aliases = {
        "black": "Black", "నలుపు": "Black", "white": "White", "తెలుపు": "White",
        "blue": "Blue", "నీలం": "Blue", "pink": "Pink", "పింక్": "Pink",
        "green": "Green", "ఆకుపచ్చ": "Green", "teal": "Teal",
        "ultramarine": "Ultramarine", "yellow": "Yellow", "purple": "Purple",
        "natural": "Natural", "titanium": "Titanium", "silver": "Silver",
        "gold": "Gold", "midnight": "Midnight", "starlight": "Starlight",
        "graphite": "Graphite", "desert": "Desert", "sierra": "Sierra", "red": "Red",
    }
    for alias, canonical in color_aliases.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text, re.I):
            result["color"] = canonical
            break

    # Condition / new-vs-used. A stated battery-health percentage is a strong
    # second-hand-device signal in this shop's catalog flow, unless the customer
    # explicitly says they want a brand-new device in the same message.
    explicit_new = bool(re.search(
        r"\b(brand new|brand-new|new piece|sealed piece|sealed|kotha phone|kottha phone|new phone)\b",
        text,
    ))
    explicit_used = bool(re.search(
        r"\b(used|second hand|second-hand|refurbished|renewed|pre owned|pre-owned)\b",
        text,
    ))
    mentions_battery_health = bool(
        re.search(r"\b(?:battery\s*health|battery\s*condition|bh)\b", text, re.I)
        and re.search(r"(?<!\d)\d{1,3}\s*%", text)
    )
    if mentions_battery_health:
        battery_match = re.search(r"(?<!\d)(\d{1,3})\s*%", text)
        if battery_match:
            result["battery_health"] = f"{battery_match.group(1)}%"
    if explicit_new:
        result["condition"] = "brand new"
    elif explicit_used or mentions_battery_health:
        result["condition"] = "used/refurbished"

    # Payment preferences, including multiple acceptable methods.

    payment_hits = []

    if re.search(r"\b(cash|cash lo)\b", text):

        payment_hits.append("cash")

    if re.search(r"\b(card|credit card|debit card)\b", text):

        payment_hits.append("card")

    if re.search(r"\b(upi|gpay|google pay|phonepe|paytm)\b", text):

        payment_hits.append("UPI")

    if re.search(r"\b(emi|installments|installment)\b", text):

        payment_hits.append("EMI")

    if payment_hits:

        result["payment_method"] = "/".join(dict.fromkeys(payment_hits))

    elif re.search(r"\b(anything is fine|anything okay|edaina okay|yedhaina okay|any method|any payment)\b", text):

        result["payment_method"] = "any"

    # Numeric budget ranges and single amounts. Accept natural variants such as
    # "40k-60k", "40,000 to 60,000", "45k", "82000 ma budget", or "82000"
    # when the agent is currently asking for the budget.
    range_match = re.search(
        r"(?:₹\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]?)\s*(?:-|–|to|and|nunchi)\s*(?:₹\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]?)",
        text,
    )
    storage_only_answer = bool(re.fullmatch(r"\s*(?:64|128|256|512|1024)\s*(?:gb|tb)?\s*", text))
    budget_context = bool(
        not storage_only_answer and (
            re.search(r"\b(budget|under|below|around|approximately|approx|rupees|rs|lopu|varaku|ma budget|naa budget)\b|₹", text)
            or (asked_field == "budget" and re.search(r"\d", text) and not re.search(r"\b(?:64|128|256|512|1024)\s*(?:gb|tb)?\b", text))
        )
    )
    # Number before the word "budget" is common in Roman Telugu: "82000 ma budget".
    number_match = re.search(
        r"(?:₹\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]?)",
        text,
    ) if budget_context else None

    def amount(number, suffix):
        number = float(number.replace(",", ""))
        if suffix.lower() == "k" or number < 1000:
            number *= 1000
        return int(number)

    try:
        if range_match:
            low = amount(range_match.group(1), range_match.group(2))
            high = amount(range_match.group(3), range_match.group(4))
            if 1000 <= low <= high <= 10000000:
                result["budget"] = {"min": low, "max": high}
                result["budget_min"] = low
                result["budget_max"] = high
        elif number_match and budget_context and not result.get("budget"):
            raw_number = float(number_match.group(1).replace(",", ""))
            raw_suffix = number_match.group(2).lower()
            # Never interpret a common phone-storage number as a budget unless
            # currency/budget wording is explicit.
            if raw_number in {64, 128, 256, 512, 1024} and not re.search(r"₹|\b(?:budget|rs|rupees|lopu|varaku)\b", text):
                raise ValueError("storage number is not a budget")
            value = amount(number_match.group(1), number_match.group(2))
            if 1000 <= value <= 10000000:
                result["budget"] = value
                result["budget_amount"] = value
    except (TypeError, ValueError):
        pass

    return result

def _localized_question(field: str, language: str, customer: dict) -> str:

    """Stable, natural qualification questions in the conversation's locked language."""

    product = str(

        customer.get("model")

        or customer.get("product_interest")

        or customer.get("brand")

        or ""

    ).strip()

    if language == "telugu":

        templates = {

            "product_interest": (
                f"{('మీరు ' + str(customer.get('storage')) + ' స్టోరేజ్ కావాలని చెప్పారు. ') if customer.get('storage') else ''}మీరు {customer.get('brand') or product} అన్నారు 😊 ఆ బ్రాండ్‌లో ఖచ్చితమైన మోడల్ ఏది? ఉదాహరణకు Galaxy A55 లేదా S24. మోడల్ నిర్ణయించకపోతే మీ అవసరానికి సరిపోయే ఆప్షన్లు సూచించగలను."
                if (customer.get("brand") or product) and not _is_specific_model_name(customer.get("product_interest")) and customer.get("shopping_mode") != "recommendation"
                else "మానియా మొబైల్స్‌కు స్వాగతం 😊 మీకు ఏ మొబైల్ కావాలి? బ్రాండ్, మోడల్ పేరు చెప్పండి."
            ),

            "storage": (
                (f"{customer.get('storage')}లో అందుబాటులో ఉన్న కలర్స్: " + ", ".join(sorted({
                    _catalog_color(item) for item in _catalog_products_for_model(product, storage=str(customer.get("storage") or ""))
                    if _catalog_color(item)
                })) + ". మీకు ఏ కలర్ కావాలి?")
                if product and _is_specific_model_name(product) and customer.get("storage") and len({
                    _catalog_color(item) for item in _catalog_products_for_model(product, storage=str(customer.get("storage") or ""))
                    if _catalog_color(item)
                }) > 1
                else ("మీకు ఏ స్టోరేజ్, కలర్ వేరియంట్ కావాలి?" if product and _is_specific_model_name(product) else "ఏ ఖచ్చితమైన మోడల్, స్టోరేజ్ వేరియంట్ కావాలి?")
            ),

            "budget": f"{product} కోసం సుమారుగా ఎంత బడ్జెట్ అనుకుంటున్నారు?" if product else "మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?",

            "purchase_timeline": "ఎప్పుడు కొనాలని ప్లాన్ చేస్తున్నారు—ఈరోజు, ఈ వారం, లేక తర్వాత?",

            "condition": "కొత్త సీల్డ్ పీస్ కావాలా, లేక వాడినది/రిఫర్బిష్డ్ కూడా పరిగణలోకి తీసుకుంటారా?",

            "location": "మీరు ఏ సిటీ/టౌన్ నుంచి కొనాలనుకుంటున్నారు? మీ దగ్గరలోని స్టోర్/అందుబాటు వివరాలు తెలుసుకోవడానికి ఉపయోగపడుతుంది.",

        }

    else:

        templates = {

            "product_interest": (
                f"{('Meeru ' + str(customer.get('storage')) + ' storage kavali ani chepparu. ') if customer.get('storage') else ''}Meeru {customer.get('brand') or product} annaru bro 😊 Aa brand lo exact model enti? Example: Galaxy A55 leka S24. Model decide cheyyakapothe mee budget/use ki set ayye options suggest chestha."
                if (customer.get("brand") or product) and not _is_specific_model_name(customer.get("product_interest")) and customer.get("shopping_mode") != "recommendation"
                else "Mania Mobiles ki welcome 😊 Meeku ye mobile kavali? Brand mariyu model peru cheppandi."
            ),

            "storage": (
                (f"{customer.get('storage')} lo available colors: " + ", ".join(sorted({
                    _catalog_color(item) for item in _catalog_products_for_model(product, storage=str(customer.get("storage") or ""))
                    if _catalog_color(item)
                })) + ". Meeku ye color kavali?")
                if product and _is_specific_model_name(product) and customer.get("storage") and len({
                    _catalog_color(item) for item in _catalog_products_for_model(product, storage=str(customer.get("storage") or ""))
                    if _catalog_color(item)
                }) > 1
                else ("Ye storage, color variant kavali?" if product and _is_specific_model_name(product) else "Ye exact model kavali? Model chepthe available storage/colors, catalog price links tho chupistha.")
            ),

            "budget": f"{product} kosam roughly entha budget anukuntunnaru?" if product else "Mee budget entha anukuntunnaru?",

            "purchase_timeline": "Eppudu konalani plan chesthunnaru—ivala, ee week, leka tarvatha?",

            "condition": "Brand-new sealed piece kavala, leka used/refurbished kuda consider chesthara?",

            "location": "Meeru ye city/town nunchi purchase chesthunnaru? Mee daggarlo store/availability details telusukovadaniki help avuthundhi.",

        }

    return templates.get(field, "")

def _parse_explicit_budget_amount(message: str, asked_field: str = ""):
    """Parse common Indian budget expressions without confusing storage with money.

    Supports ₹1.25 lakh, 1.25 lac/lakh/lakhs, 1 lakh 25 thousand, 125k,
    ₹1,25,000, 125000, and bare numbers only while the budget field is active.
    """
    text = (message or "").lower().replace("₹", " rs ")
    text = re.sub(r"\s+", " ", text).strip()
    explicit_budget_words = bool(re.search(
        r"\b(?:budget|rs\.?|rupees?|inr|under|below|around|approx(?:imately)?|lakh|lakhs|lac|lacs|lakh[s]?|thousand|koti|naa budget|na budget|lopu|varaku)\b",
        text,
    )) or " rs " in f" {text} "
    # Also recognize shorthand money amounts embedded after variant details,
    # e.g. "128GB, 53k" or "black 128 53k". Storage values themselves do not
    # match this pattern because they are followed by GB/TB, not a money unit.
    shorthand_amount = bool(re.search(
        r"(?<![\w])\d+(?:\.\d+)?\s*(?:k|lakh|lakhs|lac|lacs)\b",
        text,
    ))
    # A customer may send multiple details together: "black color, 128, 53000".
    # In a live product conversation, a standalone realistic 4-8 digit amount
    # alongside a variant/color is an explicit budget signal even without the
    # word "budget". Do not confuse 128/256/etc storage values with money.
    variant_context = bool(re.search(
        r"\b(?:black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red|color|colour|gb|tb|storage)\b",
        text,
    ))
    large_numeric_amount = bool(re.search(r"(?<![\w])\d{4,8}(?![\w])", text))
    budget_context = explicit_budget_words or shorthand_amount or (
        variant_context and large_numeric_amount
    )
    bare_amount = asked_field == "budget" and bool(re.match(r"^\s*[\d,]+(?:\.\d+)?\s*(?:k|l|lac|lacs|lakh|lakhs)?(?:\s|$)", text))
    if not budget_context and not bare_amount:
        return None
    # Storage-only answers are never budgets, even when storage is the active field.
    if re.fullmatch(r"(?:64|128|256|512|1024)\s*(?:gb|tb)?", text):
        return None
    # Indian shorthand: 1 lakh 25 thousand.
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lakhs|lac|lacs|l)\s*(?:(\d+(?:\.\d+)?)\s*(?:thousand|k))?", text)
    if m:
        value = float(m.group(1)) * 100000
        if m.group(2):
            value += float(m.group(2)) * 1000
        return int(value) if 1000 <= value <= 10000000 else None
    m = re.search(r"(?<![a-z])(?:rs\s*)?([\d,]+(?:\.\d+)?)\s*(k|thousand)\b", text)
    if m:
        value = float(m.group(1).replace(",", "")) * 1000
        return int(value) if 1000 <= value <= 10000000 else None
    # Numeric amount, including Indian comma grouping.
    numeric_matches = list(re.finditer(r"(?:rs\s*)?(\d[\d,]*(?:\.\d+)?)", text))
    if not numeric_matches:
        return None
    # For combined variant messages, the amount commonly comes last, after
    # model/storage numbers (e.g. "iPhone 16 black 128, 53000").
    m = numeric_matches[-1] if variant_context and large_numeric_amount else numeric_matches[0]
    value = float(m.group(1).replace(",", ""))
    if value in {64, 128, 256, 512, 1024} and not re.search(r"\b(?:budget|rs|rupees|inr)\b", text):
        return None
    return int(value) if 1000 <= value <= 10000000 else None


MANIA_STORE_ADDRESS = "Beside MGB Mall, Nellore, Andhra Pradesh"
MANIA_STORE_MAPS_URL = "https://www.google.com/maps/search/?api=1&query=Beside+MGB+Mall%2C+Nellore%2C+Andhra+Pradesh"

# Upper bound on model calls for unclassified messages in one session (cost/abuse cap).
try:
    FRICTION_LLM_MAX_PER_SESSION = max(0, int(os.getenv("LLM_FRICTION_MAX_PER_SESSION", "8")))
except ValueError:
    FRICTION_LLM_MAX_PER_SESSION = 8


def _store_faq_reply(message: str, language: str) -> Optional[str]:
    """Grounded answers for store policies that are explicitly provided by the business."""
    text = (message or "").casefold()
    asks_emi = any(x in text for x in ("emi", "installment", "instalment", "ఈఎంఐ", "వాయిదా"))
    asks_exchange = any(x in text for x in ("exchange", "trade in", "trade-in", "పాత ఫోన్", "ఎక్స్చేంజ్"))
    asks_location = any(x in text for x in (
        "ekkada untaru", "ekkada vuntaru", "ekkada undi", "ekkada undhi", "store ekkada",
        "shop ekkada", "store address", "shop address", "where is the store",
        "where is your store", "where are you located", "మీరు ఎక్కడ", "షాప్ ఎక్కడ",
        "స్టోర్ ఎక్కడ", "అడ్రస్",
    ))
    if not asks_emi and not asks_exchange and not asks_location:
        return None
    if language == "telugu":
        parts = []
        if asks_emi:
            parts.append("EMI సదుపాయం నెల్లూరు మరియు పరిసర ప్రాంతాల్లో మాత్రమే అందుబాటులో ఉంటుంది. అర్హత, బ్యాంక్ ఆఫర్లు, ప్రస్తుత నిబంధనలను స్టోర్‌లో నిర్ధారించండి; ఇక్కడ ఆమోదం హామీ ఇవ్వలేను.")
        if asks_exchange:
            parts.append("మీ పాత ఫోన్ ఎక్స్చేంజ్ విలువ దాని మోడల్, స్టోరేజ్, కండిషన్‌పై ఆధారపడి ఉంటుంది. ఖచ్చితమైన విలువ కోసం పాత ఫోన్‌ను స్టోర్‌కు తీసుకెళ్లి తనిఖీ చేయించండి. మీ ఫోన్ స్టోరేజ్, కండిషన్ చెబితే తదుపరి వివరాలు సూచిస్తాను.")
        parts.append(f"స్టోర్: {MANIA_STORE_ADDRESS}. Google Maps: {MANIA_STORE_MAPS_URL}")
        return "\n\n".join(parts)
    if language == "roman_telugu":
        parts = []
        if asks_emi:
            parts.append("EMI facility Nellore mariyu surrounding areas lo matrame available. Mee eligibility, current bank offers, terms store daggara confirm chesukondi; ikkada approval guarantee cheyyalenu.")
        if asks_exchange:
            parts.append("Mee old phone exchange value model, storage mariyu condition batti untundi. Exact value kosam old phone ni store ki teesukelli inspection cheyinchandi. Mee phone storage, condition chepthe next steps chepthanu.")
        parts.append(f"Store: {MANIA_STORE_ADDRESS}. Google Maps: {MANIA_STORE_MAPS_URL}")
        return "\n\n".join(parts)
    parts = []
    if asks_emi:
        parts.append("EMI may be available in Nellore and surrounding areas only. Please confirm current eligibility, bank offers and terms with the store; I can't guarantee approval here.")
    if asks_exchange:
        parts.append("Exchange value depends on your old phone's exact model, storage and condition. Tell me your current phone model and storage, and visit the store for the final exchange valuation.")
    parts.append(f"Store: {MANIA_STORE_ADDRESS}. Google Maps: {MANIA_STORE_MAPS_URL}")
    return "\n\n".join(parts)


def _process_message_impl(

    customer_id: str,

    message: str,

) -> dict:

    customer_id = customer_id.strip()

    message = message.strip()

    if not customer_id:

        raise ValueError(

            "customer_id cannot be empty"

        )

    if not message:

        raise ValueError(

            "message cannot be empty"

        )

    session = _get_session(

        customer_id

    )

    customer = session["customer"]

    # Meta's WhatsApp sender is the customer's real phone number. Persist it

    # on every turn so the dashboard never depends on extraction by the LLM.

    if customer_id.startswith("wa_"):

        customer.setdefault("phone_number", customer_id[3:])

    elif customer_id.replace("+", "").isdigit():

        customer.setdefault("phone_number", customer_id)

    # Meta's WhatsApp sender is the customer's real phone number. Persist it

    # on every turn so the dashboard never depends on extraction by the LLM.

    if customer_id.startswith("wa_"):

        customer.setdefault("phone_number", customer_id[3:])

    elif customer_id.replace("+", "").isdigit():

        customer.setdefault("phone_number", customer_id)

    # Resolve language at the START of the turn.

    # This is critical because catalog lookup/fallback

    # text is built before _localize_reply() runs.

    active_language = (

        _resolve_response_language(

            message,

            session,

        )

    )

    session["language"] = (

        active_language

    )

    print("Active conversation language:", active_language)

    if _is_ai_identity_question(message):
        return {
            "customer_id": customer_id,
            "message": _ai_identity_reply(active_language),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }
    # Deterministic rules handle common short answers and contextual objections
    # without an LLM call. Only genuinely ambiguous business messages reach the
    # configured extractor. Out-of-scope messages are rejected earlier by the
    # local scope gate in _process_message_serialized.
    asked_field_before_turn = session.get("asked_field", "")
    repair_message = _is_context_repair_message(message) and _has_business_context(session)
    payment_objection = (
        asked_field_before_turn == "payment_method"
        and _is_payment_objection(message)
        and _has_business_context(session)
    )
    price_objection = _is_price_objection(message) and _has_business_context(session)
    intent_followup = bool(
        session.get("intent_question_asked")
        and _intent_response_kind(message) in {"positive", "compare", "decline", "price_concern"}
    )
    deterministic = _apply_deterministic_customer_updates(
        message=message, extracted={}, customer=customer, asked_field=asked_field_before_turn
    )
    mixed_variant_budget_turn = bool(
        re.search(r"\b(?:black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red|color|colour|gb|tb|storage)\b", message.lower())
        and re.search(r"(?<![\w])\d{4,8}(?![\w])", message.lower())
        and not (deterministic.get("budget") or deterministic.get("budget_amount"))
    )
    simple_deterministic_turn = bool(deterministic) and not mixed_variant_budget_turn and not re.search(
        r"\b(price|how much|cost|available|in stock|compare|difference|feature|specification|warranty|discount|offer|suggest|recommend|best phone|best mobile|which phone|which mobile)\b|ధర ఎంత|ఎంత ధర|తేడా ఏంటి|మంచి ఫోన్ సూచించ|ఏ ఫోన్ మంచిది",
        message.lower(),
    )
    try:
        if repair_message:
            extracted = {}
            print("LLM extraction skipped: contextual repair message")
        elif _is_ai_identity_question(message):
            extracted = {}
            print("LLM extraction skipped: AI identity question")
        elif intent_followup:
            extracted = {}
            print("LLM extraction skipped: one-time purchase-intent answer")
        elif price_objection:
            extracted = {}
            print("LLM extraction skipped: deterministic price concern")
        elif payment_objection:
            extracted = {"payment_method": "not specified"}
            session["_qualification_ack"] = (
                "పర్లేదు 😊 పేమెంట్ విధానం చెప్పడం తప్పనిసరి కాదు. "
                if active_language == "telugu"
                else "Parledhu bro 😊 Payment method cheppadam compulsory kaadu. "
            )
            print("LLM extraction skipped: optional payment objection")
        elif simple_deterministic_turn:
            extracted = deterministic
            print("LLM extraction skipped: deterministic customer details")
        else:
            extracted = extract_customer_details(message, customer)
            if not isinstance(extracted, dict):
                extracted = {}
            extracted = _apply_deterministic_customer_updates(
                message=message, extracted=extracted, customer=customer, asked_field=asked_field_before_turn
            )
    except Exception as exc:

        print("Customer detail extraction failed:", type(exc).__name__)

        return {

            "customer_id": customer_id,

            "message": _localize_reply(

                (

                    "Sorry, I couldn't process "

                    "that message properly. "

                    "Could you please rephrase it?"

                ),

                message,

                customer,

                session,

            ),

            "customer": customer,

            "qualification": (

                session["qualification"]

            ),

            "intent": session["intent"],

            "intent_history": (

                session["intent_history"]

            ),

            "lead_id": session["lead_id"],

            "lead_saved": (

                session["lead_saved"]

            ),

        }

    if not isinstance(

        extracted,

        dict,

    ):

        extracted = {}

    if not simple_deterministic_turn and not repair_message and not payment_objection:
        extracted = _apply_deterministic_customer_updates(
            message=message, extracted=extracted, customer=customer,
            asked_field=session.get("asked_field", "")
        )

    # HARD GUARD: the LLM may suggest a budget that the customer never gave.
    # Only accept a budget when the customer explicitly states a currency/amount
    # or budget language, or when a realistic bare amount answers a budget prompt.
    message_lower = (message or "").lower()
    parsed_budget = _parse_explicit_budget_amount(message, session.get("asked_field", ""))
    if parsed_budget is not None:
        extracted["budget"] = parsed_budget
        extracted["budget_amount"] = parsed_budget
    explicit_budget_context = bool(re.search(
        r"(?:₹|\brs\.?\b|\brupees?\b|\bbudget\b|\bunder\b|\bbelow\b|\baround\b|\bapprox(?:imately)?\b|\blopu\b|\bvaraku\b|\bnaa budget\b|\bna budget\b|\blakh(?:s)?\b|\blac(?:s)?\b|\bthousand\b|(?<![\w])\d+(?:\.\d+)?\s*(?:k|lakh|lakhs|lac|lacs)\b)",
        message_lower,
    )) or bool(
        re.search(r"\b(?:black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red|color|colour|gb|tb|storage)\b", message_lower)
        and re.search(r"(?<![\w])\d{4,8}(?![\w])", message_lower)
    ) or bool(
        session.get("asked_field") == "budget"
        and re.match(r"^\s*(?:₹\s*)?\d[\d,]*(?:\.\d+)?\s*(?:k|l|lac|lakh|lakhs)?(?:\s|$)", message_lower, re.I)
    )
    storage_only_answer = bool(re.fullmatch(r"\s*(?:64|128|256|512|1024)\s*(?:gb|tb)?\s*", message_lower))
    if storage_only_answer:
        explicit_budget_context = False
    if not explicit_budget_context:
        for budget_key in ("budget", "budget_amount", "budget_min", "budget_max", "target_price"):
            extracted.pop(budget_key, None)
    else:
        # Do not accept an extracted amount from a bare storage response.
        if storage_only_answer:
            for budget_key in ("budget", "budget_amount", "budget_min", "budget_max", "target_price"):
                extracted.pop(budget_key, None)

    # Merge required and additional

    # customer details.

    _merge_customer_details(

        customer,

        extracted,

    )

    # If the customer is choosing an alternative after an unavailable exact
    # combination, resume the normal flow only when their choice exists in the
    # catalog. This state lives in the session, not in the persisted lead fields.
    if session.get("catalog_variant_unresolved"):
        selected_model = str(customer.get("model") or customer.get("product_interest") or "").strip()
        selected_model = re.sub(r"\b\d{1,4}\s*(?:gb|tb)\b", "", selected_model, flags=re.I)
        selected_model = re.sub(r"\b(black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red)\b", "", selected_model, flags=re.I)
        selected_model = re.sub(r"\s+", " ", selected_model).strip()
        selected_storage = str(customer.get("storage") or "")
        selected_color = str(customer.get("color") or "")
        try:
            selected_options = _catalog_products_for_model(
                selected_model, storage=selected_storage, color=selected_color
            )
            # If the customer chose a new storage/color from the alternatives,
            # discard the stale part of the unavailable combination when it
            # prevents an otherwise valid catalog match.
            if not selected_options and extracted.get("storage") and not extracted.get("color"):
                selected_options = _catalog_products_for_model(selected_model, storage=selected_storage)
                if selected_options:
                    customer.pop("color", None)
            if not selected_options and extracted.get("color") and not extracted.get("storage"):
                selected_options = _catalog_products_for_model(selected_model, color=selected_color)
                if selected_options:
                    customer.pop("storage", None)
        except Exception:
            selected_options = []
        if selected_options:
            session.pop("catalog_variant_unresolved", None)

    # ---------------------------------------------------------

    # INTENT

    # ---------------------------------------------------------

    # Calculate intent after every customer response.

    #

    # This does NOT replace your existing extraction,

    # qualification flow, catalog logic, or questions.

    #

    # It simply adds intent information on top.

    # ---------------------------------------------------------

    intent = calculate_intent(

        message=message,

        customer=customer,

        previous_intent=session.get(

            "intent"

        ),

    )

    if not isinstance(

        intent,

        dict,

    ):

        intent = {

            "score": 0,

            "status": "low",

            "signals": [],

        }

    # The final willingness-to-buy question is asked once only. Classify the
    # answer locally so romanized Telugu variants such as "Haa definite ga",
    # "Kondam anukuntunna", and "Models compare cheydaniki" are not mistaken
    # for ambiguous input and never trigger a repeated question.
    intent_response_kind = (
        _intent_response_kind(message) if session.get("intent_question_asked") else "unknown"
    )
    if intent_response_kind == "positive":
        intent["score"] = max(85, int(intent.get("score", 0)))
        intent.setdefault("signals", [])
        if "Customer confirmed purchase intent" not in intent["signals"]:
            intent["signals"].append("Customer confirmed purchase intent")
    elif intent_response_kind == "compare":
        intent.setdefault("signals", [])
        if "Customer is comparing models" not in intent["signals"]:
            intent["signals"].append("Customer is comparing models")
        intent["score"] = 0
    elif intent_response_kind == "price_concern":
        intent.setdefault("signals", [])
        if "Customer expressed price concern" not in intent["signals"]:
            intent["signals"].append("Customer expressed price concern")
        intent["score"] = 0
    # The scorer's phrase list may not include a bare "no". If this turn answers
    # the final buying-intent question negatively, apply that explicit signal here.
    if session.get("intent_question_asked") and intent_response_kind == "decline":
        intent["score"] = 0
        intent.setdefault("signals", [])
        if "Customer declined current purchase" not in intent["signals"]:
            intent["signals"].append("Customer declined current purchase")
    if intent_response_kind in {"positive", "compare", "decline", "price_concern"}:
        score_value = int(intent.get("score", 0))
        intent["status"] = (
            "very_high" if score_value >= 80 else
            "high" if score_value >= 60 else
            "medium" if score_value >= 40 else "low"
        )

    _record_customer_turn(

        session=session,

        message=message,

        language=active_language,

        customer=customer,

        intent=intent,

    )

    if repair_message:
        repair_reply = _context_repair_reply(active_language, customer, session)
        # A correction/repetition complaint is business-contextual: give the
        # configured LLM one chance to understand the customer's actual message
        # rather than returning a stale field question again. The deterministic
        # repair remains the safe fallback if no provider is configured/fails.
        try:
            repair_reply = generate_response(
                draft_response=repair_reply,
                customer_message=message,
                language=active_language,
                customer=customer,
                conversation_history=session.get("conversation_history", [])[-8:],
                catalog_context=[],
                force_llm=True,
            ) or repair_reply
        except TypeError:
            # Compatibility with older llm.py files that don't yet accept force_llm.
            repair_reply = generate_response(
                draft_response=repair_reply,
                customer_message=message,
                language=active_language,
                customer=customer,
                conversation_history=session.get("conversation_history", [])[-8:],
                catalog_context=[],
            ) or repair_reply
        except Exception as exc:
            print("Context-repair LLM fallback failed:", type(exc).__name__)
        return {
            "customer_id": customer_id, "message": repair_reply, "customer": customer,
            "qualification": session.get("qualification"), "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []), "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }

    # ---------------------------------------------------------

    # EXISTING LEAD

    # ---------------------------------------------------------

    # If this customer already became a lead,

    # update the same lead instead of creating

    # another lead.

    # ---------------------------------------------------------

    if session["lead_saved"]:

        # The public process_message wrapper persists the updated state once

        # after this function returns. Avoid a duplicate database write here.

        return _handle_saved_lead_follow_up(

            customer_id=customer_id,

            message=message,

            customer=customer,

            session=session,

            extracted=extracted,

        )

    # ---------------------------------------------------------

    # CATALOG

    # ---------------------------------------------------------

    # Only show catalog information when the customer supplies

    # a NEW product. The extractor may repeat the previous

    # product_interest on later turns, so we must not run the

    # catalog lookup on every message.

    previous_product = customer.get("product_interest")

    extracted_product = extracted.get("product_interest")

    product_message = ""

    new_product_received = (

        bool(extracted_product)

        and (

            not previous_product

            or str(extracted_product).strip().lower()

            != str(previous_product).strip().lower()

        )

    )

    current_product = str(customer.get("model") or customer.get("product_interest") or extracted_product or "").strip()
    # The extracted interest can contain the selected storage/color. Normalize to
    # the base model before querying the catalog; retain the selections separately.
    current_product = re.sub(r"\b\d{1,4}\s*(?:gb|tb)\b", "", current_product, flags=re.I)
    current_product = re.sub(r"\b(black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red)\b", "", current_product, flags=re.I)
    current_product = re.sub(r"\s+", " ", current_product).strip()
    current_storage = str(customer.get("storage") or "").strip()
    current_color = str(customer.get("color") or "").strip()
    variant_key = f"{current_product} {current_storage} {current_color}".strip()
    product_named_now = bool(
        _is_specific_model_name(current_product)
        and _is_specific_product_mentioned(message, current_product)
    )
    variant_supplied_now = bool(extracted.get("storage") or extracted.get("color")) and bool(current_product)
    variant_is_complete = bool(current_product and current_storage)
    variants_for_storage = _catalog_products_for_model(current_product, storage=current_storage) if current_product and current_storage else []
    colors_for_storage = {_catalog_color(item) for item in variants_for_storage if _catalog_color(item)}
    # Color is optional. Once storage is known, do not keep the customer stuck
    # asking for a color; present the matching storage options and at most one link.
    color_resolved = bool(current_color) or bool(current_storage) or len(colors_for_storage) <= 1
    # Compact sales flow: show the catalog immediately for a named model.
    # Do not require a budget, timeline, city, color or condition questionnaire.
    if _is_specific_model_name(current_product) and (product_named_now or variant_supplied_now):
        session["compact_sales_flow"] = True
    model_only_needs_preferences = False
    if _is_specific_model_name(current_product) and (product_named_now or variant_supplied_now or session.get("catalog_variant_unresolved")) and not model_only_needs_preferences:
        if variant_is_complete and color_resolved:
            # A direct product URL is returned only for a unique exact model,
            # storage, and color match. Never substitute a nearby variant.
            exact_matches = _catalog_products_for_model(
                current_product, storage=current_storage, color=current_color
            )
            requested_battery = re.search(r"(?<!\d)(\d{1,3})\s*%", message)
            if requested_battery:
                exact_matches = [
                    item for item in exact_matches
                    if re.search(
                        rf"(?<!\d){re.escape(requested_battery.group(1))}\s*%",
                        _catalog_battery_health(item),
                    )
                ]
            requested_condition = str(customer.get("condition") or "").lower()
            if requested_condition:
                wants_used_condition = any(term in requested_condition for term in ("used", "second hand", "refurbished", "pre-owned"))
                wants_new_condition = any(term in requested_condition for term in ("brand new", "sealed", "new condition"))
                if wants_used_condition:
                    filtered = [item for item in exact_matches if not _catalog_is_new(item)]
                    if filtered:
                        exact_matches = filtered
                elif wants_new_condition:
                    filtered = [item for item in exact_matches if _catalog_is_new(item)]
                    if filtered:
                        exact_matches = filtered
            if len(exact_matches) == 1:
                _remember_inferred_interest_band(customer, exact_matches[0])
                product_message = _get_product_message(
                    variant_key,
                    f"{message} battery health {customer.get('battery_health', '')}",
                    active_language,
                    condition=str(customer.get("condition") or ""),
                )
                session["catalog_product"] = variant_key if product_message else None
                session.pop("catalog_variant_unresolved", None)
            elif len(exact_matches) > 1 and current_storage and not current_color:
                # Storage is known and color is optional: show only options for
                # that storage, with one link maximum, and continue qualification
                # without asking for color again.
                product_message = _catalog_options_only_message(
                    current_product, active_language, storage=current_storage
                )
                session["catalog_product"] = None
                session.pop("catalog_variant_unresolved", None)
            elif len(exact_matches) > 1:
                product_message = _catalog_options_only_message(
                    current_product, active_language, storage=current_storage, color=current_color
                )
                session["catalog_product"] = None
                session["catalog_variant_unresolved"] = True
            else:
                # Requested combination is absent: show available alternatives
                # without links and ask the customer to select a listed variant.
                product_message = _catalog_options_only_message(current_product, active_language)
                session["catalog_product"] = None
                session["catalog_variant_unresolved"] = True
        else:
            # Model only: show catalog variants/colors without URLs. After a
            # storage choice, show matching product URLs; ask for any missing color.
            product_message = _catalog_options_only_message(
                current_product, active_language, storage=current_storage, color=current_color
            )
            session["catalog_product"] = None
            if not current_storage or (current_storage and not variants_for_storage):
                session.pop("catalog_variant_unresolved", None)
        session["catalog_message_sent"] = bool(product_message)

    # Compact sales flow: return catalog options first if the customer's
    # model/condition/storage/battery description still maps to several rows.
    if (
        session.get("compact_sales_flow")
        and _is_specific_model_name(current_product)
        and not intent_followup
    ):
        try:
            model_variants = _catalog_products_for_model(current_product)
            selected_variants = _catalog_products_for_model(
                current_product, storage=current_storage, color=current_color
            ) if current_storage or current_color else list(model_variants)
            requested_battery = re.search(r"(?<!\d)(\d{1,3})\s*%", message)
            if not requested_battery:
                requested_battery = re.search(
                    r"(?<!\d)(\d{1,3})\s*%", str(customer.get("battery_health") or "")
                )
            if requested_battery:
                selected_variants = [
                    item for item in selected_variants
                    if re.search(
                        rf"(?<!\d){re.escape(requested_battery.group(1))}\s*%",
                        _catalog_battery_health(item),
                    )
                ]
            requested_condition = str(customer.get("condition") or "").lower()
            if requested_condition:
                if any(term in requested_condition for term in ("used", "second hand", "refurbished", "pre-owned")):
                    conditioned = [item for item in selected_variants if not _catalog_is_new(item)]
                    if conditioned:
                        selected_variants = conditioned
                elif any(term in requested_condition for term in ("brand new", "sealed", "new condition")):
                    conditioned = [item for item in selected_variants if _catalog_is_new(item)]
                    if conditioned:
                        selected_variants = conditioned

            # If the customer-stated budget is below every known catalog price,
            # record a follow-up request and ask one explicit purchase-intent
            # question. Do not claim an automatic notification service exists:
            # this is persisted for the existing dashboard/team follow-up flow.
            stated_budget = _customer_budget_ceiling(customer)
            priced_variants = [
                item for item in selected_variants
                if _catalog_price_sort_value(item) != float("inf")
                and item.get("price_needs_verification") is False
            ]
            affordable_variants = [
                item for item in priced_variants
                if _catalog_price_sort_value(item) <= stated_budget
            ] if stated_budget is not None else list(selected_variants)

            previous_request = (
                customer.get("availability_notification_requested")
                and str(customer.get("availability_notification_model") or "").casefold()
                == current_product.casefold()
                and str(customer.get("availability_notification_budget") or "") == str(int(stated_budget))
            ) if stated_budget is not None else False
            if stated_budget is not None and priced_variants and not affordable_variants and not previous_request:
                budget_label = f"₹{stated_budget:,.0f}"
                customer["availability_notification_requested"] = True
                customer["availability_notification_model"] = current_product
                customer["availability_notification_budget"] = int(stated_budget)
                customer["availability_notification_status"] = "pending_team_follow_up"
                customer["budget_source"] = "customer_stated"
                session["asked_field"] = "intent"
                session["intent_question_asked"] = True

                intent = session.get("intent") or {"score": 0, "status": "low", "signals": []}
                signals = intent.setdefault("signals", [])
                signal = "Requested notification/follow-up if product becomes available within stated budget"
                if signal not in signals:
                    signals.append(signal)
                score_value = int(intent.get("score", 0) or 0)
                intent["status"] = (
                    "very_high" if score_value >= 80 else
                    "high" if score_value >= 60 else
                    "medium" if score_value >= 40 else "low"
                )
                session["intent"] = intent
                session["intent_history"] = session.setdefault("intent_history", [])
                session["intent_history"].append({
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "score": intent.get("score", 0),
                    "status": intent.get("status", "low"),
                    "signals": list(signals),
                    "reason": signal,
                })

                if active_language == "telugu":
                    budget_reply = (
                        f"క్షమించండి బ్రో, {budget_label} బడ్జెట్‌లో {current_product}కి సరిపోయే ధృవీకరించిన ధర ఉన్న ఆప్షన్ ప్రస్తుతం కాటలాగ్‌లో కనిపించడం లేదు. "
                        f"ఈ బడ్జెట్‌లో దొరికితే మీకు అప్‌డేట్ ఇవ్వడానికి రిక్వెస్ట్‌ను టీమ్ ఫాలో-అప్ కోసం నమోదు చేశాను. "
                        "ఆటోమేటిక్ అలర్ట్ అందుబాటులో ఉందని మాత్రం హామీ ఇవ్వలేను. "
                        "ఈ బడ్జెట్‌లో ఈ మోడల్ దొరికితే కొనడానికి ఆసక్తిగా ఉన్నారా, బ్రో?"
                    )
                else:
                    budget_reply = (
                        f"Sorry bro, {current_product} ki {budget_label} budget lo match ayye verified-price option catalog lo ippudu kanipinchadam ledu. "
                        "Ee budget lo dorikithe update kosam mee request ni team follow-up ki note chesanu. "
                        "Automatic alert available ani guarantee cheyyalenu. "
                        "Ee budget lo ee model dorikithe konadaniki interested ga unnara bro?"
                    )
                session["_return_template_reply"] = True
                return {
                    "customer_id": customer_id,
                    "message": budget_reply,
                    "customer": customer,
                    "qualification": session.get("qualification"),
                    "intent": intent,
                    "intent_history": session.get("intent_history", []),
                    "lead_id": session.get("lead_id"),
                    "lead_saved": bool(session.get("lead_saved")),
                    "notification_request_recorded": True,
                }

            # If a lower-priced catalog option fits the stated budget, narrow
            # the choices to those options before asking the customer to choose.
            if stated_budget is not None and affordable_variants:
                selected_variants = affordable_variants

            # If the requested battery health is absent, never substitute another
            # battery level's price. Show real alternatives and ask one choice.
            if requested_battery and not selected_variants:
                session["catalog_variant_selection_required"] = True
                session["asked_field"] = "storage"
                product_message = _catalog_options_only_message(
                    current_product, active_language, storage=current_storage, color=current_color
                )
                prefix = (
                    f"{requested_battery.group(1)}% battery health exact match catalog lo confirm kaaledu. Available options ive:\n\n"
                    if active_language != "telugu" else
                    f"{requested_battery.group(1)}% battery health ఖచ్చితమైన match catalog lo confirm kaaledu. Available options ఇవి:\n\n"
                )
                session["_return_template_reply"] = True
                return {
                    "customer_id": customer_id, "message": prefix + product_message,
                    "customer": customer, "qualification": session.get("qualification"),
                    "intent": session.get("intent"), "intent_history": session.get("intent_history", []),
                    "lead_id": session.get("lead_id"), "lead_saved": bool(session.get("lead_saved")),
                }

            needs_choice = (
                len(selected_variants) > 1
                or (not current_storage and not current_color and not requested_battery and len(model_variants) > 1)
            )
            if needs_choice:
                session["catalog_variant_selection_required"] = True
                session["asked_field"] = "storage"
                product_message = _catalog_options_only_message(
                    current_product, active_language, storage=current_storage, color=current_color
                )
                session["_return_template_reply"] = True
                return {
                    "customer_id": customer_id, "message": product_message,
                    "customer": customer, "qualification": session.get("qualification"),
                    "intent": session.get("intent"), "intent_history": session.get("intent_history", []),
                    "lead_id": session.get("lead_id"), "lead_saved": bool(session.get("lead_saved")),
                }

            if len(selected_variants) == 1:
                selected_item = selected_variants[0]
                _remember_inferred_interest_band(customer, selected_item)
                selected_storage = _catalog_storage(selected_item)
                selected_color = _catalog_color(selected_item)
                query = " ".join(x for x in (current_product, selected_storage, selected_color) if x)
                product_message = _get_product_message(
                    query,
                    f"{message} battery health {customer.get('battery_health', '')}",
                    active_language,
                    condition=str(customer.get("condition") or ""),
                )
                session["catalog_product"] = query
                session.pop("catalog_variant_selection_required", None)
            elif not model_variants:
                # Don't ask purchase intent when we couldn't find the requested model.
                session["_return_template_reply"] = True
                no_match = product_message or (
                    f"{current_product} catalog lo confirm kaaledu. Similar available models chupinchana?"
                    if active_language != "telugu" else
                    f"{current_product} catalog lo confirm kaaledu. Similar available models chupinchana?"
                )
                return {
                    "customer_id": customer_id, "message": no_match,
                    "customer": customer, "qualification": session.get("qualification"),
                    "intent": session.get("intent"), "intent_history": session.get("intent_history", []),
                    "lead_id": session.get("lead_id"), "lead_saved": bool(session.get("lead_saved")),
                }
        except Exception as exc:
            print("Compact sales variant selection failed:", type(exc).__name__)

    all_qualification_fields_complete = all(
        _field_is_complete(field, customer, session) for field in FIELD_ORDER
    )
    answering_intent_question = bool(
        session.get("intent_question_asked") and all_qualification_fields_complete
    )
    nonbuyer_mode = _nonbuyer_mode(message)

    # If the customer is responding in a way that deterministic extraction did
    # not understand, let the configured LLM interpret the intent once. Keep the
    # safe deterministic draft as fallback when no provider is configured/fails.
    recognized_business_reply = bool(
        extracted or intent_followup or price_objection or repair_message
        or product_message or _store_faq_reply(message, active_language)
        or _is_greeting_message(message)
    )
    if (
        session.get("compact_sales_flow")
        and _has_business_context(session)
        and not recognized_business_reply
        and not answering_intent_question
    ):
        fallback_draft = (
            "Mee message ni exact ga artham chesukolekapoyanu bro. Phone price, condition, availability leda purchase interest gurinchi chepthunnara?"
            if active_language != "telugu" else
            "మీ మెసేజ్‌ను పూర్తిగా అర్థం చేసుకోలేకపోయాను బ్రో. ఫోన్ ధర, కండిషన్, అందుబాటు లేదా కొనుగోలు ఆసక్తి గురించి చెబుతున్నారా?"
        )
        try:
            llm_reply = generate_response(
                draft_response=fallback_draft,
                customer_message=message,
                language=active_language,
                customer=customer,
                conversation_history=session.get("conversation_history", [])[-8:],
                catalog_context=[],
                force_llm=True,
            )
            if llm_reply and llm_reply.strip() != fallback_draft.strip():
                session["_return_template_reply"] = True
                return {
                    "customer_id": customer_id, "message": llm_reply,
                    "customer": customer, "qualification": session.get("qualification"),
                    "intent": session.get("intent"), "intent_history": session.get("intent_history", []),
                    "lead_id": session.get("lead_id"), "lead_saved": bool(session.get("lead_saved")),
                }
        except Exception as exc:
            print("Ambiguous-intent LLM response failed:", type(exc).__name__)

    if nonbuyer_mode == "decline" and not answering_intent_question:

        soft_reply = _low_pressure_reply(nonbuyer_mode, active_language)

        draft_reply = (product_message.strip() + "\n\n" if product_message else "") + soft_reply

        return {

            "customer_id": customer_id,

            "message": _localize_reply(draft_reply, message, customer, session),

            "customer": customer,

            "qualification": session.get("qualification"),

            "intent": session.get("intent"),

            "intent_history": session.get("intent_history", []),

            "lead_id": session.get("lead_id"),

            "lead_saved": False,

        }

    # ---------------------------------------------------------

    # SIX REQUIRED QUESTIONS

    # ---------------------------------------------------------

    # Find the next required field that

    # hasn't been collected.

    next_field = next(

        (

            field

            for field in FIELD_ORDER

            if not _field_is_complete(

                field,

                customer,

                session,

            )

        ),

        None,

    )

    if next_field:

        session["asked_field"] = (

            next_field

        )

        # Ask the next question directly.

        # Don't unnecessarily repeat:

        # "Thanks! I've recorded..."

        message_parts = []

        acknowledgement = session.pop("_qualification_ack", "")
        if acknowledgement:
            message_parts.append(acknowledgement.strip())

        if product_message:

            message_parts.append(

                product_message.strip()

            )

        if model_only_needs_preferences:
            # Ask once for storage, mention color is optional, and collect the
            # budget in the same turn. No catalog product list or URL here.
            if active_language == "telugu":
                preference_question = "మీకు ఏ స్టోరేజ్ వేరియంట్ కావాలి (128GB/256GB వంటివి)? ఏ కలర్ కావాలి?"
                if not customer.get("budget"):
                    preference_question += " మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
            else:
                preference_question = "Ye storage variant kavali (128GB/256GB lantivi)? Ye color kavali?"
                if not customer.get("budget"):
                    preference_question += " Mee budget entha anukuntunnaru?"
            message_parts.append(preference_question)
        elif product_message and next_field == "budget" and _is_specific_model_name(current_product):
            if session.get("catalog_variant_unresolved"):
                if active_language == "telugu":
                    combined_question = "పైన ఉన్న కాటలాగ్ ఆప్షన్లలో సరిపోయే వేరియంట్‌ను ఎంచుకోండి. మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
                else:
                    combined_question = "Paina unna catalog options lo meeku set ayye variant ni select cheyyandi. Mee budget entha anukuntunnaru?"
            elif current_storage and not current_color:
                if active_language == "telugu":
                    combined_question = "మీకు ఏ కలర్ కావాలి? మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
                else:
                    combined_question = "Ye color kavali? Mee budget entha anukuntunnaru?"
            elif current_color and not current_storage:
                if active_language == "telugu":
                    combined_question = "మీకు ఏ స్టోరేజ్ కావాలి? మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
                else:
                    combined_question = "Ye storage kavali? Mee budget entha anukuntunnaru?"
            elif current_storage and current_color:
                combined_question = _localized_question("budget", active_language, customer)
            elif active_language == "telugu":
                combined_question = "మీకు ఏ స్టోరేజ్, కలర్ కావాలి? మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?"
            else:
                combined_question = "Ye storage, color kavali? Mee budget entha anukuntunnaru?"
            message_parts.append(combined_question)
        elif product_message and next_field == "storage" and _is_specific_model_name(current_product):
            if session.get("catalog_variant_unresolved"):
                if active_language == "telugu":
                    message_parts.append("పైన ఉన్న కాటలాగ్ ఆప్షన్లలో మీకు సరిపోయే స్టోరేజ్, కలర్ వేరియంట్‌ను ఎంచుకోండి.")
                else:
                    message_parts.append("Paina unna catalog options lo meeku kavalsina storage, color variant ni select cheyyandi.")
            elif current_storage and not current_color:
                if active_language == "telugu":
                    message_parts.append("ఈ స్టోరేజ్‌లో మీకు ఏ కలర్ కావాలి?")
                else:
                    message_parts.append("Ee storage lo ye color kavali?")
            elif current_color and not current_storage:
                if active_language == "telugu":
                    message_parts.append("మీకు ఏ స్టోరేజ్ వేరియంట్ కావాలి?")
                else:
                    message_parts.append("Ye storage variant kavali?")
            else:
                if active_language == "telugu":
                    message_parts.append("మీకు ఏ స్టోరేజ్, కలర్ వేరియంట్ కావాలి?")
                else:
                    message_parts.append("Ye storage, color variant kavali?")
        else:
            message_parts.append(_localized_question(next_field, active_language, customer))

        draft_reply = (

            "\n\n".join(

                message_parts

            )

        )

        session["_return_template_reply"] = True

        session["_return_template_reply"] = True
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                draft_reply,
                message,
                customer,
                session,
            ),
            "customer": customer,
            "qualification": None,

            "intent": session[

                "intent"

            ],

            "intent_history": session[

                "intent_history"

            ],

            "lead_id": session[

                "lead_id"

            ],

            "lead_saved": False,

        }

    # ---------------------------------------------------------

    # ALL SIX QUESTIONS COMPLETE

    # ---------------------------------------------------------

    #

    # Ask the additional willingness-to-buy question.

    # This is NOT replacing the six-question flow.

    # ---------------------------------------------------------

    if not session["intent_question_asked"]:

        session["intent_question_asked"] = True

        # Ask one consistent final intent question only. Do not ask a second
        # timeline-vs-comparison clarification after the customer answers it.
        if active_language == "telugu":
            draft_reply = "ఈ ధర, స్టాక్ మీకు సరిపోతే ఈరోజే కొనడానికి ఆసక్తి ఉందా బ్రో? మీ సమాధానాన్ని మా టీమ్‌కు తెలియజేసి, వారు మిమ్మల్ని సంప్రదించడానికి సహాయం చేస్తాను."
        else:
            draft_reply = "Ee price, stock meeku set aithe eeroje konadaniki interested ga unnara bro? Mee interest maa team ki share chesi, vallu mimmalni contact avvadaniki help chestha."

        if product_message:
            draft_reply = product_message.strip() + "\n\n" + draft_reply
        session["_return_template_reply"] = True
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                draft_reply,
                message,
                customer,
                session,
            ),
            "customer": customer,
            "qualification": None,

            "intent": session[

                "intent"

            ],

            "intent_history": session[

                "intent_history"

            ],

            "lead_id": session[

                "lead_id"

            ],

            "lead_saved": False,

        }

    # The purchase-intent question is intentionally one-time. The first reply
    # after it is recorded as positive, comparison, decline, price concern, or
    # other intent; do not ask the same question again when wording is informal.

    # ---------------------------------------------------------

    # FINAL LEAD CREATION

    # ---------------------------------------------------------

    #

    # We don't call score_lead() here because the current

    # scoring module exposes calculate_intent().

    #

    # The intent object is used as the qualification result

    # while preserving the existing qualification structure:

    #

    # score

    # status

    # reasons

    # ---------------------------------------------------------

    current_intent = (

        session["intent"]

    )

    qualification = {

        "score": current_intent.get(

            "score",

            0,

        ),

        "status": current_intent.get(

            "status",

            "low",

        ),

        "reasons": current_intent.get(

            "signals",

            [],

        ),

    }

    if session.get("record_id"):

        saved_lead = update_lead(

            lead_id=session["record_id"],

            customer=customer,

            qualification=qualification,

            intent=current_intent,

            conversation_history=session["conversation_history"],

            status="completed",

        )

    else:

        saved_lead = save_lead(

            customer,

            qualification,

            intent=current_intent,

            conversation_history=session["conversation_history"],

            status="completed",

        )

    if not saved_lead:

        saved_lead = save_lead(

            customer,

            qualification,

            intent=current_intent,

            conversation_history=session["conversation_history"],

            status="completed",

        )

    session["record_id"] = saved_lead.get("lead_id")

    session["lead_saved"] = True

    session["lead_id"] = (

        saved_lead.get(

            "lead_id"

        )

    )

    session["qualification"] = (

        qualification

    )

    # ---------------------------------------------------------

    # FINAL CUSTOMER RESPONSE

    # ---------------------------------------------------------

    # Send three separate, language-locked WhatsApp messages after qualification:

    # (1) requirements recorded, (2) payment-security warning, (3) friendly close.

    final_intent_kind = _intent_response_kind(message) if session.get("intent_question_asked") else "unknown"
    if final_intent_kind in {"compare", "price_concern", "decline"}:
        if session.get("language") == "telugu":
            first_final_message = (
                "ధన్యవాదాలు బ్రో 😊 ఇప్పుడే కొనాలని నిర్ణయించుకోలేదని నమోదు చేశాము. ఎలాంటి ఒత్తిడి లేదు. ఇంకా ఏ మొబైల్ గురించి తెలుసుకోవాలన్నా మెసేజ్ చేయండి."
                if final_intent_kind == "decline" else
                _saved_purchase_acknowledgement(message, customer, session.get("language", "roman_telugu")) + "\n\nఇంకా ఏ మొబైల్ గురించి తెలుసుకోవాలన్నా మెసేజ్ చేయండి 😊"
            )
            final_messages = [first_final_message]
        else:
            first_final_message = (
                "Dhanyavaadalu bro 😊 Ippudu konalani decide avvaledani note chesam. Pressure emi ledu. Inka ye mobile gurinchi telusukovalanna message cheyyandi."
                if final_intent_kind == "decline" else
                _saved_purchase_acknowledgement(message, customer, session.get("language", "roman_telugu")) + "\n\nInka ye mobile gurinchi telusukovalanna message cheyyandi 😊"
            )
            final_messages = [first_final_message]
    elif final_intent_kind == "positive" and session.get("language") == "telugu":
        final_messages = [
            "ధన్యవాదాలు బ్రో 😊 మీ కొనుగోలు ఆసక్తిని నమోదు చేశాము. ధర, స్టాక్ నిర్ధారించడానికి మా టీమ్ మిమ్మల్ని సంప్రదించవచ్చు. ఇంకా ఏ మొబైల్ గురించి తెలుసుకోవాలన్నా మెసేజ్ చేయండి."
        ]
        _link = _purchase_link_line(customer, "telugu")
        if _link:
            final_messages[0] += "\n\n" + _link
    elif final_intent_kind == "positive":
        final_messages = [
            "Dhanyavaadalu bro 😊 Mee purchase interest note chesam. Price, stock confirm cheyadaniki maa team mimmalni contact avvachu. Inka ye mobile gurinchi telusukovalanna message cheyyandi."
        ]
        _link = _purchase_link_line(customer, "roman_telugu")
        if _link:
            final_messages[0] += "\n\n" + _link
    elif session.get("language") == "telugu":

        final_messages = [

            (

                "ధన్యవాదాలు 😊 మీ అవసరాలను Mania Mobiles కోసం నమోదు చేశాము. "

                "మోడల్ యొక్క తాజా ధర మరియు లభ్యతను స్టోర్‌తో నిర్ధారించుకోవచ్చు."

            ),

            (

                "ముఖ్యమైన విషయం: మీ కార్డ్ నంబర్, CVV, PIN లేదా OTP ఎవరితోనూ ఈ చాట్‌లో "

                "పంచుకోవద్దు. Mania Mobiles తరఫున ఎవరైనా OTP అడిగినా ఇవ్వవద్దు."

            ),

            (

                "ఇంకా ఏ మొబైల్ లేదా ప్రోడక్ట్ గురించి తెలుసుకోవాలన్నా Mania Mobiles‌కు "

                "మెసేజ్ చేయండి. 😊"

            ),

        ]

    else:

        final_messages = [

            (

                "Dhanyavaadalu 😊 Mee requirements Mania Mobiles kosam note chesam. "

                "Model yokka latest price mariyu availability ni store tho confirm chesukovachu."

            ),

            (

                "Mukhyam: Mee card number, CVV, PIN leka OTP ni ee chat lo evarithonu "

                "share cheyyakandi. Mania Mobiles peru tho evaraina OTP adigina ivvakandi."

            ),

            (

                "Inka ye mobile leda product gurinchi telusukovalanna Mania Mobiles ki "

                "message cheyyandi. 😊"

            ),

        ]

    # Dashboard/chat callers still receive one combined \`message\` string.

    # The WhatsApp webhook can use \`messages\` to send these as three bubbles.

    session["_return_template_reply"] = True

    combined_message = "\n\n".join(final_messages)

    return {

        "customer_id": customer_id,

        "message": _localize_reply(

            combined_message,

            message,

            customer,

            session,

        ),

        "messages": final_messages,

        "customer": customer,

        "qualification": qualification,

        "intent": session[

            "intent"

        ],

        "intent_history": session[

            "intent_history"

        ],

        "lead_id": session[

            "lead_id"

        ],

        "lead_saved": True,

    }

def _history_has_intent_question(history: list) -> bool:
    markers = (
        "purchase proceed avvalanukuntunnara",
        "next few weeks lo konalanukuntunnara",
        "options compare chesthunnara",
        "confirm cheyamantara",
        "would you like to proceed",
        "comparing options for now",
        "కొనుగోలు చేయడానికి ముందుకు",
        "రాబోయే కొన్ని వారాల్లో కొనాలనుకుంటున్నారా",
        "మోడళ్లను పోల్చి చూస్తున్నారా",
        "ధర మరియు స్టాక్‌ను స్టోర్‌తో నిర్ధారించమంటారా",
    )
    for item in reversed(history or []):
        if not isinstance(item, dict) or item.get("sender_type") != "assistant":
            continue
        text = str(item.get("message") or "").lower()
        if any(marker in text for marker in markers):
            return True
    return False


def _restore_session_from_storage(customer_id: str) -> Optional[dict]:

    """Recover a customer's latest lead/session after a process restart."""

    phone = customer_id[3:] if customer_id.startswith("wa_") else customer_id

    try:

        lead = get_latest_lead_by_phone(phone)

    except Exception as exc:

        print(f"Could not restore saved session: {type(exc).__name__}: {exc}")

        return None

    if not lead:

        return None

    intent = dict(lead.get("intent") or {})

    conversation_status = str(

        lead.get("conversation_status")

        or intent.get("session_status")

        or "active"

    ).lower()

    if conversation_status == "abandoned" or intent.get("session_status") == "abandoned":

        return None

    customer = dict(lead.get("customer") or {})

    if phone:

        customer.setdefault("phone_number", phone)

    qualification = dict(lead.get("qualification") or {})

    history = list(lead.get("conversation_history") or [])

    # Preserve the conversation's prior language across process restarts.

    restored_language = next(

        (

            str(item.get("language"))

            for item in reversed(history)

            if isinstance(item, dict)

            and item.get("language") in {"telugu", "roman_telugu"}

        ),

        "roman_telugu",

    )

    session = _get_session(customer_id)

    session.update({

        "customer": customer,

        "asked_field": next(

            (field for field in RESTORE_FIELD_ORDER if not _field_is_complete(field, customer)),

            "intent",

        ),

        "lead_saved": (
            conversation_status == "completed"
            or intent.get("session_status") == "completed"
        ),
        "_restored_completed_session": (
            conversation_status == "completed"
            or intent.get("session_status") == "completed"
        ),

        "lead_id": lead.get("lead_id"),

        "record_id": lead.get("lead_id"),

        "qualification": qualification,

        "language": restored_language,

        "last_activity": lead.get("last_activity") or lead.get("updated_at"),

        "intent": intent or {

            "score": qualification.get("score", 0),

            "status": qualification.get("status", "low"),

            "signals": qualification.get("reasons", []),

        },

        "intent_history": [],

        "conversation_history": history,

        "intent_question_asked": (
            conversation_status == "completed"
            or intent.get("session_status") == "completed"
            or _history_has_intent_question(history)
        ),

        "catalog_message_sent": bool(customer.get("product_interest")),

        "catalog_product": customer.get("product_interest"),

    })

    return session

# Inactivity timeout: keep a conversation active for 30 minutes after the

# customer's last message. The partial lead remains permanently in Supabase.

SESSION_TIMEOUT = timedelta(minutes=30)

def _is_greeting_message(message: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\u0c00-\u0c7f]+", " ", (message or "").lower()).strip()
    return bool(re.fullmatch(
        r"(?:hi|hii|hiii|hello|hey|hai|gm|good morning|good afternoon|good evening|namaste|namaskar|howdy|how are you|hey there|hello there|hi there|what s up|\u0c39\u0c3e\u0c2f\u0c4d|\u0c39\u0c32\u0c4b|\u0c28\u0c2e\u0c38\u0c4d\u0c15\u0c3e\u0c30\u0c02)"
        r"(?:\s+(?:bro|anna|sir|madam|boss))?",
        normalized,
    ))


def _begin_new_cycle_after_completed_lead(session: dict, customer_id: str) -> None:
    """Start a brand-new lead/session after a completed conversation.

    Keep the old completed database row intact. The next persistence pass creates
    a fresh lead row with empty budget/product/intent/history, while preserving
    only the customer's phone number and name.
    """
    old_customer = session.get("customer") or {}
    preserved = {key: old_customer[key] for key in ("phone_number", "name") if old_customer.get(key)}
    if customer_id.startswith("wa_"):
        preserved["phone_number"] = customer_id[3:]
    elif customer_id.replace("+", "").isdigit():
        preserved["phone_number"] = customer_id
    session.clear()
    session.update({
        "customer": preserved,
        "asked_field": "product_interest",
        "lead_saved": False,
        "lead_id": None,
        "record_id": None,
        "qualification": None,
        "language": "roman_telugu",
        "language_locked": False,
        "last_activity": None,
        "intent": {"score": 0, "status": "low", "signals": [], "session_status": "active"},
        "intent_history": [],
        "conversation_history": [],
        "intent_question_asked": False,
        "catalog_message_sent": False,
        "catalog_product": None,
        "compact_sales_flow": False,
        "catalog_variant_selection_required": False,
        "_restored_completed_session": False,
    })


def _has_any(text: str, phrases) -> bool:
    """Return True when any non-empty phrase occurs in text, case-insensitively."""
    normalized_text = (text or "").casefold()
    return any(
        isinstance(phrase, str)
        and phrase.strip()
        and phrase.casefold() in normalized_text
        for phrase in (phrases or ())
    )


def _is_ai_identity_question(message: str) -> bool:
    """Recognize direct questions about whether the assistant is AI/a bot."""
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    patterns = (
        r"\b(?:are you|r u) (?:an? )?(?:ai|bot|robot|chatbot|virtual assistant)\b",
        r"\b(?:are you a real person|are you human|is this automated)\b",
        r"\b(?:nv|nuvvu|meeru) (?:ai|bot|robot|chatbot|ai agent)(?: ah| aa| na)?\b",
        r"\b(?:idi|meeru) ai (?:agent|bot)(?: ah| aa| na)?\b",
        r"\b(?:ai agent ah|ai bot ah|bot ah|chatbot ah)\b",
        r"మీరు (?:ఏఐ|AI|బాట్|రోబోట్)(?: నా| ఆ)?",
        r"ఇది (?:ఏఐ|AI|బాట్)(?: నా| ఆ)?",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _is_price_objection(message: str) -> bool:
    """Recognize price shock/affordability comments without asking again."""
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    phrases = (
        "kidney ammaali", "kidney ammal", "kidney sell", "sell a kidney",
        "too expensive", "very expensive", "too costly", "so costly",
        "price is too high", "way too much", "can't afford", "cannot afford",
        "budget ki minchi", "budget kanna ekkuva", "chala costly", "chaala costly",
        "antha rate", "inta price", "intha price", "price ekkuva", "rate ekkuva",
        "ధర చాలా ఎక్కువ", "చాలా ఖరీదు", "బడ్జెట్‌కు మించి",
    )
    return any(phrase in text for phrase in phrases)


def _intent_response_kind(message: str) -> str:
    """Classify the one-time purchase-intent answer using local rules."""
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    if not text:
        return "unknown"
    if _is_ai_identity_question(text):
        return "question"
    if _is_price_objection(text):
        return "price_concern"
    compare_phrases = (
        "models compare cheydaniki", "models compare cheyadaniki",
        "model compare cheyadaniki", "compare cheyadaniki", "compare cheyyadaniki",
        "models compare", "model compare", "options compare", "just comparing",
        "only comparing", "comparing options", "compare chesthunna", "compare chestunna",
        "ippatiki compare", "inka compare", "models chusthunna", "models choosthunna",
        "ఇప్పటికి మోడల్స్ పోల్చి", "మోడళ్లను పోల్చి", "పోల్చి చూస్తున్నాను",
    )
    if any(phrase in text for phrase in compare_phrases):
        return "compare"
    negative_phrases = (
        "not interested", "don't want to buy", "do not want to buy",
        "not buying now", "not planning to buy", "no plan to buy",
        "i won't buy", "i will not buy", "maybe not", "not now", "nope",
        "not decided", "haven't decided", "have not decided",
        "present order cheyali anukovatledhu", "present order cheyyali anukovatledu",
        "nen inkaa konalani decide avvatle", "nenu inkaa konalani decide avvatle",
        "inka konalani decide avvatle", "inka konalani decide avvaledu",
        "konalani decide avvatle", "konalani decide avvaledu", "decide avvatle", "decide avvaledu",
        "konalane uddesam ledhu", "konalane uddesam ledu", "konalani uddesam ledu",
        "konanu", "konalani ledu", "konadaniki plan ledu", "ippudu konanu",
        "present order cheyali anukovatledhu", "present order cheyyali anukovatledu",
        "ippudu order cheyyanu", "not decided", "haven't decided", "have not decided",
        "not planning to order", "not ordering now", "no intention to buy", "buy later",
        "interest ledu", "konalane uddesam ledhu", "konalani uddesam ledu",
        "వద్దు", "కొనను", "కొనాలని లేదు", "ఆసక్తి లేదు", "ఇంకా కొనాలని నిర్ణయించుకోలేదు", "ఇప్పుడు ఆర్డర్ చేయను",
    )
    if text in {"no", "no thanks", "not now", "nope", "కాదు"} or any(p in text for p in negative_phrases):
        return "decline"
    positive_phrases = (
        "haa definite ga", "ha definite ga", "avunu definite ga", "definite ga",
        "haa", "ha", "yes", "yes bro", "yes anna", "avunu bro", "avunu anna",
        "kondam anukuntunna", "kondham anukuntunna", "kondam anukuntunna",
        "konalanukuntunna", "konali anukuntunna", "kondam", "kondham",
        "konestha", "konukunta", "konali", "buy cheddam", "purchase cheddam",
        "yes definitely", "yes, definitely", "definitely", "absolutely",
        "i want to buy", "i will buy", "want to purchase", "proceed with purchase",
        "avunu", "అవును", "ఖచ్చితంగా", "కొనాలనుకుంటున్నాను", "కొంటాను",
    )
    if text in {"yes", "yes definitely", "definitely", "sure", "absolutely", "yeah", "yep", "avunu", "అవును", "ఖచ్చితంగా"} or any(p in text for p in positive_phrases):
        return "positive"
    return "unknown"


def _ai_identity_reply(language: str) -> str:
    if language == "telugu":
        return "అవును 😊 నేను Mania Mobiles కోసం రూపొందించిన AI అసిస్టెంట్‌ని. మొబైల్ మోడళ్లు, ధరలు, స్టాక్ వివరాలు మరియు మీ అవసరాలకు సరిపోయే ఎంపికల్లో సహాయం చేస్తాను."
    return "Avunu bro 😊 Nenu Mania Mobiles kosam design chesina AI assistant ni. Mobile models, prices, stock details, mee requirements ki tagina options lo help chestha."


def _saved_purchase_acknowledgement(message: str, customer: dict, language: str) -> str:
    """Acknowledge follow-ups after the one-time intent answer; never re-ask it."""
    kind = _intent_response_kind(message)
    product = str(customer.get("product_interest") or customer.get("model") or "mobile").strip()
    storage = str(customer.get("storage") or "").strip()
    budget = customer.get("budget") or customer.get("budget_amount")
    product_label = f"{product} {storage}".strip()
    budget_text = f"₹{budget}" if str(budget or "").strip() else "mee budget"
    if language == "telugu":
        if kind == "price_concern":
            return f"అర్థమైంది 😊 {product_label} ధర మీ {budget_text} బడ్జెట్‌కు ఎక్కువగా అనిపిస్తోంది. తాజా ధర, స్టాక్‌ను స్టోర్‌తో నిర్ధారించాలి; మీ బడ్జెట్‌లో సరిపోయే ప్రత్యామ్నాయాలను పోల్చి చూడొచ్చు."
        if kind == "compare":
            return f"అర్థమైంది 😊 మీరు ప్రస్తుతం మోడళ్లను పోల్చి చూస్తున్నారని నమోదు చేశాను. మీ వివరాలు ఇప్పటికే సేవ్ అయ్యాయి; అదే ప్రశ్న మళ్లీ అడగను."
        if kind == "positive":
            link = _purchase_link_line(customer, language)
            return (f"సరే 😊 మీ కొనుగోలు ఆసక్తిని నమోదు చేశాను. {product_label}, బడ్జెట్ {budget_text}. తాజా ధర, స్టాక్‌ను Mania Mobiles స్టోర్‌తో నిర్ధారించాలి."
                    + (f"\n\n{link}" if link else ""))
        return "మీ వివరాలు ఇప్పటికే నమోదు చేశాను 😊 Mania Mobiles మొబైల్ వివరాల్లో మీకు సహాయం చేస్తాను."
    if kind == "price_concern":
        return f"Ardham ayyindi bro 😊 {product_label} price mee {budget_text} budget ki ekkuva anipisthondi. Latest price/stock store tho confirm cheyyali; mee budget lo fit ayye alternatives ni compare cheyochu."
    if kind == "compare":
        return "Ardham ayyindi bro 😊 Ippatiki models compare chesthunnaru ani note chesanu. Mee details already save ayyayi; ade question malli adaganu."
    if kind == "positive":
        link = _purchase_link_line(customer, language)
        return (f"Sare bro 😊 Mee purchase interest note chesanu. {product_label}, budget {budget_text}. Latest price/stock Mania Mobiles store tho confirm cheyyali."
                + (f"\n\n{link}" if link else ""))
    return "Mee details already note chesanu bro 😊 Mania Mobiles mobile details lo help chestha."


def _is_clear_intent_response(message: str) -> bool:
    """True for clear purchase, comparison, decline, or price-concern answers."""
    return _intent_response_kind(message) in {"positive", "compare", "decline", "price_concern"}


def _is_context_repair_message(message: str) -> bool:
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    phrases = (
        "what mobile did i ask", "which mobile did i ask", "what phone did i ask",
        "i already told", "already told you", "i said that already",
        "you are not understanding", "you don't understand", "not what i asked",
        "worst replies", "what are you answering", "what did i ask",
        "asalu nenu adugutundhi", "asalu nenu adigedi", "nv cheptundhi yenti",
        "nuvvu cheptundi enti", "cheppa kadha", "cheppa kada", "already cheppa",
        "nen ye mobile adigano", "nenu ye mobile adigano", "nen inka ye mobile",
        "nenu inka ye mobile", "nen inkaa ye mobile", "nenu inkaa ye mobile",
        "nen ye phone adigano", "ye mobile oo cheppaledhu", "ye mobile cheppaledhu",
        "నేను ఏ మొబైల్ అడిగానో", "నేను చెప్పాను కదా", "మీరు ఏం చెప్తున్నారు",
        "అది నేను అడగలేదు", "నేను ఇంకా ఏ మొబైల్",
    )
    return any(phrase in text for phrase in phrases)


def _is_payment_objection(message: str) -> bool:
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    patterns = (
        r"\bwhy (do you|would you|should you) (need|care|ask)",
        r"\bwhy does it matter\b", r"\bnone of your business\b",
        r"\byenduku\b.*\b(neeku|meeku)\b",
        r"\b(neeku|meeku) yenduku\b", r"\bpayment.*yenduku\b",
        r"\bappude payment.*yenduku\b", r"\bkone mundu.*decide avtha\b",
        r"\bkone mundu.*decide avthanu\b", r"\bproduct kone mundu decide\b",
        r"\bdecide avtha(nu)?\b", r"\bnenu payment.*yela chesthe.*yenduku\b",
        r"\bcompulsory kaadu kada\b", r"\bwhy are you asking about payment\b",
        r"పేమెంట్.*ఎందుకు", r"నేను ఎలా చెల్లిస్తే మీకెందుకు",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _has_business_context(session: dict) -> bool:
    customer = session.get("customer") or {}
    if any(customer.get(key) for key in ("product_interest", "brand", "storage", "budget", "use_case")):
        return True
    history = session.get("conversation_history") or []
    return any(
        isinstance(item, dict) and item.get("sender_type") == "assistant"
        and any(word in str(item.get("message") or "").lower() for word in ("mobile", "phone", "మొబైల్", "ఫోన్", "variant", "storage"))
        for item in history[-8:]
    )


def _context_repair_reply(language: str, customer: dict, session: dict) -> str:
    """Create a safe deterministic fallback for context/complaint messages."""
    product = str(customer.get("model") or customer.get("product_interest") or "").strip()
    brand = str(customer.get("brand") or "").strip()
    storage = str(customer.get("storage") or "").strip()
    budget = customer.get("budget")
    budget_text = _format_budget(budget) if budget else ""

    if language == "telugu":
        if product or brand:
            details = product if _is_specific_model_name(product) else (brand or product)
            if storage:
                details += f", {storage}"
            if budget_text:
                details += f", బడ్జెట్ {budget_text}"
            if not _is_specific_model_name(product):
                return f"క్షమించండి 😊 మీరు {details} అని చెప్పారు. బ్రాండ్/స్టోరేజ్ అర్థమైంది, కానీ ఖచ్చితమైన మోడల్ ఇంకా ఎంచుకోలేదు. ఏ మోడల్ కావాలి? తెలియకపోతే మీ బడ్జెట్‌కు సరిపోయే మోడల్స్ సూచిస్తాను."
            next_field = next((field for field in FIELD_ORDER if not _field_is_complete(field, customer, session)), None)
            if next_field:
                return f"క్షమించండి 😊 మీరు {details} అని చెప్పారు; ఆ వివరాలు గుర్తున్నాయి. " + _localized_question(next_field, language, customer)
            return f"క్షమించండి 😊 మీరు {details} గురించి అడుగుతున్నారు. ధర/స్టాక్‌ను నిర్ధారించి సహాయం చేస్తాను."
        return "క్షమించండి 😊 మీ మాటను సరిగా అర్థం చేసుకోలేదు. ఏ మొబైల్ మోడల్ గురించి మాట్లాడుతున్నామో చెప్పండి; మిగతా వివరాలు మళ్లీ అడిగి ఇబ్బంది పెట్టను."

    if product or brand:
        details = product if _is_specific_model_name(product) else (brand or product)
        if storage:
            details += f", {storage}"
        if budget_text:
            details += f", budget {budget_text}"
        if not _is_specific_model_name(product):
            return f"Sorry bro 😊 Meeru {details} ani chepparu. Brand/storage ardham ayyayi, kani exact model inka select cheyyaledu. Ye model kavali? Teliyakapotey mee budget ki set ayye models suggest chestha."
        next_field = next((field for field in FIELD_ORDER if not _field_is_complete(field, customer, session)), None)
        if next_field:
            return f"Sorry bro 😊 Meeru {details} ani chepparu; aa details gurthunnayi. " + _localized_question(next_field, language, customer)
        return f"Sorry bro 😊 Meeru {details} gurinchi adugutunnaru. Price/stock verify chesi help chestha."
    return "Sorry bro 😊 Mee message ni context tho sarigga connect cheyyaledu. Ye mobile model gurinchi matladuthunnamo cheppandi; details malli malli adaganu."


def _is_valid_answer_for_current_field(message: str, session: dict) -> bool:
    """Recognize concise answers before deciding whether a message is off-topic."""
    text = (message or "").strip().lower()
    field = session.get("asked_field")
    if not text:
        return False
    if session.get("intent_question_asked") and _is_clear_intent_response(message):
        return True
    if field == "payment_method" and _is_payment_objection(message):
        return True
    if field == "storage" and ((session.get("customer") or {}).get("product_interest") or (session.get("customer") or {}).get("model")):
        if re.search(r"\b(black|white|blue|pink|green|teal|ultramarine|yellow|purple|natural|titanium|silver|gold|midnight|starlight|graphite|desert|sierra|red)\b|నలుపు|తెలుపు|నీలం|పింక్|ఆకుపచ్చ", text, re.I):
            return True
    if _is_context_repair_message(message) and _has_business_context(session):
        return True
    if field == "budget" and re.search(r"\d", text):
        return True
    if re.search(r"\b\d{1,4}\s*(?:gb|tb)\b", text):
        return True
    if (field == "storage" or ((session.get("customer") or {}).get("product_interest") or (session.get("customer") or {}).get("brand"))) and re.search(r"(?<!\d)(64|128|256|512)\s*(?:gb)?(?:\s*(?:kavali|kavala|kaavali|variant|storage|want|please))?\b", text):
        return True
    if field == "purchase_timeline" and re.search(
        r"\b(today|tomorrow|tonight|this week|next week|this month|next month|later|within|week|month|ivala|eeroju|repu|tarvatha|tarwatha)\b|రేపు|తర్వాత|తరువాత|ఈరోజు|ఇవాళ|ఈ వారం|వచ్చే వారం|\u0c30\u0c47\u0c2a\u0c41|\u0c08 \u0c35\u0c3e\u0c30\u0c02",
        text,
    ):
        return True
    if field == "condition" and re.search(r"\b(new|brand new|sealed|used|second hand|refurbished|kotha|kottha)\b|కొత్త|సీల్డ్|వాడిన|రిఫర్బిష్డ్|\u0c15\u0c4a\u0c24\u0c4d\u0c24|\u0c35\u0c3e\u0c21\u0c3f\u0c28", text):
        return True
    if field == "payment_method" and re.search(r"\b(cash|card|upi|gpay|phonepe|paytm|emi|yes|any)\b|నగదు|కార్డు|కార్డ్|యూపీఐ|ఈఎంఐ", text):
        return True
    if field == "location" and len(text.split()) <= 3 and (
        re.fullmatch(r"[a-zA-Z][a-zA-Z .'-]{1,49}", message.strip())
        or re.fullmatch(r"[\u0c00-\u0c7f\s]{2,40}", message.strip())
    ):
        return True
    if field == "product_interest" and re.search(r"\b(iphone|samsung|oneplus|oppo|vivo|realme|redmi|xiaomi|google pixel|motorola|nothing|mobile|phone)\b|\b[a-z]+\s*\d{1,3}\b", text):
        return True
    return False


def _is_explicit_off_topic(message: str) -> bool:
    """Obvious unrelated requests. These never reach the LLM (no cost, no drift)."""
    text = (message or "").strip().lower()
    off_topic_terms = (
        "tell me a joke", "make me laugh", "write a poem", "solve my homework",
        "do my assignment", "weather forecast", "news about politics", "political news",
        "recipe for", "cook biryani", "who won the match", "cricket score",
        "translate this sentence", "write a love letter", "generate an image",
        "debug my code", "write python code", "what is the capital",
    )
    if any(term in text for term in off_topic_terms):
        return True
    return bool(re.fullmatch(
        r"(how are you|what are you doing|tell me something|who are you|what is your name)[?.! ]*",
        text,
    ))


def _has_extractable_business_detail(message: str, session: dict) -> bool:
    """True when the rule-based extractors find a real detail in a follow-up.

    Short follow-ups such as "45k varaku", "weekend lo bro", "Bhimavaram nunchi
    vastha" or "Ha" carry budget/timeline/location/confirmation data. They must
    reach the normal flow so the detail is stored, instead of being rejected as
    off-topic. Only applies once the conversation has business context.
    """
    if not _has_business_context(session):
        return False
    language = session.get("language") or "roman_telugu"
    return has_concrete_signal(extract_deterministic_details(message, language))


def _is_business_related_message(message: str, session: dict) -> bool:
    """Conservative local scope gate: no model call for obvious unrelated input."""
    text = (message or "").strip().lower()

    # Explicit off-topic intent takes precedence over field heuristics. For example,
    # "write a poem" must not be mistaken for a town name when location is pending.
    if _is_explicit_off_topic(message):
        return False
    if _is_ai_identity_question(message):
        return True
    if _is_valid_answer_for_current_field(message, session):
        return True
    if _has_extractable_business_detail(message, session):
        return True
    if _has_business_context(session) and _is_context_repair_message(message):
        return True
    if session.get("asked_field") == "payment_method" and _has_business_context(session) and _is_payment_objection(message):
        return True

    business_terms = (
        "phone", "mobile", "iphone", "samsung", "oneplus", "oppo", "vivo",
        "realme", "redmi", "xiaomi", "pixel", "motorola", "nothing phone",
        "price", "cost", "budget", "storage", "gb", "tb", "variant", "model",
        "available", "stock", "battery", "camera", "display", "gaming", "warranty",
        "exchange", "emi", "cash", "card", "upi", "delivery", "discount", "offer",
        "store", "brand new", "sealed", "used", "refurbished", "compare", "recommend",
        "decide", "konalani", "order", "reserve", "reservation", "team contact",
        "better phone", "which phone", "which mobile", "phone kavali", "mobile kavali",
        "konali", "konukunta", "dhara", "entha", "ధర", "మొబైల్", "ఫోన్", "కొనాలి",
        "స్టాక్", "బ్యాటరీ", "కెమెరా",
    )
    return any(term in text for term in business_terms)

def _out_of_scope_reply(language: str) -> str:
    """Keep unrelated requests out of the sales flow; don't restart the greeting."""
    if language == "telugu":
        return "క్షమించండి 😊 నేను Mania Mobiles‌కు సంబంధించిన మొబైల్ మోడళ్లు, ధరలు, కలర్లు, స్టోరేజ్, స్టాక్, EMI, ఎక్స్చేంజ్, కొనుగోలు వివరాల్లో మాత్రమే సహాయం చేయగలను."
    if language == "roman_telugu":
        return "Sorry 😊 Nenu Mania Mobiles ki sambandhinchina mobile models, prices, colors, storage, stock, EMI, exchange mariyu purchase details lo maatrame help cheyyagalanu."
    return "Sorry 😊 I can only help with Mania Mobiles mobile models, prices, colors, storage, stock, EMI, exchange, and purchase details."


def _friction_catalog_context(customer: dict) -> list:
    """Verified catalogue facts about the customer's current product, if any."""
    interest = str(customer.get("model") or customer.get("product_interest") or "").strip()
    if not interest:
        return []
    try:
        products = _search_catalog(interest)
    except Exception as exc:
        print("Friction catalog lookup failed:", type(exc).__name__)
        return []
    context = []
    for product in products[:5]:
        entry = {
            "product_name": product.get("product_name"),
            "storage": _catalog_storage(product) or None,
            "color": _catalog_color(product) or None,
            "condition": _catalog_condition(product) or None,
            "battery_health": _catalog_battery_health(product) or None,
            "stock_status": product.get("stock_status"),
            "product_url": product.get("product_url"),
        }
        # Never hand the model a price that the catalogue marks as unverified.
        if not product.get("price_needs_verification") and product.get("price_inr") is not None:
            entry["price_inr"] = product.get("price_inr")
        context.append({key: value for key, value in entry.items() if value not in (None, "")})
    return context


def _unhandled_clarification(language: str, customer: dict) -> str:
    """Safe template when the LLM is off or rejected: acknowledge, don't deflect."""
    product = str(customer.get("model") or customer.get("product_interest") or "").strip()
    if language == "telugu":
        if product:
            return f"సరే 😊 {product} గురించి ధర, స్టోరేజ్, కలర్ లేదా అందుబాటు — ఏది తెలుసుకోవాలి? చెప్పండి, సహాయం చేస్తాను."
        return "సరే 😊 మీకు ఏ మొబైల్ గురించి తెలుసుకోవాలి? బ్రాండ్/మోడల్ చెప్పండి."
    if product:
        return f"Sare bro 😊 {product} gurinchi price, storage, color leka availability — edi telusukovali? Cheppandi, help chestha."
    return "Sare bro 😊 Ye mobile gurinchi telusukovali? Brand/model cheppandi, help chestha."


def _llm_unhandled_reply(message: str, session: dict, language: str) -> Optional[str]:
    """Grounded, validated LLM answer for an unclassified message, or None.

    Bounded per session (LLM_FRICTION_MAX_PER_SESSION) so a chatty or abusive
    customer cannot run up model cost. Returns None when the model is disabled,
    fails, or its reply is rejected by the validator.
    """
    customer = session.get("customer") or {}
    used = int(session.get("friction_llm_calls") or 0)
    if used >= FRICTION_LLM_MAX_PER_SESSION:
        return None
    session["friction_llm_calls"] = used + 1
    return answer_unhandled_message(
        customer_message=message,
        language=language,
        customer=customer,
        conversation_history=(session.get("conversation_history") or [])[-8:],
        catalog_context=_friction_catalog_context(customer),
        store_facts={"store_address": MANIA_STORE_ADDRESS, "google_maps": MANIA_STORE_MAPS_URL},
    )


def _unhandled_message_reply(message: str, session: dict, language: str) -> str:
    """Reply to a message the rules did not understand (customer friction).

    Order: grounded LLM answer -> deterministic clarification. Explicit
    off-topic requests never get here.
    """
    reply = _llm_unhandled_reply(message, session, language)
    if reply:
        return reply
    return _unhandled_clarification(language, session.get("customer") or {})


def _normalize_reply_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _last_turn_text(history: list, sender: str) -> str:
    for item in reversed(history or []):
        if isinstance(item, dict) and item.get("sender_type") == sender:
            return _normalize_reply_text(item.get("message"))
    return ""


def _is_repeated_reply(reply: str, session: dict, message: str) -> bool:
    """Detect a response already sent anywhere in this conversation.

    A repeated reply to a new customer message means the rules did not
    understand it (for example, a battery-health question answered with the
    same product list). Identical inbound retries are excluded to avoid
    treating a Meta webhook retry as a new conversational turn.
    """
    history = session.get("conversation_history") or []
    current = _normalize_reply_text(reply)
    if not current:
        return False

    prior_assistant_replies = {
        _normalize_reply_text(item.get("message"))
        for item in history
        if isinstance(item, dict) and item.get("sender_type") == "assistant"
        and _normalize_reply_text(item.get("message"))
    }
    if current not in prior_assistant_replies:
        return False

    wanted = _normalize_reply_text(message)
    customer_turns = [
        _normalize_reply_text(item.get("message"))
        for item in history
        if isinstance(item, dict)
        and item.get("sender_type", "customer") != "assistant"
    ]
    # The current inbound turn may already have been recorded by the core flow.
    if customer_turns and customer_turns[-1] == wanted:
        customer_turns.pop()
    # A repeated inbound message is likely a retry or the customer repeating
    # themselves; don't trigger a new LLM call solely because of that retry.
    return not (customer_turns and customer_turns[-1] == wanted)


def _process_message_serialized(customer_id: str, message: str) -> dict:

    customer_id = (customer_id or "").strip()

    message = (message or "").strip()

    if not customer_id or not message:

        raise ValueError("customer_id and message are required")

    if len(message) > 4000:

        raise ValueError("message must be 4000 characters or fewer")

    now = datetime.now(timezone.utc)

    session = sessions.get(customer_id)

    if session is None:

        session = _restore_session_from_storage(customer_id)

    # Only expire incomplete active qualification sessions. Completed records
    # remain attached to the same lead so a new greeting can re-open that record
    # instead of creating a duplicate lead after 30 minutes or a worker restart.
    if session and session.get("last_activity") and not session.get("lead_saved"):

        try:

            last = datetime.fromisoformat(session["last_activity"])

            if last.tzinfo is None:

                last = last.replace(tzinfo=timezone.utc)

        except (TypeError, ValueError):

            last = now

        if now - last >= SESSION_TIMEOUT:

            if session.get("record_id") and not session.get("lead_saved"):

                current_intent = session.get("intent") or {"score": 0, "status": "low", "signals": []}

                qualification = session.get("qualification") or {

                    "score": current_intent.get("score", 0),

                    "status": current_intent.get("status", "low"),

                    "reasons": current_intent.get("signals", []),

                }

                try:

                    update_lead(

                        lead_id=session["record_id"],

                        customer=session.get("customer", {}),

                        qualification=qualification,

                        intent={**current_intent, "session_status": "abandoned"},

                        conversation_history=session.get("conversation_history", []),

                        status="abandoned",

                    )

                except Exception as exc:

                    print("Could not mark expired session abandoned:", type(exc).__name__)

            sessions.pop(customer_id, None)

            session = None

    session = _get_session(customer_id)

    # A completed lead is immutable. Every next inbound message starts a new
    # qualification cycle and a new dashboard row, regardless of whether the
    # customer sends a greeting or immediately asks about another phone.
    is_greeting = _is_greeting_message(message)
    if session and session.get("lead_saved") and session.get("conversation_history"):
        _begin_new_cycle_after_completed_lead(session, customer_id)
    elif is_greeting and session.get("conversation_history"):
        # A fresh standalone greeting starts a new lead cycle. If the previous
        # one was incomplete, preserve it as abandoned instead of carrying stale
        # product/budget data into the new conversation.
        if not session.get("lead_saved") and session.get("record_id"):
            current_intent = session.get("intent") or {"score": 0, "status": "low", "signals": []}
            qualification = session.get("qualification") or {
                "score": current_intent.get("score", 0),
                "status": current_intent.get("status", "low"),
                "reasons": current_intent.get("signals", []),
            }
            try:
                update_lead(
                    lead_id=session["record_id"], customer=session.get("customer", {}),
                    qualification=qualification,
                    intent={**current_intent, "session_status": "abandoned"},
                    conversation_history=session.get("conversation_history", []), status="abandoned",
                )
            except Exception as exc:
                print("Could not archive previous conversation:", type(exc).__name__)
        _begin_new_cycle_after_completed_lead(session, customer_id)

    previous_history_len = len(session.get("conversation_history", []))

    # Store FAQ handling must run even after a completed lead has been reset.
    # These are known business policies; don't route them to the greeting fallback.
    faq_language = _resolve_response_language(message, session)
    faq_reply = _store_faq_reply(message, faq_language)
    if faq_reply:
        result = {
            "customer_id": customer_id,
            "message": faq_reply,
            "customer": session.get("customer", {}),
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }
    elif is_greeting:
        language = _resolve_response_language(message, session)
        next_missing_field = next(
            (field for field in FIELD_ORDER if not _field_is_complete(field, session.get("customer", {}), session)),
            None,
        )
        if previous_history_len > 0 and next_missing_field:
            question = _localized_question(next_missing_field, language, session.get("customer", {}))
            greeting_reply = (
                ("హాయ్ 😊 " if language == "telugu" else "Hi 😊 ") + question
            )
        elif previous_history_len > 0 and session.get("intent_question_asked"):
            greeting_reply = (
                "ధర, స్టాక్ మీకు సరిపోతే కొనుగోలు చేయాలనుకుంటున్నారా?"
                if language == "telugu"
                else "Price mariyu stock meeku set aithe purchase proceed avvalanukuntunnara?"
            )
        elif language == "telugu":
            greeting_reply = (
                "హాయ్! Mania Mobiles కి స్వాగతం 😊 మీకు ఏ మొబైల్ గురించి తెలుసుకోవాలి? ఏ బ్రాండ్/మోడల్ అయినా చెప్పండి."
                if previous_history_len == 0
                else "హాయ్ 😊 Mania Mobiles మొబైల్ వివరాల్లో సహాయం చేస్తాను. మీరు చూస్తున్న బ్రాండ్/మోడల్ పేరు చెప్పండి."
            )
        else:
            greeting_reply = (
                "Hi! Mania Mobiles ki welcome 😊 Meeru ye mobile gurinchi telusukovali? Ye brand/model aina cheppandi."
                if previous_history_len == 0
                else "Hi 😊 Mania Mobiles mobile details lo help chestha. Ye brand/model chusthunnaru?"
            )

        result = {
            "customer_id": customer_id,
            "message": greeting_reply,
            "customer": session.get("customer", {}),
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }
    else:
        # Validate scope before extraction/response generation. Valid short answers
        # (for example "82000", "128GB", or "Bhimavaram") are allowed by field state.
        if not _is_business_related_message(message, session):
            language = _resolve_response_language(message, session)
            if _is_explicit_off_topic(message):
                scope_reply = _out_of_scope_reply(language)
            else:
                scope_reply = _unhandled_message_reply(message, session, language)
            result = {
                "customer_id": customer_id,
                "message": scope_reply,
                "customer": session.get("customer", {}),
                "qualification": session.get("qualification"),
                "intent": session.get("intent"),
                "intent_history": session.get("intent_history", []),
                "lead_id": session.get("lead_id"),
                "lead_saved": bool(session.get("lead_saved")),
            }
        else:
            result = _process_message_impl(customer_id, message)
            # Repeating the previous reply to a new message = the rules did not
            # understand it. Let the grounded LLM answer the real question; keep
            # the rule-based reply if the model is off, fails, or is rejected.
            live_session = _get_session(customer_id)
            if _is_repeated_reply(result.get("message"), live_session, message):
                friction_language = _resolve_response_language(message, live_session)
                friction_reply = _llm_unhandled_reply(message, live_session, friction_language)
                if friction_reply:
                    # If the LLM also repeats an earlier answer, use a distinct
                    # clarification tied to the current message. Never silently
                    # send an identical assistant response twice in one session.
                    if _is_repeated_reply(friction_reply, live_session, message):
                        safe_clarification = _unhandled_clarification(
                            friction_language, live_session.get("customer", {})
                        )
                        if _is_repeated_reply(safe_clarification, live_session, message):
                            safe_clarification = (
                                f"Meeru ippudu '{message[:120]}' gurinchi adigaru bro. "
                                "Daaniki exact ga answer ivvadaniki model/variant ni konchem clarify chesthara?"
                                if friction_language != "telugu" else
                                f"మీరు ఇప్పుడు '{message[:120]}' గురించి అడిగారు బ్రో. ఖచ్చితంగా సహాయం చేయడానికి మోడల్ లేదా వేరియంట్‌ను కొంచెం స్పష్టంగా చెబుతారా?"
                            )
                        friction_reply = safe_clarification
                    result["message"] = friction_reply

    session = _get_session(customer_id)

    # Extraction can fail before the core function records the customer turn;

    # keep that message in the permanent record too.

    history = session.setdefault("conversation_history", [])

    if len(history) == previous_history_len:

        history.append({

            "turn": len(history) + 1,

            "message": message,

            "sender_type": "customer",

            "language": session.get("language", "roman_telugu"),

            "created_at": now.isoformat(),

            "customer": dict(session.get("customer", {})),

            "intent": dict(session.get("intent") or {}),

        })

    activity_at = datetime.now(timezone.utc)

    session["last_activity"] = activity_at.isoformat()

    customer = session.get("customer", {})

    if customer_id.startswith("wa_"):

        customer["phone_number"] = customer_id[3:]

    elif customer_id.replace("+", "").isdigit():

        customer["phone_number"] = customer_id

    reply = str(result.get("message") or "").strip()

    if reply:

        session["conversation_history"].append({

            "turn": len(session["conversation_history"]) + 1,

            "message": reply,

            "sender_type": "assistant",

            "language": session.get("language", "roman_telugu"),

            "created_at": activity_at.isoformat(),

        })

    intent = session.get("intent") or result.get("intent") or {"score": 0, "status": "low", "signals": []}

    qualification = session.get("qualification") or {

        "score": intent.get("score", 0),

        "status": intent.get("status", "low"),

        "reasons": intent.get("signals", []),

    }

    status = "completed" if session.get("lead_saved") else "active"

    try:

        if session.get("record_id"):

            saved = update_lead(

                lead_id=session["record_id"],

                customer=customer,

                qualification=qualification,

                intent={**intent, "session_status": status},

                conversation_history=session["conversation_history"],

                status=status,

            )

        else:

            saved = save_lead(

                customer=customer,

                qualification=qualification,

                intent={**intent, "session_status": status},

                conversation_history=session["conversation_history"],

                status=status,

            )

        if saved:

            session["record_id"] = saved.get("lead_id")

            # Keep lead_id available to the existing API response, but only mark

            # lead_saved true when the qualification flow is actually complete.

            if session.get("lead_saved"):

                session["lead_id"] = saved.get("lead_id")

            result["lead_id"] = saved.get("lead_id")

    except Exception as exc:

        # Do not hide a persistence failure: log it and return the generated reply.

        print("Lead persistence failed:", type(exc).__name__)

    result["customer"] = customer

    result["intent"] = intent

    result["qualification"] = qualification

    result["lead_saved"] = bool(session.get("lead_saved"))

    result["lead_id"] = session.get("record_id")

    return result

def _attach_whatsapp_message_id(customer_id: str, message: str, message_id: str) -> bool:

    """Persist Meta's message ID only after the agent reply was accepted by Meta.

    This lets a Meta retry recover after an outbound send failure rather than

    incorrectly treating an unanswered inbound message as fully processed.

    """

    customer_id = (customer_id or "").strip()

    message = (message or "").strip()

    message_id = (message_id or "").strip()

    if not customer_id or not message or not message_id:

        return False

    session = sessions.get(customer_id)

    if session is None:

        session = _restore_session_from_storage(customer_id)

    if not session:

        return False

    history = session.get("conversation_history") or []

    matched = None

    for item in reversed(history):

        if (

            isinstance(item, dict)

            and item.get("sender_type") == "customer"

            and item.get("message") == message

        ):

            matched = item

            break

    if matched is None:

        return False

    if matched.get("whatsapp_message_id") == message_id:

        return True

    matched["whatsapp_message_id"] = message_id

    record_id = session.get("record_id")

    if not record_id:

        return False

    customer = session.get("customer") or {}

    intent = session.get("intent") or {"score": 0, "status": "low", "signals": []}

    qualification = session.get("qualification") or {

        "score": intent.get("score", 0),

        "status": intent.get("status", "low"),

        "reasons": intent.get("signals", []),

    }

    try:

        update_lead(

            lead_id=record_id,

            customer=customer,

            qualification=qualification,

            intent={**intent, "session_status": "completed" if session.get("lead_saved") else "active"},

            conversation_history=history,

            status="completed" if session.get("lead_saved") else "active",

        )

        return True

    except Exception as exc:

        print("Could not persist WhatsApp message ID:", type(exc).__name__)

        return False

def process_message(customer_id: str, message: str) -> dict:

    """Process one turn while preventing same-customer races in this worker."""

    customer_id = (customer_id or "").strip()

    with _customer_lock(customer_id):

        return _process_message_serialized(customer_id, message)

def attach_whatsapp_message_id(customer_id: str, message: str, message_id: str) -> bool:

    """Attach a successfully answered Meta message ID under the same session lock."""

    customer_id = (customer_id or "").strip()

    with _customer_lock(customer_id):

        return _attach_whatsapp_message_id(customer_id, message, message_id)