import os
import json
import hmac
import hashlib
import traceback
from threading import Lock
from collections import deque
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

# Meta may retry while the first webhook is still processing. The database
# check handles persisted IDs; this in-process guard prevents concurrent
# requests in the same Render worker from sending the same reply twice.
_WEBHOOK_ID_LOCK = Lock()
_WEBHOOK_INFLIGHT_IDS = set()
_WEBHOOK_COMPLETED_IDS = set()
_WEBHOOK_COMPLETED_ORDER = deque()
_WEBHOOK_COMPLETED_LIMIT = 5000


def _claim_whatsapp_message_id(message_id: str) -> bool:
    message_id = str(message_id or "").strip()
    if not message_id:
        return True
    with _WEBHOOK_ID_LOCK:
        if message_id in _WEBHOOK_INFLIGHT_IDS or message_id in _WEBHOOK_COMPLETED_IDS:
            return False
        _WEBHOOK_INFLIGHT_IDS.add(message_id)
        return True


def _finish_whatsapp_message_id(message_id: str, completed: bool = False) -> None:
    message_id = str(message_id or "").strip()
    if not message_id:
        return
    with _WEBHOOK_ID_LOCK:
        _WEBHOOK_INFLIGHT_IDS.discard(message_id)
        if not completed or message_id in _WEBHOOK_COMPLETED_IDS:
            return
        if len(_WEBHOOK_COMPLETED_ORDER) >= _WEBHOOK_COMPLETED_LIMIT:
            expired = _WEBHOOK_COMPLETED_ORDER.popleft()
            _WEBHOOK_COMPLETED_IDS.discard(expired)
        _WEBHOOK_COMPLETED_ORDER.append(message_id)
        _WEBHOOK_COMPLETED_IDS.add(message_id)

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
                    # Only process text messages.
                    if message_type != "text":
                        print(
                            "Ignoring unsupported message type:",
                            message_type,
                        )
                        continue
                    sender = message.get("from")
                    message_text = (
                        message.get("text", {}).get("body", "")
                    )
                    if not sender:
                        print("Ignoring message: sender missing.")
                        continue
                    if not message_text:
                        print("Ignoring message: text missing.")
                        continue
                    message_text = message_text.strip()
                    masked_sender = ("*" * max(0, len(sender) - 4)) + sender[-4:]
                    print("\n========== CUSTOMER MESSAGE ==========")
                    print("Sender (masked):", masked_sender)
                    print("Message characters:", len(message_text))
                    print("======================================\n")
                    whatsapp_message_id = message.get("id")
                    if whatsapp_message_id:
                        if not _claim_whatsapp_message_id(whatsapp_message_id):
                            print("Ignoring duplicate/in-flight WhatsApp message ID.")
                            continue
                        try:
                            if await run_in_threadpool(has_processed_whatsapp_message, whatsapp_message_id):
                                print("Ignoring previously processed WhatsApp message ID.")
                                _finish_whatsapp_message_id(whatsapp_message_id, completed=True)
                                continue
                        except Exception as exc:
                            # Continue service during a temporary lookup failure;
                            # the in-process claim still prevents concurrent duplicates.
                            print("Webhook deduplication lookup failed:", type(exc).__name__)
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
                        _finish_whatsapp_message_id(whatsapp_message_id)
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
                            _finish_whatsapp_message_id(whatsapp_message_id, completed=True)
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
                        _finish_whatsapp_message_id(whatsapp_message_id)
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
