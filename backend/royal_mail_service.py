"""Royal Mail Click & Drop API shipping rate + label provider (fallback).

Matches the interface of shippo_service so server.py can call:
    create_shipment(api_key, address_from, address_to, parcel) -> {shipment_id, rates}
    purchase_label(api_key, rate_id, ...) -> {status, transaction_id, label_url, tracking_number}

Public API:
  - GET  /api/v1/version          — used for key-validity check
  - POST /api/v1/orders           — create order (label booking)
  - POST /api/v1/orders/{id}/label— retrieve label PDF

The Click & Drop API does NOT currently expose a public "get rates" endpoint,
so we return a static rate-card for popular UK domestic services. These prices
are intentionally sensible defaults; the merchant can override them later in
settings if required. Any failure returns empty / FAILED silently.
"""
from __future__ import annotations
import logging
import base64
from typing import Dict, List, Optional

import requests

log = logging.getLogger("royal_mail")

BASE = "https://api.parcel.royalmail.com/api/v1"
TIMEOUT = 10

# Static UK domestic rate card (in GBP) used when merchant hasn't overridden.
# These are rough 2026 consumer prices for a small parcel (up to 1 kg). They
# give the customer a realistic option to pick; the actual label is priced by
# Royal Mail at dispatch time via the merchant's OBA / Click & Drop account.
_RATE_CARD: List[Dict] = [
    {"code": "TPN24", "name": "Royal Mail Tracked 24",
     "amount": 4.79, "days": 1, "duration_terms": "Next working day (tracked)"},
    {"code": "TPN48", "name": "Royal Mail Tracked 48",
     "amount": 3.39, "days": 2, "duration_terms": "2–3 working days (tracked)"},
    {"code": "SD1",   "name": "Special Delivery Guaranteed by 1pm",
     "amount": 7.95, "days": 1, "duration_terms": "By 1pm next working day (signed)"},
]


def _valid(api_key: str) -> bool:
    """Quick key-presence check. We don't hit /version on every rate lookup
    to keep latency down; we'll catch auth failures during label booking."""
    return bool(api_key and str(api_key).strip())


def create_shipment(api_key: str, address_from, address_to, parcel: Dict) -> Dict:
    """Return a Shippo-compatible rates envelope from the static rate card.
    Only UK destinations get rates here (Click & Drop is UK-based).

    We also cap the parcel at 3 kg — above that, Royal Mail Tracked options
    stop being price-competitive and the merchant prefers to route heavier
    parcels through other carriers (Shippo/Shiptheory)."""
    if not _valid(api_key):
        return {"shipment_id": None, "rates": []}

    def _get(a, k):
        if a is None:
            return ""
        return a.get(k, "") if isinstance(a, dict) else (getattr(a, k, "") or "")

    country = (_get(address_from, "country") or "GB").upper()
    if country not in ("GB", "UK"):
        return {"shipment_id": None, "rates": []}

    # Parcel weight is always kg (see server._parcel_from_doc). Hide Royal Mail
    # rates for anything over 3 kg so the customer is routed to a carrier that
    # actually handles heavier parcels.
    try:
        parcel_kg = float((parcel or {}).get("weight") or 0.0)
        if ((parcel or {}).get("weight_unit") or "kg").lower() != "kg":
            # Defensive: if ever called with lbs/g, convert to kg for the cap.
            unit = ((parcel or {}).get("weight_unit") or "").lower()
            if unit in ("g", "gram", "grams"):
                parcel_kg = parcel_kg / 1000.0
            elif unit in ("lb", "lbs", "pound", "pounds"):
                parcel_kg = parcel_kg * 0.45359237
            elif unit in ("oz", "ounce", "ounces"):
                parcel_kg = parcel_kg * 0.0283495231
    except (TypeError, ValueError):
        parcel_kg = 0.0
    if parcel_kg > 3.0:
        log.info("royal_mail: skipping — parcel %skg > 3kg cap", parcel_kg)
        return {"shipment_id": None, "rates": []}

    rates = []
    for row in _RATE_CARD:
        rates.append({
            "rate_id": f"rm_{row['code']}",
            "provider": "Royal Mail",
            "servicelevel": row["name"],
            "amount": row["amount"],
            "currency": "GBP",
            "days": row["days"],
            "duration_terms": row["duration_terms"],
            "provider_image": "https://www.royalmail.com/sites/default/themes/custom/royalmail/logo.svg",
        })
    return {"shipment_id": None, "rates": rates}


def purchase_label(api_key: str, rate_id: str,
                   *, address_from=None, address_to=None, parcel: Optional[Dict] = None,
                   reference: Optional[str] = None) -> Dict:
    """Book a Click & Drop order and retrieve the label PDF. rate_id is
    expected to start with "rm_" followed by the service code."""
    if not _valid(api_key) or not rate_id or not rate_id.startswith("rm_"):
        return {"status": "FAILED", "transaction_id": None,
                "label_url": None, "tracking_number": None}

    service_code = rate_id.split("_", 1)[1]

    def _get(a, k):
        if a is None:
            return ""
        return a.get(k, "") if isinstance(a, dict) else (getattr(a, k, "") or "")

    def _addr(a) -> Dict:
        name = _get(a, "name") or ""
        return {
            "fullName": name,
            "companyName": "",
            "addressLine1": _get(a, "street1"),
            "addressLine2": _get(a, "street2"),
            "city": _get(a, "city"),
            "county": _get(a, "state"),
            "postcode": _get(a, "zip"),
            "countryCode": (_get(a, "country") or "GB").upper(),
            "phoneNumber": _get(a, "phone"),
            "emailAddress": _get(a, "email"),
        }

    weight_g = int(float((parcel or {}).get("weight") or 1) * 1000)
    payload = {
        "orders": [{
            "orderReference": (reference or "RMA")[:40],
            "recipient": {"address": _addr(address_to)},
            "sender": {"address": _addr(address_from)},
            "packages": [{
                "weightInGrams": weight_g,
                "packageFormatIdentifier": "smallParcel",
            }],
            "shipmentInformation": {
                "shippingServiceCode": service_code,
                "contentsDescription": "Return",
                "trackingServiceCode": service_code,
            },
        }]
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        r = requests.post(f"{BASE}/orders", json=payload, headers=headers, timeout=20)
        if r.status_code not in (200, 201):
            log.warning("royal_mail order failed: %s %s", r.status_code, r.text[:200])
            return {"status": "FAILED", "transaction_id": None,
                    "label_url": None, "tracking_number": None}
        data = r.json() or {}
        orders = data.get("createdOrders") or data.get("orders") or []
        if not orders:
            return {"status": "FAILED", "transaction_id": None,
                    "label_url": None, "tracking_number": None}
        order = orders[0]
        order_id = order.get("orderIdentifier") or order.get("orderId")
        tracking = order.get("trackingNumber") or ""

        # Fetch label PDF
        label_url = None
        if order_id:
            try:
                lr = requests.get(
                    f"{BASE}/orders/{order_id}/label",
                    params={"documentType": "postageLabel"},
                    headers=headers, timeout=15,
                )
                if lr.status_code == 200 and lr.content:
                    b64 = base64.b64encode(lr.content).decode("ascii")
                    label_url = f"data:application/pdf;base64,{b64}"
            except Exception as e:
                log.warning("royal_mail label fetch exception: %s", e)

        # Additionally fetch the returns / QR label so customers can scan at
        # a Post Office drop-off without printing. Royal Mail C&D exposes it
        # via documentType=returnsLabel (PDF containing the QR). We store it
        # as a data-URL in the same shape as label_url so the frontend can
        # render it in an <img>/<iframe> without extra plumbing.
        qr_url = None
        if order_id:
            try:
                qr = requests.get(
                    f"{BASE}/orders/{order_id}/label",
                    params={"documentType": "returnsLabel"},
                    headers=headers, timeout=15,
                )
                if qr.status_code == 200 and qr.content:
                    b64 = base64.b64encode(qr.content).decode("ascii")
                    qr_url = f"data:application/pdf;base64,{b64}"
            except Exception as e:
                log.warning("royal_mail returns-label fetch exception: %s", e)

        return {
            "status": "SUCCESS",
            "transaction_id": str(order_id) if order_id else None,
            "label_url": label_url,
            "qr_code_url": qr_url,
            "tracking_number": tracking,
        }
    except Exception as e:
        log.warning("royal_mail order exception: %s", e)
        return {"status": "FAILED", "transaction_id": None,
                "label_url": None, "tracking_number": None}
