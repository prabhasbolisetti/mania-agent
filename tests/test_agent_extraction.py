from app.agent.llm import extract_customer_details


def test_english_multi_field_message_extracts_core_qualification_details():
    result = extract_customer_details(
        "I want iPhone 15 128GB under 45k, sealed, buy this weekend in Bhimavaram, cash",
        {},
    )

    assert result["product_interest"] == "iPhone 15"
    assert result["storage"] == "128GB"
    assert result["budget_max"] == 45000
    assert result["purchase_timeline"] == "this weekend"
    assert result["condition"] == "brand new"
    assert result["payment_method"] == "cash"
    assert result["location"] == "Bhimavaram"


def test_roman_telugu_short_message_extracts_budget_and_payment():
    result = extract_customer_details(
        "budget 40k, next week konali, brand new kavali, cash",
        {},
    )

    assert result["budget_amount"] == 40000
    assert result["purchase_timeline"] == "within a week"
    assert result["condition"] == "brand new"
    assert result["payment_method"] == "cash"


def test_telugu_script_condition_and_payment_survive_validation():
    result = extract_customer_details(
        "నా బడ్జెట్ 45000, ఈ వారం కొనాలి, కొత్తది కావాలి, క్యాష్",
        {},
    )

    assert result["budget_amount"] == 45000
    assert result["purchase_timeline"] == "within a week"
    assert result["condition"] == "brand new"
    assert result["payment_method"] == "cash"


def test_not_interested_is_not_treated_as_a_purchase_commitment():
    result = extract_customer_details("not interested, just checking price", {})

    assert result.get("_negative") is True
    assert result.get("product_interest") is None
