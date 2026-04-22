"""Main FastAPI server for the PGE Return Portal - Render Compatible."""
import os
import logging
import random
import string
import stripe
import json
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

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
    AdminLoginRequest, AdminLoginResponse, AdminNoteRequest,
    TrackingResponse, Address, CustomerAction, CustomerActionRequest,
    RatePreviewRequest,
)
import auth as auth_svc
import woo
import shippo_service
import easyship_service
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
    }

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
    "draft", "awaiting_payment", "awaiting_approval", "approved",
    "label_purchased", "in_transit", "delivered", "refunded",
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
    effective_method = body.method
    if any((i.reason in MANUAL_REVIEW_REASONS) for i in body.items):
        # Manual review path. We respect the customer's choice of "store_credit"
        # but still gate issuance behind admin approval (same gate as free_label).
        if effective_method != "store_credit":
            effective_method = "free_label"
    method_label = METHOD_DISPLAY.get(effective_method, effective_method)

    status = "draft"
    if effective_method == "free_label":
        status = "awaiting_approval"
    elif effective_method == "store_credit":
        # Any manual-review reason still forces admin approval first; other
        # reasons issue the coupon immediately after the record is inserted.
        if any((i.reason in MANUAL_REVIEW_REASONS) for i in body.items):
            status = "awaiting_approval"
        else:
            status = "awaiting_approval"  # temporary — flipped to store_credit_issued below
    elif effective_method in ("pay_stripe", "deduct_from_refund"):
        status = "awaiting_payment"

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
    ).model_dump()

    await db.returns.insert_one(dict(doc))
    doc.pop("_id", None)

    # Auto-issue coupon for store_credit returns that do NOT need manual
    # review. Manual-review store_credit returns wait for admin approval and
    # the coupon is issued from the approve endpoint instead.
    if effective_method == "store_credit" and not any(
        i.reason in MANUAL_REVIEW_REASONS for i in body.items
    ):
        try:
            updated = await _issue_store_credit_for_return(doc["id"])
            if updated:
                doc = updated
        except Exception as e:
            log.warning("auto store-credit issuance failed: %s", e)

    # Emails are NOT sent here anymore — they all fire from the success page
    # via POST /returns/{id}/finalize so the customer receives one clean batch
    # once they see their RMA.
    return doc


@api.get("/returns/existing-items/{order_number}/{email}")
async def existing_return_items(order_number: str, email: str):
    """Returns list of line_item_ids already in an active return for this order."""
    ids = await _already_returned_line_ids(db, order_number, email.lower())
    return {"order_number": order_number, "email": email.lower(),
            "line_item_ids": sorted(list(ids))}


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

    Merges rates from every configured provider (Shippo, Easyship, Royal
    Mail Click & Drop). Providers that fail or have no keys are silently
    skipped — the customer never sees an error.
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

    if cfg.get("easyship_api_key"):
        try:
            r = easyship_service.create_shipment(
                api_key=cfg["easyship_api_key"],
                address_from=placeholder_from, address_to=warehouse, parcel=parcel,
                box_slug=cfg.get("easyship_box_slug") or "default",
            )
            merged.extend(r.get("rates") or [])
        except Exception as e:
            log.warning("preview easyship error: %s", e)

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

    # 1. Customer confirmation
    if not skip_customer_confirmation:
        try:
            res = await email_service.send_return_initiated(
                cfg, to_email=doc["email"], to_name=doc["customer_name"],
                rma_number=doc["rma_number"], order_number=doc["order_number"],
                method_display_label=method_label,
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
    provider: shippo rates already use Shippo's opaque ids, Easyship rates
    use "es_*", Royal Mail rates use "rm_*"."""
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

    # 2) Easyship
    if cfg.get("easyship_api_key"):
        try:
            r = easyship_service.create_shipment(
                api_key=cfg["easyship_api_key"],
                address_from=addr_from, address_to=addr_to, parcel=parcel,
                box_slug=cfg.get("easyship_box_slug") or "default",
            )
            merged.extend(r.get("rates") or [])
        except Exception as e:
            log.warning("easyship rate error: %s", e)

    # 3) Royal Mail Click & Drop
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
    if rate_id.startswith("es_"):
        return "easyship"
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
        if provider == "easyship":
            res = easyship_service.purchase_label(
                api_key=cfg.get("easyship_api_key") or "",
                rate_id=rid, address_from=address_from, address_to=address_to,
                parcel=parcel, reference=reference,
                shipment_id=selected.get("_es_shipment_id"),
                box_slug=cfg.get("easyship_box_slug") or "default",
            )
        elif provider == "royal_mail":
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
    if doc["method"] != "pay_stripe":
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

# ---- Tracking & Admin Endpoints ----
@api.get("/tracking/{identifier}", response_model=TrackingResponse)
async def track_return(identifier: str):
    doc = await db.returns.find_one(
        {"$or": [{"rma_number": identifier.upper()}, {"tracking_number": identifier}]},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="No return found.")
    return TrackingResponse(**doc)

@api.post("/auth/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest):
    if not auth_svc.verify_admin(body.email, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = auth_svc.create_token(body.email.lower())
    return AdminLoginResponse(token=token, email=body.email.lower())


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

    base_refund = float(doc.get("refund_amount") or 0.0)
    if base_refund <= 0:
        log.warning("refund_amount is zero for %s, cannot issue store credit", return_id)
        return None

    coupon_amount = round(base_refund * (1.0 + bonus_pct / 100.0), 2)
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
                    f"(base £{base_refund:.2f} + {bonus_pct:g}% bonus)",
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

    # Branch: if this return was requested as "store_credit", admin approval
    # issues the coupon instead of emailing a label.
    if doc.get("method") == "store_credit":
        note_clean = (note or "").strip()
        if note_clean:
            await db.returns.update_one(
                {"id": return_id},
                {"$set": {"admin_approve_note": note_clean, "admin_note": note_clean}},
            )
        updated = await _issue_store_credit_for_return(return_id)
        if not updated:
            raise HTTPException(status_code=502, detail="Store credit could not be issued.")
        return {"status": "store_credit_issued", "email_sent": True,
                "email_provider": None, "return": _strip_heavy(updated)}

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
    return {"status": "refunded"}

@api.post("/admin/returns/{return_id}/notes")
async def add_admin_note(return_id: str, body: AdminNoteRequest, user=Depends(auth_svc.require_admin)):
    await db.returns.update_one(
        {"id": return_id}, 
        {"$set": {"admin_note": body.note, "updated_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"status": "note_added"}

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
