"""Check the WhatsApp credentials loaded by the application without exposing them.

Run from the project root:
    python scripts/check_whatsapp_config.py
    python scripts/check_whatsapp_config.py --send-to 919012345678

The --send-to option sends one real test message to the supplied number.
Only use it for a number you control or that has opted in to receive messages.
"""
import argparse
import hashlib
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.whatsapp.service import (
    _load_whatsapp_config,
    send_whatsapp_message,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-to",
        help="Optional WhatsApp recipient number with country code; sends a real test message.",
    )
    args = parser.parse_args()

    phone_number_id, token, api_version = _load_whatsapp_config()
    fingerprint = (
        hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        if token else "not-configured"
    )

    print("Config source: project .env plus environment variables (environment wins)")
    print("Phone number ID configured:", bool(phone_number_id))
    print("Access token configured:", bool(token))
    print("Token fingerprint:", fingerprint)
    print("API version:", api_version)

    if not phone_number_id or not token:
        print("ERROR: Set WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN.")
        return 2

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,display_phone_number"},
            timeout=20,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}

        print("Phone-number lookup HTTP status:", response.status_code)
        if response.ok:
            print("Meta phone number ID:", data.get("id", "not returned"))
            print("Display number:", data.get("display_phone_number", "not returned"))
        else:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            print("Meta error code:", error.get("code", "unknown"))
            print("Meta error:", error.get("message", response.text[:300]))
            print("This token/config cannot access the configured phone number.")
            return 1

    except requests.RequestException as exc:
        print("Network error while checking Meta:", type(exc).__name__, str(exc))
        return 1

    if args.send_to:
        print("Sending a real test message to:", args.send_to)
        try:
            result = send_whatsapp_message(
                args.send_to,
                "Mania Mobiles WhatsApp API connection test.",
            )
            print("Send accepted by Meta. Message ID:", result.get("messages", [{}])[0].get("id", "not returned"))
        except Exception as exc:
            print("Send failed:", type(exc).__name__, str(exc))
            print("Compare the token fingerprint above with the backend log.")
            print("OAuth error 190 requires replacing the rejected token in Meta.")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
