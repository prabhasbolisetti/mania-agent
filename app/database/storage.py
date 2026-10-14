from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from app.database.supabase_client import supabase
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
def _get_phone_number(customer: Dict[str, Any]) -> str:
    value = (
        customer.get("phone_number")
        or customer.get("phone")
        or customer.get("mobile")
        or ""
    )
    return str(value)
def _build_lead_row(
    customer: Dict[str, Any],
    qualification: Dict[str, Any],
    intent: Optional[Dict[str, Any]],
    status: str = "completed",
) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "phone_number": _get_phone_number(customer) or "unknown",
        "customer_name": customer.get("name"),
        "product_interest": customer.get(
            "product_interest"
        ),
        "product_url": customer.get(
            "product_url"
        ),
        "budget": customer.get(
            "budget",
            {}
        ),
        "purchase_timeline": customer.get(
            "purchase_timeline"
        ),
        "condition": customer.get(
            "condition"
        ),
        "payment_method": customer.get(
            "payment_method"
        ),
        "location": customer.get(
            "location"
        ),
        "intent_score": int(
            qualification.get(
                "score",
                0
            )
            or 0
        ),
        "intent_status": (
            qualification.get(
                "status"
            )
            or (intent or {}).get(
                "status"
            )
            or "low"
        ),
        "qualification_reasons": (
            qualification.get(
                "reasons"
            )
            or []
        ),
        "intent": {**(intent or {"score": 0, "status": "low", "signals": []}), "session_status": status},
        # Store the COMPLETE extracted
        # customer object.
        "customer_data": customer,
        "created_at": now,
        "updated_at": now,
    }
def _get_conversation_history(
    conversation_id: Optional[str],
) -> List[Dict[str, Any]]:
    if not conversation_id:
        return []
    result = (
        supabase
        .table("messages")
        .select(
            """
            id,
            sender_type,
            message_text,
            whatsapp_message_id,
            created_at
            """
        )
        .eq(
            "conversation_id",
            conversation_id
        )
        .order(
            "created_at"
        )
        .execute()
    )
    rows = result.data or []
    history = []
    for index, row in enumerate(
        rows,
        start=1
    ):
        history.append(
            {
                "turn": index,
                "message": row.get(
                    "message_text",
                    ""
                ),
                "sender_type": row.get(
                    "sender_type",
                    "customer"
                ),
                "whatsapp_message_id": row.get(
                    "whatsapp_message_id"
                ),
                "created_at": row.get(
                    "created_at"
                ),
            }
        )
    return history
def _row_to_lead(
    row: Dict[str, Any],
    conversation_history: Optional[
        List[Dict[str, Any]]
    ] = None,
) -> Dict[str, Any]:
    customer_data = (
        row.get(
            "customer_data"
        )
        or {}
    )
    # Backward compatibility
    # for older leads.
    if not customer_data:
        customer_data = {
            key: value
            for key, value in {
                "name": row.get(
                    "customer_name"
                ),
                "phone_number": row.get(
                    "phone_number"
                ),
                "product_interest": row.get(
                    "product_interest"
                ),
                "budget": row.get(
                    "budget"
                ),
                "purchase_timeline": row.get(
                    "purchase_timeline"
                ),
                "condition": row.get(
                    "condition"
                ),
                "payment_method": row.get(
                    "payment_method"
                ),
                "location": row.get(
                    "location"
                ),
                "product_url": row.get(
                    "product_url"
                ),
            }.items()
            if value is not None
        }
    return {
        "lead_id": str(
            row["id"]
        ),
        "created_at": row.get(
            "created_at"
        ),
        "updated_at": row.get(
            "updated_at"
        ),
        "phone_number": row.get("phone_number") or customer_data.get("phone_number") or customer_data.get("phone"),
        "call_completed": bool(row.get("call_completed", False)),
        "call_completed_at": row.get("call_completed_at"),
        "customer": customer_data,
        "qualification": {
            "score": row.get(
                "intent_score",
                0
            )
            or 0,
            "status": row.get(
                "intent_status",
                "low"
            ),
            "reasons": (
                row.get(
                    "qualification_reasons"
                )
                or []
            ),
        },
        "intent": (
            row.get(
                "intent"
            )
            or {}
        ),
        "conversation_id": row.get(
            "conversation_id"
        ),
        "conversation_history": (
            conversation_history
            or []
        ),
    }
def _replace_messages(
    conversation_id: str,
    conversation_history: List[
        Dict[str, Any]
    ],
) -> None:
    (
        supabase
        .table("messages")
        .delete()
        .eq(
            "conversation_id",
            conversation_id
        )
        .execute()
    )
    rows = []
    for item in conversation_history:
        message = item.get(
            "message"
        )
        if not message:
            continue
        rows.append(
            {
                "conversation_id": conversation_id,
                "sender_type": item.get(
                    "sender_type",
                    "customer"
                ),
                "message_text": str(
                    message
                ),
                "whatsapp_message_id": item.get(
                    "whatsapp_message_id"
                ),
                **(
                    {"created_at": item["created_at"]}
                    if item.get("created_at")
                    else {}
                ),
            }
        )
    if rows:
        (
            supabase
            .table("messages")
            .insert(rows)
            .execute()
        )
def save_lead(
    customer: Dict[str, Any],
    qualification: Dict[str, Any],
    intent: Optional[
        Dict[str, Any]
    ] = None,
    conversation_history: Optional[
        List[Dict[str, Any]]
    ] = None,
    status: str = "completed",
) -> Dict[str, Any]:
    now = _now_iso()
    phone_number = (
        _get_phone_number(
            customer
        )
        or "unknown"
    )
    # --------------------------------------------------
    # CREATE CONVERSATION
    # --------------------------------------------------
    conversation_result = (
        supabase
        .table("conversations")
        .insert(
            {
                "phone_number": phone_number,
                "status": status,
                "session_data": {
                    "customer": customer,
                    "qualification": (
                        qualification
                    ),
                    "intent": (
                        intent
                        or {}
                    ),
                },
                "created_at": now,
                "updated_at": now,
            }
        )
        .execute()
    )
    conversation = (
        conversation_result.data
        or [None]
    )[0]
    if not conversation:
        raise RuntimeError(
            "Supabase did not return the created conversation."
        )
    conversation_id = conversation[
        "id"
    ]
    # --------------------------------------------------
    # CREATE LEAD
    # --------------------------------------------------
    lead_row = _build_lead_row(
        customer,
        qualification,
        intent,
        status=status,
    )
    lead_row[
        "conversation_id"
    ] = conversation_id
    lead_result = (
        supabase
        .table("leads")
        .insert(
            lead_row
        )
        .execute()
    )
    lead = (
        lead_result.data
        or [None]
    )[0]
    if not lead:
        raise RuntimeError(
            "Supabase did not return the created lead."
        )
    lead_id = lead["id"]
    # --------------------------------------------------
    # CONNECT CONVERSATION → LEAD
    # --------------------------------------------------
    (
        supabase
        .table("conversations")
        .update(
            {
                "lead_id": lead_id,
                "updated_at": (
                    _now_iso()
                ),
            }
        )
        .eq(
            "id",
            conversation_id
        )
        .execute()
    )
    # --------------------------------------------------
    # STORE MESSAGES
    # --------------------------------------------------
    history = (
        conversation_history
        or []
    )
    _replace_messages(
        conversation_id,
        history,
    )
    return _row_to_lead(
        lead,
        _get_conversation_history(
            conversation_id
        ),
    )
def update_lead(
    lead_id: str,
    customer: Optional[
        Dict[str, Any]
    ] = None,
    qualification: Optional[
        Dict[str, Any]
    ] = None,
    intent: Optional[
        Dict[str, Any]
    ] = None,
    conversation_history: Optional[
        List[Dict[str, Any]]
    ] = None,
    status: Optional[str] = None,
) -> Optional[
    Dict[str, Any]
]:
    existing_result = (
        supabase
        .table("leads")
        .select("*")
        .eq(
            "id",
            lead_id
        )
        .maybe_single()
        .execute()
    )
    row = existing_result.data
    if not row:
        return None
    update_data: Dict[
        str,
        Any
    ] = {
        "updated_at": _now_iso()
    }
    # --------------------------------------------------
    # CUSTOMER
    # --------------------------------------------------
    if customer is not None:
        update_data.update(
            {
                "phone_number": (
                    _get_phone_number(
                        customer
                    )
                    or row.get(
                        "phone_number"
                    )
                    or "unknown"
                ),
                "customer_name": (
                    customer.get(
                        "name"
                    )
                ),
                "product_interest": (
                    customer.get(
                        "product_interest"
                    )
                ),
                "product_url": (
                    customer.get(
                        "product_url"
                    )
                ),
                "budget": (
                    customer.get(
                        "budget",
                        {}
                    )
                ),
                "purchase_timeline": (
                    customer.get(
                        "purchase_timeline"
                    )
                ),
                "condition": (
                    customer.get(
                        "condition"
                    )
                ),
                "payment_method": (
                    customer.get(
                        "payment_method"
                    )
                ),
                "location": (
                    customer.get(
                        "location"
                    )
                ),
                # COMPLETE customer object
                "customer_data": customer,
            }
        )
    # --------------------------------------------------
    # QUALIFICATION
    # --------------------------------------------------
    if qualification is not None:
        update_data.update(
            {
                "intent_score": int(
                    qualification.get(
                        "score",
                        0
                    )
                    or 0
                ),
                "intent_status": (
                    qualification.get(
                        "status",
                        "low"
                    )
                ),
                "qualification_reasons": (
                    qualification.get(
                        "reasons"
                    )
                    or []
                ),
            }
        )
    # --------------------------------------------------
    # INTENT
    # --------------------------------------------------
    if intent is not None:
        update_data["intent"] = {
            **intent,
            "session_status": status or (intent.get("session_status") if isinstance(intent, dict) else None),
        }
    # --------------------------------------------------
    # UPDATE LEAD
    # --------------------------------------------------
    updated_result = (
        supabase
        .table("leads")
        .update(
            update_data
        )
        .eq(
            "id",
            lead_id
        )
        .execute()
    )
    updated = (
        updated_result.data
        or [None]
    )[0]
    if not updated:
        return None
    conversation_id = (
        updated.get(
            "conversation_id"
        )
    )
    # --------------------------------------------------
    # UPDATE CONVERSATION
    # --------------------------------------------------
    if conversation_id:
        session_data = {
            "customer": (
                customer
                or {}
            ),
            "qualification": (
                qualification
                or {}
            ),
            "intent": (
                intent
                or {}
            ),
        }
        (
            supabase
            .table("conversations")
            .update(
                {
                    "status": (status or "completed"),
                    "session_data": (
                        session_data
                    ),
                    "updated_at": (
                        _now_iso()
                    ),
                }
            )
            .eq(
                "id",
                conversation_id
            )
            .execute()
        )
        if conversation_history is not None:
            _replace_messages(
                conversation_id,
                conversation_history,
            )
    return _row_to_lead(
        updated,
        _get_conversation_history(
            conversation_id
        ),
    )
def get_all_leads() -> List[Dict[str, Any]]:
    """Fetch all leads in pages and batch-load their conversations/messages."""
    rows: List[Dict[str, Any]] = []
    page_size = 1000
    offset = 0

    while True:
        result = (
            supabase.table("leads")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        page = result.data or []
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    if not rows:
        return []

    conversation_ids = list(dict.fromkeys(
        row.get("conversation_id")
        for row in rows
        if row.get("conversation_id")
    ))

    conversations_by_id: Dict[str, Dict[str, Any]] = {}
    messages_by_conversation: Dict[str, List[Dict[str, Any]]] = {}
    id_batch_size = 100

    for start_index in range(0, len(conversation_ids), id_batch_size):
        id_batch = conversation_ids[start_index:start_index + id_batch_size]
        try:
            conversation_result = (
                supabase.table("conversations")
                .select("id,status,updated_at")
                .in_("id", id_batch)
                .execute()
            )
            for item in conversation_result.data or []:
                if item.get("id"):
                    conversations_by_id[str(item["id"])] = item
        except Exception as exc:
            print("Could not batch-load conversations:", type(exc).__name__)

        message_offset = 0
        while True:
            try:
                message_result = (
                    supabase.table("messages")
                    .select("id,conversation_id,sender_type,message_text,whatsapp_message_id,created_at")
                    .in_("conversation_id", id_batch)
                    .order("created_at")
                    .range(message_offset, message_offset + page_size - 1)
                    .execute()
                )
            except Exception as exc:
                print("Could not batch-load conversation messages:", type(exc).__name__)
                break

            message_page = message_result.data or []
            for item in message_page:
                conversation_id = item.get("conversation_id")
                if not conversation_id:
                    continue
                history = messages_by_conversation.setdefault(str(conversation_id), [])
                history.append({
                    "turn": len(history) + 1,
                    "message": item.get("message_text", ""),
                    "sender_type": item.get("sender_type", "customer"),
                    "whatsapp_message_id": item.get("whatsapp_message_id"),
                    "created_at": item.get("created_at"),
                })
            if len(message_page) < page_size:
                break
            message_offset += page_size

    now = datetime.now(timezone.utc)
    for row in rows:
        conversation_id = row.get("conversation_id")
        if not conversation_id:
            continue
        conversation = conversations_by_id.get(str(conversation_id), {})
        if conversation.get("status") != "active" or not conversation.get("updated_at"):
            continue
        try:
            last_activity = datetime.fromisoformat(
                str(conversation["updated_at"]).replace("Z", "+00:00")
            )
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            if now - last_activity >= timedelta(minutes=30):
                session_intent = dict(row.get("intent") or {})
                session_intent["session_status"] = "abandoned"
                supabase.table("conversations").update({
                    "status": "abandoned",
                    "updated_at": _now_iso(),
                }).eq("id", conversation_id).execute()
                supabase.table("leads").update({
                    "intent": session_intent,
                }).eq("id", row["id"]).execute()
                row["intent"] = session_intent
                conversation["status"] = "abandoned"
        except Exception as exc:
            print("Could not expire stale conversation:", type(exc).__name__)

    return [
        _row_to_lead(
            row,
            messages_by_conversation.get(str(row.get("conversation_id")), []),
        )
        for row in rows
    ]




def has_processed_whatsapp_message(message_id: str) -> bool:
    """Return whether Meta's inbound message ID is already persisted."""
    message_id = str(message_id or "").strip()
    if not message_id:
        return False
    result = (
        supabase.table("messages")
        .select("id")
        .eq("whatsapp_message_id", message_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def get_latest_lead_by_phone(phone_number: str) -> Optional[Dict[str, Any]]:
    """Load the latest saved conversation for a phone number.

    This is used to recover the agent session after a worker restart, so an
    incoming message updates the existing lead instead of creating duplicates.
    """
    phone = str(phone_number or "").strip()
    if not phone:
        return None

    result = (
        supabase.table("leads")
        .select("*")
        .eq("phone_number", phone)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return None

    row = rows[0]
    conversation_id = row.get("conversation_id")
    conversation = {}
    if conversation_id:
        conversation_result = (
            supabase.table("conversations")
            .select("id,status,session_data,updated_at")
            .eq("id", conversation_id)
            .limit(1)
            .execute()
        )
        conversation_rows = conversation_result.data or []
        if conversation_rows:
            conversation = conversation_rows[0]

    lead = _row_to_lead(
        row,
        _get_conversation_history(conversation_id),
    )
    lead["conversation_status"] = conversation.get("status")
    lead["session_data"] = conversation.get("session_data") or {}
    lead["last_activity"] = (
        conversation.get("updated_at")
        or row.get("updated_at")
        or row.get("created_at")
    )
    return lead
