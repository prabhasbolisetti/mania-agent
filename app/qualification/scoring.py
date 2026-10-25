import re
from typing import Any, Dict, List, Optional


GENERIC_PRODUCTS = {
    "phone",
    "mobile",
    "smartphone",
    "iphone",
    "android phone",
    "a phone",
    "a mobile",
    "any phone",
    "any mobile",
}


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _has_any(text: str, phrases: List[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _specific_product(customer: Dict[str, Any]) -> bool:
    model = _text(customer.get("model"))
    product = _text(customer.get("product_interest"))

    if model and re.search(r"\d", model):
        return True

    if (
        product
        and product not in GENERIC_PRODUCTS
        and re.search(r"\d", product)
    ):
        return True

    return False


def _near_purchase(message: str, timeline: str) -> bool:
    text = f"{message} {timeline}".lower()

    patterns = [
        r"\btoday\b",
        r"\bright now\b",
        r"\bimmediately\b",
        r"\basap\b",
        r"\btomorrow\b",
        r"\bday after tomorrow\b",
        r"\bin\s+\d+\s+days?\b",
        r"\bwithin\s+\d+\s+days?\b",
        r"\bin\s+two\s+days?\b",
        r"\bin\s+2\s+days?\b",
        r"\brepu\b",
        r"\beeroju\b",
        r"\bivala\b",
        r"\bippude?\b",
        r"రేపు",
        r"ఈరోజు",
        r"ఇప్పుడే",
        r"రెండు\s+రోజుల్లో",
        r"2\s+రోజుల్లో",
    ]

    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _timeline_points(message: str, timeline: str) -> int:
    text = f"{message} {timeline}".lower()

    if _near_purchase(message, timeline):
        return 20

    if _has_any(
        text,
        [
            "this week",
            "within a week",
            "in a week",
            "next week",
            "ee week",
            "ee vaaram",
            "ee varam",
            "ee week lo",
            "ఈ వారం",
            "ఈ వారంలో",
            "వచ్చే వారం",
            "this weekend",
            "ee weekend",
            "ఈ వీకెండ్",
        ],
    ):
        return 15

    if _has_any(
        text,
        [
            "this month",
            "within a month",
            "in a month",
            "next month",
            "next month lo",
            "vachche nela",
            "వచ్చే నెల",
            "ఈ నెల",
            "ఈ నెలలో",
            "ఒక నెలలో",
        ],
    ):
        return 5

    return 5 if timeline.strip() else 0


def _explicit_buying(message: str) -> bool:
    text = message.lower().strip()

    phrases = [
        "i want to buy",
        "i'll buy",
        "i will buy",
        "i am buying",
        "i'm buying",
        "i want to purchase",
        "i will purchase",
        "i'd like to buy",
        "proceed with the purchase",
        "go ahead with the purchase",
        "book it",
        "reserve it",
        "reserve one",
        "please reserve",
        "send payment details",
        "payment link",
        "buy it today",
        "buy tomorrow",
        "konali",
        "konestha",
        "konukunta",
        "konalanukuntunna",
        "purchase cheyyali",
        "buy cheyyali",
        "book cheyyandi",
        "reserve cheyyandi",
        "కొనాలి",
        "కొనేస్తాను",
        "కొనేస్తా",
        "కొనుకుంటాను",
        "కొనాలని ఉంది",
        "కొనాలని అనుకుంటున్నాను",
        "కొనుగోలు చేయాలి",
        "బుక్ చేయండి",
        "రిజర్వ్ చేయండి",
    ]

    return _has_any(text, phrases)


def _strong_purchase_action(message: str) -> bool:
    text = message.lower().strip()

    phrases = [
        "i'll come today",
        "i will come today",
        "i'll come tomorrow",
        "i will come tomorrow",
        "coming to the store",
        "i'll visit the store",
        "send me the payment link",
        "where should i pay",
        "reserve one for me",
        "hold one for me",
        "keep one for me",
        "ivala store ki vastha",
        "repu store ki vastha",
        "store ki vastha",
        "store ki vasta",
        "naku okati reserve cheyyandi",
        "ఒకటి రిజర్వ్ చేయండి",
        "స్టోర్‌కు వస్తాను",
        "స్టోర్ కి వస్తాను",
    ]

    return _has_any(text, phrases)


def _negative_intent(message: str) -> int:
    text = message.lower().strip()

    strong_negative_phrases = [
        "not buying now",
        "not planning to buy",
        "not decided",
        "haven't decided",
        "have not decided",
        "present order cheyali anukovatledhu",
        "present order cheyyali anukovatledu",
        "nen inkaa konalani decide avvatle",
        "nenu inkaa konalani decide avvatle",
        "inka konalani decide avvatle",
        "konalani decide avvatle",
        "konalane uddesam ledhu",
        "konalane uddesam ledu",
        "don't want to buy",
        "do not want to buy",
        "not interested",
        "just checking",
        "just browsing",
        "only checking",
        "only comparing",
        "just comparing",
        "no plan to buy",
        "maybe later",
        "later, not now",
        "konadaniki plan ledu",
        "ippudu konanu",
        "ippudu konalani plan ledu",
        "just price check chestunnanu",
        "just check chestunnanu",
        "interest ledu",
        "konalani ledu",
        "తర్వాత కొనుగోలు చేస్తాను",
        "ఇప్పుడే కొనను",
        "ఇప్పుడు కొనను",
        "ఇప్పుడే కొనాలని లేదు",
        "కొనాలని ప్లాన్ లేదు",
        "కేవలం ధర చూస్తున్నాను",
        "ధర మాత్రమే చూస్తున్నాను",
        "కేవలం చూస్తున్నాను",
        "పోల్చి చూస్తున్నాను",
    ]

    if _has_any(text, strong_negative_phrases):
        return -25

    uncertain_phrases = [
        "not sure",
        "maybe",
        "just looking",
        "researching",
        "research only",
        "compare options",
        "checking options",
        "inka chustunnanu",
        "inka decide cheyaledu",
        "ఇంకా చూస్తున్నాను",
        "ఇంకా నిర్ణయించుకోలేదు",
        "పోల్చి చూస్తున్నాను",
    ]

    if _has_any(text, uncertain_phrases):
        return -15

    return 0


def _explicit_answer_score(message: str) -> int:
    text = message.lower().strip()

    if _negative_intent(message) < 0:
        return 0

    if _strong_purchase_action(message):
        return 15

    if _explicit_buying(message):
        return 20

    positive_answers = {
        "yes",
        "yes definitely",
        "definitely",
        "sure",
        "absolutely",
        "yes i'll buy",
        "yes i will buy",
        "yes, i'll buy",
        "avunu",
        "avunu definitely",
        "definitely konali",
        "konestha",
        "అవును",
        "ఖచ్చితంగా",
        "అవును, కొనేస్తాను",
    }

    if text in positive_answers:
        return 20

    return 0


def calculate_intent(
    message: str,
    customer: Dict[str, Any],
    previous_intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Calculate purchase intent using the latest message and known customer data.

    Profile details alone cannot establish very-high purchase intent.
    An explicit purchase action can increase the score substantially.
    The conversation handler remains responsible for interpreting a
    positive or negative answer in the context of its final intent question.
    """
    score = 0
    signals: List[str] = []
    text = message.lower().strip()

    # Product interest
    if _specific_product(customer):
        score += 15
        signals.append("Specific model")
    elif _text(customer.get("product_interest")):
        score += 8
        signals.append("Product interest")

    # Variant
    if _text(customer.get("storage")):
        score += 5
        signals.append("Specific storage/variant")

    # Budget
    if (
        customer.get("budget")
        or customer.get("budget_amount")
        or customer.get("budget_max")
    ):
        score += 10
        signals.append("Budget provided")

    # Timeline
    timeline = _text(customer.get("purchase_timeline"))
    timeline_points = _timeline_points(message, timeline)

    if timeline_points:
        score += timeline_points

        if timeline_points >= 20:
            signals.append("Very near purchase timeline")
        elif timeline_points >= 15:
            signals.append("Near purchase timeline")
        else:
            signals.append("Purchase timeline provided")

    # Condition
    if _text(customer.get("condition")):
        score += 5
        signals.append("Condition specified")

    # Payment preference
    payment_method = _text(customer.get("payment_method"))

    payment_was_deferred = (
        bool(customer.get("payment_method_declined"))
        or payment_method
        in {
            "not specified",
            "later",
            "undecided",
            "prefer not to say",
            "not decided",
        }
    )

    if payment_method and not payment_was_deferred:
        score += 10
        signals.append("Payment method provided")
    elif payment_was_deferred:
        signals.append("Payment preference deferred; no payment points")

    # Location
    if _text(customer.get("location")):
        score += 5
        signals.append("Location provided")

    # Latest-message intent
    explicit = _explicit_answer_score(message)

    if explicit:
        score += explicit
        signals.append("Explicit willingness to buy")

    strong_action = _strong_purchase_action(message)

    if strong_action:
        score += 5
        signals.append("Strong purchase action")

    # Negative intent must reduce the score.
    negative = _negative_intent(message)

    if negative:
        score += negative
        signals.append("Customer indicates low/no current buying intent")

    # A product question is an interest signal, not proof of buying intent.
    purchase_question_phrases = [
        "how much",
        "price",
        "cost",
        "available",
        "availability",
        "stock",
        "delivery",
        "emi",
        "can i get",
        "can i buy",
        "entha",
        "dhara",
        "rate",
        "undha",
        "dorukuthunda",
        "ధర",
        "అందుబాటులో",
        "స్టాక్",
        "దొరుకుతుందా",
    ]

    if _has_any(text, purchase_question_phrases):
        score += 5
        signals.append("Purchase-related question")

    # Keep a confirmed purchase signal from the current conversation.
    previous_signals = (previous_intent or {}).get("signals", [])

    previously_confirmed = any(
        str(signal).strip().lower()
        == "customer confirmed purchase intent"
        for signal in previous_signals
    )

    explicit_purchase_signal = bool(
        explicit > 0
        or strong_action
        or previously_confirmed
    )

    score = max(0, min(100, score))

    # Product details and questions alone must not create a hot lead.
    # The compact flow intentionally asks fewer questions than the old
    # six-field qualification questionnaire.
    if strong_action:
        score = max(score, 85)
    elif _explicit_buying(message):
        score = max(score, 70)
    elif not explicit_purchase_signal and score > 59:
        score = 59
        signals.append(
            "Purchase intent not explicitly confirmed; score capped"
        )

    score = min(100, score)

    if score >= 80:
        status = "very_high"
    elif score >= 60:
        status = "high"
    elif score >= 40:
        status = "medium"
    else:
        status = "low"

    return {
        "score": score,
        "status": status,
        "signals": list(dict.fromkeys(signals)),
    }