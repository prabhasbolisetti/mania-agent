# Mania Mobiles — Products & Campaigns Phase 1

This is an additive implementation bundle. It does not replace existing routes,
lead storage, conversation logic, or `data/mobiles.json`.

## Included
- `app/api/product_campaign_routes.py`: authenticated product listing, product
  editing, interest analytics, and campaign eligibility preview endpoints.
- `ui/src/pages/ProductsCampaigns.jsx` + CSS: product management UI.
- `supabase/phase1_campaign_columns.sql`: opt-in/window metadata required before
  campaign sending can be enabled.

## 1. Supabase migration
Run `supabase/phase1_campaign_columns.sql` in Supabase SQL Editor. This adds
opt-in and 24-hour-window metadata to `public.leads`. Existing rows default to
marketing opt-in false. Do not mass-mark existing leads as opted in.

The code assumes the existing `products` table has `price`, `battery_health`,
`campaign_enabled`, and the columns listed in the SELECT. The schema shared for
this project included these fields. If `price_verified` was not yet added,
remove `price_verified` from the list in the route's products SELECT.

## 2. Install backend route without replacing app/api/routes.py
In `app/main.py`, add:
```python
from app.api.product_campaign_routes import router as product_campaign_router
```
Then after the existing `app.include_router(router, prefix="/api")`, add:
```python
app.include_router(product_campaign_router, prefix="/api")
```
The feature endpoints are:
- `GET /api/products-campaigns/products`
- `PATCH /api/products-campaigns/products/{product_id}`
- `POST /api/products-campaigns/campaign-preview`

All require the existing admin session cookie.

## 3. Install UI
Copy the JSX and CSS into `ui/src/pages/`.
Then add a route in your existing React router, e.g.:
```jsx
import ProductsCampaigns from "./pages/ProductsCampaigns";
// inside the existing route tree, within the protected dashboard layout:
<Route path="/products-campaigns" element={<ProductsCampaigns />} />
```
Add a sidebar link to `/products-campaigns` in the existing Sidebar component. Do
not replace `App.jsx` or `Sidebar.jsx` wholesale; add one link/route in context.

The UI calls the backend with `credentials: "include"` so the existing cookie
authentication is retained.

## 4. Important analytics limitation
Interest counts are based on stored lead-level `product_interest`, `product_url`,
and `customer_data` fields, deduplicated by phone. This does not count every raw
message in `messages`. It is a first pass based on already-extracted lead data.
A later analytics refinement can query messages with conversation linkage and
build a materialized interest table if exact message-level counts are required.

## 5. Sending is intentionally disabled
The campaign preview distinguishes:
- leads with matching stored interest,
- leads with explicit `marketing_opt_in = true`,
- recipients whose customer-service window is still open,
- recipients needing an approved template.

`Send campaign` remains disabled. Before enabling actual sending, wire a sender
that checks opt-in, uses free-form messages only within the 24-hour customer
service window, uses approved WhatsApp templates outside it, handles opt-outs,
uses a persistent campaign queue, applies rate limits, deduplicates recipients,
and records Meta webhook delivery statuses. Do not use the interest count alone
as permission to send marketing messages.
