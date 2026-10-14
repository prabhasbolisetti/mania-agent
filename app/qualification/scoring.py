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
    if product and product not in GENERIC_PRODUCTS and re.search(r"\d", product):
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
    if _has_any(text, [
        "this week", "within a week", "in a week", "next week",
        "ee week", "ee vaaram", "ee varam", "ee week lo",
        "ఈ వారం", "ఈ వారంలో", "వచ్చే వారం",
        "this weekend", "ee weekend", "ఈ వీకెండ్",
    ]):
        return 15
    if _has_any(text, [
        "this month", "within a month", "in a month", "next month",
        "next month lo", "next month", "vachche nela", "వచ్చే నెల",
        "ఈ నెల", "ఈ నెలలో", "ఒక నెలలో",
    ]):
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
    if _has_any(text, [
        "not buying now",
        "not planning to buy",
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
    ]):
        return -25
    if _has_any(text, [
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
    ]):
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

    if text in {
        "yes", "yes definitely", "definitely", "sure", "absolutely",
        "yes i'll buy", "yes i will buy", "yes, i'll buy",
        "avunu", "avunu definitely", "definitely konali", "konestha",
        "అవును", "ఖచ్చితంగా", "అవును, కొనేస్తాను",
    }:
        return 20

    return 0


def calculate_intent(
    message: str,
    customer: Dict[str, Any],
    previous_intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Calculate current purchase intent from the full customer state and latest response."""
    score = 0
    signals: List[str] = []
    text = message.lower().strip()

    if _specific_product(customer):
        score += 15
        signals.append("Specific model")
    elif _text(customer.get("product_interest")):
        score += 8
        signals.append("Product interest")

    if _text(customer.get("storage")):
        score += 5
        signals.append("Specific storage/variant")

    if customer.get("budget") or customer.get("budget_amount") or customer.get("budget_max"):
        score += 10
        signals.append("Budget provided")

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

    if _text(customer.get("condition")):
        score += 5
        signals.append("Condition specified")

    if _text(customer.get("payment_method")):
        score += 10
        signals.append("Payment method provided")

    if _text(customer.get("location")):
        score += 5
        signals.append("Location provided")

    explicit = _explicit_answer_score(message)
    if explicit:
        score += explicit
        signals.append("Explicit willingness to buy")

    if _strong_purchase_action(message):
        score += 5
        signals.append("Strong purchase action")

    negative = _negative_intent(message)
    if negative:
        score += negative
        signals.append("Customer indicates low/no current buying intent")

    if _has_any(text, [
        "how much", "price", "cost", "available", "availability",
        "stock", "delivery", "emi", "can i get", "can i buy",
        "entha", "dhara", "rate", "undha", "dorukuthunda",
        "ధర", "అందుబాటులో", "స్టాక్", "దొరుకుతుందా",
    ]):
        score += 5
        signals.append("Purchase-related question")

    score = max(0, min(100, score))

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
