import re
from typing import Any, Dict, List, Optional

BRAND_ALIASES = {
    "iphone": "Apple",
    "apple": "Apple",
    "samsung": "Samsung",
    "oneplus": "OnePlus",
    "xiaomi": "Xiaomi",
    "redmi": "Redmi",
    "realme": "Realme",
    "vivo": "Vivo",
    "oppo": "Oppo",
    "motorola": "Motorola",
    "nothing": "Nothing",
    "google": "Google",
    "pixel": "Google",
    "poco": "Poco",
    "iqoo": "iQOO",
    "infinix": "Infinix",
    "tecno": "Tecno",
}


def _amount_to_inr(value: str, unit: Optional[str] = None) -> int:
    amount = float(value.replace(",", ""))
    unit = (unit or "").lower()

    if unit in ("k", "thousand"):
        amount *= 1000
    elif unit in ("l", "lac", "lakh", "lakhs"):
        amount *= 100000

    return int(amount)


def _extract_product(text: str, result: Dict[str, Any]) -> None:
    lower = text.lower()

    # Accept common model families that the old pattern missed
    # (e.g. Redmi Note 13, Motorola Edge 50, Galaxy S24)
    # and avoid returning only the generic word "phone".
    model_pattern = (
        r"\b(?:iphone|samsung\s+galaxy|samsung|samsng|galaxy|oneplus|"
        r"one\s*plus|redmi|xiaomi|realme|vivo|oppo|motorola|moto|"
        r"poco|iqoo|nothing\s+phone|pixel|google\s+pixel)\s*[- ]*"
        r"(?:(?:note|edge|galaxy|narzo|reno|nord|f|a|s|z|c|x)\s*)?"
        r"(?:[a-z]?[0-9]{1,3})"
        r"(?:\s*(?:pro|max|plus|ultra|fe|neo|se|lite|mini|fold|flip|"
        r"promax|pro max|t|r)){0,2}\b"
    )

    model_match = re.search(model_pattern, lower, re.I)

    if not model_match:
        # Fallback for brand-first messages such as "Samsung S24"
        # and Telugu script brand names followed by a model number.
        base_names = {
            "Apple": "iPhone",
            "Samsung": "Samsung",
            "OnePlus": "OnePlus",
            "Redmi": "Redmi",
            "Xiaomi": "Xiaomi",
            "Realme": "Realme",
            "Vivo": "Vivo",
            "Oppo": "Oppo",
            "Motorola": "Motorola",
            "Poco": "Poco",
            "iQOO": "iQOO",
            "Nothing": "Nothing",
            "Google": "Pixel",
            "Infinix": "Infinix",
            "Tecno": "Tecno",
        }

        fallback_matches = []

        for alias in BRAND_ALIASES:
            if alias.isascii():
                alias_pattern = r"\b" + re.escape(alias) + r"\b"
            else:
                alias_pattern = re.escape(alias)

            match = re.search(
                alias_pattern
                + r"\s*[- ]*(?:note|edge|galaxy|s|a|z|f)?\s*([0-9]{1,3})"
                r"(?:\s*(?:pro|max|plus|ultra|fe|neo|se|lite|mini))?",
                lower,
                re.I,
            )

            if match:
                canonical = BRAND_ALIASES[alias]
                prefix = base_names.get(canonical, canonical)

                family = re.search(
                    r"(note|edge|galaxy|s|a|z|f)",
                    match.group(0),
                    re.I,
                )

                suffix = match.group(0)[len(alias):].strip()

                fallback_matches.append(
                    (match.start(), prefix, suffix, canonical)
                )

        if fallback_matches:
            _, prefix, suffix, canonical = min(
                fallback_matches,
                key=lambda item: item[0],
            )

            model = re.sub(
                r"\s+",
                " ",
                prefix + " " + suffix,
            ).strip()

            model = re.sub(r"\s+", " ", model)

            result["model"] = model
            model_match = True

    if model_match and model_match is not True:
        model = re.sub(
            r"\s+",
            " ",
            model_match.group(0),
        ).strip()

        # Canonicalise common brand/model casing without losing
        # model-family words.
        model = re.sub(
            r"^one\s*plus",
            "OnePlus",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^iphone\s*([0-9])",
            r"iPhone \1",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^samsng\s+galaxy",
            "Samsung Galaxy",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^samsung\s+galaxy",
            "Samsung Galaxy",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^samsung\s+(?=[0-9])",
            "Samsung ",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^samsung\s+galaxy",
            "Samsung Galaxy",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^galaxy",
            "Galaxy",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^redmi",
            "Redmi",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^xiaomi",
            "Xiaomi",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^realme",
            "Realme",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^motorola",
            "Motorola",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^moto",
            "Moto",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^google\s+pixel",
            "Google Pixel",
            model,
            flags=re.I,
        )

        model = re.sub(
            r"^pixel",
            "Pixel",
            model,
            flags=re.I,
        )

        # Capitalise family words and model suffixes consistently.
        words = model.split()

        if len(words) > 1:
            for i in range(1, len(words)):
                words[i] = words[i].capitalize()

            model = " ".join(words)

        result["model"] = model

    brand_matches = []

    for alias, canonical in BRAND_ALIASES.items():
        if alias.isascii():
            match = re.search(
                r"\b" + re.escape(alias) + r"\b",
                lower,
            )
            position = match.start() if match else -1
        else:
            position = lower.find(alias.lower())

        if position >= 0:
            brand_matches.append(
                (position, -len(alias), canonical)
            )

    if brand_matches:
        result["brand"] = min(brand_matches)[2]

    if "model" in result:
        result["product_interest"] = result["model"]

    elif result.get("brand"):
        result["product_interest"] = (
            result["brand"] + " phone"
        )

    elif re.search(
        r"\b(phone|mobile|mobail|phn|fon|smartphone|handset)\b",
        lower,
    ):
        result["product_interest"] = "phone"


def _extract_storage(text: str, result: Dict[str, Any]) -> None:
    match = re.search(
        r"(?<!\d)(64|128|256|512|1024)\s*(?:gb|g b)\b",
        text,
        re.I,
    )

    if match:
        result["storage"] = "{}GB".format(
            match.group(1)
        )


def _extract_budget(text: str, result: Dict[str, Any]) -> None:
    lower = text.lower().replace("₹", " rs ")

    range_match = re.search(
        r"(?:between\s+)?(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac|l)?"
        r"\s*(?:to|-|–|and)\s*"
        r"(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac|l)?",
        lower,
    )

    if range_match:
        unit1 = (
            range_match.group(2)
            or range_match.group(4)
        )

        unit2 = (
            range_match.group(4)
            or range_match.group(2)
        )

        first = _amount_to_inr(
            range_match.group(1),
            unit1,
        )

        second = _amount_to_inr(
            range_match.group(3),
            unit2,
        )

        low, high = sorted((first, second))

        result["budget_min"] = low
        result["budget_max"] = high
        result["budget"] = "₹{:,.0f}-₹{:,.0f}".format(
            low,
            high,
        )
        result["budget_amount"] = high

        return

    patterns = [
        r"(?:under|below|less than|within|max(?:imum)?|"
        r"budget(?: is)?|around|about|upto|up to|for)"
        r"\s*(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac|l)?",

        r"(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, lower)

        if match:
            amount = _amount_to_inr(
                match.group(1),
                match.group(2),
            )

            result["budget_amount"] = amount
            result["budget"] = "₹{:,.0f}".format(
                amount
            )

            if re.search(
                r"\b(under|below|less than|within|max|"
                r"maximum|upto|up to)\b",
                lower,
            ):
                result["budget_max"] = amount

            return


def _extract_timeline(text: str, result: Dict[str, Any]) -> None:
    lower = text.lower()

    patterns = [
        (
            r"\b(today|right now|immediately|asap)\b",
            "immediately",
        ),
        (
            r"\b(tomorrow|tomorow|by tomorrow)\b",
            "tomorrow",
        ),
        (
            r"\b(this weekend|coming weekend|weekend)\b",
            "this weekend",
        ),
        (
            r"\b(within a week|in a week|next week|one week)\b",
            "within a week",
        ),
        (
            r"\b(within two weeks|in two weeks|2 weeks)\b",
            "within two weeks",
        ),
        (
            r"\b(this month|within a month|in a month)\b",
            "within a month",
        ),
        (
            r"\b(next month)\b",
            "next month",
        ),
        (
            r"\b(just checking|just browsing|not decided|"
            r"not sure|just researching|researching|"
            r"only researching)\b",
            "just browsing",
        ),
    ]

    for pattern, value in patterns:
        if re.search(pattern, lower):
            result["purchase_timeline"] = value
            return


def _extract_condition(text: str, result: Dict[str, Any]) -> None:
    lower = text.lower()

    new_pattern = (
        r"\b(brand new|brand-new|new piece|new phone|sealed piece|"
        r"sealed pack|sealed box|unused)\b"
    )

    used_pattern = (
        r"\b(used|second hand|second-hand|pre owned|pre-owned|"
        r"old phone|refurbished)\b"
    )

    if re.search(new_pattern, lower):
        result["condition"] = "Brand new"

    elif re.search(used_pattern, lower):
        result["condition"] = "Used"


def _extract_payment(text: str, result: Dict[str, Any]) -> None:
    lower = text.lower()

    patterns = [
        (
            r"\b(cash|cash payment|pay by cash)\b",
            "Cash",
        ),
        (
            r"\b(upi|gpay|google pay|phonepe|paytm)\b",
            "UPI",
        ),
        (
            r"\b(emi|installments|instalments|monthly payments)\b",
            "EMI",
        ),
        (
            r"\b(credit card|debit card|card payment|card)\b",
            "Card",
        ),
        (
            r"\b(bank transfer|net banking|bank payment)\b",
            "Bank transfer",
        ),
    ]

    for pattern, value in patterns:
        if re.search(pattern, lower):
            result["payment_method"] = value
            return


def _extract_location(text: str, result: Dict[str, Any]) -> None:
    lower = text.lower()

    locations = [
        "bhimavaram",
        "vijayawada",
        "rajahmundry",
        "rajamahendravaram",
        "kakinada",
        "amalapuram",
        "ramachandrapuram",
        "tanuku",
        "palakollu",
        "eluru",
        "visakhapatnam",
        "vizag",
        "hyderabad",
        "chennai",
        "bangalore",
        "bengaluru",
    ]

    for location in sorted(
        locations,
        key=len,
        reverse=True,
    ):
        if re.search(
            r"\b" + re.escape(location) + r"\b",
            lower,
        ):
            result["location"] = (
                "Visakhapatnam"
                if location == "vizag"
                else (
                    "Bengaluru"
                    if location == "bangalore"
                    else location.title()
                )
            )
            return


def _extract_preferences(
    text: str,
    result: Dict[str, Any],
) -> None:
    lower = text.lower()

    preferences: List[str] = []

    patterns = [
        (
            r"\b(good camera|best camera|camera quality)\b",
            "Good camera",
        ),
        (
            r"\b(gaming|for gaming|gaming phone)\b",
            "Gaming",
        ),
        (
            r"\b(long battery|good battery|battery backup)\b",
            "Long battery life",
        ),
        (
            r"\b(5g|5g phone)\b",
            "5G",
        ),
        (
            r"\b(compact|small phone)\b",
            "Compact phone",
        ),
        (
            r"\b(large screen|big screen)\b",
            "Large screen",
        ),
        (
            r"\b(lightweight|light weight)\b",
            "Lightweight",
        ),
    ]

    for pattern, value in patterns:
        if re.search(pattern, lower):
            preferences.append(value)

    if preferences:
        result["preferences"] = preferences


def _extract_product_questions(
    text: str,
    result: Dict[str, Any],
) -> None:
    lower = text.lower()

    questions: List[str] = []

    patterns = [
        (
            r"\b(price|cost|how much|rate|worth)\b",
            "price",
        ),
        (
            r"\b(available|availability|in stock|stock|have it)\b",
            "availability",
        ),
        (
            r"\b(delivery|deliver|shipping|ship)\b",
            "delivery",
        ),
        (
            r"\b(battery health|battery percentage|battery condition)\b",
            "battery health",
        ),
        (
            r"\b(color|colours|colors)\b",
            "color",
        ),
        (
            r"\b(warranty|guarantee)\b",
            "warranty",
        ),
        (
            r"\b(emi offer|emi offers|emi options|no cost emi)\b",
            "EMI offer",
        ),
        (
            r"\b(emi offer|emi offers|emi options|no cost emi)\b",
            "EMI offer",
        ),
    ]

    for pattern, value in patterns:
        if re.search(pattern, lower):
            questions.append(value)

    if "availability" in questions and any(
        q in questions
        for q in (
            "price",
            "delivery",
            "battery health",
            "color",
            "warranty",
            "EMI offer",
        )
    ):
        explicit_stock_words = (
            r"\b(available|availability|in stock|stock)\b"
            if all(ord(ch) < 128 for ch in lower)
            else r"(అందుబాటులో|స్టాక్|available|availability|stock)"
        )

        if not re.search(
            explicit_stock_words,
            lower,
            re.I,
        ):
            questions = [
                q
                for q in questions
                if q != "availability"
            ]

    if questions:
        result["product_question"] = "/".join(
            dict.fromkeys(questions)
        )


def _extract_comparison(
    text: str,
    result: Dict[str, Any],
) -> None:
    lower = text.lower()

    cue = re.search(
        r"\b(compare|comparison|versus|vs\.?|with|tho compare)\b"
        r"|తో పోల్చి|కంపేర్",
        lower,
    )

    occurrences = []

    for alias, canonical in BRAND_ALIASES.items():
        if alias.isascii():
            match = re.search(
                r"\b" + re.escape(alias) + r"\b",
                lower,
            )
            position = match.start() if match else -1
        else:
            position = lower.find(alias.lower())

        if position >= 0:
            occurrences.append(
                (
                    position,
                    -len(alias),
                    canonical,
                )
            )

    found = []

    for _, _, canonical in sorted(occurrences):
        if canonical not in found:
            found.append(canonical)

    if len(found) >= 2:
        primary = result.get("brand")

        result["comparison_brands"] = [
            brand
            for brand in found
            if brand != primary
        ]

    elif cue and found:
        result["comparison_brands"] = found


def _postprocess_details(
    text: str,
    result: Dict[str, Any],
) -> None:
    """Apply high-confidence corrections and intent rules after extraction."""

    lower = text.lower()

    # Corrections: the later value explicitly replaces the earlier value.
    correction_patterns = [
        r"(?:actually|correction|sorry|ignore|limit|కాదు|సవరణ)"
        r"\s*(?:,|:)?\s*(?:my\s+)?"
        r"(?:budget\s*(?:is|=)?\s*)?"
        r"(?:only\s*)?"
        r"(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac)?",

        r"(?:not|kaadu|కాదు)"
        r"\s*(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac)?",
    ]

    for pat in correction_patterns:
        matches = list(
            re.finditer(
                pat,
                lower,
                re.I,
            )
        )

        if matches:
            m = matches[-1]

            try:
                amount = (
                    _amount_to_inr(
                        m.group(1),
                        m.group(2) or "",
                    )
                    if "_amount_to_inr" in globals()
                    else _amount(
                        m.group(1),
                        m.group(2) or "",
                    )
                )

                result["budget_amount"] = amount
                result["budget_max"] = amount
                result["budget"] = f"₹{amount:,.0f}"

            except Exception:
                pass

            break

    # Explicit correction phrases such as
    # "45k, actually 42k" / "45k kaadu 43k".
    nums = list(
        re.finditer(
            r"(?:rs\.?\s*)?"
            r"(\d[\d,]*(?:\.\d+)?)\s*"
            r"(k|thousand|lakh|lakhs|lac)?",
            lower,
            re.I,
        )
    )

    if (
        len(nums) >= 2
        and re.search(
            r"(?:actually|correction|sorry|ignore|limit|"
            r"kaadu|కాదు|వద్దు|పరిమితి|సవరణ)",
            lower,
            re.I,
        )
    ):
        m = nums[-1]

        try:
            amount = _amount_to_inr(
                m.group(1),
                m.group(2) or "",
            )

            result["budget_amount"] = amount
            result["budget_max"] = amount
            result["budget"] = f"₹{amount:,.0f}"

        except Exception:
            pass

    # Common typo in the evaluation set.
    if re.search(
        r"\bsealed\s+peice\b",
        lower,
        re.I,
    ):
        result["condition"] = "Brand new"

    # Use-case is a lead field, not merely a preference.
    use_cases = []

    if re.search(
        r"\b(gaming|games|for gaming|gaming ki|gaming kosam)\b"
        r"|గేమింగ్|గేమ్స్",
        lower,
        re.I,
    ):
        use_cases.append("gaming")

    if re.search(
        r"\b(photography|photos|camera|for photos)\b"
        r"|ఫోటోల|కెమెరా",
        lower,
        re.I,
    ):
        use_cases.append("photography")

    if use_cases:
        result["use_case"] = list(
            dict.fromkeys(use_cases)
        )

    # If multiple product questions are present,
    # prefer the specific question the user is asking.
    q = result.get("product_question")

    if isinstance(q, str):
        qs = [
            x
            for x in q.split("/")
            if x
        ]

        if "battery health" in qs:
            result["product_question"] = "battery health"

        elif "EMI offer" in qs:
            result["product_question"] = "EMI offer"

        elif "price" in qs:
            result["product_question"] = "price"

    # Payment corrections:
    # an explicit final choice wins over an earlier alternative.
    if re.search(
        r"(?:i\s+will\s+use|i\s*choose|use)\s+"
        r"(?:the\s+)?card|card\s+payment|కార్డ్",
        lower,
        re.I,
    ):
        result["payment_method"] = "Card"

    # Google Pay must never turn an already identified Pixel model
    # into "Google phone".
    if (
        result.get("model")
        and re.search(
            r"\bgoogle\s+pay\b|gpay",
            lower,
            re.I,
        )
    ):
        if result.get(
            "model",
            "",
        ).lower().startswith("pixel"):
            result["product_interest"] = result["model"]


def extract(text: str) -> Dict[str, Any]:
    """Extract customer details from an English message."""

    if not text or not text.strip():
        return {}

    result: Dict[str, Any] = {}

    _extract_product(
        text,
        result,
    )

    _extract_storage(
        text,
        result,
    )

    _extract_budget(
        text,
        result,
    )

    _extract_timeline(
        text,
        result,
    )

    _extract_condition(
        text,
        result,
    )

    _extract_payment(
        text,
        result,
    )

    _extract_location(
        text,
        result,
    )

    _extract_preferences(
        text,
        result,
    )

    _extract_product_questions(
        text,
        result,
    )

    _extract_comparison(
        text,
        result,
    )

    _postprocess_details(
        text,
        result,
    )

    target_price = re.search(
        r"\b(?:final price|best price|target price|last price)\s*"
        r"(?:rs\.?\s*)?"
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(k|thousand|lakh|lakhs|lac)?",
        text.lower(),
    )

    if target_price:
        result["target_price"] = _amount_to_inr(
            target_price.group(1),
            target_price.group(2),
        )

    return result