import json

from app.catalog import product_lookup


def test_search_matches_tokens_and_ranks_exact_product_name(tmp_path, monkeypatch):
    products = [
        {
            "product_name": "Apple iPad 9th Gen 64GB WiFi",
            "product_url": "https://example.test/ipad",
            "price_inr": 25000,
            "price_needs_verification": False,
            "stock_status": "in_stock",
            "attributes": {"storage": "64GB"},
        },
        {
            "product_name": "Apple iPad Pro 128GB WiFi",
            "product_url": "https://example.test/ipad-pro",
            "price_inr": 65000,
            "price_needs_verification": False,
            "stock_status": "in_stock",
            "attributes": {"storage": "128GB"},
        },
    ]
    catalog_path = tmp_path / "mobiles.json"
    catalog_path.write_text(json.dumps(products), encoding="utf-8")
    monkeypatch.setattr(product_lookup, "CATALOG_PATH", catalog_path)

    result = product_lookup.search_products("I need iPad Pro 128GB under ₹65,000", limit=5)

    assert len(result) == 1
    assert result[0]["product_name"] == "Apple iPad Pro 128GB WiFi"


def test_price_filter_excludes_unverified_price(tmp_path, monkeypatch):
    products = [
        {
            "product_name": "Device A",
            "product_url": "https://example.test/a",
            "price_inr": 1000,
            "price_needs_verification": True,
            "stock_status": "in_stock",
        },
        {
            "product_name": "Device B",
            "product_url": "https://example.test/b",
            "price_inr": 2000,
            "price_needs_verification": False,
            "stock_status": "in_stock",
        },
    ]
    catalog_path = tmp_path / "mobiles.json"
    catalog_path.write_text(json.dumps(products), encoding="utf-8")
    monkeypatch.setattr(product_lookup, "CATALOG_PATH", catalog_path)

    result = product_lookup.search_products(max_price=1500)

    assert result == []


def test_unverified_product_summary_never_shows_stale_price():
    summary = product_lookup.format_product({
        "product_name": "Test Phone",
        "price_inr": 1000,
        "displayed_price": "₹99,999",
        "price_needs_verification": True,
        "stock_status": "unknown",
        "product_url": "https://example.test/product",
    })

    assert "Please confirm the current price" in summary
    assert "99,999" not in summary
