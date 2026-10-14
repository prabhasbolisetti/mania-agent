# Mania Mobiles Lead Qualification Agent

FastAPI backend + React/Vite admin dashboard + Supabase persistence + Meta WhatsApp Cloud API integration.

## Project layout

- `app/agent/` — extraction, language handling, response generation, and conversation flow
- `app/api/` — API endpoints and admin login/session handling
- `app/catalog/` — lookup against `data/mobiles.json`
- `app/database/` — Supabase persistence
- `app/qualification/` — intent scoring
- `app/whatsapp/` — Meta WhatsApp Cloud API client
- `ui/` — React dashboard
- `data/mobiles.json` — product catalogue snapshot
- `scripts/check_whatsapp_config.py` — safe WhatsApp configuration check
- `.env.example` — placeholder-only local environment template
- `tests/` — focused regression tests

## Requirements

- Python 3.11 recommended (source avoids Python 3.10-only union type syntax)
- Node.js 22 for the dashboard build
- A Supabase project with the existing `leads`, `conversations`, and `messages` tables/schema
- Meta WhatsApp Cloud API credentials if receiving/sending real WhatsApp messages

## Local setup (macOS)

```bash
cd ~/Documents/mania-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
# Optional, for running regression tests:
pip install -r requirements-dev.txt
cp .env.example .env
# Edit .env and fill in Supabase/admin values before starting the backend.

cd ui
npm ci
npm run build
cd ..
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the built dashboard and `http://127.0.0.1:8000/docs` for API docs. For frontend-only development, run `cd ui && npm run dev` in a second terminal; Vite proxies `/api` to port 8000.

## Environment variables

Copy `.env.example` to `.env` and replace every placeholder. Never commit `.env`. `SUPABASE_SECRET_KEY` must be a server-side Supabase secret/service-role key and must never be exposed to the browser. Configure `ADMIN_USERNAME`, a strong `ADMIN_PASSWORD`, and a unique random `SESSION_SECRET` (generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`). Use `COOKIE_SECURE=true` when served over HTTPS; keep it `false` only for local HTTP development. On Render, set these values in the service's Environment settings rather than uploading `.env`. Use a unique random `SESSION_SECRET`, strong admin credentials, `COOKIE_SECURE=true`, `WHATSAPP_APP_SECRET` from the Meta app, and `WHATSAPP_REQUIRE_SIGNATURE=true`. Replace every placeholder; do not deploy with template values.

LLM provider selection supports `LLM_PROVIDER=openai`, `ollama`, or `none`. Gemini integration is intentionally disabled in this version. For a hosted service, configure `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and (optionally) `OPENAI_MODEL`; without a provider, deterministic extraction and localized reply templates continue to work. For local development, configure `LLM_PROVIDER=ollama`, `OLLAMA_ENABLED=true`, and `OLLAMA_MODEL`. Ollama running on your laptop is not automatically reachable from Render. Validate quality and latency with the regression scenarios before production use.

## Docker / deployment

The included multi-stage `Dockerfile` builds the React app and runs the API from the same origin. Build and run locally:

```bash
docker build -t mania-agent .
docker run --rm -p 8000:8000 --env-file .env -e COOKIE_SECURE=true mania-agent
```

For a Render Web Service, choose **Docker** and deploy this repository. Add the required variables from `.env.example` in the Render environment settings; do not upload `.env`. Set `COOKIE_SECURE=true`. Configure the Meta webhook callback URL as `https://YOUR-SERVICE.onrender.com/api/webhook`, and use the same `WHATSAPP_VERIFY_TOKEN` in Meta and Render.

## Important production notes

- This code expects the existing Supabase schema for `leads`, `conversations`, and `messages`; do not reset or migrate production tables without a reviewed migration.
- Admin sessions are HMAC-signed, HTTP-only cookies, so they survive backend restarts and work across instances that share the same `SESSION_SECRET`. Logout deletes the browser cookie; a copied token remains valid until its 8-hour expiry, so keep the signing secret private and rotate it to invalidate existing sessions. Customer lead/conversation records are persisted in Supabase and the agent restores the latest saved session after a restart.
- In production, configure `COOKIE_SECURE=true`, `WHATSAPP_APP_SECRET`, and `WHATSAPP_REQUIRE_SIGNATURE=true`. Never deploy with placeholders. Webhook signature checks are not bypassed by default on Render.
- The local product catalogue is a snapshot. Search can match product names and catalogue attributes, but price filtering only trusts records explicitly marked `price_needs_verification: false`. Always confirm current stock and pricing with the store.
- `GET /health` checks process liveness only; it does not prove that Supabase or Meta is reachable.
- `data/mobiles.json` is a catalogue snapshot, not a live stock/price API. The agent avoids displaying prices unless a record explicitly sets `price_needs_verification: false`; current stock and prices must still be confirmed by the store.
- After checking for existing duplicate non-NULL message IDs, run `supabase_migrations/20261013_whatsapp_message_idempotency.sql` in the Supabase SQL editor to add a unique guard for successfully processed inbound Meta message IDs. Review the SQL before applying it to production.
- Webhook processing is moved off the ASGI event loop, and turns for one customer are serialized inside a worker. This is suitable for a controlled deployment/small-volume pilot, not a sudden 10,000-message burst. For that, add a durable queue (for example Redis-backed workers), shared idempotency/locking, monitoring, and load testing before scaling to multiple workers.
- `GET /health` checks process liveness only; it does not prove that Supabase or Meta is reachable.
- `data/leads/` is ignored by Git to avoid committing private customer records. Keep real secrets, customer exports, cookies, logs, backups, and build artifacts out of commits.

## Quick checks

```bash
python -m compileall -q app scripts tests
python -m pytest -q
cd ui && npm ci && npm run lint && npm run build
```
