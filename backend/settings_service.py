"""Runtime settings layer.

Values are stored in MongoDB (`app_settings` collection, single doc id="config").
Any key missing from DB falls back to the corresponding env var.
Admin can update any of these from the dashboard without redeploy.
"""
import os
from typing import Dict, Any, Optional

SETTINGS_KEYS = [
    # WooCommerce
    "wc_store_url", "wc_consumer_key", "wc_consumer_secret",
    # Shippo
    "shippo_api_key",
    # Easyship (fallback rate provider)
    "easyship_api_key",
    "easyship_box_slug",
    # Royal Mail Click & Drop (fallback rate provider)
    "royal_mail_api_key",
    # Stripe
    "stripe_api_key", "stripe_publishable_key",
    # Warehouse return address
    "warehouse_name", "warehouse_street", "warehouse_city", "warehouse_state",
    "warehouse_zip", "warehouse_country", "warehouse_phone", "warehouse_email",
    # Brand & storefront
    "store_name", "support_email", "logo_url", "hero_image_url",
    # Return policy
    "max_return_window_days",
    # Weight-based shipping
    "default_item_weight_kg",   # used when a product has no weight set in Woo
    "min_parcel_weight_kg",     # floor applied to the final parcel weight
    # Store credit / exchange (WooCommerce coupon instead of Stripe refund)
    "enable_store_credit",
    "store_credit_bonus_percent",   # e.g. "5" gives the customer 5% extra
    "store_credit_expiry_days",     # coupon life-time in days
    # Brevo email
    "brevo_api_key", "from_email", "from_name",
    # Additional email providers (fallback chain)
    "sendgrid_api_key", "resend_api_key",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass",
    "email_provider_order",  # e.g. "brevo,sendgrid,resend,smtp"
    "admin_notification_email",  # where "new return opened" emails go
]

ENV_MAP = {
    "wc_store_url": "WC_STORE_URL",
    "wc_consumer_key": "WC_CONSUMER_KEY",
    "wc_consumer_secret": "WC_CONSUMER_SECRET",
    "shippo_api_key": "SHIPPO_API_KEY",
    "easyship_api_key": "EASYSHIP_API_KEY",
    "easyship_box_slug": "EASYSHIP_BOX_SLUG",
    "royal_mail_api_key": "ROYAL_MAIL_API_KEY",
    "stripe_api_key": "STRIPE_API_KEY",
    "stripe_publishable_key": "STRIPE_PUBLISHABLE_KEY",
    "warehouse_name": "WAREHOUSE_NAME",
    "warehouse_street": "WAREHOUSE_STREET",
    "warehouse_city": "WAREHOUSE_CITY",
    "warehouse_state": "WAREHOUSE_STATE",
    "warehouse_zip": "WAREHOUSE_ZIP",
    "warehouse_country": "WAREHOUSE_COUNTRY",
    "warehouse_phone": "WAREHOUSE_PHONE",
    "warehouse_email": "WAREHOUSE_EMAIL",
    "store_name": "STORE_NAME",
    "support_email": "SUPPORT_EMAIL",
    "logo_url": "LOGO_URL",
    "hero_image_url": "HERO_IMAGE_URL",
    "max_return_window_days": "MAX_RETURN_WINDOW_DAYS",
    "default_item_weight_kg": "DEFAULT_ITEM_WEIGHT_KG",
    "min_parcel_weight_kg": "MIN_PARCEL_WEIGHT_KG",
    "enable_store_credit": "ENABLE_STORE_CREDIT",
    "store_credit_bonus_percent": "STORE_CREDIT_BONUS_PERCENT",
    "store_credit_expiry_days": "STORE_CREDIT_EXPIRY_DAYS",
    "brevo_api_key": "BREVO_API_KEY",
    "from_email": "FROM_EMAIL",
    "from_name": "FROM_NAME",
    "sendgrid_api_key": "SENDGRID_API_KEY",
    "resend_api_key": "RESEND_API_KEY",
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_pass": "SMTP_PASS",
    "email_provider_order": "EMAIL_PROVIDER_ORDER",
    "admin_notification_email": "ADMIN_NOTIFICATION_EMAIL",
}

# Fields that should be masked when exposed to the admin UI (we still return a "set/unset" flag).
SECRET_KEYS = {"wc_consumer_secret", "shippo_api_key", "stripe_api_key",
               "easyship_api_key", "royal_mail_api_key",
               "brevo_api_key", "sendgrid_api_key", "resend_api_key", "smtp_pass"}

# Hard-coded fallbacks (used only if both DB and env var are empty).
CODE_DEFAULTS = {
    "admin_notification_email": "Returns@pgelimited.com",
    "default_item_weight_kg": "1.0",
    "min_parcel_weight_kg": "0.1",
    "enable_store_credit": "true",
    "store_credit_bonus_percent": "5",
    "store_credit_expiry_days": "365",
}


async def get_settings(db) -> Dict[str, str]:
    """Return merged settings: MongoDB values overlaid on env defaults (then code defaults)."""
    doc = await db.app_settings.find_one({"_id": "config"}) or {}
    out: Dict[str, str] = {}
    for k in SETTINGS_KEYS:
        val = doc.get(k)
        if not val:
            val = os.environ.get(ENV_MAP[k], "")
        if not val:
            val = CODE_DEFAULTS.get(k, "")
        out[k] = val or ""
    return out


async def update_settings(db, updates: Dict[str, Any]) -> Dict[str, str]:
    clean: Dict[str, Any] = {}
    for k, v in updates.items():
        if k in SETTINGS_KEYS and v is not None and v != "":
            clean[k] = str(v).strip()
    if clean:
        await db.app_settings.update_one(
            {"_id": "config"}, {"$set": clean}, upsert=True
        )
    return await get_settings(db)


async def get_public_settings(db) -> Dict[str, Any]:
    """Settings safe to return to the admin UI (secrets masked, presence flagged)."""
    s = await get_settings(db)
    out: Dict[str, Any] = {}
    for k, v in s.items():
        if k in SECRET_KEYS:
            out[k] = ""  # never return the secret
            out[f"{k}_set"] = bool(v)
            if v:
                out[f"{k}_preview"] = v[:6] + "…" + v[-4:] if len(v) > 12 else "set"
        else:
            out[k] = v
    return out
