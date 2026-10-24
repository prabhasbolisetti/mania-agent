
"""Cost-conscious, tolerant product retrieval for Mania Mobiles.

Supabase is the production source of truth. Local JSON is supported only when
explicitly selected or when tests replace CATALOG_PATH. Search ranks partial
matches rather than requiring every word in a long WhatsApp message to occur
in the product title. Prices and stock are never verified by inference.
"""
from __future__ import annotations

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
PAGE_SIZE = 500
CACHE_TTL_SECONDS = max(5, int(os.getenv("PRODUCT_CATALOG_CACHE_TTL", "30")))

_cache_lock = threading.Lock()
_cached_products: Optional[list[dict]] = None
_cache_expiry = 0.0

_STOPWORDS = {
    "a", "an", "the", "for", "please", "want", "need", "looking", "search",
    "show", "me", "have", "do", "you", "is", "are", "price", "prices", "cost",
    "much", "how", "what", "available", "availability", "stock", "in", "at", "with",
    "under", "below", "upto", "up", "to", "less", "than", "budget", "my", "buy",
    "purchase", "tell", "give", "compare", "between", "which", "phone", "mobile",
    "phones", "mobiles", "second", "hand", "used", "preowned", "pre", "owned",
    "branded", "brand", "battery", "health", "above", "over", "more", "percent",
    "percentage", "kavali", "undha", "unda", "entha", "rate", "cheppu", "chepu",
    "cheppandi", "ధర", "ఎంత", "కావాలి", "ఉందా", "దొరుకుతుందా", "లోపు", "వరకు",
}

_CONDITION_ALIASES = {
    "used": {"used", "second hand", "pre owned", "preowned", "refurbished"},
    "second hand": {"used", "second hand", "pre owned", "preowned", "refurbished"},
    "pre owned": {"used", "second hand", "pre owned", "preowned", "refurbished"},
    "refurbished": {"used", "second hand", "pre owned", "preowned", "refurbished"},
    "new": {"new", "brand new", "sealed"},
    "brand new": {"new", "brand new", "sealed"},
}


def _normalise_stock_status(value: Any) -> str:
    status = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    if status in {"instock", "in_stock", "available", "available_now", "yes"}:
        return "in_stock"
    if status in {"outofstock", "out_of_stock", "unavailable", "sold_out", "no"}:
        return "out_of_stock"
    return "unknown"


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _normalise_supabase_product(row: dict) -> dict:
    """Map Supabase product columns to the stable shape used by the agent."""
    title = str(
        row.get("title") or row.get("product_name") or row.get("model") or ""
    ).strip()
    model = str(row.get("model") or title).strip()
    attributes = _as_dict(row.get("attributes"))
    specifications = _as_dict(row.get("specifications"))
    variants = _as_list(row.get("variants"))

    # Catalog fields may live in columns, attributes, or specifications.
    for key in (
        "storage", "color", "battery_health", "condition",
        "ram", "network", "battery_health_percent",
    ):
        for source in (row, attributes, specifications):
            value = source.get(key)
            if value not in (None, ""):
                attributes.setdefault(key, value)
                specifications.setdefault(key, value)
                break

    product = dict(row)
    product.update({
        "product_name": title or "Unknown product",
        "display_name": title or "Unknown product",
        "model": model,
        "brand": row.get("brand"),
        "sku": row.get("sku"),
        "price_inr": row.get("price"),
        "original_price_inr": row.get("original_price"),
        # A populated price is not proof that it has been verified.
        "price_needs_verification": row.get("price_verified") is not True,
        "stock_status": _normalise_stock_status(row.get("stock_status")),
        "product_url": row.get("product_url") or row.get("url"),
        "image_url": row.get("image_url"),
        "description": row.get("description") or "",
        "short_description": row.get("short_description") or "",
        "attributes": attributes,
        "specifications": specifications,
        "variants": variants,
        "categories": (
            row.get("categories")
            if isinstance(row.get("categories"), list)
            else ([row.get("category")] if row.get("category") else [])
        ),
        "aliases": row.get("aliases") or [],
    })
    return product


def _load_json_products() -> list[dict]:
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Product catalog not found: {CATALOG_PATH}")
    with CATALOG_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("Expected mobiles.json to contain a JSON list.")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _load_supabase_products() -> list[dict]:
    products: list[dict] = []
    offset = 0

    while True:
        response = (
            supabase.table("products")
            .select("*")
            .order("id")
            .range(offset, offset + PAGE_SIZE - 1)
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
        if len(rows) < PAGE_SIZE:
            return products
        offset += PAGE_SIZE


def invalidate_product_cache() -> None:
    """Clear the process-local catalog cache after a product update."""
    global _cached_products, _cache_expiry
    with _cache_lock:
        _cached_products = None
        _cache_expiry = 0.0


def load_products() -> list[dict]:
    """Load products with a short cache; don't silently fall back to stale JSON."""
    global _cached_products, _cache_expiry

    source = os.getenv("PRODUCT_CATALOG_SOURCE", "supabase").strip().lower()
    if source == "json" or Path(CATALOG_PATH) != DEFAULT_CATALOG_PATH:
        return _load_json_products()
    if source != "supabase":
        raise ValueError("PRODUCT_CATALOG_SOURCE must be either 'supabase' or 'json'.")

    now = time.monotonic()
    if _cached_products is not None and now < _cache_expiry:
        return [dict(item) for item in _cached_products]

    # Double-check under the lock to avoid duplicate concurrent catalog loads.
    with _cache_lock:
        now = time.monotonic()
        if _cached_products is not None and now < _cache_expiry:
            return [dict(item) for item in _cached_products]

        products = _load_supabase_products()
        _cached_products = products
        _cache_expiry = time.monotonic() + CACHE_TTL_SECONDS
        return [dict(item) for item in products]


def _normalise_text(value: Any) -> str:
    text = str(value or "").lower().replace("+", " plus ")
    text = re.sub(r"\b(\d+)\s*(?:gb|g b)\b", r"\1gb", text)
    text = re.sub(r"\b(\d+)\s*(?:tb|t b)\b", r"\1tb", text)
    text = re.sub(r"\b(\d+)\s*%", r"\1 percent", text)
    return re.sub(r"[^a-z0-9\u0c00-\u0c7f]+", " ", text).strip()


def _flatten(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            output.append(str(key))
            output.extend(_flatten(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            output.extend(_flatten(item))
    elif value is not None:
        output.append(str(value))
    return output


def _product_search_text(product: dict) -> str:
    fields = [
        product.get("product_name"), product.get("title"), product.get("brand"),
        product.get("model"), product.get("sku"), product.get("short_description"),
        product.get("description"), product.get("display_name"), product.get("product_url"),
        product.get("category"), product.get("condition"), product.get("battery_health"),
        product.get("storage"), product.get("color"), product.get("ram"),
    ]
    for key in ("categories", "aliases", "attributes", "specifications", "variants"):
        fields.extend(_flatten(product.get(key)))
    return _normalise_text(" ".join(str(value) for value in fields if value is not None))


def _query_tokens(query: str) -> list[str]:
    cleaned = str(query or "")
    cleaned = re.sub(
        r"(?:₹|\brs\.?\s*)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:k|l|lac|lakh|lakhs))?",
        " ", cleaned, flags=re.I,
    )
    cleaned = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:k|l|lac|lakh|lakhs)\b",
        " ", cleaned, flags=re.I,
    )
    cleaned = re.sub(
        r"\b(?:under|below|upto|up to|less than|budget)\s*\d[\d,]*(?:\.\d+)?\b",
        " ", cleaned, flags=re.I,
    )

    tokens: list[str] = []
    for token in _normalise_text(cleaned).split():
        if token not in _STOPWORDS and token not in tokens:
            tokens.append(token)
    return tokens


def _get_price(product: dict) -> Optional[float]:
    value = product.get("price_inr", product.get("price"))
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if price >= 0 else None


def _price_is_verified(product: dict) -> bool:
    return (
        product.get("price_needs_verification") is False
        and _get_price(product) is not None
    )


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _attribute(product: dict, *names: str) -> Any:
    sources = [
        product,
        _as_dict(product.get("attributes")),
        _as_dict(product.get("specifications")),
    ]
    aliases = {
        "condition": ("condition", "item_condition", "device_condition", "product_condition"),
        "battery_health": (
            "battery_health", "battery", "battery_percentage",
            "battery_health_percent",
        ),
        "storage": ("storage", "storage_capacity", "memory", "rom"),
    }
    for name in names:
        for source in sources:
            for key in aliases.get(name, (name,)):
                if source.get(key) not in (None, ""):
                    return source[key]
    return None


def get_available_variants(product: dict) -> list[dict]:
    variants = product.get("variants") or []
    if not isinstance(variants, list):
        return []
    return [
        item for item in variants
        if isinstance(item, dict) and item.get("in_stock") is True
    ]


def get_effective_stock_status(product: dict) -> str:
    if get_available_variants(product):
        return "in_stock"
    return _normalise_stock_status(product.get("stock_status"))


def search_products(
    query: str = "",
    max_price: Optional[float] = None,
    in_stock_only: bool = False,
    limit: int = 10,
    min_battery_health: Optional[float] = None,
    condition: Optional[str] = None,
    brand: Optional[str] = None,
) -> list[dict]:
    """Search and rank catalog products using tolerant text matching.

    Explicit filters remain strict. For example, min_battery_health=90 returns
    only products with a recorded battery-health value strictly above 90%.
    """
    products = load_products()
    raw_query = _normalise_text(query)
    tokens = _query_tokens(query)
    query_compact = raw_query.replace(" ", "")
    matches: list[tuple[float, dict]] = []

    non_identity_terms = {
        "second", "hand", "used", "preowned", "pre", "owned", "branded",
        "brand", "battery", "health", "above", "over", "more", "percent",
        "under", "below", "upto", "less", "than", "budget", "compare",
        "between", "condition", "new", "excellent", "good", "fair",
    }
    identity_tokens = [token for token in tokens if token not in non_identity_terms]

    for product in products:
        search_text = _product_search_text(product)
        search_compact = search_text.replace(" ", "")
        name = _normalise_text(
            product.get("product_name") or product.get("title") or product.get("model")
        )
        name_tokens = set(name.split())
        text_tokens = set(search_text.split())

        if identity_tokens:
            hits = sum(
                1 for token in identity_tokens
                if token in text_tokens or token in search_compact
            )
            # Long conversational messages need not match every preference word.
            # Require a meaningful identity overlap, not an exact full sentence.
            if hits == 0 or hits / len(identity_tokens) < 0.60:
                continue

        price = _get_price(product)
        if max_price is not None:
            if not _price_is_verified(product) or price is None or price > max_price:
                continue
        if in_stock_only and get_effective_stock_status(product) != "in_stock":
            continue

        if min_battery_health is not None:
            battery = _number(_attribute(product, "battery_health"))
            if battery is None or battery <= min_battery_health:
                continue

        if condition:
            actual = _normalise_text(_attribute(product, "condition"))
            requested = _normalise_text(condition)
            allowed = _CONDITION_ALIASES.get(requested, {requested})
            if not actual or not any(alias in actual for alias in allowed):
                continue

        if brand and _normalise_text(brand) not in search_text:
            continue

        score = 0.0
        if raw_query and raw_query in name:
            score += 100
        if query_compact and len(query_compact) >= 6 and query_compact in search_compact:
            score += 45
        score += sum(8 for token in tokens if token in name_tokens)
        score += sum(2 for token in tokens if token in text_tokens)

        model = _normalise_text(product.get("model"))
        if model and model in raw_query:
            score += 20
        if condition and _attribute(product, "condition") is not None:
            score += 5
        if min_battery_health is not None:
            score += 5

        matches.append((score, product))

    matches.sort(
        key=lambda item: (
            -item[0],
            _normalise_text(item[1].get("product_name")),
        )
    )
    try:
        safe_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        safe_limit = 10
    return [product for _, product in matches[:safe_limit]]


def get_product_by_url(url: str) -> Optional[dict]:
    if not url:
        return None
    return next(
        (product for product in load_products() if product.get("product_url") == url),
        None,
    )


def format_product(product: dict) -> str:
    name = product.get("product_name") or product.get("title") or "Unknown product"
    price = _get_price(product)
    price_text = (
        f"₹{price:,.0f} (catalogue price; confirm current price)"
        if _price_is_verified(product) and price is not None
        else "Please confirm the current price with the store"
    )
    stock = get_effective_stock_status(product)
    stock_text = {
        "in_stock": "Listed in stock in the catalogue snapshot; confirm exact variant",
        "out_of_stock": "Listed out of stock in the catalogue snapshot; confirm with the store",
        "unknown": "Current availability needs confirmation",
    }[stock]

    lines = [
        f"Product: {name}",
        f"Price: {price_text}",
        f"Availability: {stock_text}",
    ]
    for label, key in (
        ("Storage", "storage"),
        ("Color", "color"),
        ("Condition", "condition"),
        ("Battery health", "battery_health"),
    ):
        value = _attribute(product, key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    description = product.get("short_description") or product.get("description")
    if description:
        lines.append(
            "Details: " + re.sub(r"\s+", " ", str(description)).strip()[:400]
        )

    for variant in get_available_variants(product)[:6]:
        attrs = variant.get("attributes") or {}
        if isinstance(attrs, dict) and attrs:
            details = ", ".join(
                f"{str(key).replace('_', ' ')}: {value}"
                for key, value in attrs.items()
            )
            lines.append(f"Variant: {details}")

    lines.append(f"Product URL: {product.get('product_url') or 'Unavailable'}")
    return "\n".join(lines)
