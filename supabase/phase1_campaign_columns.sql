-- Mania Mobiles: product campaigns Phase 1
-- Run in Supabase SQL Editor. Existing product rows and mobiles.json are untouched.
-- products.price, products.battery_health, and products.campaign_enabled are
-- assumed to exist in the current schema as described by the application.

ALTER TABLE public.leads
  ADD COLUMN IF NOT EXISTS marketing_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS marketing_opt_in_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS whatsapp_window_expires_at TIMESTAMPTZ;

COMMENT ON COLUMN public.leads.marketing_opt_in IS
  'Explicit permission to receive promotional WhatsApp messages; default false.';
COMMENT ON COLUMN public.leads.marketing_opt_in_at IS
  'Timestamp when marketing opt-in was recorded from an approved consent flow.';
COMMENT ON COLUMN public.leads.whatsapp_window_expires_at IS
  'End of the 24-hour customer service window, updated from inbound customer messages.';
