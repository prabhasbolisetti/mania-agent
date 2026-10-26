"""Safely patch an existing Mania Mobiles routes.py without replacing the file.

Run from the mania-agent project root:
    python fix_webhook_duplicates.py

The patch adds an in-process lock for concurrent Meta webhook retries while
retaining the existing Supabase message-ID deduplication check and all routes.
"""
from pathlib import Path
import sys

ROOT = Path.cwd()
route_path = ROOT / "app" / "api" / "routes.py"
storage_path = ROOT / "app" / "database" / "storage.py"
if not route_path.exists():
    print("ERROR: Run this script from the mania-agent project root; app/api/routes.py was not found.")
    sys.exit(1)

s = route_path.read_text(encoding="utf-8")
if "_WEBHOOK_INFLIGHT_IDS" in s and "_claim_whatsapp_message_id" in s:
    print("Webhook in-flight deduplication is already installed; no changes made.")
    sys.exit(0)

if "from starlette.concurrency import run_in_threadpool" not in s:
    print("ERROR: routes.py does not match the expected run_in_threadpool webhook layout. No changes made.")
    sys.exit(2)

if "has_processed_whatsapp_message" not in s or "attach_whatsapp_message_id" not in s:
    print("ERROR: Existing persistent WhatsApp message-ID deduplication imports were not found. No changes made.")
    sys.exit(3)

if "from threading import Lock" not in s:
    s = s.replace("import traceback\n", "import traceback\nfrom threading import Lock\nfrom collections import deque\n", 1)

anchor = '''WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")\n'''
helper = '''WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

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
'''
if anchor not in s:
    print("ERROR: Could not find WhatsApp route configuration anchor. No changes made.")
    sys.exit(4)
s = s.replace(anchor, helper, 1)

old = '''                    whatsapp_message_id = message.get("id")
                    if whatsapp_message_id:
                        try:
                            if await run_in_threadpool(has_processed_whatsapp_message, whatsapp_message_id):
                                print("Ignoring duplicate WhatsApp message ID.")
                                continue
                        except Exception as exc:
                            # Continue service during a temporary lookup failure;
                            # database errors are logged without exposing customer data.
                            print("Webhook deduplication lookup failed:", type(exc).__name__)
                    customer_id = f"wa_{sender}"
'''
new = '''                    whatsapp_message_id = message.get("id")
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
'''
if old not in s:
    print("ERROR: Could not find the existing webhook message-ID check. No changes made.")
    sys.exit(5)
s = s.replace(old, new, 1)

old = '''                        # Do not send a fake response. The traceback above
                        # exposes the real failure so it can be fixed.
                        continue
                    # -------------------------------------------------
                    # 5. Extract and send agent response
'''
new = '''                        # Do not send a fake response. The traceback above
                        # exposes the real failure so it can be fixed.
                        _finish_whatsapp_message_id(whatsapp_message_id)
                        continue
                    # -------------------------------------------------
                    # 5. Extract and send agent response
'''
if old not in s:
    print("ERROR: Could not find the agent-processing error branch. No changes made.")
    sys.exit(6)
s = s.replace(old, new, 1)

old = '''                        if whatsapp_message_id:
                            await run_in_threadpool(
                                attach_whatsapp_message_id,
                                customer_id,
                                message_text,
                                whatsapp_message_id,
                            )
                        print("\\n========== WHATSAPP SEND RESULT ==========")
'''
new = '''                        if whatsapp_message_id:
                            await run_in_threadpool(
                                attach_whatsapp_message_id,
                                customer_id,
                                message_text,
                                whatsapp_message_id,
                            )
                            _finish_whatsapp_message_id(whatsapp_message_id, completed=True)
                        print("\\n========== WHATSAPP SEND RESULT ==========")
'''
if old not in s:
    print("ERROR: Could not find the successful WhatsApp send branch. No changes made.")
    sys.exit(7)
s = s.replace(old, new, 1)

old = '''                        print("==========================================\\n")
                        continue
    except Exception as exc:
'''
new = '''                        print("==========================================\\n")
                        _finish_whatsapp_message_id(whatsapp_message_id)
                        continue
    except Exception as exc:
'''
if old not in s:
    print("ERROR: Could not find the WhatsApp send-error branch. No changes made.")
    sys.exit(8)
s = s.replace(old, new, 1)

route_path.write_text(s, encoding="utf-8")
print("Patched app/api/routes.py in place; all other routes were preserved.")
print("Next run: python -m py_compile app/api/routes.py && python -m pytest -q")
