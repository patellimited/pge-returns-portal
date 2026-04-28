# Apple Pay Domain Verification

This folder (`public/.well-known/`) serves Apple's domain-verification file
required to enable **Apple Pay on Stripe Checkout** on your production site.

## How to fill it in (one-time setup)

1. Open **Stripe Dashboard** → https://dashboard.stripe.com/settings/payment_methods
2. Find **Apple Pay** → click **Configure** → **Add new domain**
3. Enter your live Render domain (e.g. `returns.pgelimited.com`)
4. Stripe shows a file called `apple-developer-merchantid-domain-association`
   with a long opaque string. **Click "Download" or "Copy contents"**.
5. Open the file at
   `frontend/public/.well-known/apple-developer-merchantid-domain-association`
   in your repo, **delete the placeholder line**, paste the Stripe-provided
   contents, save, commit, push.
6. Back in Stripe → click **Verify**. Stripe fetches
   `https://YOUR_DOMAIN/.well-known/apple-developer-merchantid-domain-association`
   and confirms it matches.
7. Apple Pay is now live — iOS/macOS Safari customers will see the Apple Pay
   button automatically on your Stripe Checkout page.

## Why this works

- Files inside `frontend/public/` are copied verbatim into the React build
  output by Create React App, preserving the folder structure.
- So after build, this file lives at `/.well-known/apple-developer-merchantid-domain-association`
  on your site — exactly where Apple + Stripe look for it.

## No code change needed in the app

Apple Pay is rendered automatically by Stripe Checkout once the domain is
verified. Nothing else to do.
