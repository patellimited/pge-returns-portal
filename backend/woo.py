"""WooCommerce REST client. Config is injected so it can be changed at runtime via admin settings."""
import asyncio
import time
import httpx
from typing import Optional, Dict, Tuple
from models import OrderResponse, LineItem, Address


# Simple in-process cache for product weight lookups to avoid hammering
# WooCommerce when the same product appears across multiple orders.
# Key: (store_url, product_id) -> (weight_float_or_None, timestamp)
_PRODUCT_WEIGHT_CACHE: Dict[Tuple[str, str], Tuple[Optional[float], float]] = {}
# Key: store_url -> (weight_unit, timestamp)
_WEIGHT_UNIT_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL = 10 * 60  # 10 minutes


async def _fetch_weight_unit(client: httpx.AsyncClient, store: str, auth) -> str:
    """Read the store-wide weight unit (kg/lbs/g/oz). Cached for 10 min.
    Falls back to 'kg' on any failure — safest default for UK stores."""
    now = time.time()
    cached = _WEIGHT_UNIT_CACHE.get(store)
    if cached and cached[1] > now:
        return cached[0]
    unit = "kg"
    try:
        r = await client.get(
            f"{store}/wp-json/wc/v3/settings/products/woocommerce_weight_unit",
            auth=auth,
        )
        if r.status_code == 200:
            data = r.json() or {}
            val = (data.get("value") or data.get("default") or "kg")
            if isinstance(val, str) and val.strip():
                unit = val.strip().lower()
    except Exception:
        pass
    _WEIGHT_UNIT_CACHE[store] = (unit, now + _CACHE_TTL)
    return unit


async def _fetch_product_weight(client: httpx.AsyncClient, store: str, auth,
                                product_id: str) -> Optional[float]:
    """Read a product's weight as configured in WooCommerce. Returns None
    when the field is empty/missing. Cached for 10 min to cut round-trips."""
    if not product_id:
        return None
    now = time.time()
    key = (store, str(product_id))
    cached = _PRODUCT_WEIGHT_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]
    weight: Optional[float] = None
    try:
        r = await client.get(f"{store}/wp-json/wc/v3/products/{product_id}", auth=auth)
        if r.status_code == 200:
            prod = r.json() or {}
            raw = prod.get("weight")
            if raw not in (None, "", "0", 0):
                try:
                    weight = float(raw)
                    if weight <= 0:
                        weight = None
                except (TypeError, ValueError):
                    weight = None
            # Variation products carry their own weight; parent is the fallback.
            if weight is None and prod.get("parent_id"):
                try:
                    pr = await client.get(
                        f"{store}/wp-json/wc/v3/products/{prod['parent_id']}", auth=auth)
                    if pr.status_code == 200:
                        praw = (pr.json() or {}).get("weight")
                        if praw not in (None, "", "0", 0):
                            weight = float(praw)
                            if weight <= 0:
                                weight = None
                except Exception:
                    pass
    except Exception:
        weight = None
    _PRODUCT_WEIGHT_CACHE[key] = (weight, now + _CACHE_TTL)
    return weight


def _build_address(data: Optional[dict], fallback_email: str) -> Optional[Address]:
    if not data:
        return None
    name = f"{data.get('first_name','').strip()} {data.get('last_name','').strip()}".strip() or "Customer"
    return Address(
        name=name,
        street1=data.get("address_1", "") or "",
        street2=data.get("address_2", "") or "",
        city=data.get("city", "") or "",
        state=data.get("state", "") or "",
        zip=data.get("postcode", "") or "",
        country=data.get("country", "US") or "US",
        phone=data.get("phone", "") or "",
        email=data.get("email", fallback_email) or fallback_email,
    )


async def fetch_order(order_id: str, email: str, cfg: Dict[str, str]) -> Optional[OrderResponse]:
    """cfg must contain wc_store_url, wc_consumer_key, wc_consumer_secret.

    Smart matching:
      1. Try direct order_id lookup (fastest when it's a real WooCommerce id).
      2. If that fails or email doesn't match, fall back to searching by
         `number` (the human-facing order number) + email. Multiple orders can
         share the same `number` in edge cases — we pick the one whose billing
         email matches the email provided.
    """
    order_id = (order_id or "").strip()
    email = (email or "").strip().lower()

    store = (cfg.get("wc_store_url") or "").rstrip("/")
    key = cfg.get("wc_consumer_key") or ""
    secret = cfg.get("wc_consumer_secret") or ""
    if not store or not key or not secret:
        return None

    async def _direct():
        url = f"{store}/wp-json/wc/v3/orders/{order_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, auth=(key, secret))
        except Exception:
            return None
        return r if r.status_code == 200 else None

    async def _search_by_number():
        url = f"{store}/wp-json/wc/v3/orders"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # WooCommerce supports ?search={number} which checks order number & emails
                r = await client.get(url, auth=(key, secret),
                                     params={"search": order_id, "per_page": 20})
        except Exception:
            return None
        if r.status_code != 200:
            return None
        results = r.json() or []
        # Prefer exact order_number match whose billing email == provided email
        exact_match = None
        for item in results:
            num = str(item.get("number") or item.get("id") or "")
            billing_email = ((item.get("billing") or {}).get("email") or "").lower()
            if num == order_id and billing_email == email:
                exact_match = item
                break
        if exact_match:
            return exact_match
        # Fall-back: any order in results with matching email
        for item in results:
            billing_email = ((item.get("billing") or {}).get("email") or "").lower()
            if billing_email == email:
                return item
        return None

    direct = await _direct()
    data: Optional[dict] = None
    if direct is not None:
        data = direct.json()
        billing = data.get("billing", {}) or {}
        order_email = (billing.get("email") or data.get("customer_email") or "").lower()
        if order_email and order_email != email:
            # Email mismatch on direct id — try the search path
            data = await _search_by_number()
    else:
        data = await _search_by_number()

    if not data:
        return None

    billing = data.get("billing", {}) or {}
    order_email = (billing.get("email") or data.get("customer_email") or "").lower()
    if order_email and order_email != email:
        return None

    # Pull the store-wide weight unit + each product's weight in parallel.
    async with httpx.AsyncClient(timeout=15.0) as client:
        auth = (key, secret)
        weight_unit = await _fetch_weight_unit(client, store, auth)
        raw_line_items = data.get("line_items", []) or []
        unique_product_ids = list({str(li.get("product_id")) for li in raw_line_items
                                   if li.get("product_id")})
        weight_tasks = [
            _fetch_product_weight(client, store, auth, pid)
            for pid in unique_product_ids
        ]
        weights = await asyncio.gather(*weight_tasks) if weight_tasks else []
    weight_by_pid: Dict[str, Optional[float]] = dict(zip(unique_product_ids, weights))

    line_items = []
    for li in raw_line_items:
        image_url = ""
        if isinstance(li.get("image"), dict):
            image_url = li["image"].get("src", "")
        price = float(li.get("price") or (float(li.get("total", 0)) / max(li.get("quantity", 1), 1)))
        pid = str(li.get("product_id") or "")
        line_items.append(LineItem(
            id=str(li.get("id")),
            product_id=pid,
            name=li.get("name", "Item"),
            sku=li.get("sku", ""),
            quantity=int(li.get("quantity", 1)),
            price=price,
            image=image_url,
            weight=weight_by_pid.get(pid),
            weight_unit=weight_unit,
        ))

    first_name = (billing.get("first_name") or "").strip()
    last_name = (billing.get("last_name") or "").strip()
    customer_name = (f"{first_name} {last_name}").strip() or "Customer"

    return OrderResponse(
        order_id=str(data.get("id")),
        order_number=str(data.get("number") or data.get("id")),
        email=order_email or email,
        customer_name=customer_name,
        billing_address=_build_address(billing, order_email or email),
        shipping_address=_build_address(data.get("shipping") or billing, order_email or email),
        line_items=line_items,
        total=float(data.get("total", 0)),
        currency=data.get("currency", "USD"),
        status=data.get("status"),
        date_created=data.get("date_created"),
    )



async def create_coupon(
    cfg: Dict[str, str],
    *,
    email: str,
    amount: float,
    expires_on: Optional[str] = None,
    code_prefix: str = "RMA",
    reference: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[Dict]:
    """Create a single-use WooCommerce fixed-cart coupon for store credit.

    Returns the created coupon dict (including `code` and `id`) or None on
    failure. We intentionally swallow errors here so the caller can decide
    how to surface the problem to the customer.

    Security-sensible defaults:
      - fixed_cart (amount applies to the whole cart total)
      - individual_use = True  (cannot be combined with other coupons)
      - usage_limit = 1        (single-redeem)
      - email_restrictions     (locked to the customer's email)
      - date_expires           (optional; ISO date yyyy-mm-dd)
    """
    store = (cfg.get("wc_store_url") or "").rstrip("/")
    key = cfg.get("wc_consumer_key") or ""
    secret = cfg.get("wc_consumer_secret") or ""
    if not store or not key or not secret or amount <= 0 or not email:
        return None

    import secrets as _secrets
    code_suffix = _secrets.token_hex(4).upper()  # 8 hex chars
    ref = (reference or "").replace(" ", "").upper()[:10] or code_suffix
    code = f"{code_prefix}-{ref}-{code_suffix}"

    payload = {
        "code": code,
        "discount_type": "fixed_cart",
        "amount": f"{float(amount):.2f}",
        "individual_use": True,
        "usage_limit": 1,
        "usage_limit_per_user": 1,
        "email_restrictions": [email],
        "description": description or f"Store credit for RMA {ref}",
    }
    if expires_on:
        payload["date_expires"] = expires_on

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{store}/wp-json/wc/v3/coupons",
                auth=(key, secret),
                json=payload,
            )
        if r.status_code in (200, 201):
            return r.json()
        # 400 with `woocommerce_rest_coupon_code_already_exists` -> retry once
        # with a new suffix (extremely unlikely thanks to secrets.token_hex).
        return None
    except Exception:
        return None
