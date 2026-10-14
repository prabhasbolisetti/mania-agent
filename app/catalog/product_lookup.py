"""Safe product catalogue lookup for the Mania Mobiles agent.

The catalogue is a snapshot, not a live inventory API. Search is forgiving of
WhatsApp phrasing, but price filtering only uses prices explicitly marked as
verified in the snapshot.
"""
import json
import re
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "data" / "mobiles.json"

_SEARCH_STOPWORDS = {
    "a", "an", "the", "for", "please", "want", "need", "looking", "search",
    "show", "me", "have", "do", "you", "is", "are", "price", "cost", "much",
    "how", "what", "available", "availability", "stock", "in", "at", "with",
    "under", "below", "upto", "up", "to", "less", "than", "budget", "my",
    "buy", "purchase", "kavali", "kavాలి", "undha", "unda", "entha", "rate",
    "ధర", "ఎంత", "కావాలి", "ఉందా", "దొరుకుతుందా", "లోపు", "వరకు",
}


def load_products() -> list:
    """Load and validate product records from the local JSON catalogue."""
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Product catalog not found: {CATALOG_PATH}")
    with CATALOG_PATH.open("r", encoding="utf-8") as file:
        products = json.load(file)
    if not isinstance(products, list):
        raise ValueError("Expected mobiles.json to contain a JSON list.")
    return [product for product in products if isinstance(product, dict)]


def get_available_variants(product: dict) -> list:
    """Return variants explicitly marked as in stock."""
    variants = product.get("variants") or []
    if not isinstance(variants, list):
        return []
    return [
        variant for variant in variants
        if isinstance(variant, dict) and variant.get("in_stock") is True
    ]


def get_effective_stock_status(product: dict) -> str:
    """Resolve catalogue stock without pretending it is live stock."""
    if get_available_variants(product):
        return "in_stock"
    status = str(product.get("stock_status") or "unknown").strip().lower()
    return status if status in {"in_stock", "out_of_stock"} else "unknown"


def _get_price(product: dict) -> Optional[float]:
    """Return a non-negative numeric price, or None if it is invalid."""
    price = product.get("price_inr")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _price_is_verified(product: dict) -> bool:
    """Only trust prices explicitly marked verified in the snapshot."""
    return product.get("price_needs_verification") is False and _get_price(product) is not None


def _normalise_text(value: Any) -> str:
    text = str(value or "").lower().replace("+", " plus ")
    return re.sub(r"[^a-z0-9\u0c00-\u0c7f]+", " ", text).strip()


def _product_search_text(product: dict) -> str:
    fields = [
        product.get("product_name"), product.get("brand"), product.get("model"),
        product.get("sku"), product.get("short_description"), product.get("description"),
        product.get("display_name"), product.get("aliases"),
    ]
    for key in ("categories", "attributes", "specifications", "variants"):
        value = product.get(key)
        if isinstance(value, dict):
            fields.extend(value.keys())
            fields.extend(value.values())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    fields.extend(item.keys())
                    fields.extend(item.values())
                else:
                    fields.append(item)
        elif value is not None:
            fields.append(value)
    return _normalise_text(" ".join(str(v) for v in fields if v is not None))


def _query_tokens(query: str) -> list:
    # Strip currency/budget amounts without stripping model numbers like iPhone 15.
    cleaned = re.sub(
        r"(?:₹|\brs\.?\s*)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:k|l|lac|lakh|lakhs))?",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:k|l|lac|lakh|lakhs)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:under|below|upto|up to|less than|budget)\s*\d[\d,]*(?:\.\d+)?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    text = _normalise_text(cleaned)
    tokens = []
    for token in text.split():
        if token in _SEARCH_STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def search_products(
    query: str = "",
    max_price: Optional[float] = None,
    in_stock_only: bool = False,
    limit: int = 10,
) -> list:
    """Search product names/specs with forgiving token matching.

    Every meaningful query token must be represented in a result, preventing
    broad unrelated matches. Exact phrase matches rank above token matches.
    """
    products = load_products()
    raw_query = _normalise_text(query)
    tokens = _query_tokens(query)
    matches = []

    for product in products:
        search_text = _product_search_text(product)
        if raw_query and not tokens:
            if raw_query not in search_text:
                continue
        elif tokens and not all(token in search_text.split() or token in search_text for token in tokens):
            continue

        price = _get_price(product)
        verified = _price_is_verified(product)
        if max_price is not None:
            if not verified or price is None or price > max_price:
                continue
        if in_stock_only and get_effective_stock_status(product) != "in_stock":
            continue

        name = _normalise_text(product.get("product_name"))
        score = 0
        if raw_query and raw_query in name:
            score += 100
        if tokens:
            score += sum(5 for token in tokens if token in name.split())
            score += sum(2 for token in tokens if token in search_text)
        if product.get("model") and _normalise_text(product["model"]) in raw_query:
            score += 15
        matches.append((score, product))

    matches.sort(key=lambda pair: (-pair[0], _normalise_text(pair[1].get("product_name"))))
    return [product for _, product in matches[:max(1, min(int(limit), 100))]]


def get_product_by_url(url: str) -> Optional[dict]:
    """Retrieve a product using its exact catalogue URL."""
    for product in load_products():
        if product.get("product_url") == url:
            return product
    return None


def format_product(product: dict) -> str:
    """Format a cautious summary using only trustworthy catalogue fields."""
    name = product.get("product_name") or "Unknown product"
    price = _get_price(product)
    if _price_is_verified(product) and price is not None:
        price_text = f"₹{price:,.0f} (catalogue snapshot; confirm current price)"
    else:
        price_text = "Please confirm the current price with the store"

    stock = get_effective_stock_status(product)
    stock_labels = {
        "in_stock": "Listed in stock in the catalogue snapshot; confirm exact variant",
        "out_of_stock": "Listed as out of stock in the catalogue snapshot; confirm with the store",
        "unknown": "Current availability needs confirmation",
    }
    lines = [f"Product: {name}", f"Price: {price_text}", f"Availability: {stock_labels[stock]}"]

    description = product.get("short_description") or product.get("description")
    if description:
        clean_description = re.sub(r"\s+", " ", str(description)).strip()
        if clean_description:
            lines.append(f"Details: {clean_description[:400]}")

    variants = get_available_variants(product)
    if variants:
        lines.append("Available catalogue variants (confirm with store):")
        for variant in variants[:8]:
            attributes = variant.get("attributes") or {}
            details = ", ".join(
                f"{str(key).replace('attribute_pa_', '').replace('_', ' ')}: {value}"
                for key, value in attributes.items()
            ) if isinstance(attributes, dict) else ""
            variant_price = variant.get("price_inr")
            if variant_price is not None and variant.get("price_needs_verification") is False:
                try:
                    details += f" | Variant price: ₹{float(variant_price):,.0f}"
                except (TypeError, ValueError):
                    pass
            if details:
                lines.append(f"- {details}")
    lines.append(f"Product URL: {product.get('product_url') or 'Unavailable'}")
    return "\n".join(lines)
