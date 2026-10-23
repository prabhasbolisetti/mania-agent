"""Safe product catalogue lookup for the Mania Mobiles agent.

Supabase is the primary production source. Local JSON remains available for
tests and explicitly selected development mode.

Prices and stock are treated conservatively: a populated database column does
not itself prove that a price or availability is current.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.database.supabase_client import supabase


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "mobiles.json"
CATALOG_PATH = DEFAULT_CATALOG_PATH

_PAGE_SIZE = 500
_CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()
_cached_products = None
_cache_expiry = 0.0

_SEARCH_STOPWORDS = {
    "a", "an", "the", "for", "please", "want", "need", "looking", "search",
    "show", "me", "have", "do", "you", "is", "are", "price", "cost", "much",
    "how", "what", "available", "availability", "stock", "in", "at", "with",
    "under", "below", "upto", "up", "to", "less", "than", "budget", "my",
    "buy", "purchase", "kavali", "undha", "unda", "entha", "rate",
    "ధర", "ఎంత", "కావాలి", "ఉందా", "దొరుకుతుందా", "లోపు", "వరకు",
}


def _normalise_stock_status(value: Any) -> str:
    """Normalize common inventory labels without claiming live availability."""
    status = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())

    if status in {
        "instock", "in_stock", "available", "available_now",
        "in stock", "yes",
    }:
        return "in_stock"

    if status in {
        "outofstock", "out_of_stock", "out_ofstock", "unavailable",
        "out of stock", "sold_out",
    }:
        return "out_of_stock"

    return "unknown"


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    return {}


def _normalise_supabase_product(row: dict) -> dict:
    """Map the Supabase products schema to the agent's existing data shape."""
    title = str(row.get("title") or row.get("model") or "").strip()
    model = str(row.get("model") or title).strip()

    specifications = _as_dict(row.get("specifications"))
    attributes = _as_dict(row.get("attributes"))

    for key in ("color", "storage", "battery_health", "condition"):
        value = row.get(key)
        if value is not None and str(value).strip():
            attributes.setdefault(key, value)

    # A Supabase price is verified only when explicitly marked true.
    # Do not infer verification from a populated price or original_price.
    price_needs_verification = row.get("price_verified") is not True

    raw_stock = row.get("stock_status")
    stock_status = _normalise_stock_status(raw_stock)

    product = dict(row)
    product.update({
        "product_name": title or "Unknown product",
        "display_name": title or "Unknown product",
        "price_inr": row.get("price"),
        "price_needs_verification": price_needs_verification,
        "stock_status": stock_status,
        "model": model,
        "brand": row.get("brand"),
        "sku": row.get("sku"),
        "product_url": row.get("product_url"),
        "description": row.get("description") or "",
        "short_description": row.get("short_description") or "",
        "image_url": row.get("image_url"),
        "attributes": attributes,
        "specifications": specifications,
        "categories": [row.get("category")] if row.get("category") else [],
        "aliases": [],
        "variants": [],
    })

    # Keep the original listed price for internal inspection, but never
    # promote original_price into a verified price or claim a discount.
    product["original_price_inr"] = row.get("original_price")
    return product


def _load_json_products() -> list:
    """Load the local JSON catalogue, primarily for tests and development."""
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Product catalog not found: {CATALOG_PATH}")

    with CATALOG_PATH.open("r", encoding="utf-8") as file:
        products = json.load(file)

    if not isinstance(products, list):
        raise ValueError("Expected mobiles.json to contain a JSON list.")

    return [item for item in products if isinstance(item, dict)]


def _load_supabase_products() -> list:
    """Read all product rows in pages; errors are surfaced to the caller."""
    products = []
    offset = 0

    while True:
        response = (
            supabase.table("products")
            .select("*")
            .order("id")
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )

        rows = getattr(response, "data", None)
        if not isinstance(rows, list):
            raise RuntimeError("Supabase returned an invalid products response.")

        products.extend(
            _normalise_supabase_product(row)
            for row in rows
            if isinstance(row, dict)
        )

        if len(rows) < _PAGE_SIZE:
            break

        offset += _PAGE_SIZE

    return products


def load_products() -> list:
    """Load products from the configured source.

    PRODUCT_CATALOG_SOURCE=json explicitly selects JSON. Tests that monkeypatch
    CATALOG_PATH automatically use JSON. In production, a Supabase error is
    raised instead of silently serving potentially stale local data.
    """
    global _cached_products, _cache_expiry

    source = os.getenv("PRODUCT_CATALOG_SOURCE", "supabase").strip().lower()

    # Keep existing tests and explicit local-development mode compatible.
    if source == "json" or Path(CATALOG_PATH) != DEFAULT_CATALOG_PATH:
        return _load_json_products()

    if source != "supabase":
        raise ValueError(
            "PRODUCT_CATALOG_SOURCE must be either 'supabase' or 'json'."
        )

    now = time.monotonic()

    with _cache_lock:
        if _cached_products is not None and now < _cache_expiry:
            return [dict(item) for item in _cached_products]

        products = _load_supabase_products()
        _cached_products = products
        _cache_expiry = time.monotonic() + _CACHE_TTL_SECONDS
        return [dict(item) for item in products]


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

    return _normalise_stock_status(product.get("stock_status"))


def _get_price(product: dict) -> Optional[float]:
    """Return a non-negative numeric price, or None if invalid."""
    price = product.get("price_inr")
    if price is None:
        return None

    try:
        price = float(price)
    except (TypeError, ValueError):
        return None

    return price if price >= 0 else None


def _price_is_verified(product: dict) -> bool:
    """Only trust prices explicitly marked verified."""
    return (
        product.get("price_needs_verification") is False
        and _get_price(product) is not None
    )


def _normalise_text(value: Any) -> str:
    text = str(value or "").lower().replace("+", " plus ")
    return re.sub(r"[^a-z0-9\u0c00-\u0c7f]+", " ", text).strip()


def _product_search_text(product: dict) -> str:
    fields = [
        product.get("product_name"),
        product.get("title"),
        product.get("brand"),
        product.get("model"),
        product.get("sku"),
        product.get("short_description"),
        product.get("description"),
        product.get("display_name"),
        product.get("aliases"),
        product.get("category"),
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
    # Remove currency amounts while preserving model numbers such as iPhone 15.
    cleaned = re.sub(
        r"(?:₹|\brs\.?\s*)\s*\d[\d,]*(?:\.\d+)?"
        r"(?:\s*(?:k|l|lac|lakh|lakhs))?",
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
        r"\b(?:under|below|upto|up to|less than|budget)\s*"
        r"\d[\d,]*(?:\.\d+)?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    text = _normalise_text(cleaned)
    tokens = []

    for token in text.split():
        if token not in _SEARCH_STOPWORDS and token not in tokens:
            tokens.append(token)

    return tokens


def search_products(
    query: str = "",
    max_price: Optional[float] = None,
    in_stock_only: bool = False,
    limit: int = 10,
) -> list:
    """Search products with forgiving token matching and exact-name ranking.

    Budget filtering intentionally excludes unverified prices.
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
        elif tokens and not all(
            token in search_text.split() or token in search_text
            for token in tokens
        ):
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

        model = product.get("model")
        if model and _normalise_text(model) in raw_query:
            score += 15

        matches.append((score, product))

    matches.sort(
        key=lambda pair: (
            -pair[0],
            _normalise_text(pair[1].get("product_name")),
        )
    )

    safe_limit = max(1, min(int(limit), 100))
    return [product for _, product in matches[:safe_limit]]


def get_product_by_url(url: str) -> Optional[dict]:
    """Retrieve a product using its exact catalogue URL."""
    if not url:
        return None

    for product in load_products():
        if product.get("product_url") == url:
            return product

    return None


def format_product(product: dict) -> str:
    """Format a cautious summary using trustworthy catalogue fields."""
    name = product.get("product_name") or "Unknown product"
    price = _get_price(product)

    if _price_is_verified(product) and price is not None:
        price_text = f"₹{price:,.0f} (catalogue snapshot; confirm current price)"
    else:
        price_text = "Please confirm the current price with the store"

    stock = get_effective_stock_status(product)
    stock_labels = {
        "in_stock": (
            "Listed in stock in the catalogue snapshot; confirm exact variant"
        ),
        "out_of_stock": (
            "Listed as out of stock in the catalogue snapshot; confirm with the store"
        ),
        "unknown": "Current availability needs confirmation",
    }

    lines = [
        f"Product: {name}",
        f"Price: {price_text}",
        f"Availability: {stock_labels[stock]}",
    ]

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
            if (
                variant_price is not None
                and variant.get("price_needs_verification") is False
            ):
                try:
                    details += f" | Variant price: ₹{float(variant_price):,.0f}"
                except (TypeError, ValueError):
                    pass

            if details:
                lines.append(f"- {details}")

    lines.append(f"Product URL: {product.get('product_url') or 'Unavailable'}")
    return "\n".join(lines)
