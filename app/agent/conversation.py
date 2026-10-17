import re
from datetime import datetime, timedelta, timezone
from threading import Lock, RLock
from typing import Any, Dict, Optional
from weakref import WeakValueDictionary
from app.agent.llm import (
    extract_customer_details,
    detect_language,
    generate_response,
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
    "product_interest": "Which mobile are you interested in? Please tell me the brand and model.",
    "budget": "What's your budget for the mobile?",
    "purchase_timeline": "When are you planning to buy it?",
    "condition": "Are you looking for a brand-new or a second-hand/refurbished phone?",
    "payment_method": "How would you prefer to pay—cash, card, UPI, or EMI?",
    "location": "Which city or town are you purchasing from?",
}
# Natural question variants prevent the qualification flow from sounding like
# the same rigid form on every turn. The language is locked by the first message.
FIELD_QUESTION_VARIANTS = {
    "roman_telugu": {
        "product_interest": [
            "Meeku ye mobile kavali bro? Brand/model peru cheppandi.",
            "Ye phone kosam chusthunnaru? Model telisthe correct details check chestha.",
        ],
        "budget": [
            "Ee phone kosam roughly entha budget anukuntunnaru?",
            "Mee budget range entha bro? Daani batti suitable options cheptha.",
            "Price range chepthe unnecessary expensive models suggest cheyyanu.",
        ],
        "purchase_timeline": [
            "Eppudu konalani plan chesthunnaru—ivala, ee week, leka tarvatha?",
            "Purchase ki mee timeline enti bro? Urgent aa, leka compare chesi decide avuthara?",
        ],
        "condition": [
            "Brand-new sealed piece kavala, leka used/refurbished kuda consider chesthara?",
            "New phone matrame chusthunnara, second-hand options kuda okay na?",
        ],
        "payment_method": [
            "Payment ki cash, UPI, card, leka EMI lo edhi convenient?",
            "Ela pay cheyyalani anukuntunnaru bro—cash/UPI/card/EMI?",
        ],
        "location": [
            "Meeru ye city/town nunchi purchase chesthunnaru? Store options confirm cheyyadaniki help avuthundhi.",
            "Mee location cheppandi bro; availability/store details verify cheyyadaniki useful.",
        ],
        "storage": [
            "Storage variant 128GB kavala, 256GB kavala? Vere variant anukunte cheppandi.",
            "Ee model lo ye storage kavali bro—128GB, 256GB, leka vere variant?",
        ],
    },
    "telugu": {
        "product_interest": [
            "మీకు ఏ మొబైల్ కావాలి? బ్రాండ్, మోడల్ పేరు చెప్పండి.",
            "ఏ ఫోన్ కోసం చూస్తున్నారు? మోడల్ తెలిస్తే సరైన వివరాలు చెక్ చేస్తాను.",
        ],
        "budget": [
            "ఈ ఫోన్ కోసం సుమారుగా ఎంత బడ్జెట్ అనుకుంటున్నారు?",
            "మీ బడ్జెట్ ఎంత? దానికి సరిపోయే ఎంపికలు చెప్తాను.",
            "బడ్జెట్ చెప్తే అవసరం లేని ఖరీదైన మోడల్స్ సూచించను.",
        ],
        "purchase_timeline": [
            "ఎప్పుడు కొనాలనుకుంటున్నారు—ఈరోజా, ఈ వారంలోనా, లేక తర్వాతనా?",
            "కొనుగోలు ఎప్పటికి ప్లాన్ చేస్తున్నారు? వెంటనే కావాలా, లేక పోల్చి నిర్ణయం తీసుకుంటారా?",
        ],
        "condition": [
            "కొత్త సీల్డ్ పీస్ కావాలా, లేక వాడిన/రిఫర్బిష్డ్ ఫోన్ కూడా పరిశీలిస్తారా?",
            "కొత్త ఫోన్ మాత్రమే కావాలా, సెకండ్‌హ్యాండ్ ఎంపికలు కూడా సరేనా?",
        ],
        "payment_method": [
            "చెల్లింపుకు క్యాష్, UPI, కార్డ్, లేక EMI—ఏది సౌకర్యంగా ఉంటుంది?",
            "ఎలా చెల్లించాలనుకుంటున్నారు—క్యాష్/UPI/కార్డ్/EMI?",
        ],
        "location": [
            "మీరు ఏ నగరం లేదా పట్టణం నుంచి కొనాలనుకుంటున్నారు? స్టోర్ వివరాలు నిర్ధారించడానికి ఉపయోగపడుతుంది.",
            "మీ లొకేషన్ చెప్పండి; స్టాక్ లేదా స్టోర్ ఎంపికలు నిర్ధారించడంలో సహాయపడుతుంది.",
        ],
        "storage": [
            "128GB కావాలా, 256GB కావాలా? వేరే స్టోరేజ్ వేరియంట్ కావాలంటే చెప్పండి.",
            "ఈ మోడల్‌లో ఏ స్టోరేజ్ కావాలి—128GB, 256GB, లేక వేరేదా?",
        ],
    },
}
# This is asked after the six required qualification questions.
INTENT_QUESTION = (
    "If the price and availability work for you, would you like to proceed "
    "with the purchase?"
)
FIELD_ORDER = [
    "product_interest",
    "budget",
    "purchase_timeline",
    "condition",
    "payment_method",
    "location",
]
# Additional details that must be preserved in the lead record.
EXTRA_FIELDS = {
    "storage",
    "preferences",
    "battery_health_min",
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
}

def _get_session(customer_id: str) -> dict:
    if customer_id not in sessions:
        sessions[customer_id] = {
            "customer": {},
            "asked_field": "product_interest",
            "lead_saved": False,
            "lead_id": None,
            "qualification": None,
            # Language is selected from the first inbound message and never
            # changes during this conversation.
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
        }
    return sessions[customer_id]

def _field_is_complete(field: str, customer: dict) -> bool:
    value = customer.get(field)
    if field == "budget":
        if isinstance(value, dict):
            try:
                minimum = float(value.get("min", 0))
                maximum = float(value.get("max", 0))
                return (
                    minimum >= 0
                    and maximum > 0
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

def _get_product_message(
    product_interest: str,
) -> str:
    """
    Return catalog information without promising
    live availability.
    """
    if not product_interest:
        return ""
    try:
        matches = _search_catalog(
            product_interest
        )
        if matches:
            return (
                "\n\nHere are the closest matches "
                "in our catalog:\n\n"
                + "\n\n".join(
                    format_product(product)
                    for product in matches
                )
                + "\n\nPlease confirm the latest "
                "price, exact variant, and availability "
                "with the store."
            )
        return (
            "\n\nI couldn't find an exact match "
            "in the demo catalog. The store can "
            "confirm the model, price, and availability."
        )
    except Exception as exc:
        print("Product catalog lookup failed:", type(exc).__name__)
        return (
            "\n\nI couldn't check the product catalog "
            "right now. The store can confirm the "
            "model, price, and availability."
        )

def _handle_saved_lead_follow_up(
    customer_id: str,
    message: str,
    customer: dict,
    session: dict,
    extracted: dict,
    previous_product_before_turn: Optional[str] = None,
) -> dict:
    """
    Handle follow-up messages after lead creation.
    Do not create a duplicate lead.
    """
    previous_product = (
        previous_product_before_turn
        if previous_product_before_turn is not None
        else customer.get("product_interest")
    )
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
    if new_product_received:
        product_message = _get_product_message(
            product_interest
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
    language = session.get("language", "roman_telugu")
    joke_kind = _detect_obvious_joke(message)
    if joke_kind:
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                _obvious_joke_reply(joke_kind, language),
                message,
                customer,
                session,
            ),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": True,
        }
    question_type = _detect_customer_question(message)
    if question_type:
        answer = _answer_customer_question(
            question_type=question_type,
            message=message,
            customer=customer,
            language=language,
            product_message=product_message,
        )
        if question_type == "emi":
            if language == "telugu":
                answer += " మీ కార్డ్ నంబర్, CVV లేదా OTP ఈ చాట్‌లో షేర్ చేయకండి."
            else:
                answer += " Mee card number, CVV or OTP ni ee chat lo share cheyyakandi."
        return {
            "customer_id": customer_id,
            "message": _localize_reply(answer, message, customer, session),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": True,
        }

    social_kind = _simple_social_kind(message)
    if social_kind:
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                _courtesy_reply(social_kind, language),
                message,
                customer,
                session,
            ),
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
    draft_reply = (
        reply
        + product_message
    )
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
    """
    Select the reply language once, from the first customer message.
    Telugu Unicode in the first message selects Telugu script. Every other
    first message (including "Hi", English, Roman Telugu, numbers, and short
    answers) selects Roman Telugu. Subsequent messages never change it.
    """
    supported = {"telugu", "roman_telugu"}
    current = session.get("language")
    history = session.get("conversation_history") or []
    # Existing/restored sessions may predate language_locked. Recover the
    # language from the earliest saved turn before making a choice.
    if not session.get("language_locked") and history:
        for item in history:
            if not isinstance(item, dict):
                continue
            saved_language = item.get("language")
            # Older records may not contain sender_type, so use the first
            # saved language-bearing turn as the original language choice.
            if saved_language in supported:
                session["language"] = saved_language
                session["language_locked"] = True
                return saved_language
        if current in supported:
            session["language_locked"] = True
            return current
    if session.get("language_locked") and current in supported:
        return current
    text = (message or "").strip()
    language = (
        "telugu"
        if re.search(r"[\u0C00-\u0C7F]", text)
        else "roman_telugu"
    )
    session["language"] = language
    session["language_locked"] = True
    return language

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
    # Supply grounded catalogue details to the response model, including on
    # follow-up questions about a product already discussed.
    catalog_context = []
    queries = []
    for value in (
        customer.get("product_interest"),
        customer.get("model"),
        customer.get("brand"),
    ):
        if isinstance(value, str) and value.strip() and value.strip() not in queries:
            queries.append(value.strip())
    comparisons = customer.get("comparison_brands") or []
    if isinstance(comparisons, str):
        comparisons = [comparisons]
    if isinstance(comparisons, list):
        queries.extend(str(value).strip() for value in comparisons if str(value).strip())
    seen_urls = set()
    for query in queries[:4]:
        try:
            for product in search_products(query=query, limit=2):
                url = product.get("product_url") or product.get("sku") or product.get("product_name")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                catalog_context.append({
                    "product_name": product.get("product_name"),
                    "brand": product.get("brand"),
                    "model": product.get("model"),
                    # Never feed stale/malformed display-price strings to the model.
                    # Only expose a numeric price when the catalogue explicitly marks it verified.
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
    return generate_response(
        draft_response=draft,
        customer_message=customer_message,
        language=language,
        customer=customer,
        conversation_history=session.get("conversation_history", [])[-8:],
        catalog_context=catalog_context[:6],
    )

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
        "sender_type": "customer",
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
        "i won't buy", "i will not buy", "konanu", "konalani ledu",
        "konadaniki plan ledu", "ippudu konanu", "interest ledu",
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

def _detect_customer_question(message: str) -> str:
    """Classify a customer's actual question/request without treating short answers as questions."""
    text = (message or "").strip().lower()
    if not text:
        return ""
    recommendation_request = any(token in text for token in (
        "suggest", "recommend", "best phone", "which phone", "what phone",
        "phone kavali", "phone suggest", "suggest cheyyi", "suggest cheyandi",
        "ఏ ఫోన్", "ఏ మొబైల్", "సూచించండి", "సజెస్ట్",
    ))
    if recommendation_request:
        return "recommendation"
    has_question_shape = (
        "?" in text
        or any(token in text for token in (
            "how much", "how many", "is there", "do you have", "can i",
            "could you", "does it", "what is", "what's", "which one",
            "entha", "entha?", "undha", "untunda", "untundha",
            "available aa", "dorukuthunda", "cheppandi", "better aa",
            "which is better", "which is best", "android better", "iphone better",
            "compare cheyy", "ఏది మంచిది", "ఏ ఫోన్", "ఏ మొబైల్",
            "ఎంత", "ఎలా", "ఏది", "ఉందా", "ఉంటుందా", "దొరుకుతుందా",
            "చెప్పండి", "ధర ఎంత", "స్టాక్ ఉందా",
        ))
    )
    request_shape = any(token in text for token in (
        "discount kavali", "offer kavali", "tagginchandi", "tagguthunda",
        "less cheyy", "price cheppu", "price cheppandi", "ధర చెప్పండి",
        "డిస్కౌంట్", "ఆఫర్ ఉందా",
    ))
    # "EMI", "cash", "yes", "128GB" and similar short replies should proceed
    # through extraction instead of being misread as a new question.
    if not has_question_shape and not request_shape:
        return ""
    patterns = [
        ("reservation", ("reserve", "reservation", "book it", "book this", "hold it", "రిజర్వ్", "బుక్ చేయ")),
        ("exchange", ("exchange", "trade-in", "trade in", "old phone", "ఎక్స్చేంజ్", "పాత ఫోన్")),
        ("emi", ("emi", "installment", "monthly payment", "finance", "ఈఎంఐ", "వాయిదా")),
        ("payment", ("do you accept", "accept upi", "accept card", "payment method", "చెల్లింపు", "upi తీసుకుంటారా")),
        ("discount", ("discount", "offer", "tagguthunda", "tagginchandi", "less cheyy", "తగ్గింపు", "డిస్కౌంట్", "ఆఫర్")),
        ("delivery", ("delivery", "deliver", "shipping", "home delivery", "డెలివరీ", "ఇంటికి పంప")),
        ("warranty", ("warranty", "original", "sealed", "bill", "imei", "వారంటీ", "ఒరిజినల్", "సీల్డ్", "బిల్")),
        ("battery", ("battery health", "battery backup", "battery life", "బ్యాటరీ హెల్త్", "బ్యాటరీ బ్యాకప్", "బ్యాటరీ")),
        ("availability", ("stock", "available", "availability", "undha", "untunda", "untundha", "dorukuthunda", "స్టాక్", "అందుబాటులో", "ఉందా", "దొరుకుతుందా")),
        ("comparison", ("compare", "comparison", "difference", "which is better", "which is best", "android better", "iphone better", "vs", "పోల్చి", "ఏది మంచిది", "తేడా")),
        ("price", ("how much", "price", "cost", "rate", "entha", "ధర", "ఎంత")),
    ]
    for kind, terms in patterns:
        if any(term in text for term in terms):
            return kind
    return "general"

def _question_prompt_for_field(
    field: Optional[str],
    customer: dict,
    session: dict,
    language: str,
) -> str:
    """Choose one natural, contextual question and avoid repeating the exact same wording."""
    if not field:
        return ""
    language = language if language in FIELD_QUESTION_VARIANTS else "roman_telugu"
    variants = FIELD_QUESTION_VARIANTS[language].get(field)
    if not variants:
        return QUESTIONS.get(field, "")
    counts = session.setdefault("field_prompt_counts", {})
    count = int(counts.get(field, 0) or 0)
    counts[field] = count + 1
    product = str(customer.get("product_interest") or customer.get("model") or "").strip()
    if field == "budget" and product:
        if language == "telugu":
            variants = [
                f"{product} కోసం సుమారుగా ఎంత బడ్జెట్ అనుకుంటున్నారు?",
                f"{product} ధర మీ బడ్జెట్‌కు సరిపోతుందో చూడటానికి మీ రేంజ్ ఎంత?",
                "మీ బడ్జెట్ రేంజ్ చెప్తే దానికి దగ్గరలోని ఆప్షన్లు కూడా చూస్తాను.",
            ]
        else:
            variants = [
                f"{product} kosam roughly entha budget anukuntunnaru?",
                f"{product} mee budget ki suit avuthundho chudataniki budget range entha?",
                "Mee budget range chepthe daniki daggaraga unna options kuda chustha.",
            ]
    return variants[count % len(variants)]

def _question_follow_up(
    question_type: str,
    customer: dict,
    session: dict,
    language: str,
) -> tuple:
    """Return (follow-up text, field name) for the single most useful next question."""
    product = str(customer.get("product_interest") or customer.get("model") or "").strip()
    storage = str(customer.get("storage") or "").strip()
    has_storage_in_product = bool(re.search(r"\b\d+\s?gb\b", product, flags=re.IGNORECASE))
    if question_type == "recommendation" and not product:
        return "", None
    if question_type == "exchange":
        return "", None
    if question_type == "reservation":
        return "", None
    if question_type == "price" and not product:
        return "", None
    if question_type == "comparison" and not product:
        return "", None
    if question_type in {"price", "availability"} and product and not storage and not has_storage_in_product:
        field = "storage"
    else:
        field = next(
            (item for item in FIELD_ORDER if not _field_is_complete(item, customer)),
            None,
        )
    if not field:
        if all(_field_is_complete(item, customer) for item in FIELD_ORDER):
            session["asked_field"] = "intent"
        return "", None
    prompt = _question_prompt_for_field(field, customer, session, language)
    session["asked_field"] = field
    return prompt, field

def _answer_customer_question(
    question_type: str,
    message: str,
    customer: dict,
    language: str,
    product_message: str = "",
) -> str:
    """Give a useful, cautious answer before asking for another qualification detail."""
    product = str(customer.get("product_interest") or customer.get("model") or "").strip()
    details = product_message.strip() if product_message else ""
    if question_type == "price":
        if not details and product:
            details = _get_product_message(product).strip()
        if details:
            # The catalog formatter already includes the product details and
            # a price-verification caveat; don't repeat that explanation.
            return details
        if language == "telugu":
            return "ఏ మోడల్ ధర కావాలో చెప్తే సరైన వివరాలు చెక్ చేస్తాను. ప్రస్తుత ఫైనల్ ధర, ఆఫర్లను స్టోర్ నిర్ధారించాలి."
        return "Ye model price kavalo chepthe correct details check chestha. Current final price and offers store tho confirm cheyyali."
    if question_type == "availability":
        if language == "telugu":
            answer = "క్యాటలాగ్‌లో మోడల్ వివరాలు చెక్ చేయగలను, కానీ లైవ్ స్టాక్‌ను స్టోర్ నిర్ధారించాలి."
        else:
            answer = "Catalog details check cheyyagalanu, kani live stock ni store confirm cheyyali."
        return (details + "\n\n" + answer) if details else answer
    if question_type == "discount":
        if language == "telugu":
            return "ప్రస్తుత డిస్కౌంట్లు, బ్యాంక్ ఆఫర్లు మారవచ్చు. వర్తించే ఆఫర్లను నిర్ధారించి చెప్తాను; నిర్ధారణ లేకుండా ధర హామీ ఇవ్వను."
        return "Current discounts and bank offers marochu. Applicable offers verify chesi cheptha; confirm kakunda price guarantee cheyyalenu."
    if question_type == "reservation":
        if language == "telugu":
            return "మీ బుకింగ్/రిజర్వేషన్ అభ్యర్థనను గమనించగలను, కానీ ఇక్కడి నుంచి బుకింగ్ నిర్ధారించలేను. స్టాక్, ఫోన్‌ను హోల్డ్ చేసే అవకాశం స్టోర్‌తో నిర్ధారించాలి."
        return "Mee booking/reservation request note cheyyagalanu, kani ikkada booking confirm cheyyalenu. Stock and phone hold cheyyagalara ani store tho confirm cheyyali."
    if question_type == "exchange":
        if language == "telugu":
            return "ఎక్స్చేంజ్ విలువ పాత ఫోన్ మోడల్, స్టోరేజ్, కండిషన్, ఇన్‌స్పెక్షన్‌పై ఆధారపడుతుంది. పాత ఫోన్ మోడల్, స్క్రీన్/బ్యాటరీ కండిషన్ చెప్తే తదుపరి వివరాలు చెప్తాను."
        return "Exchange value old phone model, storage, condition and inspection batti untundhi. Mee old phone model and screen/battery condition chepthe next steps cheptha."
    if question_type == "payment":
        if language == "telugu":
            return "క్యాష్, UPI, కార్డ్ లేదా EMI ఆప్షన్లు స్టోర్‌తో నిర్ధారించాలి. కార్డ్ నంబర్, CVV లేదా OTP ఈ చాట్‌లో షేర్ చేయకండి."
        return "Cash, UPI, card or EMI options store tho confirm cheyyali. Card number, CVV or OTP ni ee chat lo share cheyyakandi."
    if question_type == "emi":
        if language == "telugu":
            return "EMI అర్హత, డౌన్ పేమెంట్, వడ్డీ, ప్రాసెసింగ్ ఫీజు కాలవ్యవధి/బ్యాంక్‌పై ఆధారపడతాయి. స్టోర్‌తో నిర్ధారించిన తర్వాతే ఖచ్చితమైన మొత్తం చెప్పగలం."
        return "EMI eligibility, down payment, interest and processing fee tenure/bank batti maruthayi. Store tho verify chesaka exact amount cheppagalamu."
    if question_type == "delivery":
        if language == "telugu":
            return "డెలివరీ అందుబాటు, ఛార్జీలు, సమయం మీ లొకేషన్‌పై ఆధారపడతాయి. మీ ప్రాంతానికి స్టోర్‌తో నిర్ధారించాలి."
        return "Delivery availability, charges and timing mee location batti untayi. Mee area ki store tho confirm cheyyali."
    if question_type == "warranty":
        if language == "telugu":
            return "కొనుగోలు ముందు బిల్, IMEI వివరాలు, సీల్డ్ బాక్స్, వర్తించే తయారీదారు వారంటీ నిర్ధారించుకోండి. నిర్దిష్ట యూనిట్ వివరాలను స్టోర్ నిర్ధారించాలి."
        return "Purchase mundhu bill, IMEI details, sealed box and applicable manufacturer warranty verify cheskondi. Exact unit details store confirm cheyyali."
    if question_type == "battery":
        if language == "telugu":
            return "బ్యాటరీ హెల్త్ లేదా బ్యాకప్ ఖచ్చితమైన మోడల్, యూనిట్ కండిషన్‌పై ఆధారపడుతుంది. నిర్దిష్ట యూనిట్‌ను స్టోర్ పరీక్షించి నిర్ధారించాలి."
        return "Battery health/backup exact model and unit condition batti untundhi. Specific unit ni store test chesi confirm cheyyali."
    if question_type == "comparison":
        if language == "telugu":
            return "రెండు మోడల్స్ పేర్లు చెప్తే ధర, కెమెరా, బ్యాటరీ, పనితీరు, సాఫ్ట్‌వేర్ సపోర్ట్ ఆధారంగా పోల్చి చెప్తాను."
        return "Rendu model names chepthe price, camera, battery, performance and software support batti compare chesi cheptha."
    if question_type == "recommendation":
        if language == "telugu":
            return "తప్పకుండా. మీ బడ్జెట్ ఎంత? కెమెరా, బ్యాటరీ, గేమింగ్ లేదా సాధారణ వాడకంలో ఏది ముఖ్యం? దాని ఆధారంగా సరైన ఎంపికలు సూచిస్తాను."
        return "Sure bro. Mee budget entha? Camera, battery, gaming, leka normal use lo deniki priority? Daani batti suitable options suggest chestha."
    if language == "telugu":
        return "మీ ప్రశ్న అర్థమైంది. మోడల్ వివరాలు చెప్తే సరైన సమాచారం చెక్ చేసి సహాయం చేస్తాను."
    return "Mee question ardham ayyindhi. Model details chepthe correct information check chesi help chestha."

def _detect_obvious_joke(message: str) -> str:
    text = (message or "").lower()
    if "kidney" in text or "కిడ్నీ" in text:
        return "price_joke"
    if ("pro max" in text or "ప్రో మాక్స్" in text) and re.search(r"\b(?:10k|10000|10,000)\b", text):
        return "unrealistic_price_joke"
    if "just kidding" in text or "just joking" in text or "జస్ట్ జోక్" in text:
        return "just_joking"
    return ""

def _obvious_joke_reply(kind: str, language: str) -> str:
    if language == "telugu":
        if kind == "price_joke":
            return "😂 కిడ్నీ మాత్రం సేఫ్‌గా ఉంచండి! మీ బడ్జెట్‌కు సరిపోయే నిజమైన ఫోన్ ఎంపికలు చూద్దాం."
        if kind == "unrealistic_price_joke":
            return "😂 ఆ బడ్జెట్‌లో కొత్త Pro Max దొరకడం వాస్తవికంగా సాధ్యం కాకపోవచ్చు. ఆ బడ్జెట్‌లో నిజమైన ఫోన్ ఎంపికలు కావాలంటే చూపిస్తాను."
        return "హాహా, అర్థమైంది 😄 పర్వాలేదు!"
    if kind == "price_joke":
        return "😂 Kidney safe ga unchandi bro! Mee budget ki suit ayye genuine phone options chuddam."
    if kind == "unrealistic_price_joke":
        return "😂 Aa budget lo brand-new Pro Max dorakadam realistic kaadu bro. Aa range lo genuine phone options kavali ante chustha."
    return "Haha, ardham ayyindhi bro 😄 Parledhu!"

def _simple_social_kind(message: str) -> str:
    """Recognize short courtesy messages so they don't trigger a qualification question."""
    raw = (message or "").strip().lower()
    if raw in {"😂", "🤣", "lol", "haha", "హాహా"}:
        return "laughter"
    normalized = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", "", raw)
    if normalized in {
        "thanks", "thankyou", "thankyoubro", "thanksbro", "ధన్యవాదాలు",
        "థాంక్స్", "చాలా థాంక్స్",
    }:
        return "thanks"
    if normalized in {
        "justjoking", "justkidding", "joke", "timepass", "timepassbro",
        "జస్ట్జోక్",
    }:
        return "laughter"
    return ""

def _courtesy_reply(kind: str, language: str) -> str:
    if language == "telugu":
        if kind == "thanks":
            return "స్వాగతం 😊 మీకు అవసరమైనప్పుడు కొనసాగిద్దాం. ఏదైనా మోడల్ వివరాలు కావాలంటే చెప్పండి."
        return "హాహా 😄 సరే, మీకు సహాయం కావాలంటే చెప్పండి."
    if kind == "thanks":
        return "Welcome bro 😊 Meeku ready ayinappudu continue cheddam. Ye model details kavalo cheppandi."
    return "Haha 😄 Sare bro, help kavali anipisthe cheppandi."


def _parse_money_amount(raw: str, unit: str = "") -> Optional[float]:
    """Parse an INR amount such as 45k, 45,000, or 1.2 lakh."""
    try:
        number = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    unit = (unit or "").lower().strip()
    if unit in {"k", "thousand"}:
        number *= 1000
    elif unit in {"l", "lac", "lakh"}:
        number *= 100000
    return number if number >= 0 else None


def _answer_for_asked_field(
    message: str,
    customer: dict,
    extracted: dict,
    session: dict,
) -> dict:
    """
    Deterministically capture short answers to the last question asked.

    LLM extraction remains the main extractor. This safety net handles common
    short answers (45k, 128GB, cash, Bhimavaram, brand new) when the model
    returns an empty or incomplete extraction, so the agent advances instead
    of repeating a question whose answer the customer already supplied.
    """
    result = dict(extracted or {})
    text = (message or "").strip()
    lowered = text.lower()
    field = session.get("asked_field")
    if not text:
        return result

    # Storage is often supplied together with a model name (for example,
    # "iPhone 15 128GB"). Capture it regardless of which question was asked.
    storage_match = re.search(r"\b(32|64|128|256|512|1024)\s*(gb|tb)\b", lowered)
    if storage_match and not result.get("storage"):
        result["storage"] = f"{storage_match.group(1)}{storage_match.group(2).upper()}"

    if field not in set(FIELD_ORDER) | {"storage"}:
        return result

    # Only fill a field from this deterministic path if the message plausibly
    # answers that field. Do not overwrite a valid extraction with a guess.
    if field == "budget" or re.search(
        r"(?:₹\s*\d|\b\d+(?:\.\d+)?\s*(?:k|thousand|lakh|lac|l)\b)",
        lowered,
        flags=re.IGNORECASE,
    ):
        amount_pattern = (
            r"(?:₹\s*)?(\d[\d,]*(?:\.\d+)?)\s*"
            r"(k|thousand|lakh|lac|l)?"
        )
        range_match = re.search(
            amount_pattern
            + r"\s*(?:-|–|—|\bto\b|\buntil\b)\s*"
            + amount_pattern,
            lowered,
            flags=re.IGNORECASE,
        )
        parsed_budget = None
        if range_match:
            first = _parse_money_amount(range_match.group(1), range_match.group(2))
            second = _parse_money_amount(range_match.group(3), range_match.group(4))
            # In "40-45k", the suffix applies to both ends of the range.
            if first is not None and second is not None:
                if not range_match.group(2):
                    first = _parse_money_amount(
                        range_match.group(1), range_match.group(4)
                    )
                if not range_match.group(4):
                    second = _parse_money_amount(
                        range_match.group(3), range_match.group(2)
                    )
                if first is not None and second is not None:
                    parsed_budget = {
                        "min": min(first, second),
                        "max": max(first, second),
                    }
        if parsed_budget is None:
            single = re.search(
                r"(?:₹\s*)(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac|l)?"
                r"|(?<![\w])(\d+(?:\.\d+)?)\s*(k|thousand|lakh|lac|l)\b",
                lowered,
                flags=re.IGNORECASE,
            )
            if single:
                raw_amount = single.group(1) or single.group(3)
                unit = single.group(2) or single.group(4) or ""
                amount = _parse_money_amount(raw_amount, unit)
                if amount and amount > 0:
                    if re.search(r"\b(?:under|below|less than|upto|up to|max(?:imum)?)\b", lowered):
                        parsed_budget = {"min": 0, "max": amount}
                    elif re.search(r"\b(?:above|over|more than|at least)\b", lowered):
                        parsed_budget = {"min": amount, "max": amount}
                    else:
                        parsed_budget = amount
            elif re.fullmatch(r"\s*(?:₹\s*)?\d[\d,]*(?:\.\d+)?\s*", lowered):
                amount = _parse_money_amount(
                    re.sub(r"^[₹\s]+", "", lowered)
                )
                if amount and amount > 0:
                    parsed_budget = amount
        if parsed_budget is not None:
            # The customer is answering the budget question now; a confidently
            # parsed current answer should replace any earlier budget value.
            result["budget"] = parsed_budget

    elif field == "storage":
        match = re.search(r"\b(32|64|128|256|512|1024)\s*(gb|tb)\b", lowered)
        if match:
            storage = f"{match.group(1)}{match.group(2).upper()}"
            if not result.get("storage"):
                result["storage"] = storage

    elif field == "purchase_timeline":
        timeline_terms = (
            "today", "tonight", "tomorrow", "this week", "within a week",
            "in a week", "weekend", "next week", "this month", "next month",
            "asap", "immediately", "later", "after", "month", "week",
            "ivala", "eeroju", "repu", "ee week", "ee vaaram", "next month",
            "tarvatha", "taruvatha", "ippude", "వారంలో", "ఈరోజు", "రేపు",
            "తర్వాత", "వెంటనే", "ఈ నెల", "వచ్చే నెల",
        )
        if any(term in lowered for term in timeline_terms):
            result["purchase_timeline"] = text

    elif field == "condition":
        if lowered.strip(" .,!") == "new" or any(term in lowered for term in (
            "brand new", "brand-new", "sealed", "new phone", "new piece",
            "new only", "kotha", "kottha", "కొత్త", "సీల్డ్",
        )):
            result["condition"] = "brand_new"
        elif any(term in lowered for term in (
            "second hand", "second-hand", "used", "refurbished", "renewed",
            "used phone", "పాత ఫోన్", "సెకండ్ హ్యాండ్", "రిఫర్బిష్డ్",
        )):
            result["condition"] = "used_or_refurbished"

    elif field == "payment_method":
        payment_map = (
            (("upi", "phonepe", "google pay", "gpay", "paytm", "యూపీఐ"), "UPI"),
            (("cash", "క్యాష్", "నగదు"), "cash"),
            (("credit card", "debit card", "card", "కార్డ్"), "card"),
            (("emi", "installment", "instalment", "ఈఎంఐ"), "EMI"),
        )
        for terms, value in payment_map:
            if any(term in lowered for term in terms):
                result["payment_method"] = value
                break

    elif field == "location":
        # Do not store obvious non-location replies as a city. Short place-like
        # answers and phrases such as "I'm from Bhimavaram" are acceptable.
        cleaned = re.sub(
            r"^(?:i am from|i'm from|from|my location is|location:|nenu|memu)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" .,!؟?")
        looks_like_location = (
            1 <= len(cleaned) <= 80
            and not re.search(r"\b(?:cash|upi|emi|card|yes|no|128gb|256gb)\b", cleaned.lower())
            and not re.search(r"[\?]", cleaned)
        )
        if looks_like_location:
            result["location"] = cleaned

    elif field == "product_interest":
        if not result.get("product_interest"):
            product_markers = (
                "iphone", "apple", "samsung", "oneplus", "redmi", "xiaomi",
                "realme", "vivo", "oppo", "pixel", "motorola", "nothing",
                "poco", "iqoo", "mobile", "phone", "ఫోన్", "మొబైల్",
            )
            has_model_number = bool(re.search(r"\b[a-z]{1,12}[- ]?\d{1,4}\b", lowered))
            if len(text) <= 140 and (
                any(marker in lowered for marker in product_markers)
                or has_model_number
            ):
                result["product_interest"] = text

    return result


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
    previous_product_before_turn = customer.get("product_interest")
    # Persist the WhatsApp sender as the phone number; never depend on LLM extraction.
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
    # Extract details even after a lead is saved,
    # so corrections and follow-up information
    # can still be understood.
    try:
        extracted = extract_customer_details(
            message,
            customer,
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
    # LLM extraction can miss short, valid answers. Resolve the last asked
    # field deterministically before merging so completed fields aren't asked
    # for again just because the model returned an empty value.
    extracted = _answer_for_asked_field(
        message=message,
        customer=customer,
        extracted=extracted,
        session=session,
    )
    # Merge required and additional
    # customer details.
    _merge_customer_details(
        customer,
        extracted,
    )
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
    _record_customer_turn(
        session=session,
        message=message,
        language=active_language,
        customer=customer,
        intent=intent,
    )
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
            previous_product_before_turn=previous_product_before_turn,
        )
    # ---------------------------------------------------------
    # CATALOG
    # ---------------------------------------------------------
    # Only show catalog information when the customer supplies
    # a NEW product. The extractor may repeat the previous
    # product_interest on later turns, so we must not run the
    # catalog lookup on every message.
    previous_product = previous_product_before_turn
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
    if new_product_received:
        product_message = _get_product_message(
            extracted_product
        )
        session["catalog_message_sent"] = True
        session["catalog_product"] = extracted_product
    nonbuyer_mode = _nonbuyer_mode(message)
    if nonbuyer_mode:
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
    # Answer a customer's actual question before asking for qualification details.
    # A short reply such as "EMI", "cash", "yes", or "128GB" is not classified
    # as a question, so it continues through normal detail extraction.
    joke_kind = _detect_obvious_joke(message)
    if joke_kind:
        draft_reply = _obvious_joke_reply(joke_kind, active_language)
        if joke_kind == "price_joke":
            follow_up = _question_prompt_for_field(
                "budget", customer, session, active_language
            )
        else:
            follow_up = ""
        if follow_up and joke_kind != "just_joking":
            draft_reply += "\n\n" + follow_up
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                draft_reply, message, customer, session
            ),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }
    question_type = _detect_customer_question(message)
    missing_required_fields = any(
        not _field_is_complete(field, customer) for field in FIELD_ORDER
    )
    if question_type and (
        missing_required_fields or not session.get("intent_question_asked")
    ):
        answer = _answer_customer_question(
            question_type=question_type,
            message=message,
            customer=customer,
            language=active_language,
            product_message=product_message,
        )
        follow_up, _follow_up_field = _question_follow_up(
            question_type=question_type,
            customer=customer,
            session=session,
            language=active_language,
        )
        draft_parts = [answer] if answer else []
        if follow_up:
            draft_parts.append(follow_up)
        draft_reply = "\n\n".join(draft_parts)
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                draft_reply, message, customer, session
            ),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }
    social_kind = _simple_social_kind(message)
    if social_kind:
        draft_reply = _courtesy_reply(social_kind, active_language)
        return {
            "customer_id": customer_id,
            "message": _localize_reply(
                draft_reply, message, customer, session
            ),
            "customer": customer,
            "qualification": session.get("qualification"),
            "intent": session.get("intent"),
            "intent_history": session.get("intent_history", []),
            "lead_id": session.get("lead_id"),
            "lead_saved": bool(session.get("lead_saved")),
        }
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
        if product_message:
            message_parts.append(
                product_message.strip()
            )
        message_parts.append(
            _question_prompt_for_field(
                next_field,
                customer,
                session,
                active_language,
            )
        )
        draft_reply = (
            "\n\n".join(
                message_parts
            )
        )
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
    if not session[
        "intent_question_asked"
    ]:
        session["asked_field"] = "intent"
        session[
            "intent_question_asked"
        ] = True
        # Ask a question that matches the customer's buying stage instead of
        # using the same hard close for every customer.
        intent_status = str(session.get("intent", {}).get("status", "low")).lower()
        if active_language == "telugu":
            if intent_status in {"very_high", "high"}:
                draft_reply = "ధర, స్టాక్ సరిపోతే కొనుగోలును ముందుకు తీసుకెళ్లాలనుకుంటున్నారా?"
            elif intent_status == "medium":
                draft_reply = "సరిగ్గా గైడ్ చేయడానికి అడుగుతున్నాను—రాబోయే కొన్ని వారాల్లో కొనాలనుకుంటున్నారా, లేక ఇప్పటికి ఎంపికలు పోల్చి చూస్తున్నారా?"
            else:
                draft_reply = "ఒత్తిడి ఏమీ లేదు 😊 మోడల్స్ పోల్చడంలో కూడా సహాయం చేస్తాను. ఇప్పుడే చూస్తున్నారా, లేక కొనడానికి ఒక అంచనా సమయం ఉందా?"
        elif intent_status in {"very_high", "high"}:
            draft_reply = "Price and stock okay ayithe purchase ni proceed cheddama bro?"
        elif intent_status == "medium":
            draft_reply = "Correct ga guide cheyyadaniki aduguthunna—next few weeks lo konalani plan chesthunnara, leka ippatiki options compare chesthunnara?"
        else:
            draft_reply = "Pressure em ledu bro 😊 Models compare cheyyadaniki help chestha. Ippudu just explore chesthunnara, leka purchase ki rough timeline emaina undha?"
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
    if not isinstance(saved_lead, dict) or not saved_lead:
        # Do not retry an insert blindly: a second insert can create a duplicate
        # if the first write reached the database but its response was lost.
        print("Lead completion persistence did not return a saved record.")
        session["lead_saved"] = False
        session["qualification"] = qualification
    else:
        session["record_id"] = saved_lead.get("lead_id") or session.get("record_id")
        session["lead_id"] = session.get("record_id")
        session["lead_saved"] = bool(session.get("record_id"))
        session["qualification"] = qualification
    # ---------------------------------------------------------
    # FINAL CUSTOMER RESPONSE
    # ---------------------------------------------------------
    if question_type:
        question_reply = _answer_customer_question(
            question_type=question_type,
            message=message,
            customer=customer,
            language=active_language,
            product_message=product_message,
        )
        if session.get("lead_saved"):
            if active_language == "telugu":
                confirmation = "మీ వివరాలను Mania Mobiles కోసం నమోదు చేశాను. ధర, స్టాక్‌ను స్టోర్ నిర్ధారించాలి."
            else:
                confirmation = "Mee details Mania Mobiles kosam record chesanu. Final price and stock store confirm cheyyali."
        elif active_language == "telugu":
            confirmation = "మీ వివరాలు అందాయి. ధర, స్టాక్‌ను స్టోర్ టీమ్‌తో నిర్ధారించుకోండి; అవసరమైతే సేల్స్ టీమ్‌ను సంప్రదించండి."
        else:
            confirmation = "Mee details ardham ayyayi. Final price and stock store team tho confirm cheskondi; avasaram ayithe sales team ni contact cheyyandi."
        draft_reply = question_reply + "\n\n" + confirmation
    elif active_language == "telugu":
        if session.get("lead_saved"):
            draft_reply = (
                "మీ అవసరాలను Mania Mobiles కోసం నమోదు చేశాను. "
                "ఫోన్ ధర, స్టాక్‌ను మా స్టోర్ టీమ్ నిర్ధారిస్తుంది."
            )
        else:
            draft_reply = (
                "మీ అవసరాలు అర్థమయ్యాయి. ఫోన్ ధర, స్టాక్‌ను స్టోర్ టీమ్‌తో నిర్ధారించుకోండి."
            )
    else:
        if session.get("lead_saved"):
            draft_reply = (
                "Mee requirements Mania Mobiles kosam record chesanu. "
                "Phone price and stock ni maa store team confirm chestharu."
            )
        else:
            draft_reply = (
                "Mee requirements ardham ayyayi. Phone price and stock ni store team tho confirm cheskondi."
            )
    return {
        "customer_id": customer_id,
        "message": _localize_reply(
            draft_reply,
            message,
            customer,
            session,
        ),
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
        "lead_saved": bool(session.get("lead_saved")),
    }

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
            (field for field in FIELD_ORDER if not _field_is_complete(field, customer)),
            "intent",
        ),
        "lead_saved": (
            conversation_status == "completed"
            or intent.get("session_status") == "completed"
        ),
        "lead_id": lead.get("lead_id"),
        "record_id": lead.get("lead_id"),
        "qualification": qualification,
        "language": restored_language,
        "language_locked": bool(history),
        "last_activity": lead.get("last_activity") or lead.get("updated_at"),
        "intent": intent or {
            "score": qualification.get("score", 0),
            "status": qualification.get("status", "low"),
            "signals": qualification.get("reasons", []),
        },
        "intent_history": [],
        "conversation_history": history,
        "intent_question_asked": conversation_status == "completed",
        "catalog_message_sent": bool(customer.get("product_interest")),
        "catalog_product": customer.get("product_interest"),
    })
    return session
# Inactivity timeout: keep a conversation active for 30 minutes after the
# customer's last message. The partial lead remains permanently in Supabase.
SESSION_TIMEOUT = timedelta(minutes=30)

def _is_greeting(message: str) -> bool:
    import re
    normalized = re.sub(
        r"[^\w\u0C00-\u0C7F]+",
        "",
        str(message or "").strip().lower(),
    )
    return normalized in {
        "hi", "hii", "hiii", "hello", "hey", "hai",
        "హాయ్", "హలో", "నమస్కారం",
    }

def _greeting_reply(language: str) -> str:
    if language == "telugu":
        return (
            "నమస్కారం! Mania Mobiles‌కు స్వాగతం 😊 "
            "మీకు ఏ మొబైల్ కావాలి? బ్రాండ్, మోడల్ పేరు చెప్పండి."
        )
    return (
        "Hi! Mania Mobiles ki welcome 😊 "
        "Meeku ye mobile kavali? Brand mariyu model peru cheppandi."
    )

def _process_message_serialized(customer_id: str, message: str) -> dict:
    customer_id = (customer_id or "").strip()
    message = (message or "").strip()
    if not customer_id or not message:
        raise ValueError("customer_id and message are required")
    original_message_length = len(message)
    oversized_message = original_message_length > 4000
    if oversized_message:
        # WhatsApp can deliver pasted text far beyond the model's safe input
        # size. Process a bounded prefix rather than crashing the whole webhook.
        # The reply explicitly tells the customer that only part was processed.
        print(
            "Oversized inbound message bounded for processing:",
            original_message_length,
            "characters; using first 4000",
        )
        message = message[:4000]
    now = datetime.now(timezone.utc)
    session = sessions.get(customer_id)
    if session is None:
        session = _restore_session_from_storage(customer_id)

    # A completed lead belongs to the previous qualification session. If the
    # same WhatsApp customer starts again with a greeting, do not route that
    # greeting (or all subsequent answers) through the completed-lead follow-up
    # handler. Keep the old lead in storage and create a fresh in-memory session;
    # the first turn below will persist a new active record for this conversation.
    # This is especially important after server restarts, when the completed
    # session is restored from the database rather than found in `sessions`.
    restored_intent = (session or {}).get("intent") or {}
    is_completed_session = bool(
        (session or {}).get("lead_saved")
        or str(restored_intent.get("session_status", "")).lower() == "completed"
    )
    if session and is_completed_session and _is_greeting(message):
        print(
            "Starting a new qualification session after completed lead; "
            "previous lead retained:",
            bool((session or {}).get("record_id")),
        )
        sessions.pop(customer_id, None)
        session = None

    if session and session.get("last_activity"):
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
    previous_history_len = len(session.get("conversation_history", []))
    result = _process_message_impl(customer_id, message)
    session = _get_session(customer_id)
    if oversized_message:
        if session.get("language") == "telugu":
            notice = (
                f"మీ మెసేజ్ {original_message_length} అక్షరాల కంటే ఎక్కువగా ఉంది. "
                "మొదటి భాగాన్ని మాత్రమే ప్రాసెస్ చేశాను. మిగతా వివరాలను చిన్న మెసేజ్‌లుగా పంపండి."
            )
        else:
            notice = (
                f"Mee message chala long ga undhi ({original_message_length} characters). "
                "First 4000 characters matrame process chesanu. Migatha details ni short messages ga pampandi."
            )
        result["message"] = notice + "\n\n" + str(result.get("message") or "")
    if previous_history_len == 0 and _is_greeting(message):
        # Deterministic greeting avoids an English/incorrect-script first reply.
        # The next turn follows the same six-field qualification flow.
        result["message"] = _greeting_reply(session.get("language", "roman_telugu"))
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
        print(f"Lead persistence failed: {type(exc).__name__}: {exc}")
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
