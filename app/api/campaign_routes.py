
"""Authenticated product management and product-specific WhatsApp campaigns.

Campaign sends are explicit admin actions, require verified catalog pricing and
explicit marketing consent, and never enter the normal AI conversation flow.
"""

from __future__ import annotations

import logging
import uuid
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.auth import get_current_admin
from app.database.supabase_client import supabase
from app.whatsapp.service import send_whatsapp_message

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
                "product_url,campaign_enabled,stock_status,price_verified,"
                "price_verified_at"
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
        "price_verified": product.get("price_verified") is True,
        "stock_status": product.get("stock_status"),
        "send_enabled": (
            product.get("campaign_enabled") is True
            and product.get("price_verified") is True
            and product.get("price") is not None
            and str(product.get("stock_status") or "").lower()
                not in {"out_of_stock", "out of stock", "unavailable", "sold_out"}
            and len(opted_in) > 0
        ),
        "send_block_reason": _campaign_block_reason(product, opted_in),
    }



class CampaignSendRequest(BaseModel):
    """An explicit, idempotent admin-triggered product offer campaign."""
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0, le=MAX_PRODUCT_ID)
    audience_scope: str = Field(default="brand", pattern="^(brand|product)$")
    # Reuse the same UUID when retrying the same UI action to avoid duplicate sends.
    campaign_id: str = Field(min_length=8, max_length=80)


def _campaign_block_reason(product: dict, eligible: list[dict]) -> Optional[str]:
    if product.get("campaign_enabled") is not True:
        return "Enable this product's campaign in the catalog first."
    if product.get("price_verified") is not True:
        return "The current price is not verified. Verify it in the dashboard first."
    try:
        if product.get("price") is None or float(product["price"]) <= 0:
            return "A valid, positive catalog price is required."
    except (TypeError, ValueError, OverflowError):
        return "A valid, positive catalog price is required."
    stock = _norm(product.get("stock_status"))
    if stock in {"out of stock", "unavailable", "sold out", "outofstock"}:
        return "This product is marked out of stock."
    if not eligible:
        return "No matching customers have valid explicit marketing consent."
    return None


def _campaign_message(product: dict) -> str:
    name = str(product.get("title") or product.get("model") or "Product").strip()
    brand = str(product.get("brand") or "").strip()
    if brand and _norm(brand) not in _norm(name):
        name = f"{brand} {name}"
    price = float(product["price"])
    message = f"{name} manchi offer lo undhi! Current verified price: ₹{price:,.0f}."
    battery = str(product.get("battery_health") or "").strip()
    if battery and _norm(battery) not in {"na", "n a", "not applicable", "unknown", "none"}:
        message += f" Battery health: {battery}."
    product_url = str(product.get("product_url") or "").strip()
    if product_url.startswith(("https://", "http://")):
        message += f" Details: {product_url}"
    message += (
        " Interest unte INTERESTED ani reply cheyyandi. "
        "Promotional messages aapadaniki STOP ani reply cheyyandi."
    )
    return message[:1024]


@router.post("/campaign-send")
def send_campaign(
    payload: CampaignSendRequest,
    admin: dict = Depends(get_current_admin),
):
    """Send a product-specific text offer to eligible matching customers.

    Free-form text is sent only while the WhatsApp customer-service window is
    open. Outside that window, the recipient is skipped until an approved Meta
    Marketing template is explicitly integrated. This endpoint is manual.
    """
    try:
        product_result = (
            supabase.table("products")
            .select(
                "id,title,model,brand,price,battery_health,product_url,"
                "campaign_enabled,stock_status,price_verified"
            )
            .eq("id", payload.product_id)
            .limit(1)
            .execute()
        )
        products = product_result.data or []
        if not products:
            raise HTTPException(status_code=404, detail="Product not found.")
        product = products[0]
        leads = _paged_select(
            "leads",
            "id,phone_number,product_interest,product_url,customer_data,"
            "marketing_opt_in,marketing_opt_in_at,marketing_opt_out,"
            "whatsapp_window_expires_at",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _database_error("Could not load campaign data.", exc) from exc

    matched = _unique_leads([
        lead for lead in leads
        if _product_matches_lead(product, lead, payload.audience_scope)
    ])
    eligible = [
        lead for lead in matched
        if _has_valid_phone(lead) and _is_opted_in_at_valid_time(lead)
    ]
    block_reason = _campaign_block_reason(product, eligible)
    if block_reason:
        raise HTTPException(status_code=409, detail=block_reason)

    # Validate the idempotency key format; callers should reuse it on retries.
    try:
        campaign_id = str(uuid.UUID(payload.campaign_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="campaign_id must be a UUID.")

    message = _campaign_message(product)
    sent = 0
    skipped = 0
    failures = []

    for lead in eligible:
        phone = re.sub(r"\D", "", str(lead.get("phone_number") or ""))
        if not phone:
            skipped += 1
            continue
        # Avoid duplicate delivery for a repeated request with the same campaign ID.
        try:
            existing = (
                supabase.table("campaign_dispatches")
                .select("id,status")
                .eq("campaign_id", campaign_id)
                .eq("phone_number", phone)
                .limit(1)
                .execute()
            ).data or []
        except Exception as exc:
            raise _database_error(
                "Campaign dispatch tracking is not available. Run the campaign migration first.", exc
            ) from exc
        if existing:
            skipped += 1
            continue

        within_window = _has_open_customer_service_window(lead)
        if not within_window:
            # Never send free-form marketing text outside the 24-hour window.
            # A correctly configured, approved Meta Marketing template is
            # required; this route intentionally does not guess its name/body.
            skipped += 1
            failures.append({
                "phone_number": phone,
                "reason": "outside_24h_window_approved_marketing_template_required",
            })
            continue

        # Insert a pending dispatch before sending to guard concurrent/retried requests.
        try:
            supabase.table("campaign_dispatches").insert({
                "campaign_id": campaign_id,
                "product_id": int(product["id"]),
                "phone_number": phone,
                "status": "pending",
            }).execute()
        except Exception:
            # A concurrent request may have inserted the unique key first.
            skipped += 1
            continue

        try:
            result = send_whatsapp_message(phone, message)
            messages = result.get("messages") or []
            message_id = messages[0].get("id") if messages else None
            supabase.table("campaign_dispatches").update({
                "status": "sent",
                "whatsapp_message_id": message_id,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }).eq("campaign_id", campaign_id).eq("phone_number", phone).execute()
            sent += 1
        except Exception as exc:
            logger.exception("Campaign send failed for a recipient")
            try:
                supabase.table("campaign_dispatches").update({
                    "status": "failed",
                    "failure_reason": type(exc).__name__,
                }).eq("campaign_id", campaign_id).eq("phone_number", phone).execute()
            except Exception:
                logger.exception("Could not update campaign dispatch failure status")
            failures.append({"phone_number": phone, "reason": "whatsapp_send_failed"})

    return {
        "campaign_id": campaign_id,
        "product_id": int(product["id"]),
        "matched_count": len(matched),
        "eligible_count": len(eligible),
        "sent_count": sent,
        "skipped_count": skipped,
        "failed_count": len(failures),
        "failures": failures[:50],
        "message": "Campaign processing finished. Check dispatch status before retrying.",
    }


@router.get("/campaign-responses")
def list_campaign_responses(
    product_id: Optional[int] = None,
    response: Optional[str] = None,
    limit: int = 100,
    admin: dict = Depends(get_current_admin),
):
    """Return text-reply campaign responses for the authenticated dashboard."""
    if response not in (None, "interested", "not_interested"):
        raise HTTPException(status_code=422, detail="Invalid response filter.")
    limit = max(1, min(int(limit), 500))
    try:
        query = (
            supabase.table("campaign_responses")
            .select("id,campaign_id,product_id,phone_number,response,"
                    "whatsapp_message_id,responded_at,created_at")
            .order("responded_at", desc=True)
            .limit(limit)
        )
        if product_id is not None:
            query = query.eq("product_id", product_id)
        if response is not None:
            query = query.eq("response", response)
        rows = query.execute().data or []
        return {"count": len(rows), "responses": rows}
    except Exception as exc:
        raise _database_error(
            "Could not load campaign responses. Run the campaign database migration first.", exc
        ) from exc
