
"""Meta WhatsApp Cloud API client with safe configuration and diagnostics.

Configuration priority:
1. Process/deployment environment variables (Render, etc.).
2. Project-root .env file (local development fallback).

The .env file is read afresh whenever configuration is requested.
Credentials are never intentionally printed in logs.
"""

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Tuple

import requests
from dotenv import dotenv_values


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_API_VERSION = "v26.0"
REQUEST_TIMEOUT_SECONDS = 20
MAX_WHATSAPP_TEXT_LENGTH = 4096


# ---------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------

def _read_setting(name: str, default: str = "") -> str:
    """Read deployment environment first, then fall back to .env."""

    # Deployment variables must override values in a local .env file.
    environment_value = os.environ.get(name)

    if environment_value is not None:
        environment_value = environment_value.strip()
        if environment_value:
            return environment_value

    # .env is a fallback for local development.
    if ENV_FILE.is_file():
        try:
            file_values = dotenv_values(dotenv_path=str(ENV_FILE))
            file_value = file_values.get(name)

            if file_value is not None:
                file_value = str(file_value).strip()
                if file_value:
                    return file_value

        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"Could not read project configuration file: {ENV_FILE}"
            ) from exc

    return default.strip()


def _token_fingerprint(token: str) -> str:
    """Return a short fingerprint without exposing the token."""

    if not token:
        return "missing"

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()[:12]


def _mask_identifier(value: str) -> str:
    """Mask an identifier while retaining its last four characters."""

    value = str(value or "")

    if not value:
        return "missing"

    if len(value) <= 4:
        return "*" * len(value)

    return "*" * (len(value) - 4) + value[-4:]


def _load_whatsapp_config() -> Tuple[str, str, str]:
    """Load the latest WhatsApp configuration."""

    phone_number_id = _read_setting("WHATSAPP_PHONE_NUMBER_ID")
    access_token = _read_setting("WHATSAPP_ACCESS_TOKEN")

    api_version = (
        _read_setting("WHATSAPP_API_VERSION", DEFAULT_API_VERSION)
        or DEFAULT_API_VERSION
    )

    if not api_version.startswith("v"):
        api_version = f"v{api_version}"

    return phone_number_id, access_token, api_version


# ---------------------------------------------------------
# Safe diagnostics
# ---------------------------------------------------------

def get_whatsapp_diagnostics() -> Dict[str, Any]:
    """Return configuration diagnostics without exposing credentials."""

    phone_number_id, access_token, api_version = (
        _load_whatsapp_config()
    )

    return {
        "project_root": str(PROJECT_ROOT),
        "env_file_path": str(ENV_FILE),
        "env_file_exists": ENV_FILE.is_file(),
        "access_token_present": bool(access_token),
        "access_token_length": len(access_token),
        "access_token_fingerprint": _token_fingerprint(access_token),
        "phone_number_id_present": bool(phone_number_id),
        "phone_number_id_masked": _mask_identifier(phone_number_id),
        "api_version": api_version,
        "config_priority": (
            "process environment first, .env fallback"
        ),
    }


# ---------------------------------------------------------
# WhatsApp Cloud API
# ---------------------------------------------------------

def send_whatsapp_message(to: str, message: str) -> Dict[str, Any]:
    """Send a text message using the configured WhatsApp phone number."""

    # Read settings at send time so configuration changes are picked up.
    phone_number_id, access_token, api_version = (
        _load_whatsapp_config()
    )

    if not phone_number_id:
        raise RuntimeError(
            "WHATSAPP_PHONE_NUMBER_ID is missing. "
            "Configure it in the deployment environment or local .env."
        )

    if not access_token:
        raise RuntimeError(
            "WHATSAPP_ACCESS_TOKEN is missing. "
            "Configure it in the deployment environment or local .env."
        )

    recipient = str(to or "").strip()

    if not recipient:
        raise ValueError("Recipient phone number cannot be empty.")

    body = str(message or "").strip()

    if not body:
        raise ValueError("WhatsApp message cannot be empty.")

    if len(body) > MAX_WHATSAPP_TEXT_LENGTH:
        raise ValueError(
            "WhatsApp text messages must be "
            f"{MAX_WHATSAPP_TEXT_LENGTH} characters or fewer."
        )

    url = (
        "https://graph.facebook.com/"
        f"{api_version}/{phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": body},
    }

    # Safe diagnostics: never print the access token or authorization header.
    print("\n========== WHATSAPP OUTBOUND REQUEST ==========")
    print("Recipient:", _mask_identifier(recipient))
    print("Sender Phone Number ID:", _mask_identifier(phone_number_id))
    print("API version:", api_version)
    print("Token fingerprint:", _token_fingerprint(access_token))
    print("Project .env exists:", ENV_FILE.is_file())
    print(
        "Configuration priority: "
        "process environment first, .env fallback"
    )
    print("===============================================\n")

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.exceptions.Timeout as exc:
        print("WhatsApp API request timed out.")

        raise RuntimeError(
            "WhatsApp API timed out. Delivery status may be unknown. "
            "Check WhatsApp webhook status updates before retrying."
        ) from exc

    except requests.exceptions.RequestException as exc:
        # Do not log request headers, tokens, or the complete request.
        print(
            "WhatsApp network request failed:",
            type(exc).__name__,
        )

        raise RuntimeError(
            "WhatsApp network request failed: "
            f"{type(exc).__name__}"
        ) from exc

    try:
        result = response.json()
    except ValueError:
        result = {"response": response.text[:500]}

    if not response.ok:
        error = (
            result.get("error", {})
            if isinstance(result, dict)
            else {}
        )

        code = error.get("code", "unknown")
        subcode = error.get("error_subcode", "unknown")
        error_type = error.get("type", "unknown")
        error_message = error.get(
            "message",
            "No error message returned by Meta",
        )

        print("\n========== WHATSAPP API ERROR ==========")
        print("HTTP status:", response.status_code)
        print("Meta error code:", code)
        print("Meta error subcode:", subcode)
        print("Meta error type:", error_type)
        print("Meta error message:", error_message)

        trace_id = error.get("fbtrace_id")
        if trace_id:
            print("Meta trace ID:", trace_id)

        if response.status_code == 401 or str(code) == "190":
            print("\nAuthentication checks:")
            print("1. Verify the configured token fingerprint.")
            print("2. Confirm the token has not expired or been revoked.")
            print("3. Confirm the required WhatsApp permissions.")
            print("4. Confirm access to the configured phone number.")
            print("5. Confirm the intended token is configured.")
            print(
                "6. If Meta rejects the token, obtain a valid token "
                "from Meta."
            )

        print("========================================\n")

        raise RuntimeError(
            "WhatsApp send failed: "
            f"HTTP {response.status_code}, "
            f"Meta code {code}, "
            f"subcode {subcode}: {error_message}"
        )

    if not isinstance(result, dict):
        raise RuntimeError(
            "WhatsApp API returned an unexpected response format."
        )

    messages = result.get("messages")

    if not messages:
        print(
            "Warning: Meta returned success without a messages entry."
        )
    else:
        print("Meta accepted outbound message.")
        print(
            "WhatsApp message ID:",
            messages[0].get("id", "unknown"),
        )

    # Meta accepting a request does not guarantee delivery.
    return result
