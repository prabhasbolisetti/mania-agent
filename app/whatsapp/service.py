
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_API_VERSION = "v26.0"
REQUEST_TIMEOUT_SECONDS = 20
MAX_WHATSAPP_TEXT_LENGTH = 4096
MAX_INTERACTIVE_BODY_LENGTH = 1024
MAX_BUTTON_TITLE_LENGTH = 20
MAX_HEADER_TEXT_LENGTH = 60
MAX_FOOTER_TEXT_LENGTH = 60
MAX_BUTTON_ID_LENGTH = 256

_PHONE_RE = re.compile(r"^[0-9]{5,20}$")
_TEMPLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}(?:_[A-Z]{2})?$")


def _read_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read process environment first, then the project .env file."""
    value = os.getenv(name)
    if value:
        return value

    try:
        value = dotenv_values(ENV_FILE).get(name)
        if value:
            return str(value)
    except Exception:
        pass

    return default


def _token_fingerprint(token: Optional[str]) -> str:
    """Return a short, non-reversible token fingerprint for diagnostics."""
    if not token:
        return "missing"

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


def _mask_identifier(value: Optional[str], keep_last: int = 4) -> str:
    """Mask phone numbers and account identifiers in logs."""
    if not value:
        return "missing"

    value = str(value)

    if len(value) <= keep_last:
        return "*" * len(value)

    return ("*" * (len(value) - keep_last)) + value[-keep_last:]


def _load_whatsapp_config() -> Tuple[str, str, str]:
    """
    Return access token, phone number ID and API version.

    Existing environment variable aliases are retained.
    """
    token = (
        _read_setting("WHATSAPP_ACCESS_TOKEN")
        or _read_setting("META_ACCESS_TOKEN")
        or _read_setting("WHATSAPP_TOKEN")
    )

    phone_number_id = (
        _read_setting("WHATSAPP_PHONE_NUMBER_ID")
        or _read_setting("PHONE_NUMBER_ID")
        or _read_setting("META_PHONE_NUMBER_ID")
    )

    api_version = (
        _read_setting("WHATSAPP_API_VERSION")
        or _read_setting("META_API_VERSION")
        or DEFAULT_API_VERSION
    )

    if not token:
        raise RuntimeError(
            "WhatsApp access token is missing. Set WHATSAPP_ACCESS_TOKEN "
            "(or META_ACCESS_TOKEN / WHATSAPP_TOKEN) in the environment or .env."
        )

    if not phone_number_id:
        raise RuntimeError(
            "WhatsApp phone number ID is missing. Set WHATSAPP_PHONE_NUMBER_ID "
            "(or PHONE_NUMBER_ID / META_PHONE_NUMBER_ID) in the environment or .env."
        )

    return token, phone_number_id, api_version


def get_whatsapp_diagnostics() -> Dict[str, Any]:
    """Return safe configuration diagnostics without exposing the token."""
    token = (
        _read_setting("WHATSAPP_ACCESS_TOKEN")
        or _read_setting("META_ACCESS_TOKEN")
        or _read_setting("WHATSAPP_TOKEN")
    )

    phone_number_id = (
        _read_setting("WHATSAPP_PHONE_NUMBER_ID")
        or _read_setting("PHONE_NUMBER_ID")
        or _read_setting("META_PHONE_NUMBER_ID")
    )

    api_version = (
        _read_setting("WHATSAPP_API_VERSION")
        or _read_setting("META_API_VERSION")
        or DEFAULT_API_VERSION
    )

    return {
        "configured": bool(token and phone_number_id),
        "access_token_present": bool(token),
        "token_fingerprint": _token_fingerprint(token),
        "phone_number_id_present": bool(phone_number_id),
        "phone_number_id_masked": _mask_identifier(phone_number_id),
        "api_version": api_version,
        "env_file_exists": ENV_FILE.exists(),
    }


def _validate_recipient(to: str) -> str:
    """Validate a recipient number and normalize an optional leading +."""
    recipient = str(to or "").strip()

    if recipient.startswith("+"):
        recipient = recipient[1:]

    if not _PHONE_RE.fullmatch(recipient):
        raise ValueError(
            "Recipient must contain 5-20 digits, including the country code, "
            "without spaces or punctuation."
        )

    return recipient


def _whatsapp_request(
    recipient: str,
    payload: Dict[str, Any],
    *,
    action_name: str,
) -> Dict[str, Any]:
    """
    Send an interactive or template message through WhatsApp Cloud API.

    Uses the existing configuration, timeout and safe diagnostic helpers.
    """
    token, phone_number_id, api_version = _load_whatsapp_config()

    url = (
        f"https://graph.facebook.com/{api_version}/"
        f"{phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    print(
        f"[WhatsApp] action={action_name} "
        f"recipient={_mask_identifier(recipient)} "
        f"phone_number_id={_mask_identifier(phone_number_id)} "
        f"token_fingerprint={_token_fingerprint(token)}"
    )

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.Timeout as exc:
        print(f"[WhatsApp] action={action_name} request timed out")

        raise RuntimeError(
            f"WhatsApp {action_name} request timed out after "
            f"{REQUEST_TIMEOUT_SECONDS} seconds."
        ) from exc

    except requests.RequestException as exc:
        print(
            f"[WhatsApp] action={action_name} network error "
            f"type={type(exc).__name__}"
        )

        raise RuntimeError(
            f"WhatsApp {action_name} request failed due to a network error "
            f"({type(exc).__name__})."
        ) from exc

    try:
        result = response.json()
    except ValueError:
        result = {"raw_response": response.text[:1000]}

    if not response.ok:
        error = result.get("error", {}) if isinstance(result, dict) else {}

        print(
            f"[WhatsApp] action={action_name} failed "
            f"http_status={response.status_code} "
            f"error_code={error.get('code', 'unknown')} "
            f"error_subcode={error.get('error_subcode', 'unknown')} "
            f"error_type={error.get('type', 'unknown')} "
            f"fbtrace_id={error.get('fbtrace_id', 'unknown')} "
            f"message={str(error.get('message', 'No error message'))[:300]}"
        )

        raise RuntimeError(
            f"WhatsApp {action_name} failed with HTTP "
            f"{response.status_code}: "
            f"{error.get('message', 'Unknown WhatsApp API error')}"
        )

    print(
        f"[WhatsApp] action={action_name} success "
        f"http_status={response.status_code}"
    )

    return result if isinstance(result, dict) else {"response": result}


def send_whatsapp_message(to: str, message: str) -> Dict[str, Any]:
    """
    Send a plain-text WhatsApp message.

    Retains the existing sender interface so current agent callers can
    continue using send_whatsapp_message(to, message).
    """
    recipient = str(to or "").strip()
    text = str(message or "").strip()

    if not recipient:
        raise ValueError("WhatsApp recipient phone number is required.")

    if not text:
        raise ValueError("WhatsApp message text cannot be empty.")

    if len(text) > MAX_WHATSAPP_TEXT_LENGTH:
        raise ValueError(
            f"WhatsApp text messages cannot exceed "
            f"{MAX_WHATSAPP_TEXT_LENGTH} characters."
        )

    token, phone_number_id, api_version = _load_whatsapp_config()

    url = (
        f"https://graph.facebook.com/{api_version}/"
        f"{phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text,
        },
    }

    print(
        f"[WhatsApp] sending text "
        f"recipient={_mask_identifier(recipient)} "
        f"phone_number_id={_mask_identifier(phone_number_id)} "
        f"token_fingerprint={_token_fingerprint(token)}"
    )

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.Timeout as exc:
        print(
            f"[WhatsApp] text request timed out after "
            f"{REQUEST_TIMEOUT_SECONDS}s"
        )

        raise RuntimeError(
            f"WhatsApp request timed out after "
            f"{REQUEST_TIMEOUT_SECONDS} seconds."
        ) from exc

    except requests.RequestException as exc:
        print(
            f"[WhatsApp] text network error "
            f"type={type(exc).__name__}"
        )

        raise RuntimeError(
            f"WhatsApp request failed due to a network error "
            f"({type(exc).__name__})."
        ) from exc

    try:
        result = response.json()
    except ValueError:
        result = {"raw_response": response.text[:1000]}

    if not response.ok:
        error = result.get("error", {}) if isinstance(result, dict) else {}

        print(
            f"[WhatsApp] Meta API error "
            f"http_status={response.status_code} "
            f"error_code={error.get('code', 'unknown')} "
            f"error_subcode={error.get('error_subcode', 'unknown')} "
            f"error_type={error.get('type', 'unknown')} "
            f"message={str(error.get('message', 'No error message'))[:300]} "
            f"fbtrace_id={error.get('fbtrace_id', 'unknown')}"
        )

        raise RuntimeError(
            f"WhatsApp API error {response.status_code}: "
            f"{error.get('message', 'Unknown WhatsApp API error')}"
        )

    print(
        f"[WhatsApp] message sent successfully "
        f"http_status={response.status_code}"
    )

    return result if isinstance(result, dict) else {"response": result}


def send_whatsapp_interactive_buttons(
    to: str,
    body: str,
    buttons: Sequence[Dict[str, str]],
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a WhatsApp interactive reply-button message.

    `buttons` must contain exactly two entries, each with an `id` and `title`.

    Example:
        buttons=[
            {"id": "campaign123:456:interested", "title": "Interested"},
            {"id": "campaign123:456:not_interested", "title": "Not Interested"},
        ]

    Interactive reply buttons are subject to WhatsApp messaging-window rules.
    Use an approved template for business-initiated marketing outside the
    customer-service window.
    """
    recipient = _validate_recipient(to)
    body_text = str(body or "").strip()

    if not body_text:
        raise ValueError("Interactive message body cannot be empty.")

    if len(body_text) > MAX_INTERACTIVE_BODY_LENGTH:
        raise ValueError(
            f"Interactive message body cannot exceed "
            f"{MAX_INTERACTIVE_BODY_LENGTH} characters."
        )

    if len(buttons) != 2:
        raise ValueError("Exactly two interactive reply buttons are required.")

    formatted_buttons: List[Dict[str, Any]] = []
    seen_ids = set()

    for button in buttons:
        if not isinstance(button, dict):
            raise ValueError(
                "Each button must be a dictionary containing id and title."
            )

        button_id = str(button.get("id", "")).strip()
        button_title = str(button.get("title", "")).strip()

        if not button_id:
            raise ValueError("Every button must have a non-empty id.")

        if len(button_id) > MAX_BUTTON_ID_LENGTH:
            raise ValueError(
                f"Button IDs cannot exceed {MAX_BUTTON_ID_LENGTH} characters."
            )

        if not button_title:
            raise ValueError("Every button must have a non-empty title.")

        if len(button_title) > MAX_BUTTON_TITLE_LENGTH:
            raise ValueError(
                f"Button titles cannot exceed "
                f"{MAX_BUTTON_TITLE_LENGTH} characters."
            )

        if button_id in seen_ids:
            raise ValueError("Interactive button IDs must be unique.")

        seen_ids.add(button_id)

        formatted_buttons.append(
            {
                "type": "reply",
                "reply": {
                    "id": button_id,
                    "title": button_title,
                },
            }
        )

    interactive: Dict[str, Any] = {
        "type": "button",
        "body": {"text": body_text},
        "action": {"buttons": formatted_buttons},
    }

    if header is not None:
        header_text = str(header).strip()

        if not header_text:
            raise ValueError("Header cannot be an empty string.")

        if len(header_text) > MAX_HEADER_TEXT_LENGTH:
            raise ValueError(
                f"Interactive header cannot exceed "
                f"{MAX_HEADER_TEXT_LENGTH} characters."
            )

        interactive["header"] = {
            "type": "text",
            "text": header_text,
        }

    if footer is not None:
        footer_text = str(footer).strip()

        if not footer_text:
            raise ValueError("Footer cannot be an empty string.")

        if len(footer_text) > MAX_FOOTER_TEXT_LENGTH:
            raise ValueError(
                f"Interactive footer cannot exceed "
                f"{MAX_FOOTER_TEXT_LENGTH} characters."
            )

        interactive["footer"] = {"text": footer_text}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "interactive",
        "interactive": interactive,
    }

    return _whatsapp_request(
        recipient,
        payload,
        action_name="interactive_buttons",
    )


def send_whatsapp_template_message(
    to: str,
    template_name: str,
    language_code: str = "en_US",
    components: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Send a WhatsApp template message.

    The template must already exist in the connected WhatsApp Business account
    and be approved by Meta. The template's language and supplied components
    must match the approved template definition.
    """
    recipient = _validate_recipient(to)
    name = str(template_name or "").strip()
    language = str(language_code or "").strip()

    if not _TEMPLATE_NAME_RE.fullmatch(name):
        raise ValueError(
            "Template name must contain only letters, numbers, and underscores."
        )

    if not _LANGUAGE_CODE_RE.fullmatch(language):
        raise ValueError(
            "Language code must look like 'en_US' or 'pt_BR'."
        )

    template: Dict[str, Any] = {
        "name": name,
        "language": {"code": language},
    }

    if components is not None:
        if not isinstance(components, (list, tuple)):
            raise ValueError(
                "Template components must be a list or tuple."
            )

        template["components"] = list(components)

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": template,
    }

    return _whatsapp_request(
        recipient,
        payload,
        action_name="template_message",
    )



def send_whatsapp_campaign_template(
    to: str,
    template_name: str,
    language_code: str = "en_US",
    components: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Backward-compatible campaign template sender expected by app.api.routes.

    Delegates to send_whatsapp_template_message so configuration handling,
    payload validation, timeout handling, and API diagnostics remain shared.
    The named template must already be approved in Meta.
    """
    return send_whatsapp_template_message(
        to=to,
        template_name=template_name,
        language_code=language_code,
        components=components,
    )
