
"""Authenticated product management and campaign-preview endpoints.

Supports:
- Product listing and editing
- Unique-customer brand and product interest counts
- Campaign audience previews
- WhatsApp consent and messaging-window eligibility checks

Actual campaign sending remains disabled until the existing WhatsApp sender,
consent capture, opt-out handling, approved templates, rate limiting, and
delivery tracking have been integrated and tested.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.auth import get_current_admin
from app.database.supabase_client import supabase

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/products-campaigns",
    tags=["Products & Campaigns"],
)

PAGE_SIZE = 500
MAX_PRODUCT_ID = 2_147_483_647


class ProductUpdate(BaseModel):
    """Fields that the owner can edit from the product dashboard."""

    model_config = ConfigDict(extra="forbid")

    price: Optional[float] = Field(
        default=None,
        ge=0,
        le=100_000_000,
        allow_inf_nan=False,
    )
    battery_health: Optional[str] = Field(
        default=None,
        max_length=40,
    )
    campaign_enabled: Optional[bool] = None
    stock_status: Optional[str] = Field(
        default=None,
        max_length=40,
    )

    @model_validator(mode="after")
    def must_include_a_change(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")

        return self


class CampaignPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0, le=MAX_PRODUCT_ID)
    audience_scope: str = Field(
        default="brand",
        pattern="^(brand|product)$",
    )


def _paged_select(table: str, columns: str) -> list[dict]:
    """Read a table in pages to avoid silently relying on the default row cap."""
    rows: list[dict] = []
    offset = 0

    while True:
        result = (
            supabase.table(table)
            .select(columns)
            .order("id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )

        batch = result.data or []
        rows.extend(
            row for row in batch if isinstance(row, dict)
        )

        if len(batch) < PAGE_SIZE:
            return rows

        offset += PAGE_SIZE


def _database_error(message: str, exc: Exception) -> HTTPException:
    """Log database details privately and return a safe API error."""
    logger.exception("%s", message, exc_info=exc)

    return HTTPException(
        status_code=500,
        detail=message,
    )


def _norm(value: Any) -> str:
    """Normalize text for case-insensitive matching."""
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def _lead_search_text(lead: dict) -> str:
    """Build searchable text from the structured lead-interest fields."""
    customer_data = lead.get("customer_data")

    if isinstance(customer_data, str):
        # Some older records may contain JSON serialized as text.
        # Avoid guessing its structure if it cannot be parsed.
        import json

        try:
            customer_data = json.loads(customer_data)
        except (ValueError, TypeError):
            customer_data = {}

    if not isinstance(customer_data, dict):
        customer_data = {}

    fields = [
        lead.get("product_interest"),
        lead.get("product_url"),
        customer_data.get("product_interest"),
        customer_data.get("product"),
        customer_data.get("model"),
        customer_data.get("brand"),
        customer_data.get("product_url"),
    ]

    return _norm(" ".join(str(value) for value in fields if value))


def _product_matches_lead(
    product: dict,
    lead: dict,
    scope: str,
) -> bool:
    """Match stored lead-interest fields against a product.

    This intentionally uses structured lead data, not raw conversation
    messages. Counts therefore reflect stored interest fields, not every
    mention in conversation history.
    """
    text = _lead_search_text(lead)

    if not text:
        return False

    brand = _norm(product.get("brand"))
    title = _norm(product.get("title") or product.get("model"))
    model = _norm(product.get("model"))

    if scope == "brand":
        if not brand:
            return False

        return bool(
            re.search(
                rf"\b{re.escape(brand)}\b",
                text,
            )
        )

    candidates = [
        candidate
        for candidate in (title, model)
        if candidate and len(candidate) >= 4
    ]

    return any(
        re.search(
            rf"\b{re.escape(candidate)}\b",
            text,
        )
        for candidate in candidates
    )


def _customer_key(lead: dict) -> str:
    """Deduplicate records for the same known phone number."""
    phone = str(lead.get("phone_number") or "").strip()

    if phone and phone.lower() != "unknown":
        # Normalize common formatting differences.
        digits = re.sub(r"\D", "", phone)

        if digits:
            return f"phone:{digits}"

    # Leads without a usable number must not be counted as one shared customer.
    return f"lead:{lead.get('id')}"


def _unique_leads(leads: list[dict]) -> list[dict]:
    """Keep one record per customer, preferring the first matching record."""
    unique: dict[str, dict] = {}

    for lead in leads:
        key = _customer_key(lead)

        if key not in unique:
            unique[key] = lead

    return list(unique.values())


def _product_interest_counts(
    products: list[dict],
    leads: list[dict],
) -> dict[int, dict]:
    """Calculate unique-customer brand and product interest counts."""
    counts: dict[int, dict] = {}

    for product in products:
        product_id = int(product["id"])
        brand_customers: set[str] = set()
        product_customers: set[str] = set()

        for lead in leads:
            key = _customer_key(lead)

            if _product_matches_lead(product, lead, "brand"):
                brand_customers.add(key)

            if _product_matches_lead(product, lead, "product"):
                product_customers.add(key)

        counts[product_id] = {
            "brand_interest_count": len(brand_customers),
            "product_interest_count": len(product_customers),
        }

    return counts


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse a timestamp as UTC-aware datetime; return None if invalid."""
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError):
        return None


def _has_valid_phone(lead: dict) -> bool:
    phone = str(lead.get("phone_number") or "").strip()

    return bool(
        phone
        and phone.lower() != "unknown"
        and re.search(r"\d", phone)
    )


def _is_explicitly_opted_in(lead: dict) -> bool:
    """Require explicit consent and exclude anyone who has opted out."""
    return (
        lead.get("marketing_opt_in") is True
        and lead.get("marketing_opt_out") is not True
    )


def _is_opted_in_at_valid_time(lead: dict) -> bool:
    """Check that an opt-in timestamp exists for opted-in records."""
    if not _is_explicitly_opted_in(lead):
        return False

    opted_in_at = _parse_datetime(lead.get("marketing_opt_in_at"))

    if opted_in_at is None:
        return False

    return opted_in_at <= datetime.now(timezone.utc)


def _has_open_customer_service_window(lead: dict) -> bool:
    expiry = _parse_datetime(
        lead.get("whatsapp_window_expires_at")
    )

    return bool(
        expiry
        and expiry > datetime.now(timezone.utc)
    )


@router.get("/products")
def list_products(admin: dict = Depends(get_current_admin)):
    """Return product rows and interest counts without returning lead PII."""
    try:
        products = _paged_select(
            "products",
            "id,title,model,brand,sku,price,original_price,battery_health,"
            "stock_status,campaign_enabled,product_url,image_url,category,"
            "storage,color,updated_at,price_verified,price_verified_at,"
            "price_verified_by",
        )

        leads = (
            _paged_select(
                "leads",
                "id,phone_number,product_interest,product_url,customer_data",
            )
            if products
            else []
        )

        counts = _product_interest_counts(products, leads)

        return {
            "count": len(products),
            "products": [
                {
                    **product,
                    **counts.get(int(product["id"]), {}),
                }
                for product in products
            ],
            "interest_source": "stored lead product-interest fields",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise _database_error(
            "Could not load products and interest counts.",
            exc,
        ) from exc


@router.patch("/products/{product_id}")
def update_product(
    product_id: int,
    payload: ProductUpdate,
    admin: dict = Depends(get_current_admin),
):
    """Update product fields and invalidate verification if its price changes."""
    changes = payload.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(
            status_code=400,
            detail="No product fields supplied.",
        )

    try:
        if "price" in changes:
            # Read the current price before deciding whether verification
            # must be invalidated. Verification metadata is never accepted
            # from the client because ProductUpdate forbids extra fields.
            current_result = (
                supabase.table("products")
                .select("id,price")
                .eq("id", product_id)
                .limit(1)
                .execute()
            )

            current_rows = current_result.data or []

            if not current_rows:
                raise HTTPException(
                    status_code=404,
                    detail="Product not found.",
                )

            current_price = current_rows[0].get("price")
            new_price = changes["price"]

            def normalized_price(value):
                if value is None:
                    return None
                try:
                    return round(float(value), 8)
                except (TypeError, ValueError, OverflowError):
                    return value

            if normalized_price(current_price) != normalized_price(new_price):
                changes.update({
                    "price_verified": False,
                    "price_verified_at": None,
                    "price_verified_by": None,
                })

        result = (
            supabase.table("products")
            .update(changes)
            .eq("id", product_id)
            .select("*")
            .execute()
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise _database_error(
            "Could not update the product. Check the database schema and server logs.",
            exc,
        ) from exc

    rows = result.data or []

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Product not found.",
        )

    return {
        "updated": True,
        "product": rows[0],
    }

@router.post("/campaign-preview")
def campaign_preview(
    payload: CampaignPreviewRequest,
    admin: dict = Depends(get_current_admin),
):
    """Preview matching leads and messaging eligibility without sending."""
    try:
        product_result = (
            supabase.table("products")
            .select(
                "id,title,model,brand,price,battery_health,"
                "product_url,campaign_enabled"
            )
            .eq("id", payload.product_id)
            .limit(1)
            .execute()
        )

        product_rows = product_result.data or []

        if not product_rows:
            raise HTTPException(
                status_code=404,
                detail="Product not found.",
            )

        product = product_rows[0]

        leads = _paged_select(
            "leads",
            "id,phone_number,product_interest,product_url,customer_data,"
            "marketing_opt_in,marketing_opt_in_at,marketing_opt_out,"
            "whatsapp_window_expires_at",
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise _database_error(
            "Could not prepare the campaign preview. Check the database schema and server logs.",
            exc,
        ) from exc

    matched = _unique_leads(
        [
            lead
            for lead in leads
            if _product_matches_lead(
                product,
                lead,
                payload.audience_scope,
            )
        ]
    )

    # A campaign must not be enabled merely because a preview can be created.
    # Sending is disabled in this phase.
    opted_in = [
        lead
        for lead in matched
        if _has_valid_phone(lead)
        and _is_opted_in_at_valid_time(lead)
    ]

    now = datetime.now(timezone.utc)

    within_window = [
        lead
        for lead in opted_in
        if (
            _parse_datetime(lead.get("whatsapp_window_expires_at"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ) > now
    ]

    template_required = [
        lead
        for lead in opted_in
        if lead not in within_window
    ]

    return {
        "product": {
            "id": product["id"],
            "title": product.get("title") or product.get("model"),
            "brand": product.get("brand"),
            "price": product.get("price"),
            "battery_health": product.get("battery_health"),
            "product_url": product.get("product_url"),
            "campaign_enabled": product.get("campaign_enabled") is True,
        },
        "audience_scope": payload.audience_scope,
        "matched_interest_count": len(matched),
        "marketing_opted_in_count": len(opted_in),
        "freeform_window_count": len(within_window),
        "approved_template_required_count": len(template_required),
        "send_enabled": False,
        "send_block_reason": (
            "Campaign sending is disabled. Before enabling it, integrate the "
            "existing WhatsApp sender, verified opt-in and opt-out handling, "
            "approved templates for out-of-window messages, rate limits, "
            "duplicate prevention, and delivery tracking."
        ),
    }
