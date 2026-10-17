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

    "payment_method": (

        "How would you prefer to pay—cash, card, UPI, or EMI?"

    ),

    "location": "Which city or town are you purchasing from?",

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
        # Brand names like iPhone are a valid product cue only if the user
        # actually wrote that brand in the current message.
        if product == "iphone":
            return bool(re.search(r"\biphone\b", raw))
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


def _get_product_message(
    product_interest: str,
    customer_message: str = "",
    language: str = "roman_telugu",
) -> str:
    """Return one exact product link only when the current turn names it.

    A catalog miss produces a short demo-catalog note in the conversation's
    locked language. This helper never lists unrelated products.
    """
    if not product_interest or not _is_specific_product_mentioned(
        customer_message, product_interest
    ):
        return ""

    is_telugu = language == "telugu"
    try:
        matches = _search_catalog(product_interest)
        matches = [
            item for item in matches
            if _is_specific_product_mentioned(
                customer_message,
                str(item.get("product_name") or item.get("model") or ""),
            )
        ]

        if matches:
            product = matches[0]
            name = str(
                product.get("product_name")
                or product.get("model")
                or product_interest
            ).strip()
            url = str(product.get("product_url") or "").strip()
            if url.startswith(("https://", "http://")):
                if is_telugu:
                    return (
                        f"మీరు అడిగిన ప్రోడక్ట్: {name}\n"
                        f"డైరెక్ట్ లింక్: {url}\n"
                        "ప్రస్తుత ధర, స్టాక్ వివరాలు లింక్‌లో చూసి నిర్ధారించుకోండి."
                    )
                return (
                    f"Mee product: {name}\n"
                    f"Direct link: {url}\n"
                    "Current price mariyu stock link lo check chesi confirm chesukondi."
                )
            if is_telugu:
                return (
                    f"{name} మా డెమో కాటలాగ్‌లో కనిపించింది, కానీ డైరెక్ట్ లింక్ లేదు. "
                    "ధర, స్టాక్‌ను స్టోర్‌తో నిర్ధారించుకోండి."
                )
            return (
                f"{name} maa demo catalog lo dorikindi, kani direct link available ledu. "
                "Price mariyu stock store tho confirm chesukondi."
            )

        if is_telugu:
            return (
                "ఈ ప్రోడక్ట్ మా డెమో కాటలాగ్‌లో కనిపించలేదు. "
                "మోడల్ పేరు లేదా వేరియంట్‌ను మరింత స్పష్టంగా చెప్పగలరా? 😊"
            )
        return (
            "Ee product maa demo catalog lo dorakaledu. "
            "Model peru leda variant konchem clear ga cheppagalara? 😊"
        )
    except Exception as exc:
        print("Product catalog lookup failed:", type(exc).__name__)
        if is_telugu:
            return (
                "ప్రస్తుతం డెమో కాటలాగ్‌ను చెక్ చేయలేకపోతున్నాను. "
                "మోడల్ పేరు పంపండి లేదా కొద్దిసేపటి తర్వాత ప్రయత్నించండి."
            )
        return (
            "Ippudu demo catalog check cheyalekapothunnanu. "
            "Model peru pampandi leda konchem sepu tarvatha try cheyyandi."
        )


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

    if extracted_product and _is_specific_product_mentioned(message, product_interest):
        product_message = _get_product_message(
            product_interest,
            message,
            session.get("language", "roman_telugu"),
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




def _apply_deterministic_customer_updates(message: str, extracted: dict, customer: dict, asked_field: str = "") -> dict:
    """Fill common qualification answers locally so the flow doesn't repeat questions.

    The LLM extractor remains responsible for broad language understanding. These
    high-confidence rules cover frequent Roman-Telugu replies that are easy to miss.
    Existing customer answers are preserved when a turn is only a clarification.
    """
    result = dict(extracted or {})
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    compact = re.sub(r"[^a-z0-9₹]+", "", text)

    # Timeline: explicitly map short answers such as "Tarwatha" and
    # "compare chesi confirm chestha" to a completed timeline field.
    timeline_patterns = [
        (r"\b(ivala|eeroju|today|right now|immediately|ippude)\b", "today"),
        (r"\b(ee week|this week|within (a|one) week|one week lo|week lopu|ee vaaram)\b", "within a week"),
        (r"\b(next week|next month|next year|next few weeks|few weeks tarvatha|few weeks later)\b", "later"),
        (r"\b(tarvatha|tarwatha|tarvata|taruvatha|later|not now|after some time|sometime later)\b", "later"),
        (r"\b(compare chesi|compare che(si|si)|compare chestha|compare chesi confirm|decide chesaka|confirm chesaka|think and decide|after comparing|comparing options|compare first)\b", "later"),
    ]
    for pattern, canonical in timeline_patterns:
        if re.search(pattern, text):
            result["purchase_timeline"] = canonical
            break

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
        and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,49}", (message or "").strip())
        and not re.search(r"\b(cash|card|upi|emi|yes|no|okay|fine|later|today|tomorrow)\b", text)
    ):
        result["location"] = message.strip()

    # Condition / new-vs-used.
    if re.search(r"\b(brand new|brand-new|new piece|sealed piece|sealed|kotha phone|kottha phone|new phone)\b", text):
        result["condition"] = "brand new"
    elif re.search(r"\b(used|second hand|second-hand|refurbished|renewed|pre owned|pre-owned)\b", text):
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

    # Numeric budget ranges. Keep this conservative; the LLM handles more complex
    # expressions. Examples: "40k-60k", "40,000 to 60,000", "45k".
    if not result.get("budget"):
        range_match = re.search(
            r"(?:₹\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]?)\s*(?:-|–|to|and|nunchi)\s*(?:₹\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]?)",
            text,
        )
        single_match = re.search(
            r"(?:budget|under|below|around|approx(?:imately)?|₹)\s*(?:₹\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]?)",
            text,
        )
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
            elif single_match:
                value = amount(single_match.group(1), single_match.group(2))
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
            "product_interest": "మానియా మొబైల్స్‌కు స్వాగతం 😊 మీకు ఏ మొబైల్ కావాలి? బ్రాండ్, మోడల్ పేరు చెప్పండి.",
            "budget": f"{product} కోసం సుమారుగా ఎంత బడ్జెట్ అనుకుంటున్నారు?" if product else "మీ బడ్జెట్ ఎంత అనుకుంటున్నారు?",
            "purchase_timeline": "ఎప్పుడు కొనాలని ప్లాన్ చేస్తున్నారు—ఈరోజు, ఈ వారం, లేక తర్వాత?",
            "condition": "కొత్త సీల్డ్ పీస్ కావాలా, లేక వాడినది/రిఫర్బిష్డ్ కూడా పరిగణలోకి తీసుకుంటారా?",
            "payment_method": "పేమెంట్‌కు క్యాష్, UPI, కార్డ్, లేక EMI—ఏది సౌకర్యంగా ఉంటుంది?",
            "location": "మీరు ఏ సిటీ/టౌన్ నుంచి కొనాలనుకుంటున్నారు? స్టోర్ వివరాలు నిర్ధారించడానికి ఉపయోగపడుతుంది.",
        }
    else:
        templates = {
            "product_interest": "Mania Mobiles ki welcome 😊 Meeku ye mobile kavali? Brand mariyu model peru cheppandi.",
            "budget": f"{product} kosam roughly entha budget anukuntunnaru?" if product else "Mee budget entha anukuntunnaru?",
            "purchase_timeline": "Eppudu konalani plan chesthunnaru—ivala, ee week, leka tarvatha?",
            "condition": "Brand-new sealed piece kavala, leka used/refurbished kuda consider chesthara?",
            "payment_method": "Payment ki cash, UPI, card, leka EMI lo edhi convenient?",
            "location": "Meeru ye city/town nunchi purchase chesthunnaru? Store options confirm cheyyadaniki help avuthundhi.",
        }
    return templates.get(field, "")



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

    extracted = _apply_deterministic_customer_updates(
        message=message,
        extracted=extracted,
        customer=customer,
        asked_field=session.get("asked_field", ""),
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

    if extracted_product and _is_specific_product_mentioned(message, extracted_product):
        product_message = _get_product_message(
            extracted_product,
            message,
            active_language,
        )

        session["catalog_message_sent"] = bool(product_message)
        session["catalog_product"] = extracted_product if product_message else None

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
            _localized_question(next_field, active_language, customer)
        )



        draft_reply = (

            "\n\n".join(

                message_parts

            )

        )



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



    if not session[

        "intent_question_asked"

    ]:

        session[

            "intent_question_asked"

        ] = True



        # Ask a question that matches the customer's buying stage instead of
        # using the same hard close for every customer.
        intent_status = str(session.get("intent", {}).get("status", "low")).lower()
        if intent_status in {"very_high", "high"}:
            draft_reply = INTENT_QUESTION
        elif intent_status == "medium":
            draft_reply = (
                "Just so I can guide you properly, are you planning to buy "
                "within the next few weeks, or are you comparing options for now?"
            )
        else:
            if customer.get("purchase_timeline"):
                draft_reply = (
                    "No pressure at all. I can help compare options. "
                    "If you find a phone that fits your budget, would you like the store "
                    "to confirm its latest price and availability?"
                )
            else:
                draft_reply = (
                    "No pressure at all. I can help compare a few phones too. "
                    "Are you exploring options, or do you have a rough time in mind to buy?"
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
    if session.get("language") == "telugu":
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

    # Dashboard/chat callers still receive one combined `message` string.
    # The WhatsApp webhook can use `messages` to send these as three bubbles.
    session["_return_template_reply"] = True
    combined_message = "\\n\\n".join(final_messages)

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
    if previous_history_len == 0 and message.lower().strip() in {"hi", "hello", "hey", "hii", "hai", "నమస్కారం", "హాయ్"}:
        if session.get("language") == "telugu":
            result["message"] = "నమస్కారం! Mania Mobiles‌కు స్వాగతం 😊 మీకు ఏ మొబైల్ కావాలి? బ్రాండ్, మోడల్ పేరు చెప్పండి."
        else:
            result["message"] = "Hi! Mania Mobiles ki welcome 😊 Meeku ye mobile kavali? Brand mariyu model peru cheppandi."
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
