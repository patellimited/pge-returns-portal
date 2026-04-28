# PGE Returns Portal — PRD

## Source
- Cloned from https://github.com/patellimited/pge-returns-portal (preserves `.git` /
  `.emergent` so the user can push to `main` via the "Save to GitHub" feature).
- Tech stack: FastAPI + Motor (MongoDB) backend, React (CRACO) + Tailwind + Radix UI
  frontend.

## Original problem statement (Jan 2026 iteration)
User asked us to clone the repo and fix a list of 11 bugs / improvements, organised
across the customer flow, admin dashboard, store-credit math, email templates, the
landing page badge, and global branding hooks.

## What's been implemented (Jan 2026)
### Customer flow (ReturnMethod.jsx)
- Restricted (manual-review) reason + Store Credit no longer skips shipping selection.
  A 2-option sub-picker appears: **Free shipping label** (note: "Label will be emailed
  once approved") OR **Self-ship**. The "Pay with card" / "Deduct from refund" cards
  are hidden in this case (it's a free return).
- Non-restricted Store Credit gains **Self-ship** as a 3rd handling option alongside
  "Deduct from store credit" and "Pay with card". Self-ship goes through the normal
  awaiting_tracking flow; admin still issues the coupon when the parcel arrives.
- "Refund subtotal" line is now bonus-aware: shows base + bonus − any label deduction
  for store-credit, and "Store credit total" for the customer.

### Backend `restricted_shipping_choice` field
- New optional field on `CreateReturnRequest`: `"free_label" | "self_ship" | null`.
- For restricted + store_credit it determines which approval flow the admin sees.
- For non-restricted store_credit it's reused for the new "self-ship" sub-choice.

### Admin Dashboard (AdminDashboard.jsx + server.py)
- Two-stage approval for restricted store_credit:
  1. **Approve label** (or **Approve self-ship**, depending on customer choice) —
     status flips to `approved` / `label_purchased` / `awaiting_tracking`. **No coupon
     is issued.**
  2. **Approve store credit** — admin clicks once the parcel physically arrives.
     Coupon is created in WooCommerce and emailed to the customer.
- `/api/admin/returns/{id}/approve-free` is now stage-aware (uses the doc's status to
  decide which step it is).

### Store-credit math
- Bonus is applied to the FULL refund subtotal, then any label deduction is subtracted:
  `coupon_amount = round(refund_amount * (1 + bonus_pct/100) − refund_deduction, 2)`.
- New doc field `coupon_label_deduction` records the deduction for audit / display.

### Revoke store credit
- `/api/admin/returns/{id}/revoke-store-credit` now sends a customer-facing email
  (`send_store_credit_revoked_to_customer`) asking them to open a support ticket.
  The wrong `send_admin_label_failure` email it used before has been removed.
- Idempotent: re-calling on an already-revoked return returns `{already_revoked: true}`
  instead of a 400.

### Email templates (email_service.py)
- `_base_html` now accepts `cfg` and renders a single dynamic footer:
  `Questions? Email <support_email>` + `<portal_public_url>` + `© <year> <store_name>`.
- All transactional templates updated to pass `cfg` and to drop their inline
  "Questions? Email …" line — no more duplicated footer text.
- Self-ship instructions email now includes a prominent **Add tracking number** button
  pointing at `<portal_public_url>/track?rma=<rma>`.
- Smart copy via `_smart_post_arrival_line()`:
  - store_credit → "We'll process your store credit once the parcel arrives…"
  - deduct_from_refund → "Your refund (minus the return shipping label cost) will be
    processed…"
  - pay_stripe → "We'll process your refund to your card once…"
  - free_label → "We'll process your refund to your original payment method…"

### Internal admin notes (Issue F)
- New endpoint `POST /api/admin/returns/{id}/internal-notes` (admin auth, max 5000 chars,
  rejects empty input). Each note is timestamped + tagged with admin email.
- New `internal_notes: List[{at, author, text}]` field on `ReturnRequestDoc`.
- AdminDashboard drawer renders an `InternalNotesPanel` with timeline view + add form.
  Notes are admin-only — never emailed, never returned to customers.

### Landing page badge (Issue G)
- Replaced the static "Live" pill with a dynamic **"X happy returns processed"** badge,
  pulled from `GET /api/stats/public` (counts `refunded`, `store_credit_issued`, and
  `delivered` returns).
- Hidden on a fresh install (count = 0).
- Mobile-friendly (smaller tracking + shortened "happy returns" label on small screens).

### Global branding (Issue H)
- Already in place: `store_name`, `support_email`, `logo_url`, `hero_image_url`,
  `portal_public_url`, `from_name`, `from_email` are all settings-driven (Admin →
  Settings → Global branding). Adding new brands is a no-deploy change.

## Test status
- **Backend regression: 27/27 passing** (testing agent iter6, see
  `/app/test_reports/iteration_6.json` and `/app/backend/tests/test_iter6_bug_bundle.py`).
- Frontend was NOT exercised in this iteration per the user's instruction (he asked
  not to spend credits on previewing).

## Mocked / unconfigured externals (preview env only)
- WooCommerce, Stripe, Shippo, Brevo / SendGrid / Resend / SMTP — no creds in this
  preview env. Coupon issuance & label purchase will return 502 until creds are
  configured under Admin → Settings (real production keys live on Render).

## Next action items (post user push to `main`)
- Run real end-to-end smoke on production once the user pushes (admin login, restricted
  + store_credit + free_label, restricted + store_credit + self_ship, non-restricted
  + self_ship sub-choice, revoke-store-credit, internal note add/list).
- Validate the new "happy returns" badge looks good with real DB counts.
- (Optional) Migrate FastAPI `@app.on_event` to lifespan handlers — not needed for
  correctness, just keeps us off deprecation warnings.

## Backlog (future)
- Split `server.py` (2.5k lines) into per-concern routers.
- Extract the 4 sub-flows of `approve_free_return` into named helpers for testability.
- Auto-retry queue for failed WooCommerce coupon creation.
