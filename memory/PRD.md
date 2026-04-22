# PGE Returns Portal — PRD

## Source
Original repo: https://github.com/patellimited/pge-returns-portal (deployed on Render). Cloned into `/app` for iterative feature work in this session.

## Scope of This Session (2026-04-22)
User asked for two feature changes on top of the existing portal, plus three UI refinements. Nothing else was to be touched.

### Features Completed
1. **Store credit closes the return (per-item blocking preserved)**
   - When store credit is issued, the return document is now explicitly marked:
     `closed: true`, `closed_reason: "store_credit_applied"`, `closed_at: <ISO timestamp>`.
   - `POST /api/admin/returns/{id}/mark-refunded` similarly sets `closed=true`, `closed_reason="refunded"`, `closed_at`.
   - Frontend status label updated from "Store credit issued" → **"Closed — Store credit applied"**.
   - Admin dashboard detail panel now shows a dark "Closed" badge next to the status when the return is closed.
   - `POST /api/returns` conflict error message improved: when a line item already has store credit issued, the customer gets *"Store credit has already been applied for: {item}. This item's return is closed. You can still return other items from this order."* (per-item blocking behavior was already in place via `ACTIVE_RETURN_STATUSES`, just made clearer).
   - New fields added to `ReturnRequestDoc` model: `closed`, `closed_reason`, `closed_at`.

2. **Shiptheory → Easyship swap (fallback rate provider)**
   - Deleted `backend/shiptheory_service.py`.
   - Added `backend/easyship_service.py` — implements `create_shipment`, `purchase_label`, and `ping` against Easyship v2023-01 public API. Rate IDs are prefixed `es_` to route label purchases back to Easyship.
   - `settings_service.py`: removed `shiptheory_email` / `shiptheory_password`; added `easyship_api_key` (secret).
   - `integrations_ping.py`: removed nothing, added Easyship ping via `easyship_service.ping()`.
   - `server.py`: imports `easyship_service` instead of `shiptheory_service`; rate-merge loops and `_provider_for_rate` / `_purchase_label_multi` now route to Easyship.
   - `AdminSettings.jsx`: Shiptheory section replaced with Easyship (single API-key field).
   - CONN_LABELS and the "Test connections" panel now show **Easyship**.

### UI Refinements
3. **Landing page trust card removed** (the "Trusted process · Secure & encrypted" floating card). Replaced with a subtle footer chip `🔒 SECURE · EASY · ENCRYPTED` (Landing footer + OrderLookup bottom).
4. **Store credit button is now shiny** (`method-card-shine` class): warm gold gradient, animated light-sweep shimmer, pulsing bonus chip, custom SVG gift box with bobbing ribbon and sparkles. All motion respects `prefers-reduced-motion`.

## Untouched (user requested)
- WooCommerce, Stripe, Shippo, Royal Mail Click & Drop integrations.
- All email templates + Brevo/SendGrid/Resend/SMTP routing.
- Admin approve/reject flow with attachment upload.
- Analytics dashboard, customer proof uploads, weight-based shipping.
- Warehouse address config, branding, archive/delete flow.
- Any of the user's previous feature iterations.

## Architecture
- **Backend:** FastAPI (Python), motor (async MongoDB), supervisor-managed on `0.0.0.0:8001`, all routes under `/api`.
- **Frontend:** React + CRACO, Tailwind, Phosphor icons, sonner toasts, served on `:3000`.
- **DB:** MongoDB `test_database` (dev) with collections `returns`, `payment_transactions`, `app_settings`.
- **Hosting:** Render (prod) + Emergent preview (`https://pge-portal-dev.preview.emergentagent.com`) for this dev cycle.

## Testing
- Backend iteration 6 tests live at `/app/backend/tests/test_iter6_easyship_storecredit.py` — 10/10 passing.
- See `/app/test_reports/iteration_1.json`.

## Backlog / Future
- P1: Wire Easyship rate refresh on address change (same UX hints as Shippo).
- P2: Split `server.py` into admin/customer/webhook routers for maintainability.
- P2: Emit a dedicated "Closed — store credit" email confirmation (today's single coupon email already covers this).
- P3: Admin UI filter/badge specifically for `closed` returns vs active ones.

## Credentials
See `/app/memory/test_credentials.md`.
