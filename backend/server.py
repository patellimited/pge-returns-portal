"""Main FastAPI server for the PGE Return Portal - Render Compatible."""
import os
import logging
import random
import string
import stripe
import json
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, UploadFile, File, Form
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import base64

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from models import (
    OrderLookupRequest, OrderResponse, CreateReturnRequest, ReturnRequestDoc,
    CheckoutRequest, CheckoutResponse, PaymentStatusResponse,
    AdminLoginRequest, AdminLoginResponse, AdminNoteRequest, InternalNoteRequest,
    TrackingResponse, Address, CustomerAction, CustomerActionRequest,
    RatePreviewRequest, SelfShipTrackingRequest, SubscribeStatusRequest,
)
import auth as auth_svc
import woo
import shippo_service
import royal_mail_service
import settings_service
import brevo_service  # kept for backward compatibility
import email_service
import integrations_ping

# --- Emergent Polyfill (Replaces private tools with standard Stripe) ---
class CheckoutSessionRequest:
    def __init__(self, amount, currency, success_url, cancel_url, metadata):
        self.amount = amount
        self.currency = currency
        self.success_url = success_url
        self.cancel_url = cancel_url
        self.metadata = metadata

class StripeCheckout:
    def __init__(self, api_key: str, webhook_url: str):
        stripe.api_key = api_key
    
    async def create_checkout_session(self, req):
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': req.currency,
                    'product_data': {'name': f"Return Shipping - {req.metadata.get('rma', '')}"},
                    'unit_amount': int(req.amount * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=req.success_url,
            cancel_url=req.cancel_url,
            metadata=req.metadata
        )
        class SessionWrap:
            def __init__(self, s_id, url):
                self.session_id = s_id
                self.url = url
        return SessionWrap(session.id, session.url)

    async def get_checkout_status(self, session_id: str):
        session = stripe.checkout.Session.retrieve(session_id)
        class StatusWrap:
            def __init__(self, s, ps):
                self.status = s
                self.payment_status = ps
        return StatusWrap(session.status, session.payment_status)

    async def handle_webhook(self, body, sig):
        data = json.loads(body)
        class EventWrap:
            def __init__(self, ps, sid):
                self.payment_status = ps
                self.session_id = sid
        obj = data.get('data', {}).get('object', {})
        return EventWrap(obj.get('payment_status'), obj.get('id'))

# --- Setup ---
mongo_url = os.environ["MONGO_URL"]
db_name = os.environ["DB_NAME"]
mongo_client = AsyncIOMotorClient(mongo_url)
db = mongo_client[db_name]

app = FastAPI(title="PGE Return Portal")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("return-portal")

def _rma() -> str:
    return "RMA-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Prefers the leftmost X-Forwarded-For entry
    (Render / most reverse proxies), then X-Real-IP, then the direct
    socket peer. Falls back to "unknown" if everything is missing — rate
    limiting still works (all unknowns share a bucket, which is the safer
    failure mode)."""
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip") or request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

def _warehouse_from_cfg(cfg: Dict[str, str]) -> Address:
    return Address(
        name=cfg.get("warehouse_name") or "Returns Center",
        street1=cfg.get("warehouse_street") or "",
        city=cfg.get("warehouse_city") or "",
        state=cfg.get("warehouse_state") or "",
        zip=cfg.get("warehouse_zip") or "",
        country=cfg.get("warehouse_country") or "US",
        phone=cfg.get("warehouse_phone") or "",
        email=cfg.get("warehouse_email") or "",
    )

# Heavy fields (base64 attachments) that should be stripped from list / detail
# responses to keep payloads small. A dedicated download endpoint streams them.
_HEAVY_FIELDS = ("admin_label_attachment", "admin_reject_attachment")

# Customer proof photo limits
MAX_CUSTOMER_PROOF_PHOTOS = 3
MAX_CUSTOMER_PROOF_BYTES = 2 * 1024 * 1024  # 2 MB


def _strip_customer_proof(doc: dict) -> dict:
    """Replace heavy base64 in customer_proof_photos with lightweight metadata."""
    if not doc:
        return doc
    photos = doc.get("customer_proof_photos")
    if isinstance(photos, list) and photos:
        doc["customer_proof_photos"] = [
            {
                "filename": p.get("filename"),
                "content_type": p.get("content_type"),
                "size_bytes": p.get("size_bytes"),
            }
            for p in photos if isinstance(p, dict)
        ]
    return doc


def _strip_heavy(doc: dict) -> dict:
    if not doc:
        return doc
    for k in _HEAVY_FIELDS:
        att = doc.get(k)
        if isinstance(att, dict) and "content_base64" in att:
            doc[k] = {
                "filename": att.get("filename"),
                "content_type": att.get("content_type"),
                "size_bytes": att.get("size_bytes"),
            }
    _strip_customer_proof(doc)
    return doc


# =============== CUSTOMER ENDPOINTS ===============

@api.get("/")
async def root():
    return {"service": "PGE Return Portal", "status": "ok"}

@api.get("/branding")
async def public_branding():
    cfg = await settings_service.get_settings(db)
    sc_enabled = str(cfg.get("enable_store_credit", "true")).lower() in ("1", "true", "yes", "on")
    try:
        sc_bonus = float(cfg.get("store_credit_bonus_percent") or 0.0)
    except (TypeError, ValueError):
        sc_bonus = 0.0
    try:
        sc_expiry = int(float(cfg.get("store_credit_expiry_days") or 365))
    except (TypeError, ValueError):
        sc_expiry = 365
    return {
        "store_name": cfg.get("store_name") or "Returns",
        "support_email": cfg.get("support_email") or "",
        "logo_url": cfg.get("logo_url") or "",
        "hero_image_url": cfg.get("hero_image_url") or "",
        "max_return_window_days": int(cfg.get("max_return_window_days") or 0) or None,
        "store_credit_enabled": sc_enabled,
        "store_credit_bonus_percent": sc_bonus,
        "store_credit_expiry_days": sc_expiry,
        # Admin route prefix is exposed publicly so the React shell can
        # mount the admin pages at the configured path. Brute-force on the
        # /api/auth/login endpoint is the actual access-control layer; this
        # prefix mainly removes signposting from the homepage.
        "admin_route_prefix": (cfg.get("admin_route_prefix") or "admin").strip("/") or "admin",
    }


# Public stats — used by the landing page hero badge to show real volume.
# We only count "completed" returns (refunded, store credit issued, or
# delivered to the warehouse) so the number reflects genuine, finished work.
# Returns 0 on a fresh install, in which case the frontend hides the badge.
@api.get("/stats/public")
async def public_stats():
    HAPPY_STATUSES = ["refunded", "store_credit_issued", "delivered"]
    try:
        n = await db.returns.count_documents({
            "status": {"$in": HAPPY_STATUSES},
            "archived": {"$ne": True},
        })
    except Exception:
        n = 0
    return {"happy_returns": int(n or 0)}

def _within_return_window(order_date_iso: Optional[str], max_days_raw) -> bool:
    try:
        max_days = int(max_days_raw or 0)
    except Exception:
        max_days = 0
    if max_days <= 0 or not order_date_iso:
        return True
    try:
        s = order_date_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    age = (datetime.now(timezone.utc) - dt).days
    return age <= max_days

@api.post("/orders/lookup", response_model=OrderResponse)
async def lookup_order(body: OrderLookupRequest):
    cfg = await settings_service.get_settings(db)
    order = await woo.fetch_order(body.order_id, body.email, cfg)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found. Check your order number and email.")
    if not _within_return_window(order.date_created, cfg.get("max_return_window_days")):
        raise HTTPException(
            status_code=403,
            detail=f"Return window expired. Orders must be returned within {cfg.get('max_return_window_days')} days of purchase.",
        )
    return order

METHOD_DISPLAY = {
    "pay_stripe": "Pay for label (Stripe)",
    "deduct_from_refund": "Deduct shipping from refund",
    "free_label": "Request free label (admin approval)",
    "store_credit": "Store credit (instead of cash refund)",
    "self_ship": "Send with my own carrier",
}

# Reasons that trigger mandatory manual admin review (seller-responsible).
# Any return that contains an item with one of these reasons is forced onto
# the "free_label" path so it goes through the admin approve/reject flow.
MANUAL_REVIEW_REASONS = {
    "defective",
    "damaged_outer_box",
    "wrong_item",
    "missing_parts",
    "damaged",  # legacy alias
}

# Statuses that still block re-returning the same line item
ACTIVE_RETURN_STATUSES = {
    "draft", "awaiting_payment", "awaiting_approval", "awaiting_tracking",
    "approved", "label_purchased", "in_transit", "delivered", "refunded",
    "store_credit_issued",
}


async def _already_returned_line_ids(db, order_number: str, email: str) -> set:
    """line_item_ids in any non-rejected/cancelled return for this order+email."""
    q = {
        "order_number": str(order_number),
        "email": (email or "").lower(),
        "status": {"$in": list(ACTIVE_RETURN_STATUSES)},
    }
    cursor = db.returns.find(q, {"_id": 0, "items.line_item_id": 1})
    ids: set = set()
    async for doc in cursor:
        for itm in (doc.get("items") or []):
            if itm.get("line_item_id"):
                ids.add(str(itm["line_item_id"]))
    return ids


async def _existing_returns_by_line_item(db, order_number: str, email: str) -> Dict[str, Dict[str, Any]]:
    """Per-item status lookup. Returns {line_item_id: {status_label, method, rma_number}}
    so the Select Items page can show a distinct chip per already-started line
    (e.g. 'Store voucher issued' vs 'Return in progress' vs 'Refunded')."""
    q = {
        "order_number": str(order_number),
        "email": (email or "").lower(),
        "status": {"$in": list(ACTIVE_RETURN_STATUSES)},
    }
    cursor = db.returns.find(q, {"_id": 0, "items.line_item_id": 1, "status": 1,
                                  "method": 1, "rma_number": 1, "coupon_code": 1})
    # Per-item priority ordering: if multiple returns exist for the same line
    # item (shouldn't usually but can happen with rejected+reopen), surface
    # the most "final" status first.
    rank = {"refunded": 1, "store_credit_issued": 2, "delivered": 3,
            "in_transit": 4, "label_purchased": 5, "approved": 6,
            "awaiting_payment": 7, "awaiting_approval": 8, "draft": 9}
    out: Dict[str, Dict[str, Any]] = {}
    async for d in cursor:
        status = d.get("status", "")
        method = d.get("method", "")
        has_coupon = bool(d.get("coupon_code"))
        # Friendly label — grouped so customers don't get spooked by tech terms.
        if status == "refunded":
            label = "Refunded"
        elif status == "store_credit_issued" or (has_coupon and method == "store_credit"):
            label = "Store voucher issued"
        elif method == "store_credit":
            label = "Store voucher pending approval"
        elif status in ("delivered", "in_transit"):
            label = "Return in transit"
        elif status in ("label_purchased", "approved"):
            label = "Return label issued"
        elif status == "awaiting_payment":
            label = "Awaiting payment"
        elif status == "awaiting_approval":
            label = "Awaiting approval"
        else:
            label = "Return in progress"
        payload = {
            "status": status,
            "status_label": label,
            "method": method,
            "rma_number": d.get("rma_number", ""),
        }
        for itm in (d.get("items") or []):
            lid = str(itm.get("line_item_id") or "")
            if not lid:
                continue
            prev = out.get(lid)
            if not prev or rank.get(status, 99) < rank.get(prev["status"], 99):
                out[lid] = payload
    return out


@api.post("/returns")
async def create_return(body: CreateReturnRequest):
    cfg = await settings_service.get_settings(db)
    order = await woo.fetch_order(body.order_id, body.email, cfg)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    valid_ids = {li.id for li in order.line_items}
    for itm in body.items:
        if itm.line_item_id not in valid_ids:
            raise HTTPException(status_code=400, detail=f"Item {itm.line_item_id} not in order.")

    # Stamp each return item with the weight + SKU info from the live
    # WooCommerce order so shipping rates and analytics later use real data
    # (the frontend may omit these fields — the server is the source of truth).
    meta_by_li_id = {li.id: (li.weight, li.weight_unit, li.sku, li.product_id)
                     for li in order.line_items}
    for itm in body.items:
        w, u, sku, pid = meta_by_li_id.get(itm.line_item_id, (None, None, "", ""))
        if itm.weight is None:
            itm.weight = w
        if not itm.weight_unit:
            itm.weight_unit = u
        if not itm.sku:
            itm.sku = sku or ""
        if not itm.product_id:
            itm.product_id = pid or ""

    # Prevent duplicate returns on the same line item (allow other items in the order)
    already = await _already_returned_line_ids(db, order.order_number, order.email)
    conflicts = [i for i in body.items if i.line_item_id in already]
    if conflicts:
        names = ", ".join(i.name for i in conflicts)
        conflict_ids = [i.line_item_id for i in conflicts]
        # Check if any of these conflicting items are already refunded — if so,
        # give a clear message saying no further returns are possible for them.
        refunded_q = {
            "order_number": str(order.order_number),
            "email": (order.email or "").lower(),
            "status": "refunded",
            "items.line_item_id": {"$in": conflict_ids},
        }
        already_refunded = await db.returns.count_documents(refunded_q)
        if already_refunded:
            raise HTTPException(
                status_code=409,
                detail=f"These items have already been refunded and cannot be "
                       f"returned again: {names}.",
            )
        # If store credit has already been applied to any of these items, tell
        # the customer clearly — that item's return is closed.
        store_credit_q = {
            "order_number": str(order.order_number),
            "email": (order.email or "").lower(),
            "status": "store_credit_issued",
            "items.line_item_id": {"$in": conflict_ids},
        }
        already_store_credit = await db.returns.count_documents(store_credit_q)
        if already_store_credit:
            raise HTTPException(
                status_code=409,
                detail=f"Store credit has already been applied for: {names}. "
                       f"This item's return is closed. You can still return "
                       f"other items from this order.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"A return has already been started for: {names}. "
                   f"You can still return other items from this order.",
        )

    refund_amount = sum(i.price * i.quantity for i in body.items)

    # Server-side safety: if ANY item has a seller-responsible reason, force the
    # return onto the manual-review path — the customer cannot bypass this by
    # crafting a request with "pay_stripe" / "deduct_from_refund" / "store_credit".
    # Self-ship is allowed through but still gated behind admin approval.
    effective_method = body.method
    has_manual_reason = any((i.reason in MANUAL_REVIEW_REASONS) for i in body.items)
    if has_manual_reason:
        # Manual review path. We respect store_credit and self_ship choices
        # but still gate them behind admin approval (same gate as free_label).
        if effective_method not in ("store_credit", "self_ship"):
            effective_method = "free_label"
    method_label = METHOD_DISPLAY.get(effective_method, effective_method)

    # Restricted (manual-review) + store_credit: the customer must also tell
    # us how they want the parcel physically returned. Both options are free
    # to them — the admin will approve the shipping path and (separately,
    # later) the store credit issuance once the parcel arrives.
    restricted_shipping_choice = None
    if has_manual_reason and effective_method == "store_credit":
        # Default to free_label if the customer didn't pick — keeps backward
        # compatibility with older clients that haven't been updated yet.
        restricted_shipping_choice = body.restricted_shipping_choice or "free_label"
    elif effective_method == "store_credit" and body.restricted_shipping_choice == "self_ship":
        # Non-restricted store_credit can also choose to self-ship instead
        # of buying a carrier label. Same field is reused for consistency.
        restricted_shipping_choice = "self_ship"

    status = "draft"
    if effective_method == "free_label":
        status = "awaiting_approval"
    elif effective_method == "store_credit":
        # Restricted store_credit (regardless of sub-choice) waits for admin.
        # Non-restricted store_credit:
        #   self_ship sub-choice → awaiting_tracking (customer ships, admin
        #     issues coupon on receipt)
        #   carrier-rate sub-choices (deduct/stripe) → awaiting_approval at
        #     create time; the rate-confirm POST flips it to label_purchased.
        if has_manual_reason:
            status = "awaiting_approval"
        elif restricted_shipping_choice == "self_ship":
            status = "awaiting_tracking"
        else:
            status = "awaiting_approval"
    elif effective_method in ("pay_stripe", "deduct_from_refund"):
        status = "awaiting_payment"
    elif effective_method == "self_ship":
        # Self-ship: locked reasons need admin approval before the customer
        # ships; all other reasons go straight to awaiting_tracking so the
        # customer can post and submit tracking right away.
        if has_manual_reason:
            status = "awaiting_approval"
        else:
            status = "awaiting_tracking"

    initial_actions = [
        CustomerAction(
            kind="return_created",
            label=f"Started a return · method: {method_label}",
            meta={"method": effective_method, "item_count": len(body.items),
                  "refund_amount": refund_amount},
        ).model_dump(),
    ]

    doc = ReturnRequestDoc(
        rma_number=_rma(),
        order_id=str(order.order_id),
        order_number=str(order.order_number),
        email=order.email,
        customer_name=order.customer_name,
        items=body.items,
        method=effective_method,
        method_display_label=method_label,
        status=status,
        customer_note=body.customer_note or "",
        return_address=body.return_address,
        warehouse_address=_warehouse_from_cfg(cfg),
        refund_amount=refund_amount,
        refund_net=refund_amount,
        customer_actions=initial_actions,
        restricted_shipping_choice=restricted_shipping_choice,
    ).model_dump()

    await db.returns.insert_one(dict(doc))
    doc.pop("_id", None)

    # Auto-issue coupon for store_credit returns is NO LONGER done at creation
    # time. All store_credit returns now wait for admin to physically receive
    # the parcel and click "Approve & issue store credit" on the dashboard —
    # this closes the "keep item + spend voucher" abuse hole. The coupon is
    # issued from /admin/returns/{id}/approve-free (the existing admin button).

    # Emails are NOT sent here anymore — they all fire from the success page
    # via POST /returns/{id}/finalize so the customer receives one clean batch
    # once they see their RMA.
    return doc


@api.get("/returns/existing-items/{order_number}/{email}")
async def existing_return_items(order_number: str, email: str):
    """Returns line_item_ids already in an active return + a per-item status
    map so the Select Items page can show a distinct chip per line (e.g.
    'Store voucher issued' vs 'Return in progress' vs 'Refunded')."""
    email_l = email.lower()
    by_item = await _existing_returns_by_line_item(db, order_number, email_l)
    return {"order_number": order_number, "email": email_l,
            "line_item_ids": sorted(list(by_item.keys())),
            "items": by_item}


@api.post("/returns/{return_id}/track-action")
async def track_customer_action(return_id: str, body: CustomerActionRequest):
    """Record a customer interaction (e.g. method selected, rate selected) for admin visibility."""
    action = CustomerAction(
        kind=body.kind, label=body.label, meta=body.meta or {}
    ).model_dump()
    result = await db.returns.update_one(
        {"id": return_id},
        {"$push": {"customer_actions": action},
         "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Return not found")
    return {"ok": True, "action": action}

@api.get("/returns/{return_id}")
async def get_return(return_id: str):
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    return _strip_heavy(doc)


@api.post("/returns/{return_id}/proof")
async def upload_customer_proof(
    return_id: str,
    files: List[UploadFile] = File(...),
):
    """Customer uploads up to 3 proof photos (max 2MB each, images only).
    Replaces any existing proof photos for this return."""
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
    if len(files) > MAX_CUSTOMER_PROOF_PHOTOS:
        raise HTTPException(
            status_code=400,
            detail=f"Max {MAX_CUSTOMER_PROOF_PHOTOS} photos allowed.",
        )
    stored = []
    for f in files:
        if not f or not f.filename:
            continue
        data = await f.read()
        if not data:
            continue
        if len(data) > MAX_CUSTOMER_PROOF_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"'{f.filename}' is larger than 2 MB.",
            )
        ct = (f.content_type or "").lower()
        if not ct.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail=f"'{f.filename}' is not an image.",
            )
        stored.append({
            "filename": f.filename,
            "content_type": ct,
            "content_base64": base64.b64encode(data).decode("ascii"),
            "size_bytes": len(data),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
    if not stored:
        raise HTTPException(status_code=400, detail="No valid images provided.")

    await db.returns.update_one(
        {"id": return_id},
        {"$set": {
            "customer_proof_photos": stored,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
         "$push": {"customer_actions": CustomerAction(
             kind="customer_uploaded_proof",
             label=f"Customer uploaded {len(stored)} proof photo(s)",
             meta={"count": len(stored),
                   "filenames": [s["filename"] for s in stored]},
         ).model_dump()}},
    )
    return {"ok": True, "count": len(stored),
            "photos": [{"filename": s["filename"],
                        "content_type": s["content_type"],
                        "size_bytes": s["size_bytes"]} for s in stored]}

@api.post("/rates/preview")
async def preview_rates(body: RatePreviewRequest):
    """Quick rate lookup from postcode only (no return doc required).

    Merges rates from every configured provider (Shippo, Royal Mail Click
    & Drop). Providers that fail or have no keys are silently skipped — the
    customer never sees an error.
    """
    cfg = await settings_service.get_settings(db)
    zip_code = (body.zip or "").strip()
    if not zip_code:
        raise HTTPException(status_code=400, detail="Postcode is required.")
    country = (body.country or "US").strip() or "US"
    placeholder_from = Address(
        name="Customer",
        street1="Address pending",
        city=(body.city or "").strip() or "City",
        state=(body.state or "").strip() or (zip_code[:2] or "--"),
        zip=zip_code,
        country=country,
    )
    warehouse = _warehouse_from_cfg(cfg)
    # Preview is fired before the customer has selected items (just a postcode
    # probe), so we use the admin-configurable default weight here. Once the
    # real return is created, the rate recalculation uses the actual item
    # weights from WooCommerce — see `_parcel_from_doc`.
    try:
        preview_kg = float(cfg.get("default_item_weight_kg") or 1.0)
    except (TypeError, ValueError):
        preview_kg = 1.0
    parcel = {"length": 10, "width": 8, "height": 4,
              "weight": round(preview_kg, 3), "weight_unit": "kg"}

    merged: List[Dict] = []

    if cfg.get("shippo_api_key"):
        try:
            r = shippo_service.create_shipment(
                api_key=cfg["shippo_api_key"],
                address_from=placeholder_from, address_to=warehouse, parcel=parcel,
            )
            merged.extend(r.get("rates") or [])
        except Exception as e:
            log.warning("preview shippo error: %s", e)

    if cfg.get("royal_mail_api_key"):
        try:
            r = royal_mail_service.create_shipment(
                api_key=cfg["royal_mail_api_key"],
                address_from=placeholder_from, address_to=warehouse, parcel=parcel,
            )
            merged.extend(r.get("rates") or [])
        except Exception as e:
            log.warning("preview royal_mail error: %s", e)

    merged.sort(key=lambda x: float(x.get("amount") or 0))
    return {"rates": merged}


@api.post("/returns/{return_id}/finalize")
async def finalize_return(return_id: str):
    """Send all customer & admin emails for this return (idempotent).

    Called by the customer's success page once the RMA is visible. This keeps
    every email aligned with the "here is your RMA + label" moment instead of
    firing mid-flow.
    """
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    if doc.get("emails_finalized"):
        return {"ok": True, "skipped": True, "reason": "already sent"}

    cfg = await settings_service.get_settings(db)
    method_label = doc.get("method_display_label") or METHOD_DISPLAY.get(doc["method"], doc["method"])
    currency = (doc.get("selected_rate") or {}).get("currency") or "GBP"
    refund_amount = float(doc.get("refund_amount") or 0.0)
    refund_deduction = float(doc.get("refund_deduction") or 0.0)
    refund_net = float(doc.get("refund_net") or max(refund_amount - refund_deduction, 0.0))

    sent_log: List[Dict[str, Any]] = []
    provider_used = None

    # Store-credit returns get ONLY the coupon email (sent separately from
    # `_issue_store_credit_for_return`). We intentionally skip the generic
    # "return initiated" confirmation so the customer receives one clean
    # email with their code, not two overlapping ones.
    skip_customer_confirmation = (doc.get("method") == "store_credit")

    # Self-ship returns get a dedicated instructions email instead of the
    # generic "return initiated" + "label ready" pair.
    is_self_ship = (doc.get("method") == "self_ship")
    if is_self_ship:
        skip_customer_confirmation = True
        try:
            ss_res = await email_service.send_self_ship_instructions(
                cfg, to_email=doc["email"], to_name=doc["customer_name"],
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                requires_admin_first=(doc.get("status") == "awaiting_approval"),
            )
            sent_log.extend([{**a, "kind": "self_ship_instructions"}
                             for a in ss_res.get("attempts", [])])
            if ss_res.get("ok"):
                provider_used = ss_res.get("provider")
        except Exception as e:
            log.warning("finalize: self_ship_instructions failed: %s", e)

    # 1. Customer confirmation
    if not skip_customer_confirmation:
        try:
            res = await email_service.send_return_initiated(
                cfg, to_email=doc["email"], to_name=doc["customer_name"],
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                method_display_label=method_label,
                method=doc.get("method") or "",
                refund_amount=refund_amount, refund_deduction=refund_deduction,
                refund_net=refund_net, currency=currency,
            )
            sent_log.extend([{**a, "kind": "return_initiated"} for a in res.get("attempts", [])])
            if res.get("ok"):
                provider_used = res.get("provider")
        except Exception as e:
            log.warning("finalize: return_initiated failed: %s", e)

    # 2. Admin notification
    try:
        admin_res = await email_service.send_admin_new_return(
            cfg,
            rma_number=doc["rma_number"],
            order_number=doc["order_number"],
            customer_name=doc["customer_name"],
            customer_email=doc["email"],
            method_display_label=method_label,
            items=doc.get("items") or [],
            refund_amount=refund_amount,
            refund_deduction=refund_deduction,
            refund_net=refund_net,
            customer_note=doc.get("customer_note") or "",
            currency=currency,
        )
        sent_log.extend([{**a, "kind": "admin_new_return"} for a in admin_res.get("attempts", [])])
    except Exception as e:
        log.warning("finalize: admin_new_return failed: %s", e)

    # 3. Label-ready (only if label was purchased)
    if doc.get("label_url") and doc.get("tracking_number"):
        try:
            lbl_res = await email_service.send_label_ready(
                cfg, to_email=doc["email"], to_name=doc["customer_name"],
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                tracking_number=doc["tracking_number"], label_url=doc["label_url"],
            )
            sent_log.extend([{**a, "kind": "label_ready"} for a in lbl_res.get("attempts", [])])
            if lbl_res.get("ok") and not provider_used:
                provider_used = lbl_res.get("provider")
        except Exception as e:
            log.warning("finalize: label_ready failed: %s", e)

    await db.returns.update_one(
        {"id": return_id},
        {"$set": {"emails_finalized": True,
                  "email_provider_used": provider_used,
                  "updated_at": datetime.now(timezone.utc).isoformat()},
         "$push": {"email_log": {"$each": sent_log}}},
    )
    any_ok = any(a.get("ok") for a in sent_log)
    return {"ok": any_ok, "provider": provider_used, "sent": sent_log}



# --- Weight-based parcel helpers --------------------------------------------

# Conversion factors to kilograms. Anything else we don't recognise falls
# back to 1.0 so we treat the number as-if it were already kg — the safest
# no-op default.
_WEIGHT_TO_KG = {
    "kg": 1.0, "kgs": 1.0, "kilogram": 1.0, "kilograms": 1.0,
    "g": 0.001, "gram": 0.001, "grams": 0.001,
    "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    "oz": 0.0283495231, "ounce": 0.0283495231, "ounces": 0.0283495231,
}


def _to_kg(weight: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Normalise a WooCommerce weight + unit pair into kilograms."""
    if weight is None:
        return None
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return None
    if w <= 0:
        return None
    factor = _WEIGHT_TO_KG.get((unit or "kg").strip().lower(), 1.0)
    return w * factor


def _parcel_from_doc(doc: dict, cfg: Dict[str, str]) -> Dict:
    """Build a carrier-agnostic parcel dict from the items in a return.

    - Sums `item.weight × item.quantity` across every return item.
    - Missing/zero weights are replaced per-item with `default_item_weight_kg`.
    - A floor of `min_parcel_weight_kg` is applied so carriers never get 0.
    - Weight is always in kilograms (providers convert as needed).
    """
    try:
        default_kg = float(cfg.get("default_item_weight_kg") or 1.0)
    except (TypeError, ValueError):
        default_kg = 1.0
    try:
        min_kg = float(cfg.get("min_parcel_weight_kg") or 0.1)
    except (TypeError, ValueError):
        min_kg = 0.1

    total_kg = 0.0
    for it in (doc.get("items") or []):
        qty = int(it.get("quantity") or 1)
        kg = _to_kg(it.get("weight"), it.get("weight_unit"))
        if kg is None:
            kg = default_kg
        total_kg += kg * qty

    if total_kg < min_kg:
        total_kg = min_kg

    # Round to 3 decimals — carriers don't need higher precision than grams.
    return {
        "length": 10, "width": 8, "height": 4,
        "weight": round(total_kg, 3),
        "weight_unit": "kg",
    }


async def _shippo_rates(doc) -> Dict:
    """Fetch rates from Shippo only (legacy helper, still used internally).

    Use `_all_rates` to get merged rates from every configured provider.
    Silent failure here too — if Shippo isn't configured, returns empty rates
    so the caller can fall back to other providers.
    """
    cfg = await settings_service.get_settings(db)
    if not cfg.get("shippo_api_key"):
        return {"shipment_id": None, "rates": []}
    try:
        return shippo_service.create_shipment(
            api_key=cfg["shippo_api_key"],
            address_from=Address(**doc["return_address"]),
            address_to=Address(**doc["warehouse_address"]),
            parcel=_parcel_from_doc(doc, cfg),
        )
    except Exception as e:
        log.warning("shippo rate error: %s", e)
        return {"shipment_id": None, "rates": []}


async def _all_rates(doc) -> Dict:
    """Merge rates from every configured provider. Providers that fail, have
    no keys, or return nothing are silently skipped. Each rate.rate_id is
    prefixed so we can later route the label purchase back to the right
    provider: shippo rates already use Shippo's opaque ids,
    Royal Mail rates use "rm_*"."""
    cfg = await settings_service.get_settings(db)
    addr_from = Address(**doc["return_address"])
    addr_to = Address(**doc["warehouse_address"])
    parcel = _parcel_from_doc(doc, cfg)

    merged: List[Dict] = []
    shipment_id: Optional[str] = None

    # 1) Shippo
    if cfg.get("shippo_api_key"):
        try:
            r = shippo_service.create_shipment(
                api_key=cfg["shippo_api_key"],
                address_from=addr_from, address_to=addr_to, parcel=parcel,
            )
            shipment_id = r.get("shipment_id")
            merged.extend(r.get("rates") or [])
        except Exception as e:
            log.warning("shippo rate error: %s", e)

    # 2) Royal Mail Click & Drop
    if cfg.get("royal_mail_api_key"):
        try:
            r = royal_mail_service.create_shipment(
                api_key=cfg["royal_mail_api_key"],
                address_from=addr_from, address_to=addr_to, parcel=parcel,
            )
            merged.extend(r.get("rates") or [])
        except Exception as e:
            log.warning("royal_mail rate error: %s", e)

    # Sort by price ascending so the customer sees cheapest first.
    merged.sort(key=lambda x: float(x.get("amount") or 0))
    return {"shipment_id": shipment_id, "rates": merged}


def _provider_for_rate(rate_id: str) -> str:
    """Which provider owns this rate_id? Used to route label purchase."""
    if not rate_id:
        return "shippo"
    if rate_id.startswith("rm_"):
        return "royal_mail"
    return "shippo"


def _purchase_label_multi(cfg: Dict[str, str], selected: Dict,
                          *, address_from=None, address_to=None,
                          parcel: Optional[Dict] = None,
                          reference: Optional[str] = None) -> Dict:
    """Book a label with the provider that owns this rate. Returns Shippo-
    compatible envelope {status, transaction_id, label_url, tracking_number,
    message}. `message` carries the carrier's error text on failure so the
    admin alert email can surface it (e.g. "shipping provider not found")."""
    provider = _provider_for_rate(selected.get("rate_id"))
    rid = selected["rate_id"]
    try:
        if provider == "royal_mail":
            res = royal_mail_service.purchase_label(
                api_key=cfg.get("royal_mail_api_key") or "",
                rate_id=rid, address_from=address_from, address_to=address_to,
                parcel=parcel, reference=reference,
            )
        else:
            # default: Shippo
            res = shippo_service.purchase_label(cfg.get("shippo_api_key") or "", rid)
        # Normalise: ensure a `message` field is present so callers can
        # surface the carrier's error text if status != SUCCESS.
        res = dict(res or {})
        if res.get("status") != "SUCCESS" and not res.get("message"):
            msgs = res.get("messages") or []
            res["message"] = "; ".join([str(m) for m in msgs if m]) \
                or f"{provider} returned status={res.get('status')}"
        return res
    except Exception as e:
        log.warning("label purchase (%s) error: %s", provider, e)
        return {"status": "FAILED", "transaction_id": None,
                "label_url": None, "tracking_number": None,
                "message": f"{provider}: {e}"}

@api.post("/returns/{return_id}/rates")
async def fetch_rates(return_id: str):
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    result = await _all_rates(doc)
    await db.returns.update_one(
        {"id": return_id},
        {"$set": {"shippo_shipment_id": result["shipment_id"],
                  "available_rates": result["rates"],
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"shipment_id": result["shipment_id"], "rates": result["rates"]}

def _find_stored_rate(doc: dict, rate_id: str) -> Optional[dict]:
    for r in (doc.get("available_rates") or []):
        if r.get("rate_id") == rate_id:
            return r
    return None


def _match_rate_by_hint(rates: List[dict], *, provider: Optional[str],
                        servicelevel: Optional[str], amount: Optional[float]) -> Optional[dict]:
    """Fallback matcher used when the preview rate_id no longer exists on a
    newly-created shipment. Preview rates are fetched against a placeholder
    address (postcode-only), so Shippo issues different rate_ids once the full
    address is provided. We match by provider+service to pick the equivalent
    rate, then nudge by amount as a tiebreaker.
    """
    if not rates:
        return None
    if provider and servicelevel:
        for r in rates:
            if (r.get("provider") == provider
                    and r.get("servicelevel") == servicelevel):
                return r
    if provider:
        same_provider = [r for r in rates if r.get("provider") == provider]
        if same_provider:
            if amount is not None:
                same_provider.sort(key=lambda r: abs(float(r.get("amount") or 0) - float(amount)))
            return same_provider[0]
    if amount is not None:
        return min(rates, key=lambda r: abs(float(r.get("amount") or 0) - float(amount)))
    return None


async def _resolve_rate(doc: dict, body: "CheckoutRequest", return_id: str) -> dict:
    """Find the rate the customer picked, re-fetching against the real
    address if the stored/preview id no longer resolves.
    """
    selected = _find_stored_rate(doc, body.rate_id)
    if selected:
        return selected

    fresh = await _all_rates(doc)
    await db.returns.update_one(
        {"id": return_id},
        {"$set": {"shippo_shipment_id": fresh["shipment_id"],
                  "available_rates": fresh["rates"]}},
    )
    # Exact rate_id match on the fresh shipment
    selected = next((r for r in fresh["rates"] if r["rate_id"] == body.rate_id), None)
    if selected:
        return selected
    # Fallback: match by provider + service level (+ amount tiebreaker)
    selected = _match_rate_by_hint(
        fresh["rates"],
        provider=getattr(body, "provider", None),
        servicelevel=getattr(body, "servicelevel", None),
        amount=getattr(body, "amount", None),
    )
    if selected:
        return selected
    raise HTTPException(
        status_code=400,
        detail="Selected rate no longer available. Please go back and choose a rate again.",
    )

@api.post("/returns/{return_id}/checkout", response_model=CheckoutResponse)
async def create_checkout(return_id: str, body: CheckoutRequest, request: Request):
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    if doc["method"] not in ("pay_stripe", "store_credit"):
        raise HTTPException(status_code=400, detail="Return is not set to pay via Stripe.")

    cfg = await settings_service.get_settings(db)
    stripe_key = cfg.get("stripe_api_key")
    if not stripe_key:
        raise HTTPException(status_code=500, detail="Stripe API key not configured.")

    selected = await _resolve_rate(doc, body, return_id)

    amount = float(selected["amount"])
    currency = (selected.get("currency") or "USD").lower()

    origin = body.origin_url.rstrip("/")
    success_url = f"{origin}/return/{return_id}/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/return/{return_id}/method"

    host_url = str(request.base_url)
    webhook_url = f"{host_url.rstrip('/')}/api/webhook/stripe"
    stripe_co = StripeCheckout(api_key=stripe_key, webhook_url=webhook_url)

    req = CheckoutSessionRequest(
        amount=amount, currency=currency,
        success_url=success_url, cancel_url=cancel_url,
        metadata={"return_id": return_id, "rate_id": selected["rate_id"], "rma": doc["rma_number"]},
    )
    session = await stripe_co.create_checkout_session(req)

    await db.returns.update_one(
        {"id": return_id},
        {"$set": {
            "shippo_rate_id": selected["rate_id"],
            "selected_rate": selected,
            "label_cost": amount,
            "stripe_session_id": session.session_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )

    await db.payment_transactions.insert_one({
        "session_id": session.session_id, "return_id": return_id,
        "amount": amount, "currency": currency,
        "metadata": {"return_id": return_id, "rate_id": selected["rate_id"], "rma": doc["rma_number"]},
        "status": "initiated", "payment_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    return CheckoutResponse(url=session.url, session_id=session.session_id)

@api.post("/returns/{return_id}/deduct-from-refund")
async def deduct_from_refund(return_id: str, body: CheckoutRequest):
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    
    cfg = await settings_service.get_settings(db)
    selected = await _resolve_rate(doc, body, return_id)

    label = _purchase_label_multi(
        cfg, selected,
        address_from=Address(**doc["return_address"]),
        address_to=Address(**doc["warehouse_address"]),
        parcel=_parcel_from_doc(doc, cfg),
        reference=doc.get("rma_number"),
    )
    if label.get("status") != "SUCCESS":
        # Graceful fallback: record the rate the customer picked + the error,
        # mark awaiting-approval (so admin can fulfil manually), alert admin,
        # and still return the return doc so the success page can render.
        err_msg = label.get("message") or "Unknown carrier error"
        log.warning("deduct-from-refund label failed: rma=%s err=%s",
                    doc.get("rma_number"), err_msg)
        await db.returns.update_one(
            {"id": return_id},
            {"$set": {
                "shippo_rate_id": selected["rate_id"],
                "selected_rate": selected,
                "label_cost": float(selected["amount"]),
                "refund_deduction": float(selected["amount"]),
                "refund_net": max(float(doc.get("refund_amount") or 0.0) - float(selected["amount"]), 0.0),
                "status": "awaiting_approval",
                "label_error": err_msg,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
             "$push": {"customer_actions": CustomerAction(
                 kind="label_purchase_failed",
                 label="Label purchase failed — admin notified",
                 meta={"error": err_msg, "rate_id": selected["rate_id"],
                       "amount": float(selected["amount"])},
             ).model_dump()}},
        )
        try:
            await email_service.send_admin_label_failure(
                cfg,
                rma_number=doc.get("rma_number", ""),
                return_id=return_id,
                error_message=err_msg,
                method_display_label=doc.get("method_display_label")
                    or METHOD_DISPLAY.get(doc.get("method", ""), doc.get("method", "")),
                amount=float(selected["amount"]),
                currency=(selected.get("currency") or "GBP").upper(),
                customer_name=(doc.get("return_address") or {}).get("name", ""),
                customer_email=doc.get("email", ""),
                rate_provider=selected.get("provider", ""),
                rate_servicelevel=selected.get("servicelevel", ""),
            )
        except Exception as e:
            log.warning("admin label-failure email error: %s", e)
        updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
        return updated

    await db.returns.update_one(
        {"id": return_id},
        {"$set": {
            "shippo_rate_id": selected["rate_id"],
            "selected_rate": selected,
            "label_cost": float(selected["amount"]),
            "refund_deduction": float(selected["amount"]),
            "refund_net": max(float(doc.get("refund_amount") or 0.0) - float(selected["amount"]), 0.0),
            "shippo_transaction_id": label["transaction_id"],
            "label_url": label["label_url"],
            "label_qr_url": label.get("qr_code_url"),
            "tracking_number": label["tracking_number"],
            "tracking_carrier": selected["provider"],
            "status": "label_purchased",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
         "$push": {"customer_actions": CustomerAction(
             kind="deduct_from_refund_confirmed",
             label=f"Confirmed: deduct shipping ({selected['provider']} {selected.get('servicelevel','')}) from refund",
             meta={"rate_id": selected["rate_id"], "amount": float(selected["amount"]),
                   "currency": selected.get("currency", "USD")},
         ).model_dump()}},
    )
    updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
    # Email is sent by /finalize when the customer reaches the success page.
    return updated

@api.get("/payments/status/{session_id}", response_model=PaymentStatusResponse)
async def payment_status(session_id: str, request: Request):
    tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if not tx:
        raise HTTPException(status_code=404, detail="Session not found")

    cfg = await settings_service.get_settings(db)
    stripe_key = cfg.get("stripe_api_key")
    host_url = str(request.base_url)
    webhook_url = f"{host_url.rstrip('/')}/api/webhook/stripe"
    stripe_co = StripeCheckout(api_key=stripe_key, webhook_url=webhook_url)

    try:
        status_resp = await stripe_co.get_checkout_status(session_id)
    except Exception as e:
        log.warning("stripe status lookup failed: %s", e)
        doc = await db.returns.find_one({"id": tx["return_id"]}, {"_id": 0})
        return PaymentStatusResponse(
            status="unknown",
            payment_status=tx.get("payment_status") or "pending",
            return_id=tx["return_id"],
            rma_number=doc["rma_number"] if doc else "",
        )

    return_id = tx["return_id"]
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    
    already_processed = tx.get("payment_status") == "paid"
    if status_resp.payment_status == "paid" and not already_processed:
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"status": status_resp.status, "payment_status": "paid",
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        try:
            selected = doc.get("selected_rate") or {"rate_id": doc.get("shippo_rate_id", "")}
            label = _purchase_label_multi(
                cfg, selected,
                address_from=Address(**doc["return_address"]),
                address_to=Address(**doc["warehouse_address"]),
                parcel=_parcel_from_doc(doc, cfg),
                reference=doc.get("rma_number"),
            )
            if label.get("status") == "SUCCESS":
                await db.returns.update_one(
                    {"id": return_id},
                    {"$set": {
                        "paid": True,
                        "shippo_transaction_id": label["transaction_id"],
                        "label_url": label["label_url"],
                        "label_qr_url": label.get("qr_code_url"),
                        "tracking_number": label["tracking_number"],
                        "tracking_carrier": (doc.get("selected_rate") or {}).get("provider"),
                        "status": "label_purchased",
                        "label_error": None,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                     "$push": {"customer_actions": CustomerAction(
                         kind="paid_for_label",
                         label="Customer paid for shipping label (Stripe)",
                         meta={"amount": float(tx.get("amount") or 0)},
                     ).model_dump()}},
                )
                # Email is sent by /finalize when the customer's success page loads.
            else:
                # Label purchase failed AFTER the customer was charged.
                # Mark the return as paid-but-awaiting-manual-label and alert
                # the admin so they can buy a label manually and email it.
                err_msg = label.get("message") or "Unknown carrier error"
                log.warning("label purchase failed after stripe paid: rma=%s err=%s",
                            doc.get("rma_number"), err_msg)
                await db.returns.update_one(
                    {"id": return_id},
                    {"$set": {
                        "paid": True,
                        "status": "awaiting_approval",
                        "label_error": err_msg,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                     "$push": {"customer_actions": CustomerAction(
                         kind="label_purchase_failed",
                         label="Label purchase failed after payment — admin notified",
                         meta={"error": err_msg,
                               "amount": float(tx.get("amount") or 0)},
                     ).model_dump()}},
                )
                try:
                    await email_service.send_admin_label_failure(
                        cfg,
                        rma_number=doc.get("rma_number", ""),
                        return_id=return_id,
                        error_message=err_msg,
                        method_display_label=doc.get("method_display_label")
                            or METHOD_DISPLAY.get(doc.get("method", ""), doc.get("method", "")),
                        amount=float(tx.get("amount") or 0),
                        currency=(tx.get("currency") or "GBP").upper(),
                        customer_name=(doc.get("return_address") or {}).get("name", ""),
                        customer_email=doc.get("email", ""),
                        rate_provider=(doc.get("selected_rate") or {}).get("provider", ""),
                        rate_servicelevel=(doc.get("selected_rate") or {}).get("servicelevel", ""),
                    )
                except Exception as e:
                    log.warning("admin label-failure email error: %s", e)
        except Exception as e:
            log.exception("label purchase exception")

    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    return PaymentStatusResponse(
        status=status_resp.status,
        payment_status=status_resp.payment_status,
        return_id=return_id,
        rma_number=doc["rma_number"],
    )

@api.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    cfg = await settings_service.get_settings(db)
    host_url = str(request.base_url)
    webhook_url = f"{host_url.rstrip('/')}/api/webhook/stripe"
    stripe_co = StripeCheckout(api_key=cfg.get("stripe_api_key", ""), webhook_url=webhook_url)
    try:
        event = await stripe_co.handle_webhook(body, sig)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if event.payment_status == "paid" and event.session_id:
        tx = await db.payment_transactions.find_one({"session_id": event.session_id})
        if tx and tx.get("payment_status") != "paid":
            await db.payment_transactions.update_one(
                {"session_id": event.session_id},
                {"$set": {"payment_status": "paid", "status": "complete"}},
            )
    return {"received": True}

# ---- Self-ship (customer uses their own carrier) -------------------------

# Whitelist of carrier names accepted from the dropdown. "Other" lets the
# customer type a free-text carrier name. Names match the labels the
# frontend dropdown shows so they can be persisted as-is.
_SELF_SHIP_CARRIERS = {"Royal Mail", "Evri", "DPD", "UPS", "FedEx", "Other"}

# Map our friendly carrier names to Shippo carrier slugs for tracking lookups.
# Carriers without a Shippo slug here are stored as-is but never auto-tracked.
_SELF_SHIP_TRACKING_SLUGS = {
    "Royal Mail": "royal_mail",
    "Evri": "hermes_uk",  # Hermes UK rebranded to Evri; Shippo still uses hermes_uk
    "DPD": "dpd_uk",
    "UPS": "ups",
    "FedEx": "fedex",
}


def _normalize_self_ship_carrier(carrier: str, carrier_other: str) -> str:
    """Pick the display string we store on the doc."""
    c = (carrier or "").strip()
    if c == "Other":
        co = (carrier_other or "").strip()
        return co or "Other"
    return c


@api.post("/returns/{return_id}/self-ship/tracking")
async def submit_self_ship_tracking(return_id: str, body: SelfShipTrackingRequest):
    """Customer submits the carrier + tracking number for a self-ship return.

    Flow:
      - Doc must have method=self_ship
      - Status must be 'awaiting_tracking' (non-locked) or 'approved' (after
        admin OK on a locked-reason self-ship)
      - is_tracked=False is OK (customer used untracked service); we still
        record the carrier name + reference for our records.
    """
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    if doc.get("method") != "self_ship":
        raise HTTPException(status_code=400, detail="This return is not a self-ship return.")
    if doc.get("status") not in ("awaiting_tracking", "approved"):
        raise HTTPException(
            status_code=400,
            detail="Tracking can only be added once your return is ready to ship. "
                   "If your return needs admin review, please wait for the approval email.",
        )

    carrier_in = (body.carrier or "").strip()
    if carrier_in not in _SELF_SHIP_CARRIERS:
        raise HTTPException(status_code=400, detail="Unknown carrier.")
    carrier_other = (body.carrier_other or "").strip()[:120]
    tracking_number = (body.tracking_number or "").strip()[:80]
    is_tracked = bool(body.is_tracked)
    # Tracked returns require a tracking number; untracked returns may leave
    # the field blank (a reference is fine if they have one).
    if is_tracked and not tracking_number:
        raise HTTPException(
            status_code=400,
            detail="Please enter a tracking number, or tick \"I sent this without tracking\".",
        )

    carrier_display = _normalize_self_ship_carrier(carrier_in, carrier_other)

    now = datetime.now(timezone.utc).isoformat()
    update_set: Dict[str, Any] = {
        "self_ship_carrier": carrier_display,
        "self_ship_carrier_other": carrier_other if carrier_in == "Other" else "",
        "self_ship_tracking_number": tracking_number,
        "self_ship_is_tracked": is_tracked,
        "self_ship_submitted_at": now,
        "tracking_carrier": carrier_display,
        "tracking_number": tracking_number or None,
        "status": "in_transit",
        "updated_at": now,
    }
    action = CustomerAction(
        kind="self_ship_tracking_submitted",
        label=(f"Customer submitted self-ship tracking · {carrier_display}"
               + (f" · {tracking_number}" if tracking_number else "")
               + ("" if is_tracked else " (untracked)")),
        meta={"carrier": carrier_display, "tracking_number": tracking_number,
              "is_tracked": is_tracked},
    ).model_dump()

    await db.returns.update_one(
        {"id": return_id},
        {"$set": update_set, "$push": {"customer_actions": action}},
    )

    # Send tracking-received email to customer (best effort)
    cfg = await settings_service.get_settings(db)
    try:
        await email_service.send_self_ship_tracking_added(
            cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
            rma_number=doc["rma_number"], order_number=doc["order_number"],
            carrier=carrier_display, tracking_number=tracking_number,
            is_tracked=is_tracked,
            method=doc.get("method") or "",
            has_deduction=float(doc.get("refund_deduction") or 0.0) > 0,
        )
    except Exception as e:
        log.warning("self-ship tracking-added email failed: %s", e)

    # Best-effort: notify subscribed customers that their parcel is now in_transit.
    await _notify_status_subscriber(doc["rma_number"], "in_transit")

    # Best-effort initial Shippo tracking poll for tracked carriers.
    if is_tracked and tracking_number and cfg.get("shippo_api_key"):
        slug = _SELF_SHIP_TRACKING_SLUGS.get(carrier_display)
        if slug:
            try:
                tr = shippo_service.track(cfg["shippo_api_key"], slug, tracking_number)
                if tr:
                    await db.returns.update_one(
                        {"id": return_id},
                        {"$set": {
                            "tracking_status": tr.get("status"),
                            "tracking_updates": tr.get("history") or [],
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }},
                    )
            except Exception as e:
                log.info("self-ship initial Shippo track failed (carrier=%s, tn=%s): %s",
                         carrier_display, tracking_number, e)

    updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
    return _strip_heavy(updated)


async def _self_ship_reminder_tick():
    """One pass of the self-ship reminder + tracking-poll background loop.

    Reminder rules:
      - Status == awaiting_tracking AND no tracking submitted yet
      - Up to 5 reminders, spaced 24h apart
      - Reminder counter incremented each send so we never double-send

    Tracking poll rules:
      - Status == in_transit AND self_ship_tracking_number set AND is_tracked
      - Pull Shippo for fresh status; flip to 'delivered' when carrier confirms
    """
    cfg = await settings_service.get_settings(db)
    now = datetime.now(timezone.utc)

    # 1) Reminders --------------------------------------------------------
    cursor = db.returns.find(
        {"method": "self_ship", "status": "awaiting_tracking",
         "self_ship_tracking_number": {"$in": [None, ""]},
         "self_ship_is_tracked": {"$ne": False},
         "self_ship_reminder_count": {"$lt": 5},
         "archived": {"$ne": True}},
        {"_id": 0},
    )
    async for doc in cursor:
        last_at = doc.get("self_ship_last_reminder_at")
        # First reminder fires 24h after the return was created
        anchor_iso = last_at or doc.get("created_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_iso).replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - anchor).total_seconds() < 24 * 3600:
            continue
        attempt = int(doc.get("self_ship_reminder_count") or 0) + 1
        portal_url = (cfg.get("portal_public_url") or "").rstrip("/")
        link = f"{portal_url}/return/{doc['id']}/success" if portal_url else ""
        try:
            await email_service.send_self_ship_tracking_reminder(
                cfg, to_email=doc["email"],
                to_name=doc.get("customer_name") or "",
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                attempt=attempt,
                tracking_link=link,
            )
        except Exception as e:
            log.warning("self-ship reminder email failed (rma=%s): %s",
                        doc.get("rma_number"), e)
            continue
        await db.returns.update_one(
            {"id": doc["id"]},
            {"$set": {"self_ship_reminder_count": attempt,
                      "self_ship_last_reminder_at": now.isoformat(),
                      "updated_at": now.isoformat()},
             "$push": {"customer_actions": CustomerAction(
                 kind="self_ship_reminder_sent",
                 label=f"Reminder #{attempt} sent — please add tracking",
                 meta={"attempt": attempt},
             ).model_dump()}},
        )

    # 2) Tracking poll ----------------------------------------------------
    if cfg.get("shippo_api_key"):
        cursor2 = db.returns.find(
            {"method": "self_ship", "status": "in_transit",
             "self_ship_is_tracked": True,
             "self_ship_tracking_number": {"$nin": [None, ""]},
             "archived": {"$ne": True}},
            {"_id": 0, "id": 1, "self_ship_carrier": 1,
             "self_ship_tracking_number": 1, "tracking_status": 1},
        )
        async for doc in cursor2:
            slug = _SELF_SHIP_TRACKING_SLUGS.get(doc.get("self_ship_carrier") or "")
            if not slug:
                continue
            try:
                tr = shippo_service.track(
                    cfg["shippo_api_key"], slug,
                    doc.get("self_ship_tracking_number") or "",
                )
            except Exception as e:
                log.info("self-ship poll track exception (rma=%s): %s",
                         doc.get("rma_number"), e)
                continue
            if not tr:
                continue
            new_status_raw = (tr.get("status") or "").lower()
            update: Dict[str, Any] = {
                "tracking_updates": tr.get("history") or [],
                "tracking_status": tr.get("status"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            new_app_status: Optional[str] = None
            if new_status_raw in ("delivered", "delivery"):
                update["status"] = "delivered"
                new_app_status = "delivered"
            await db.returns.update_one({"id": doc["id"]}, {"$set": update})
            # Best-effort customer notification if they opted in.
            if new_app_status:
                rma = (await db.returns.find_one(
                    {"id": doc["id"]}, {"_id": 0, "rma_number": 1}
                ) or {}).get("rma_number")
                if rma:
                    await _notify_status_subscriber(rma, new_app_status)


async def _self_ship_loop():
    """Background loop wrapper. Runs `_self_ship_reminder_tick` every hour
    and silently swallows failures so a single bad iteration doesn't break
    the loop."""
    import asyncio as _asyncio
    while True:
        try:
            await _self_ship_reminder_tick()
        except Exception as e:
            log.warning("self_ship loop tick error: %s", e)
        await _asyncio.sleep(3600)  # 1 hour


@app.on_event("startup")
async def _start_self_ship_loop():
    import asyncio as _asyncio
    _asyncio.create_task(_self_ship_loop())


@app.on_event("startup")
async def _ensure_indexes():
    """Backend indexes that are safe to create on every boot. Idempotent —
    create_index is a no-op when the index already matches."""
    try:
        await db.login_attempts.create_index("ip")
        await db.login_attempts.create_index("email")
        await db.login_attempts.create_index("last_attempt_at")
    except Exception as e:
        log.warning("login_attempts index creation skipped: %s", e)


# ---- Tracking & Admin Endpoints ----

# Sensible defaults if we don't yet have enough delivered samples for a carrier.
# Tuned for UK return flows (warehouse in UK).
_DEFAULT_ETA_DAYS = {
    "Royal Mail": (2, 3),
    "Evri": (2, 4),
    "Hermes": (2, 4),
    "DPD": (1, 2),
    "UPS": (1, 3),
    "FedEx": (1, 3),
    "Parcelforce": (1, 2),
}
_FALLBACK_ETA_DAYS = (2, 5)
# Re-compute carrier averages at most this often per process — small in-memory
# cache so the public tracking endpoint stays cheap under traffic.
_CARRIER_AVG_CACHE: Dict[str, Any] = {"at": None, "data": {}}
_CARRIER_AVG_TTL_SEC = 600  # 10 minutes


async def _carrier_avg_days() -> Dict[str, float]:
    """Average label_purchased → delivered transit time per carrier, in days,
    from real delivered returns. Cached for `_CARRIER_AVG_TTL_SEC`."""
    now = datetime.now(timezone.utc)
    cached_at = _CARRIER_AVG_CACHE.get("at")
    if cached_at and (now - cached_at).total_seconds() < _CARRIER_AVG_TTL_SEC:
        return _CARRIER_AVG_CACHE["data"]

    bucket: Dict[str, List[float]] = {}
    cursor = db.returns.find(
        {"status": "delivered", "tracking_carrier": {"$nin": [None, ""]},
         "archived": {"$ne": True}},
        {"_id": 0, "tracking_carrier": 1, "customer_actions": 1,
         "tracking_updates": 1, "created_at": 1, "updated_at": 1},
    )
    async for d in cursor:
        carrier = str(d.get("tracking_carrier") or "").strip()
        if not carrier:
            continue
        lp_iso = None
        for act in (d.get("customer_actions") or []):
            kind = (act or {}).get("kind") or ""
            if kind in ("paid_for_label", "deduct_from_refund_confirmed",
                        "admin_approved_free_label"):
                lp_iso = (act or {}).get("at") or lp_iso
                break
        if not lp_iso:
            lp_iso = d.get("created_at")
        dl_iso = None
        for u in (d.get("tracking_updates") or []):
            if str((u or {}).get("status") or "").lower() in ("delivered", "delivery"):
                dl_iso = (u or {}).get("status_date") or (u or {}).get("at")
                break
        if not dl_iso:
            dl_iso = d.get("updated_at")
        try:
            lp = datetime.fromisoformat(str(lp_iso).replace("Z", "+00:00"))
            dl = datetime.fromisoformat(str(dl_iso).replace("Z", "+00:00"))
            days = (dl - lp).total_seconds() / 86400.0
            if 0 < days < 30:
                bucket.setdefault(carrier, []).append(days)
        except Exception:
            continue
    avg = {c: round(sum(v) / len(v), 1) for c, v in bucket.items()
           if len(v) >= 3}  # need ≥3 samples to trust an average
    _CARRIER_AVG_CACHE["at"] = now
    _CARRIER_AVG_CACHE["data"] = avg
    return avg


def _eta_for_doc(doc: Dict[str, Any], carrier_avgs: Dict[str, float]) -> Dict[str, Any]:
    """Pick the best ETA window for this return: data-driven if we have
    enough carrier samples, sensible default otherwise. Returns
    {min_days, max_days, label, source}. Empty when delivered/refunded."""
    status = (doc.get("status") or "").lower()
    if status in ("delivered", "refunded", "store_credit_issued",
                  "rejected", "cancelled"):
        return {}
    carrier = (doc.get("tracking_carrier") or "").strip()
    avg = carrier_avgs.get(carrier) if carrier else None
    if avg is not None:
        # Build a tight window around the average: ±25%, floored at 1 day.
        lo = max(1, int(round(avg * 0.75)))
        hi = max(lo + 1, int(round(avg * 1.25)))
        source = "carrier_average"
    else:
        lo, hi = _DEFAULT_ETA_DAYS.get(carrier, _FALLBACK_ETA_DAYS)
        source = "default" if carrier else "fallback"
    if lo == hi:
        label = f"Usually arrives in {lo} business day{'' if lo == 1 else 's'}"
    else:
        label = f"Usually arrives in {lo}–{hi} business days"
    return {"min_days": lo, "max_days": hi, "label": label, "source": source}


# Status transitions worth proactively emailing the customer about. Other
# transitions either already trigger a dedicated email (label_ready,
# coupon-issued) or aren't customer-visible enough to warrant one.
_NOTIFY_STATUSES = {"in_transit", "delivered", "refunded",
                    "store_credit_issued", "approved", "rejected"}

# In-process dedupe so the same status notification can't fire twice within
# the same minute (handy when the Shippo poll loop double-runs).
_NOTIFY_DEDUP: Dict[str, datetime] = {}


async def _notify_status_subscriber(rma_number: str, new_status: str) -> bool:
    """Fire a status-update email iff the customer subscribed via the public
    tracking page. Always fail-safe — never raises."""
    if not rma_number or new_status not in _NOTIFY_STATUSES:
        return False
    dedup_key = f"{rma_number}:{new_status}"
    now = datetime.now(timezone.utc)
    last = _NOTIFY_DEDUP.get(dedup_key)
    if last and (now - last).total_seconds() < 60:
        return False
    _NOTIFY_DEDUP[dedup_key] = now
    try:
        doc = await db.returns.find_one(
            {"rma_number": rma_number},
            {"_id": 0, "rma_number": 1, "order_number": 1, "email": 1,
             "customer_name": 1, "notify_status_email": 1,
             "notify_status_email_address": 1},
        )
        if not doc or not doc.get("notify_status_email"):
            return False
        to_email = (doc.get("notify_status_email_address")
                    or doc.get("email") or "").strip()
        if not to_email:
            return False
        cfg = await settings_service.get_settings(db)
        from models import ReturnStatus  # avoid heavy top-of-file circular
        try:
            label = ReturnStatus(new_status).value if hasattr(ReturnStatus, '__members__') else new_status
        except Exception:
            label = new_status
        # Use STATUS_LABELS from frontend? No — we don't have a Python copy.
        # Stick with the raw status_label and let the email template handle copy.
        await email_service.send_status_update(
            cfg, to_email=to_email,
            to_name=doc.get("customer_name") or "",
            rma_number=doc["rma_number"],
            order_number=doc.get("order_number") or "",
            new_status=new_status,
            status_label=label,
        )
        return True
    except Exception as e:
        log.warning("status-update notify failed (rma=%s, status=%s): %s",
                    rma_number, new_status, e)
        return False


async def _find_return_by_identifier(
    identifier: str,
    projection: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Public-facing return lookup — used by `/tracking/{identifier}` and the
    subscribe endpoint. Resolves the customer's input against, in order:

    1. RMA number (case-insensitive, e.g. "rma-123abc" → "RMA-123ABC")
    2. Carrier tracking number on the purchased label
    3. Self-ship tracking number the customer pasted
    4. Order number (the human-friendly invoice number from WooCommerce)
    5. Order ID (the internal numeric Woo ID — some customers paste this
       instead of the display number)

    Steps 4 + 5 also try integer comparisons because historic returns may
    have stored these fields as int rather than str. When several returns
    share the same order, we surface the most recently updated one so the
    customer lands on the active return — this is the right call for
    repeat-return orders (e.g. someone returns more items from the same
    order a week later) and for any return method (refund, store credit,
    self-ship — method is irrelevant to lookup).
    """
    if not identifier:
        return None
    ident = identifier.strip()
    if not ident:
        return None
    no_hash = ident.lstrip("#").strip()
    proj = projection if projection is not None else {"_id": 0}

    # Pass 1: exact identifier (RMA / tracking)
    doc = await db.returns.find_one(
        {"$or": [{"rma_number": ident.upper()},
                 {"tracking_number": ident},
                 {"self_ship_tracking_number": ident}]},
        proj,
    )
    if doc:
        return doc

    # Pass 2: order number / order ID, type-agnostic.
    if not no_hash:
        return None
    or_clauses: List[Dict[str, Any]] = [
        {"order_number": no_hash},
        {"order_id": no_hash},
    ]
    try:
        as_int = int(no_hash)
        or_clauses.extend([{"order_number": as_int}, {"order_id": as_int}])
    except (TypeError, ValueError):
        pass
    return await db.returns.find_one(
        {"$or": or_clauses},
        proj,
        sort=[("updated_at", -1), ("created_at", -1)],
    )


@api.get("/tracking/{identifier}", response_model=TrackingResponse)
async def track_return(identifier: str):
    doc = await _find_return_by_identifier(identifier, projection={"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="No return found.")
    # Map `tracking_updates` -> `updates` since the public model field name differs
    if "tracking_updates" in doc and "updates" not in doc:
        doc["updates"] = doc.get("tracking_updates") or []
    # Smart ETA window — only included while the parcel is in flight.
    try:
        avgs = await _carrier_avg_days()
        eta = _eta_for_doc(doc, avgs)
        if eta:
            doc["eta_min_days"] = eta["min_days"]
            doc["eta_max_days"] = eta["max_days"]
            doc["eta_label"] = eta["label"]
            doc["eta_source"] = eta["source"]
    except Exception as e:
        log.info("eta calc skipped: %s", e)
    # Surface current subscription state so the frontend toggle reflects DB truth.
    doc["notify_status_email"] = bool(doc.get("notify_status_email"))
    return TrackingResponse(**doc)


@api.post("/tracking/{identifier}/subscribe")
async def subscribe_status_updates(identifier: str, body: SubscribeStatusRequest):
    """Customer-facing toggle: opt in / out of email-on-status-change.

    Looks the return up the same way as `GET /tracking/{identifier}` (RMA,
    tracking number, self-ship tracking, order number, or order ID). When
    `enabled` is True we store the customer-supplied email (or fall back to
    the order email). When False we just clear the flag — the email stays
    on the doc so re-enabling later is one click.
    """
    doc = await _find_return_by_identifier(
        identifier,
        projection={"_id": 0, "id": 1, "rma_number": 1, "email": 1,
                    "notify_status_email": 1, "notify_status_email_address": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="No return found.")

    update_set: Dict[str, Any] = {
        "notify_status_email": bool(body.enabled),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if body.enabled:
        # Light-touch validation: must look like an email if provided. Falls
        # back to the order's email if blank (most common case).
        chosen = (body.email or "").strip() or (doc.get("email") or "").strip()
        if not chosen or "@" not in chosen or "." not in chosen.split("@")[-1]:
            raise HTTPException(status_code=400,
                                detail="Please enter a valid email address.")
        update_set["notify_status_email_address"] = chosen
    await db.returns.update_one({"id": doc["id"]}, {"$set": update_set})

    return {
        "ok": True,
        "rma_number": doc["rma_number"],
        "notify_status_email": update_set["notify_status_email"],
        "notify_status_email_address": update_set.get(
            "notify_status_email_address",
            doc.get("notify_status_email_address") or "",
        ),
    }


@api.post("/auth/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest, request: Request):
    """Admin login with per-IP/email brute-force protection.

    Failed attempts are tracked in the `login_attempts` collection keyed by
    "{client_ip}:{email}". Once `login_max_attempts` failures land in any
    `login_window_minutes` rolling window, further attempts get HTTP 429 for
    the next `login_lockout_minutes`. A successful login clears the counter.
    All thresholds are tunable from the admin Settings UI without redeploy.
    """
    cfg = await settings_service.get_settings(db)
    try:
        max_attempts = int(float(cfg.get("login_max_attempts") or 5))
    except (TypeError, ValueError):
        max_attempts = 5
    try:
        window_min = int(float(cfg.get("login_window_minutes") or 15))
    except (TypeError, ValueError):
        window_min = 15
    try:
        lockout_min = int(float(cfg.get("login_lockout_minutes") or 15))
    except (TypeError, ValueError):
        lockout_min = 15

    ip = _client_ip(request)
    email_norm = (body.email or "").lower().strip()
    identifier = f"{ip}:{email_norm}"
    now = datetime.now(timezone.utc)

    # 1. Reject up-front if currently locked out.
    rec = await db.login_attempts.find_one({"_id": identifier}, {"_id": 0})
    if rec and rec.get("locked_until"):
        try:
            locked_until = datetime.fromisoformat(rec["locked_until"])
        except Exception:
            locked_until = None
        if locked_until and locked_until > now:
            retry_after = max(int((locked_until - now).total_seconds()), 1)
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts. Try again in "
                       f"{(retry_after + 59) // 60} minute(s).",
                headers={"Retry-After": str(retry_after)},
            )

    # 2. Verify credentials.
    if not auth_svc.verify_admin(body.email, body.password):
        # Increment attempts, opening a new window on first failure or when
        # the previous window has expired.
        attempts = 1
        if rec:
            try:
                window_start = datetime.fromisoformat(rec.get("window_start") or "")
            except Exception:
                window_start = None
            if window_start and (now - window_start).total_seconds() < window_min * 60:
                attempts = int(rec.get("attempts") or 0) + 1
        update_set = {
            "attempts": attempts,
            "window_start": (rec.get("window_start") if rec and attempts > 1
                             else now.isoformat()),
            "last_attempt_at": now.isoformat(),
            "ip": ip,
            "email": email_norm,
        }
        if attempts >= max_attempts:
            update_set["locked_until"] = (
                now + timedelta(minutes=lockout_min)
            ).isoformat()
        await db.login_attempts.update_one(
            {"_id": identifier}, {"$set": update_set}, upsert=True,
        )
        if attempts >= max_attempts:
            retry_after = lockout_min * 60
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts. Try again in "
                       f"{lockout_min} minute(s).",
                headers={"Retry-After": str(retry_after)},
            )
        remaining = max_attempts - attempts
        raise HTTPException(
            status_code=401,
            detail=f"Invalid credentials. {remaining} attempt"
                   f"{'s' if remaining != 1 else ''} remaining before lockout.",
        )

    # 3. Success — reset the per-IP/email counter.
    await db.login_attempts.delete_one({"_id": identifier})
    token = auth_svc.create_token(email_norm)
    return AdminLoginResponse(token=token, email=email_norm)


# --- Store credit issuance -------------------------------------------------

async def _issue_store_credit_for_return(return_id: str) -> Optional[dict]:
    """Create a WooCommerce coupon worth refund + bonus%, store it on the
    return doc, and email the customer. Idempotent — if a coupon was already
    issued, returns the doc unchanged.

    Returns the updated doc on success, None on failure. Errors are logged
    and swallowed so callers can decide how to surface them.
    """
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        return None
    if doc.get("coupon_code"):
        return doc  # already issued — don't double-up

    cfg = await settings_service.get_settings(db)
    if str(cfg.get("enable_store_credit", "true")).lower() not in ("1", "true", "yes", "on"):
        log.warning("store_credit disabled by setting; skipping issuance for %s", return_id)
        return None

    try:
        bonus_pct = float(cfg.get("store_credit_bonus_percent") or 0.0)
    except (TypeError, ValueError):
        bonus_pct = 0.0
    try:
        expiry_days = int(float(cfg.get("store_credit_expiry_days") or 365))
    except (TypeError, ValueError):
        expiry_days = 365

    # Bonus is applied to the FULL refund subtotal (not the post-deduction
    # net). Any label cost the customer chose to deduct is subtracted from
    # the bonus-adjusted total, so the customer gets the full perk of the
    # bonus on every penny they were owed for the items.
    base_refund = float(doc.get("refund_amount") or 0.0)
    if base_refund <= 0:
        log.warning("refund_amount is zero for %s, cannot issue store credit", return_id)
        return None
    label_deduction = float(doc.get("refund_deduction") or 0.0)
    bonus_amount = round(base_refund * (bonus_pct / 100.0), 2)
    coupon_amount = round(base_refund + bonus_amount - label_deduction, 2)
    if coupon_amount < 0:
        coupon_amount = 0.0
    from datetime import timedelta
    expires_at_dt = datetime.now(timezone.utc) + timedelta(days=expiry_days)
    expires_on = expires_at_dt.date().isoformat()  # yyyy-mm-dd for Woo

    coupon = await woo.create_coupon(
        cfg,
        email=doc["email"],
        amount=coupon_amount,
        expires_on=expires_on,
        code_prefix="RMA",
        reference=doc.get("rma_number") or "",
        description=f"Store credit for return {doc.get('rma_number')} "
                    f"(base £{base_refund:.2f} + {bonus_pct:g}% bonus"
                    + (f" − £{label_deduction:.2f} label" if label_deduction > 0 else "") + ")",
    )
    if not coupon or not coupon.get("code"):
        log.warning("store_credit coupon creation failed for %s", return_id)
        return None

    currency = doc.get("currency") or cfg.get("currency") or "GBP"
    action = CustomerAction(
        kind="store_credit_issued",
        label=f"Store credit issued: {coupon['code']} "
              f"({currency} {coupon_amount:.2f}, +{bonus_pct:g}% bonus)",
        meta={"code": coupon["code"], "amount": coupon_amount,
              "bonus_percent": bonus_pct, "expires_on": expires_on},
    ).model_dump()

    await db.returns.update_one(
        {"id": return_id},
        {"$set": {
            "coupon_code": coupon["code"],
            "coupon_amount": coupon_amount,
            "coupon_currency": currency,
            "coupon_expires_at": expires_at_dt.isoformat(),
            "store_credit_bonus_percent_applied": bonus_pct,
            "coupon_label_deduction": label_deduction,
            "store_credit_issued_at": datetime.now(timezone.utc).isoformat(),
            "status": "store_credit_issued",
            # Mark the return as closed — the customer cannot re-open a return
            # on the same line items, and admins see a clear "Closed" badge.
            "closed": True,
            "closed_reason": "store_credit_applied",
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
         "$push": {"customer_actions": action}},
    )

    # Email the customer. Non-blocking — failures don't undo the coupon.
    try:
        email_res = await email_service.send_store_credit_issued(
            cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
            rma_number=doc["rma_number"], order_number=doc["order_number"],
            coupon_code=coupon["code"], coupon_amount=coupon_amount,
            currency=currency, bonus_percent=bonus_pct, expires_on=expires_on,
            label_deduction=label_deduction,
        )
        if email_res.get("attempts"):
            await db.returns.update_one(
                {"id": return_id},
                {"$push": {"email_log": {"$each": [{**a, "kind": "store_credit_issued"}
                                                   for a in email_res.get("attempts", [])]}}},
            )
    except Exception as e:
        log.warning("store_credit email send failed: %s", e)

    return await db.returns.find_one({"id": return_id}, {"_id": 0})


@api.post("/returns/{return_id}/issue-store-credit")
async def issue_store_credit(return_id: str):
    """Idempotent public endpoint. Safe to retry from the frontend if a
    network glitch hides the result of the auto-issuance at submission time."""
    updated = await _issue_store_credit_for_return(return_id)
    if not updated:
        raise HTTPException(status_code=502, detail="Store credit could not be issued.")
    return {"ok": True, "return": _strip_heavy(updated)}



@api.get("/admin/me")
async def get_admin_me(user=Depends(auth_svc.require_admin)):
    return {"email": user}

@api.get("/admin/returns")
async def list_returns(
    status: Optional[str] = None,
    archived: Optional[bool] = False,
    user=Depends(auth_svc.require_admin),
):
    q: Dict[str, Any] = {}
    if status:
        q["status"] = status
    # By default, hide archived rows. Pass ?archived=true to see only archived.
    if archived:
        q["archived"] = True
    else:
        q["archived"] = {"$ne": True}
    cursor = db.returns.find(q, {"_id": 0}).sort("created_at", -1)
    rows = await cursor.to_list(length=500)
    return [_strip_heavy(r) for r in rows]

@api.get("/admin/stats")
async def get_admin_stats(user=Depends(auth_svc.require_admin)):
    try:
        total_returns = await db.returns.count_documents({})
        pending_returns = await db.returns.count_documents({"status": "label_purchased"})

        by_status_pipeline = [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        by_status = {x["_id"]: x["n"] async for x in db.returns.aggregate(by_status_pipeline)}

        pipeline = [
            {"$match": {"refunded": True}},
            {"$group": {"_id": None, "total": {"$sum": "$refund_amount"}}}
        ]
        result = await db.returns.aggregate(pipeline).to_list(1)
        total_refunded = result[0]["total"] if result else 0

        deduction_pipeline = [
            {"$match": {"refund_deduction": {"$gt": 0}}},
            {"$group": {"_id": None, "total": {"$sum": "$refund_deduction"}, "n": {"$sum": 1}}}
        ]
        ded_res = await db.returns.aggregate(deduction_pipeline).to_list(1)

        return {
            "total": total_returns,
            "total_returns": total_returns,
            "pending_returns": pending_returns,
            "total_refunded": total_refunded,
            "by_status": by_status,
            "total_deducted_shipping": (ded_res[0]["total"] if ded_res else 0),
            "deduction_count": (ded_res[0]["n"] if ded_res else 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.get("/admin/analytics")
async def get_admin_analytics(
    weeks: int = 12,
    top_sku_limit: int = 10,
    reason_days: int = 90,
    user=Depends(auth_svc.require_admin),
):
    """One-shot dashboard payload.

    - `weekly`: returns opened each ISO week for the last `weeks` weeks
    - `top_skus`: most-returned items (by unit quantity) with % of all returned units
    - `reasons`: reason breakdown for the last `reason_days` days
    - `carrier_transit`: avg hours from label_purchased -> delivered per carrier
    - `financials`: £ refunded, £ deducted, £ store-credit issued (+ counts)
    - `method_split`: how many returns chose each method
    """
    from datetime import timedelta as _td

    now = datetime.now(timezone.utc)

    # --- Weekly (last N weeks) -----------------------------------------------
    weeks = max(1, min(int(weeks or 12), 52))
    start_weekly = (now - _td(weeks=weeks - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Snap start to the beginning of its ISO week (Monday).
    start_weekly = start_weekly - _td(days=start_weekly.weekday())

    # created_at is stored as ISO string; compare strings (lexicographic) —
    # ISO-8601 sorts correctly.
    weekly_docs = db.returns.find(
        {"created_at": {"$gte": start_weekly.isoformat()},
         "archived": {"$ne": True}},
        {"_id": 0, "created_at": 1},
    )
    week_buckets: Dict[str, int] = {}
    async for d in weekly_docs:
        try:
            dt = datetime.fromisoformat(str(d["created_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        iso_year, iso_week, _ = dt.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        week_buckets[key] = week_buckets.get(key, 0) + 1

    weekly: List[Dict] = []
    cursor = start_weekly
    for _ in range(weeks):
        iso_year, iso_week, _dow = cursor.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        weekly.append({
            "week": key,
            "week_start": cursor.date().isoformat(),
            "count": week_buckets.get(key, 0),
        })
        cursor = cursor + _td(weeks=1)

    # --- Top returned SKUs ---------------------------------------------------
    top_sku_limit = max(1, min(int(top_sku_limit or 10), 50))
    sku_pipeline = [
        {"$match": {"archived": {"$ne": True}}},
        {"$unwind": "$items"},
        {"$group": {
            "_id": {
                "sku": {"$ifNull": ["$items.sku", ""]},
                "name": {"$ifNull": ["$items.name", "Item"]},
                "product_id": {"$ifNull": ["$items.product_id", ""]},
            },
            "units": {"$sum": {"$ifNull": ["$items.quantity", 1]}},
            "return_count": {"$sum": 1},
        }},
        {"$sort": {"units": -1}},
        {"$limit": top_sku_limit},
    ]
    total_units_pipeline = [
        {"$match": {"archived": {"$ne": True}}},
        {"$unwind": "$items"},
        {"$group": {"_id": None, "n": {"$sum": {"$ifNull": ["$items.quantity", 1]}}}},
    ]
    total_units_res = await db.returns.aggregate(total_units_pipeline).to_list(1)
    total_units = int(total_units_res[0]["n"]) if total_units_res else 0
    top_skus: List[Dict] = []
    async for row in db.returns.aggregate(sku_pipeline):
        units = int(row.get("units") or 0)
        top_skus.append({
            "sku": row["_id"].get("sku") or "",
            "name": row["_id"].get("name") or "",
            "product_id": row["_id"].get("product_id") or "",
            "units": units,
            "return_count": int(row.get("return_count") or 0),
            "share_pct": round((units / total_units) * 100, 1) if total_units else 0.0,
        })

    # --- Reasons (last `reason_days` days) -----------------------------------
    reason_days = max(1, min(int(reason_days or 90), 365))
    reasons_since = (now - _td(days=reason_days)).isoformat()
    reason_pipeline = [
        {"$match": {"archived": {"$ne": True},
                    "created_at": {"$gte": reasons_since}}},
        {"$unwind": "$items"},
        {"$group": {"_id": "$items.reason", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    reasons: List[Dict] = []
    async for row in db.returns.aggregate(reason_pipeline):
        reasons.append({"reason": row["_id"] or "unknown",
                        "count": int(row.get("count") or 0)})

    # --- Carrier transit time (label_purchased → delivered, per carrier) -----
    # Approximated from `customer_actions` timestamps (label purchased action)
    # and the delivered update timestamp on `tracking_updates`. Falls back to
    # computing from the top-level fields where possible.
    carrier_docs = db.returns.find(
        {"status": "delivered", "tracking_carrier": {"$nin": [None, ""]},
         "archived": {"$ne": True}},
        {"_id": 0, "tracking_carrier": 1, "customer_actions": 1,
         "tracking_updates": 1, "created_at": 1, "updated_at": 1},
    )
    carrier_bucket: Dict[str, List[float]] = {}
    async for d in carrier_docs:
        carrier = str(d.get("tracking_carrier") or "").strip()
        if not carrier:
            continue
        # Earliest "label purchased" moment
        lp_iso = None
        for act in (d.get("customer_actions") or []):
            kind = (act or {}).get("kind") or ""
            if kind in ("paid_for_label", "deduct_from_refund_confirmed",
                        "admin_approved_free_label"):
                lp_iso = (act or {}).get("at") or lp_iso
                break  # customer_actions are time-ordered; first match is earliest
        # Fall back to `updated_at` when status first flipped — rough, but usable
        if not lp_iso:
            lp_iso = d.get("updated_at") or d.get("created_at")
        # Delivered moment from tracking_updates
        dl_iso = None
        for u in (d.get("tracking_updates") or []):
            if str((u or {}).get("status") or "").lower() in ("delivered", "delivery"):
                dl_iso = (u or {}).get("status_date") or (u or {}).get("at")
                break
        if not dl_iso:
            dl_iso = d.get("updated_at")
        try:
            lp = datetime.fromisoformat(str(lp_iso).replace("Z", "+00:00"))
            dl = datetime.fromisoformat(str(dl_iso).replace("Z", "+00:00"))
            hours = (dl - lp).total_seconds() / 3600.0
            if 0 < hours < 24 * 30:  # sanity filter: 0 < t < 30 days
                carrier_bucket.setdefault(carrier, []).append(hours)
        except Exception:
            continue
    carrier_transit = [
        {"carrier": c, "avg_hours": round(sum(v) / len(v), 1), "count": len(v)}
        for c, v in sorted(carrier_bucket.items(), key=lambda kv: kv[0])
        if v
    ]

    # --- Financials ---------------------------------------------------------
    refunded_res = await db.returns.aggregate([
        {"$match": {"refunded": True, "archived": {"$ne": True}}},
        {"$group": {"_id": None, "total": {"$sum": "$refund_amount"},
                    "n": {"$sum": 1}}},
    ]).to_list(1)
    deducted_res = await db.returns.aggregate([
        {"$match": {"refund_deduction": {"$gt": 0}, "archived": {"$ne": True}}},
        {"$group": {"_id": None, "total": {"$sum": "$refund_deduction"},
                    "n": {"$sum": 1}}},
    ]).to_list(1)
    credit_res = await db.returns.aggregate([
        {"$match": {"coupon_amount": {"$gt": 0}, "archived": {"$ne": True}}},
        {"$group": {"_id": None, "total": {"$sum": "$coupon_amount"},
                    "n": {"$sum": 1}}},
    ]).to_list(1)
    financials = {
        "total_refunded": float(refunded_res[0]["total"]) if refunded_res else 0.0,
        "refunded_count": int(refunded_res[0]["n"]) if refunded_res else 0,
        "total_deducted": float(deducted_res[0]["total"]) if deducted_res else 0.0,
        "deducted_count": int(deducted_res[0]["n"]) if deducted_res else 0,
        "total_store_credit": float(credit_res[0]["total"]) if credit_res else 0.0,
        "store_credit_count": int(credit_res[0]["n"]) if credit_res else 0,
    }

    # --- Method split -------------------------------------------------------
    method_pipeline = [
        {"$match": {"archived": {"$ne": True}}},
        {"$group": {"_id": "$method", "count": {"$sum": 1}}},
    ]
    method_split = {row["_id"]: int(row["count"])
                    async for row in db.returns.aggregate(method_pipeline)
                    if row["_id"]}

    return {
        "weekly": weekly,
        "top_skus": top_skus,
        "reasons": reasons,
        "carrier_transit": carrier_transit,
        "financials": financials,
        "method_split": method_split,
        "generated_at": now.isoformat(),
    }



@api.get("/admin/settings")
async def get_admin_settings(user=Depends(auth_svc.require_admin)):
    return await settings_service.get_public_settings(db)

@api.put("/admin/settings")
async def update_admin_settings(body: dict, user=Depends(auth_svc.require_admin)):
    # Only fields present with non-empty values are updated — other settings remain intact.
    await settings_service.update_settings(db, body)
    settings = await settings_service.get_public_settings(db)
    cfg = await settings_service.get_settings(db)
    try:
        connections = await integrations_ping.test_all(cfg)
    except Exception as e:
        connections = {"error": {"ok": False, "message": str(e)}}
    return {"settings": settings, "connections": connections}


@api.post("/admin/settings/test")
async def test_admin_settings(user=Depends(auth_svc.require_admin)):
    cfg = await settings_service.get_settings(db)
    return await integrations_ping.test_all(cfg)


@api.post("/admin/reset-test-data")
async def reset_test_data(user=Depends(auth_svc.require_admin)):
    r1 = await db.returns.delete_many({})
    r2 = await db.payment_transactions.delete_many({})
    return {"returns_deleted": r1.deleted_count, "transactions_deleted": r2.deleted_count}

# Max upload size for admin attachments (5 MB) — stored inline as base64 in
# MongoDB doc and attached to the customer email.
MAX_ADMIN_UPLOAD_BYTES = 5 * 1024 * 1024


async def _read_upload_as_attachment(upload: Optional[UploadFile]) -> Optional[Dict[str, Any]]:
    """Turn an UploadFile into the attachment dict our email_service accepts.
    Returns None if no file was supplied. Raises HTTPException if too large."""
    if not upload or not upload.filename:
        return None
    data = await upload.read()
    if not data:
        return None
    if len(data) > MAX_ADMIN_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max is {MAX_ADMIN_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    return {
        "filename": upload.filename,
        "content_type": upload.content_type or "application/octet-stream",
        "content_base64": base64.b64encode(data).decode("ascii"),
        "size_bytes": len(data),
    }


# --- New Admin Action Routes ---
@api.post("/admin/returns/{return_id}/approve-free")
async def approve_free_return(
    return_id: str,
    note: str = Form(""),
    label_file: Optional[UploadFile] = File(None),
    user=Depends(auth_svc.require_admin),
):
    """Approve a free-label return. Admin can attach a label file (PDF/image)
    and an optional note. Both are emailed to the customer and stored on the
    return doc for future reference."""
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")

    # Branch: store_credit returns under manual-review. Approval here approves
    # the SHIPPING path only (free label OR self-ship per customer's choice) —
    # the store credit itself is issued separately later, once the parcel
    # physically arrives, via "Parcel received — issue store credit".
    if doc.get("method") == "store_credit":
        # Two-stage admin approval for store_credit returns:
        #   stage 1 (status == awaiting_approval): approve the shipping path
        #   stage 2 (status in shipped/received states): issue the coupon
        # The same endpoint handles both so older clients keep working —
        # the button labels in the admin UI tell the admin which stage they're
        # acting on.
        sub_choice = doc.get("restricted_shipping_choice") or "free_label"
        note_clean = (note or "").strip()
        st = doc.get("status") or ""
        is_financial_stage = st in (
            "label_purchased", "in_transit", "delivered",
            "approved", "awaiting_tracking",
        )
        if is_financial_stage:
            # Financial approval: parcel has arrived, issue the coupon now.
            if note_clean:
                await db.returns.update_one(
                    {"id": return_id},
                    {"$set": {"admin_approve_note": note_clean,
                              "admin_note": note_clean}},
                )
            updated = await _issue_store_credit_for_return(return_id)
            if not updated:
                raise HTTPException(status_code=502,
                                    detail="Store credit could not be issued.")
            return {"status": "store_credit_issued", "email_sent": True,
                    "email_provider": None, "return": _strip_heavy(updated)}

        if sub_choice == "self_ship":
            # Mirror the self-ship branch below: flip to awaiting_tracking,
            # email the customer that they can post the parcel, but DO NOT
            # issue the coupon yet.
            now = datetime.now(timezone.utc).isoformat()
            update_set: Dict[str, Any] = {
                "status": "awaiting_tracking",
                "updated_at": now,
            }
            if note_clean:
                update_set["admin_approve_note"] = note_clean
                update_set["admin_note"] = note_clean
            action = CustomerAction(
                kind="admin_approved_self_ship",
                label=("Admin approved self-ship for store-credit return"
                       + (f" · note: \"{note_clean}\"" if note_clean else "")),
                meta={"note_preview": note_clean[:200], "shipping_choice": "self_ship"},
            ).model_dump()
            await db.returns.update_one(
                {"id": return_id},
                {"$set": update_set, "$push": {"customer_actions": action}},
            )
            cfg = await settings_service.get_settings(db)
            email_res = {"ok": False, "provider": None, "attempts": []}
            try:
                email_res = await email_service.send_self_ship_approved_to_ship(
                    cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
                    rma_number=doc["rma_number"], order_number=doc["order_number"],
                    admin_note=note_clean,
                )
            except Exception as e:
                log.warning("self-ship (store_credit) approval email failed: %s", e)
            await db.returns.update_one(
                {"id": return_id},
                {"$push": {"email_log": {"$each": [{**a, "kind": "self_ship_approved_to_ship"}
                                                   for a in email_res.get("attempts", [])]}}},
            )
            updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
            return {"status": "awaiting_tracking",
                    "email_sent": bool(email_res.get("ok")),
                    "email_provider": email_res.get("provider"),
                    "return": _strip_heavy(updated)}

        # sub_choice == "free_label" (default): approve the free label,
        # attach it if uploaded, email the customer. Coupon issuance still
        # waits for parcel-received.
        attachment = await _read_upload_as_attachment(label_file)
        update_set: Dict[str, Any] = {
            "status": "label_purchased" if attachment else "approved",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if note_clean:
            update_set["admin_approve_note"] = note_clean
            update_set["admin_note"] = note_clean
        if attachment:
            update_set["admin_label_attachment"] = attachment
        action = CustomerAction(
            kind="admin_approved_free_label",
            label=("Admin approved free label for store-credit return"
                   + (" with attached label" if attachment else "")
                   + (f" · note: \"{note_clean}\"" if note_clean else "")),
            meta={"has_attachment": bool(attachment),
                  "attachment_filename": attachment["filename"] if attachment else None,
                  "note_preview": note_clean[:200],
                  "shipping_choice": "free_label"},
        ).model_dump()
        await db.returns.update_one(
            {"id": return_id},
            {"$set": update_set, "$push": {"customer_actions": action}},
        )
        cfg = await settings_service.get_settings(db)
        email_attachments = None
        if attachment:
            email_attachments = [{
                "filename": attachment["filename"],
                "content_type": attachment["content_type"],
                "content_base64": attachment["content_base64"],
            }]
        email_res = {"ok": False, "provider": None, "attempts": []}
        try:
            email_res = await email_service.send_free_label_approved(
                cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                admin_note=note_clean, attachments=email_attachments,
            )
        except Exception as e:
            log.warning("approve-free (store_credit) email failed: %s", e)
        await db.returns.update_one(
            {"id": return_id},
            {"$push": {"email_log": {"$each": [{**a, "kind": "free_label_approved"}
                                               for a in email_res.get("attempts", [])]}}},
        )
        updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
        return {"status": update_set["status"],
                "email_sent": bool(email_res.get("ok")),
                "email_provider": email_res.get("provider"),
                "return": _strip_heavy(updated)}

    # Branch: self-ship returns flagged for admin review (locked reasons).
    # Approval flips the status to awaiting_tracking so the customer can
    # ship and submit their tracking number — no label is uploaded here.
    if doc.get("method") == "self_ship":
        note_clean = (note or "").strip()
        now = datetime.now(timezone.utc).isoformat()
        update_set: Dict[str, Any] = {
            "status": "awaiting_tracking",
            "updated_at": now,
        }
        if note_clean:
            update_set["admin_approve_note"] = note_clean
            update_set["admin_note"] = note_clean
        action = CustomerAction(
            kind="admin_approved_self_ship",
            label=("Admin approved self-ship return"
                   + (f" · note: \"{note_clean}\"" if note_clean else "")),
            meta={"note_preview": note_clean[:200]},
        ).model_dump()
        await db.returns.update_one(
            {"id": return_id},
            {"$set": update_set, "$push": {"customer_actions": action}},
        )

        cfg = await settings_service.get_settings(db)
        email_res = {"ok": False, "provider": None, "attempts": []}
        try:
            email_res = await email_service.send_self_ship_approved_to_ship(
                cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                admin_note=note_clean,
            )
        except Exception as e:
            log.warning("self-ship approval email failed: %s", e)
        await db.returns.update_one(
            {"id": return_id},
            {"$push": {"email_log": {"$each": [{**a, "kind": "self_ship_approved_to_ship"}
                                               for a in email_res.get("attempts", [])]}}},
        )
        updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
        return {"status": "awaiting_tracking", "email_sent": bool(email_res.get("ok")),
                "email_provider": email_res.get("provider"), "return": _strip_heavy(updated)}

    attachment = await _read_upload_as_attachment(label_file)
    note_clean = (note or "").strip()

    update_set: Dict[str, Any] = {
        "status": "label_purchased" if attachment else "approved",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if note_clean:
        update_set["admin_approve_note"] = note_clean
        update_set["admin_note"] = note_clean  # keep legacy field in sync
    if attachment:
        update_set["admin_label_attachment"] = attachment

    action = CustomerAction(
        kind="admin_approved_free_label",
        label=("Admin approved free return" + (" with attached label" if attachment else "")
               + (f" · note: \"{note_clean}\"" if note_clean else "")),
        meta={"has_attachment": bool(attachment),
              "attachment_filename": attachment["filename"] if attachment else None,
              "note_preview": note_clean[:200]},
    ).model_dump()

    await db.returns.update_one(
        {"id": return_id},
        {"$set": update_set, "$push": {"customer_actions": action}},
    )

    # Email customer with the label + note
    cfg = await settings_service.get_settings(db)
    email_attachments = None
    if attachment:
        email_attachments = [{
            "filename": attachment["filename"],
            "content_type": attachment["content_type"],
            "content_base64": attachment["content_base64"],
        }]
    email_res = {"ok": False, "provider": None, "attempts": []}
    try:
        email_res = await email_service.send_free_label_approved(
            cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
            rma_number=doc["rma_number"], order_number=doc["order_number"],
            admin_note=note_clean, attachments=email_attachments,
        )
    except Exception as e:
        log.warning("approve-free email failed: %s", e)

    await db.returns.update_one(
        {"id": return_id},
        {"$push": {"email_log": {"$each": [{**a, "kind": "free_label_approved"}
                                           for a in email_res.get("attempts", [])]}}},
    )

    updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
    return {"status": update_set["status"], "email_sent": bool(email_res.get("ok")),
            "email_provider": email_res.get("provider"), "return": _strip_heavy(updated)}


@api.post("/admin/returns/{return_id}/reject")
async def reject_return(
    return_id: str,
    note: str = Form(""),
    evidence_file: Optional[UploadFile] = File(None),
    user=Depends(auth_svc.require_admin),
):
    """Reject a return. Admin can add a reason (note) and attach evidence
    (e.g. a photo). Both are emailed to the customer so they know why."""
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")

    attachment = await _read_upload_as_attachment(evidence_file)
    note_clean = (note or "").strip()

    update_set: Dict[str, Any] = {
        "status": "rejected",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if note_clean:
        update_set["admin_reject_note"] = note_clean
        update_set["admin_note"] = note_clean
    if attachment:
        update_set["admin_reject_attachment"] = attachment

    action = CustomerAction(
        kind="admin_rejected_return",
        label=("Admin rejected return"
               + (f" · reason: \"{note_clean}\"" if note_clean else "")
               + (" · evidence attached" if attachment else "")),
        meta={"has_attachment": bool(attachment),
              "attachment_filename": attachment["filename"] if attachment else None,
              "note_preview": note_clean[:200]},
    ).model_dump()

    await db.returns.update_one(
        {"id": return_id},
        {"$set": update_set, "$push": {"customer_actions": action}},
    )

    cfg = await settings_service.get_settings(db)
    email_attachments = None
    if attachment:
        email_attachments = [{
            "filename": attachment["filename"],
            "content_type": attachment["content_type"],
            "content_base64": attachment["content_base64"],
        }]
    email_res = {"ok": False, "provider": None, "attempts": []}
    try:
        email_res = await email_service.send_return_rejected(
            cfg, to_email=doc["email"], to_name=doc.get("customer_name") or "",
            rma_number=doc["rma_number"], order_number=doc["order_number"],
            admin_note=note_clean, attachments=email_attachments,
        )
    except Exception as e:
        log.warning("reject email failed: %s", e)

    await db.returns.update_one(
        {"id": return_id},
        {"$push": {"email_log": {"$each": [{**a, "kind": "return_rejected"}
                                           for a in email_res.get("attempts", [])]}}},
    )

    updated = await db.returns.find_one({"id": return_id}, {"_id": 0})
    return {"status": "rejected", "email_sent": bool(email_res.get("ok")),
            "email_provider": email_res.get("provider"), "return": _strip_heavy(updated)}


@api.get("/admin/returns/{return_id}/attachment/{kind}")
async def get_admin_attachment(return_id: str, kind: str, user=Depends(auth_svc.require_admin)):
    """Stream back an admin-uploaded attachment (label or rejection evidence)."""
    from fastapi.responses import Response
    if kind not in ("label", "reject"):
        raise HTTPException(status_code=400, detail="kind must be 'label' or 'reject'")
    field = "admin_label_attachment" if kind == "label" else "admin_reject_attachment"
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0, field: 1})
    att = (doc or {}).get(field)
    if not att or not att.get("content_base64"):
        raise HTTPException(status_code=404, detail="No attachment for this return.")
    data = base64.b64decode(att["content_base64"])
    return Response(
        content=data,
        media_type=att.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{att.get("filename","file")}"'},
    )

@api.get("/admin/returns/{return_id}/proof/{idx}")
async def get_customer_proof(return_id: str, idx: int, user=Depends(auth_svc.require_admin)):
    """Stream a customer-uploaded proof photo by index (0, 1, 2)."""
    from fastapi.responses import Response
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0, "customer_proof_photos": 1})
    photos = (doc or {}).get("customer_proof_photos") or []
    if idx < 0 or idx >= len(photos):
        raise HTTPException(status_code=404, detail="Proof photo not found.")
    p = photos[idx]
    if not p or not p.get("content_base64"):
        raise HTTPException(status_code=404, detail="Proof photo data missing.")
    data = base64.b64decode(p["content_base64"])
    return Response(
        content=data,
        media_type=p.get("content_type") or "image/jpeg",
        headers={"Content-Disposition": f'inline; filename="{p.get("filename","photo")}"'},
    )


@api.post("/admin/returns/{return_id}/archive")
async def archive_return(return_id: str, user=Depends(auth_svc.require_admin)):
    """Hide a return from the main dashboard without deleting it."""
    r = await db.returns.update_one(
        {"id": return_id},
        {"$set": {"archived": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Return not found")
    return {"archived": True}


@api.post("/admin/returns/{return_id}/unarchive")
async def unarchive_return(return_id: str, user=Depends(auth_svc.require_admin)):
    r = await db.returns.update_one(
        {"id": return_id},
        {"$set": {"archived": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Return not found")
    return {"archived": False}


@api.delete("/admin/returns/{return_id}")
async def delete_return(return_id: str, user=Depends(auth_svc.require_admin)):
    """Permanently delete a return request from the system."""
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    await db.returns.delete_one({"id": return_id})
    await db.payment_transactions.delete_many({"return_id": return_id})
    return {"deleted": True, "id": return_id}


@api.post("/admin/returns/{return_id}/mark-refunded")
async def mark_refunded(return_id: str, user=Depends(auth_svc.require_admin)):
    now = datetime.now(timezone.utc).isoformat()
    await db.returns.update_one(
        {"id": return_id},
        {"$set": {"refunded": True, "status": "refunded",
                  "closed": True, "closed_reason": "refunded", "closed_at": now,
                  "updated_at": now}}
    )
    # Best-effort customer notification if they subscribed to status updates.
    rma = (await db.returns.find_one(
        {"id": return_id}, {"_id": 0, "rma_number": 1}
    ) or {}).get("rma_number")
    if rma:
        await _notify_status_subscriber(rma, "refunded")
    return {"status": "refunded"}

@api.post("/admin/returns/{return_id}/notes")
async def add_admin_note(return_id: str, body: AdminNoteRequest, user=Depends(auth_svc.require_admin)):
    await db.returns.update_one(
        {"id": return_id}, 
        {"$set": {"admin_note": body.note, "updated_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"status": "note_added"}


@api.post("/admin/returns/{return_id}/internal-notes")
async def add_internal_note(return_id: str, body: InternalNoteRequest,
                            user=Depends(auth_svc.require_admin)):
    """Append a private internal note to a return. Notes are admin-only —
    never shown to the customer, never emailed. Each note is timestamped
    and tagged with the admin's email (from the JWT)."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text cannot be empty.")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Note is too long (max 5000 chars).")
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "author": (user or {}).get("sub") or "admin",
        "text": text,
    }
    r = await db.returns.update_one(
        {"id": return_id},
        {"$push": {"internal_notes": entry},
         "$set": {"updated_at": entry["at"]}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Return not found")
    return {"ok": True, "note": entry}


@api.post("/admin/returns/{return_id}/revoke-store-credit")
async def revoke_store_credit(return_id: str, user=Depends(auth_svc.require_admin)):
    """Revoke a previously-issued store-credit coupon (e.g. parcel arrived
    empty, damaged, or wrong item). Expires the coupon in WooCommerce so it
    can no longer be redeemed, flips the return status, logs the action, and
    best-effort emails the customer. Idempotent — safe to call twice."""
    doc = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    # Idempotent shortcut: if already revoked, just confirm — don't 400.
    if doc.get("store_credit_revoked"):
        return {"status": "store_credit_revoked", "already_revoked": True,
                "coupon_code": doc.get("coupon_code") or ""}
    if not doc.get("coupon_code"):
        raise HTTPException(
            status_code=400,
            detail="This return has no active store-credit coupon to revoke.",
        )
    if doc.get("status") != "store_credit_issued":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot revoke: return is in status '{doc.get('status')}'.",
        )

    cfg = await settings_service.get_settings(db)
    deactivated = await woo.deactivate_coupon(cfg, code=doc["coupon_code"])
    now = datetime.now(timezone.utc).isoformat()
    action = CustomerAction(
        kind="store_credit_revoked",
        label=f"Store credit revoked: {doc['coupon_code']} · "
              f"{'deactivated in WooCommerce' if deactivated else 'WooCommerce update failed — please expire manually'}",
        meta={"code": doc["coupon_code"], "deactivated_in_woo": bool(deactivated)},
    ).model_dump()
    await db.returns.update_one(
        {"id": return_id},
        {"$set": {
            "store_credit_revoked": True,
            "store_credit_revoked_at": now,
            "store_credit_revoked_in_woo": bool(deactivated),
            "status": "rejected",
            "closed": True,
            "closed_reason": "store_credit_revoked",
            "closed_at": now,
            "updated_at": now,
        },
         "$push": {"customer_actions": action}},
    )
    # Best-effort customer email — asks them to open a support ticket if
    # they think the revocation was a mistake. Non-blocking.
    try:
        await email_service.send_store_credit_revoked_to_customer(
            cfg,
            to_email=doc.get("email", ""),
            to_name=doc.get("customer_name") or (doc.get("return_address") or {}).get("name", ""),
            rma_number=doc.get("rma_number", ""),
            order_number=doc.get("order_number", ""),
            coupon_code=doc.get("coupon_code", ""),
            coupon_amount=float(doc.get("coupon_amount") or 0.0),
            currency=(doc.get("coupon_currency") or "GBP").upper(),
        )
    except Exception as e:
        log.warning("revoke-store-credit notification failed: %s", e)

    return {"status": "store_credit_revoked",
            "deactivated_in_woo": bool(deactivated),
            "coupon_code": doc["coupon_code"]}

# =============== APP WIRING ===============
app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    mongo_client.close()
