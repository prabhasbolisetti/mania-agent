import os
import json
import hmac
import hashlib
import traceback
from fastapi import APIRouter, HTTPException, Request, Depends, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from app.api.auth import (
    authenticate_admin,
    create_session,
    delete_session,
    get_current_admin,
    get_optional_current_admin,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
)
from app.database.storage import get_all_leads, has_processed_whatsapp_message
from app.database.supabase_client import supabase
from datetime import datetime, timezone
from app.agent.conversation import process_message, attach_whatsapp_message_id
from app.whatsapp.service import send_whatsapp_message
from app.config import env_bool
router = APIRouter()
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

# =========================================================
# ADMIN AUTHENTICATION

# =========================================================

class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/auth/login")

def admin_login(request: LoginRequest, response: Response):
    username = request.username.strip()
    password = request.password
    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail="Username and password are required.",
        )
    try:
        authenticated = authenticate_admin(
            username,
            password,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )
    if not authenticated:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password.",
        )
    session_id = create_session(username)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=env_bool("COOKIE_SECURE", default=bool(os.getenv("RENDER")),),
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    return {
        "authenticated": True,
        "username": username,
    }

@router.get("/auth/me")

def current_admin(request: Request):
    """Return session state without treating a normal logged-out check as an error."""
    admin = get_optional_current_admin(request)
    if not admin:
        return {"authenticated": False, "username": None}
    return {
        "authenticated": True,
        "username": admin.get("username"),
    }

# =========================================================
# LEAD DASHBOARD API

# =========================================================

@router.get("/leads")

def get_leads(
    admin: dict = Depends(get_current_admin),
):
    """
    Return all saved leads.
    Authentication required.
    """
    leads = get_all_leads()
    return {
        "count": len(leads),
        "leads": leads,
    }

class CallStatusUpdate(BaseModel):
    call_completed: bool

@router.patch("/leads/{lead_id}/call-status")

def update_lead_call_status(
    lead_id: str,
    payload: CallStatusUpdate,
    admin: dict = Depends(get_current_admin),
):
    """Persist call completion status for an authenticated admin."""
    completed_at = datetime.now(timezone.utc).isoformat() if payload.call_completed else None
    try:
        result = (
            supabase.table("leads")
            .update({
                "call_completed": payload.call_completed,
                "call_completed_at": completed_at,
            })
            .eq("id", lead_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to save call status.") from exc
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return result.data[0]

@router.get("/leads/{lead_id}")

def get_lead(
    lead_id: str,
    admin: dict = Depends(get_current_admin),
):
    """
    Return one lead by lead_id.
    Authentication required.
    """
    leads = get_all_leads()
    for lead in leads:
        if lead.get("lead_id") == lead_id:
            return lead
    raise HTTPException(
        status_code=404,
        detail="Lead not found.",
    )

# =========================================================
# CHAT API

# =========================================================

class ChatRequest(BaseModel):
    customer_id: str
    message: str

@router.post("/chat")

def chat(request: ChatRequest):
    customer_id = request.customer_id.strip()
    message = request.message.strip()
    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="customer_id cannot be empty",
        )
    if not message:
        raise HTTPException(
            status_code=400,
            detail="message cannot be empty",
        )
    try:
        return process_message(
            customer_id,
            message,
        )
    except Exception as exc:
        print(
            f"Chat processing error: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to process the message. "
                "Check the server terminal."
            ),
        )

# =========================================================
# WHATSAPP WEBHOOK VERIFICATION

# =========================================================

@router.get("/webhook")

def verify_webhook(request: Request):
    """
    Meta webhook verification endpoint.
    Meta sends:
        hub.mode
        hub.verify_token
        hub.challenge
    We return hub.challenge when our verification
    token matches.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    print("\n========== WEBHOOK VERIFICATION ==========")
    print("Mode:", mode)
    print("Verify token configured:", bool(WHATSAPP_VERIFY_TOKEN))
    print("Challenge present:", bool(challenge))
    print("==========================================\n")
    if (
        mode == "subscribe"
        and token == WHATSAPP_VERIFY_TOKEN
    ):
        return int(challenge)
    raise HTTPException(
        status_code=403,
        detail="Webhook verification failed",
    )

# =========================================================
# META SIGNATURE VERIFICATION

# =========================================================

def verify_meta_signature(
    raw_body: bytes,
    signature: str,
) -> bool:
    """
    Verify Meta's X-Hub-Signature-256 header.
    This protects the webhook from arbitrary requests.
    """
    if not WHATSAPP_APP_SECRET:
        # Local development can opt out, but deployed services should verify
        # Meta signatures by default.
        require_signature = env_bool(
            "WHATSAPP_REQUIRE_SIGNATURE",
            default=bool(os.getenv("RENDER")),
        )
        if require_signature:
            print("ERROR: WHATSAPP_APP_SECRET is required for webhook verification.")
            return False
        print("WARNING: Meta signature verification is disabled for this environment.")
        return True
    if not signature:
        print(
            "ERROR: Missing "
            "X-Hub-Signature-256 header."
        )
        return False
    if not signature.startswith("sha256="):
        print(
            "ERROR: Invalid "
            "X-Hub-Signature-256 format."
        )
        return False
    received_signature = signature.split(
        "=",
        1,
    )[1]
    expected_signature = hmac.new(
        WHATSAPP_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    is_valid = hmac.compare_digest(
        received_signature,
        expected_signature,
    )
    if not is_valid:
        print(
            "ERROR: Invalid Meta webhook signature."
        )
    else:
        print(
            "Meta webhook signature "
            "verified successfully."
        )
    return is_valid

# =========================================================
# WHATSAPP WEBHOOK

# =========================================================

def _record_campaign_button_response(
    button_id: str,
    sender: str,
    whatsapp_message_id: str = "",
) -> dict:
    """Persist a campaign button reply without sending it through the AI agent."""
    import re

    match = re.fullmatch(
        r"mm_campaign:([A-Za-z0-9_-]{1,100}):([1-9][0-9]{0,18}):(interested|not_interested)",
        str(button_id or "").strip(),
    )
    if not match:
        raise ValueError("Invalid campaign button ID.")

    campaign_id, product_id_text, response_value = match.groups()
    phone_number = str(sender or "").strip()
    if not phone_number.isdigit():
        raise ValueError("Invalid sender phone number.")

    row = {
        "campaign_id": campaign_id,
        "product_id": int(product_id_text),
        "phone_number": phone_number,
        "response": response_value,
        "whatsapp_message_id": str(whatsapp_message_id or "") or None,
        "responded_at": datetime.now(timezone.utc).isoformat(),
    }

    # Requires a UNIQUE constraint on (campaign_id, product_id, phone_number).
    # Upsert makes repeat delivery update the existing response instead of
    # creating another row for the same campaign/product/phone combination.
    result = (
        supabase.table("campaign_responses")
        .upsert(
            row,
            on_conflict="campaign_id,product_id,phone_number",
        )
        .execute()
    )
    if not result.data:
        raise RuntimeError("Campaign response was not saved.")
    return result.data[0]




def _handle_campaign_text_reply(
    sender: str,
    message_text: str,
    whatsapp_message_id: str = "",
) -> dict:
    """Handle campaign text commands before they reach the normal AI agent."""
    import re

    phone = re.sub(r"\D", "", str(sender or ""))
    normalized = re.sub(r"\s+", " ", str(message_text or "").strip()).upper()

    if not phone:
        return {"handled": False, "reason": "invalid_phone"}

    # Persist opt-outs independently of leads, so a number can opt out
    # even if no lead record exists yet. Create campaign_opt_outs using
    # supabase/campaign_opt_outs.sql before deploying this change.
    opt_out_commands = {
        "STOP",
        "STOP ALL",
        "UNSUBSCRIBE",
        "CANCEL SUBSCRIPTION",
        "PLEASE STOP",
        "STOP PLEASE",
        "UNSUBSCRIBE ME",
        "REMOVE ME",
        "STOP SENDING ME MESSAGES",
        "STOP SENDING ME PROMOTIONS",
        "STOP SENDING PROMOTIONAL MESSAGES",
        "DO NOT MESSAGE ME",
        "DON'T MESSAGE ME",
        "DO NOT SEND ME MESSAGES",
        "DON'T SEND ME MESSAGES",
    }
    is_opt_out = normalized in opt_out_commands or bool(
        re.search(
            r"\b(stop|unsubscribe)\b.*\b(messages|message|promotions|promotional|marketing|offers)\b",
            normalized,
        )
    )

    if is_opt_out:
        try:
            supabase.table("campaign_opt_outs").upsert(
                {
                    "phone_number": phone,
                    "opted_out_at": datetime.now(timezone.utc).isoformat(),
                    "source": "whatsapp",
                    "last_whatsapp_message_id": (
                        str(whatsapp_message_id or "") or None
                    ),
                },
                on_conflict="phone_number",
            ).execute()

            rows = (
                supabase.table("leads")
                .select("id,phone_number")
                .execute()
                .data
                or []
            )
            matching_ids = [
                row.get("id")
                for row in rows
                if row.get("id") is not None
                and re.sub(r"\D", "", str(row.get("phone_number") or "")) == phone
            ]
            for lead_id in matching_ids:
                updated = (
                    supabase.table("leads")
                    .update({"marketing_opt_out": True})
                    .eq("id", lead_id)
                    .execute()
                )
                if not updated.data:
                    raise RuntimeError(
                        "A lead marketing opt-out update was not confirmed."
                    )
        except Exception as exc:
            raise RuntimeError("Marketing opt-out could not be persisted.") from exc

        return {
            "handled": True,
            "reply": (
                "Sare. Mee promotional messages aapeyadaniki "
                "request save chesam. Inka promotional messages pampamu."
            ),
            "type": "opt_out",
        }

    # Classify clear campaign replies in natural language. NOT INTERESTED
    # must be checked first so it is never mistaken for INTERESTED.
    not_interested_phrases = (
        "NOT INTERESTED",
        "NO THANKS",
        "NO THANK YOU",
        "NOT LOOKING",
        "NO LONGER INTERESTED",
        "DON'T WANT",
        "DO NOT WANT",
        "NOT NEEDED",
        "NO NEED",
    )
    interested_phrases = (
        "INTERESTED",
        "TELL ME MORE",
        "MORE DETAILS",
        "SEND DETAILS",
        "SHARE DETAILS",
        "I WANT THIS",
        "I LIKE THIS",
    )

    if any(phrase in normalized for phrase in not_interested_phrases):
        response_value = "not_interested"
    elif any(phrase in normalized for phrase in interested_phrases):
        response_value = "interested"
    else:
        return {"handled": False, "reason": "not_campaign_command"}

    # Do not silently re-subscribe an opted-out customer. Check the
    # persistent opt-out registry as well as existing lead records.
    try:
        opt_out_row = (
            supabase.table("campaign_opt_outs")
            .select("phone_number")
            .eq("phone_number", phone)
            .limit(1)
            .execute()
            .data
            or []
        )
        lead_rows = (
            supabase.table("leads")
            .select("id,phone_number,marketing_opt_out")
            .execute()
            .data
            or []
        )
    except Exception as exc:
        raise RuntimeError("Could not verify the customer's marketing preference.") from exc

    matching_leads = [
        row for row in lead_rows
        if re.sub(r"\D", "", str(row.get("phone_number") or "")) == phone
    ]
    if opt_out_row or any(
        row.get("marketing_opt_out") is True for row in matching_leads
    ):
        return {
            "handled": True,
            "reply": (
                "Mee promotional messages opt-out request active lo undhi. "
                "Malli marketing messages pondalante store tho direct ga "
                "consent confirm cheyyandi."
            ),
            "type": "opted_out",
        }

    # Use the most recently sent campaign for this phone number.
    try:
        dispatches = (
            supabase.table("campaign_dispatches")
            .select("campaign_id,product_id,phone_number,status,created_at")
            .eq("status", "sent")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        raise RuntimeError("Could not find the customer's campaign.") from exc

    dispatch = next(
        (
            row for row in dispatches
            if re.sub(r"\D", "", str(row.get("phone_number") or "")) == phone
        ),
        None,
    )
    if not dispatch:
        return {"handled": False, "reason": "no_matching_campaign"}

    response_row = {
        "campaign_id": str(dispatch["campaign_id"]),
        "product_id": int(dispatch["product_id"]),
        "phone_number": phone,
        "response": response_value,
        "whatsapp_message_id": str(whatsapp_message_id or "") or None,
        "responded_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        saved = (
            supabase.table("campaign_responses")
            .upsert(
                response_row,
                on_conflict="campaign_id,product_id,phone_number",
            )
            .execute()
        )
    except Exception as exc:
        raise RuntimeError("Campaign text response could not be saved.") from exc

    if not saved.data:
        raise RuntimeError("Campaign text response was not confirmed as saved.")

    reply = (
        "Super! Mee interest ni Mania Mobiles team ki update chesam. "
        "Product details gurinchi team follow-up chestharu."
        if response_value == "interested"
        else "Sare, mee response record chesam. Thank you!"
    )
    return {
        "handled": True,
        "reply": reply,
        "type": response_value,
    }


@router.post("/webhook")

async def whatsapp_webhook(request: Request):
    """
    Receive incoming WhatsApp messages from Meta.
    Individual message failures are logged with a full traceback so the
    actual failing component is visible in the terminal. One bad message
    will not crash the entire webhook request.
    """
    raw_body = await request.body()
    # Do not log full headers or webhook bodies: they contain customer
    # phone numbers, message contents, and signature metadata.
    print("\n========== WHATSAPP WEBHOOK RECEIVED ==========")
    print("Content length:", len(raw_body))
    print("Signature header present:", bool(request.headers.get("X-Hub-Signature-256")))
    print("===============================================\n")
    # -----------------------------------------------------
    # 1. Verify Meta signature
    # -----------------------------------------------------
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(raw_body, signature):
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook signature",
        )
    # -----------------------------------------------------
    # 2. Parse JSON
    # -----------------------------------------------------
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        print("ERROR: Invalid JSON received from webhook.")
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload",
        )
    processed_messages = 0
    failed_messages = 0
    # -----------------------------------------------------
    # 3. Process WhatsApp event
    # -----------------------------------------------------
    try:
        entries = payload.get("entry", [])
        print(f"Entries received: {len(entries)}")
        for entry in entries:
            changes = entry.get("changes", [])
            print(f"Changes received: {len(changes)}")
            for change in changes:
                value = change.get("value", {})
                field = change.get("field", "unknown")
                messages = value.get("messages") or []
                statuses = value.get("statuses") or []
                print("Webhook field:", field)
                print("Incoming messages:", len(messages))
                print("Status updates:", len(statuses))
                for message in messages:
                    message_type = message.get("type")
                    print(
                        "Incoming WhatsApp message type:",
                        message_type,
                    )
                    sender = str(message.get("from") or "").strip()
                    whatsapp_message_id = str(message.get("id") or "").strip()

                    if not sender:
                        print("Ignoring message: sender missing.")
                        continue

                    # Campaign button clicks are persisted separately and must
                    # never enter the normal AI qualification conversation.
                    if message_type == "interactive":
                        interactive = message.get("interactive") or {}
                        button_reply = interactive.get("button_reply") or {}
                        button_id = str(button_reply.get("id") or "").strip()

                        if button_id.startswith("mm_campaign:"):
                            try:
                                await run_in_threadpool(
                                    _record_campaign_button_response,
                                    button_id,
                                    sender,
                                    whatsapp_message_id,
                                )
                                processed_messages += 1
                                print("Campaign button response recorded.")
                            except Exception as exc:
                                failed_messages += 1
                                print(
                                    "Campaign button response could not be saved:",
                                    type(exc).__name__,
                                )
                                traceback.print_exc()
                            # Always keep campaign clicks out of process_message().
                            continue

                        print("Ignoring unsupported interactive WhatsApp event.")
                        continue

                    if message_type != "text":
                        print("Ignoring unsupported message type:", message_type)
                        continue

                    message_text = str(
                        (message.get("text") or {}).get("body") or ""
                    ).strip()
                    if not message_text:
                        print("Ignoring message: text missing.")
                        continue

                    masked_sender = ("*" * max(0, len(sender) - 4)) + sender[-4:]
                    print("\n========== CUSTOMER MESSAGE ==========")
                    print("Sender (masked):", masked_sender)
                    print("Message characters:", len(message_text))
                    print("======================================\n")
                    if whatsapp_message_id:
                        try:
                            if await run_in_threadpool(has_processed_whatsapp_message, whatsapp_message_id):
                                print("Ignoring duplicate WhatsApp message ID.")
                                continue
                        except Exception as exc:
                            # Continue service during a temporary lookup failure;
                            # database errors are logged without exposing customer data.
                            print("Webhook deduplication lookup failed:", type(exc).__name__)
                    # Campaign commands are handled before the normal AI agent.
                    # Non-campaign text falls through to the existing conversation.
                    try:
                        campaign_reply = await run_in_threadpool(
                            _handle_campaign_text_reply,
                            sender,
                            message_text,
                            whatsapp_message_id,
                        )
                    except Exception as exc:
                        failed_messages += 1
                        print(
                            "Campaign text handling failed:",
                            type(exc).__name__,
                        )
                        continue

                    if campaign_reply.get("handled"):
                        try:
                            await run_in_threadpool(
                                send_whatsapp_message,
                                sender,
                                campaign_reply["reply"],
                            )
                            processed_messages += 1
                        except Exception as exc:
                            failed_messages += 1
                            print(
                                "Campaign acknowledgement failed:",
                                type(exc).__name__,
                            )
                        continue

                    customer_id = f"wa_{sender}"
                    # -------------------------------------------------
                    # 4. Run the existing agent
                    # -------------------------------------------------
                    try:
                        result = await run_in_threadpool(
                            process_message,
                            customer_id,
                            message_text,
                        )
                        print("\n========== AGENT RESULT ==========")
                        print("Response prepared:", bool(result.get("message")))
                        print("Lead record persisted:", bool(result.get("lead_id")))
                        print("Qualification completed:", bool(result.get("lead_saved")))
                        print("===================================\n")
                    except Exception as exc:
                        failed_messages += 1
                        print(
                            "\n========== AGENT PROCESSING ERROR =========="
                        )
                        print("Customer (masked):", masked_sender)
                        print("Error type:", type(exc).__name__)
                        print("Error:", str(exc))
                        print("Full traceback:")
                        traceback.print_exc()
                        print("============================================\n")
                        # Do not send a fake response. The traceback above
                        # exposes the real failure so it can be fixed.
                        continue
                    # -------------------------------------------------
                    # 5. Extract and send agent response
                    # -------------------------------------------------
                    reply = result.get(
                        "message",
                        "Sorry, I couldn't process that message.",
                    )
                    print("Reply prepared; character count:", len(reply))
                    try:
                        whatsapp_result = await run_in_threadpool(
                            send_whatsapp_message,
                            sender,
                            reply,
                        )
                        if whatsapp_message_id:
                            await run_in_threadpool(
                                attach_whatsapp_message_id,
                                customer_id,
                                message_text,
                                whatsapp_message_id,
                            )
                        print("\n========== WHATSAPP SEND RESULT ==========")
                        print("Meta accepted outbound message.")
                        print("===========================================\n")
                        processed_messages += 1
                    except Exception as exc:
                        failed_messages += 1
                        print(
                            "\n========== WHATSAPP SEND ERROR =========="
                        )
                        print("Customer (masked):", masked_sender)
                        print("Error type:", type(exc).__name__)
                        print("Error:", str(exc))
                        print("Full traceback:")
                        traceback.print_exc()
                        print("==========================================\n")
                        continue
    except Exception as exc:
        print(
            "\n========== WHATSAPP WEBHOOK ERROR =========="
        )
        print("Error type:", type(exc).__name__)
        print("Error:", str(exc))
        print("Full traceback:")
        traceback.print_exc()
        print("============================================\n")
        failed_messages += 1
    # -----------------------------------------------------
    # 6. Always acknowledge Meta
    # -----------------------------------------------------
    print("\n========== WEBHOOK SUMMARY ==========")
    print("Processed messages:", processed_messages)
    print("Failed messages:", failed_messages)
    print("=====================================\n")
    return {
        "status": "ok",
        "processed_messages": processed_messages,
        "failed_messages": failed_messages,
    }

@router.post("/auth/logout")

def admin_logout(
    request: Request,
    response: Response,
    admin: dict = Depends(get_current_admin),
):
    session_id = admin.get("session_id")
    if session_id:
        delete_session(session_id)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
    )
    return {
        "authenticated": False,
        "message": "Logged out successfully.",
    }
