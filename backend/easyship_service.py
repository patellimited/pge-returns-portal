"""Easyship shipping rate + label provider.

Uses the NEW Easyship Advanced API hosted at https://api.easyship.com.
(The legacy public-api.easyship.com/2023-01 endpoint authenticates but no
longer returns rates for API keys issued from the new "Advanced API"
dashboard — that's why rates silently come back empty.)

Interface matches shippo_service so server.py can call:
    create_shipment(api_key, address_from, address_to, parcel) -> {shipment_id, rates}
    purchase_label(api_key, rate_id, ...)                      -> {status, transaction_id, label_url, tracking_number}

Flow:
  - Rates:   POST https://api.easyship.com/rates      (Advanced API scope: Rates)
  - Label:   POST https://api.easyship.com/shipments  (synchronous buy via
             shipping_settings.buy_label + courier_selection.selected_courier_id)

Auth: `Authorization: Bearer {api_key}` — token from Easyship dashboard
      → Connect → API Integrations → Access Token (production token prefixed `prod_`).

Any failure (missing key, network, auth, malformed response) returns empty
rates / a FAILED status so the customer never sees a raw error; the admin
alert email (sent by server.py) surfaces the real reason.
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional

import requests

log = logging.getLogger("easyship")

BASE = "https://api.easyship.com"
PING_BASE = "https://public-api.easyship.com/2023-01"  # /account still lives here
TIMEOUT = 20


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _addr(a) -> Dict[str, str]:
    """Accept dict or pydantic-style obj with Address fields."""
    get = (lambda k: a.get(k, "") or "") if isinstance(a, dict) else (lambda k: getattr(a, k, "") or "")
    # Normalise common country aliases to the 2-letter ISO code Easyship requires.
    raw_country = (get("country") or "GB").strip().upper()
    country = {
        "UK": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB",
        "USA": "US", "UNITED STATES": "US",
    }.get(raw_country, raw_country[:2])
    name = (get("name") or "").strip()
    first, _, last = name.partition(" ")
    return {
        "contact_name": name or "Customer",
        "contact_first_name": first or name or "Customer",
        "contact_last_name": last or "-",
        "company_name": "",
        "contact_email": get("email") or "noreply@example.com",
        "contact_phone": get("phone") or "0000000000",
        "line_1": get("street1") or "-",
        "line_2": get("street2") or "",
        "city": get("city") or "-",
        "state": get("state") or "",
        "postal_code": get("zip") or "",
        "country_alpha2": country,
    }


def _items_and_weight(parcel: Dict) -> (List[Dict], int):
    """Build items[] + total weight in GRAMS (integer) — new API requirement."""
    weight_kg = float((parcel or {}).get("weight") or 1.0)
    weight_g = max(int(round(weight_kg * 1000.0)), 50)   # 50g floor so couriers quote
    items = [{
        "quantity": 1,
        "hs_code": "",
        "category": "others",
        "declared_currency": "GBP",
        "declared_customs_value": 1,
        "description": "Customer return",
        "weight": weight_g,
        "weight_unit": "g",
        "dimension_unit": "cm",
        "height": float((parcel or {}).get("height") or 4),
        "width": float((parcel or {}).get("width") or 8),
        "length": float((parcel or {}).get("length") or 10),
    }]
    return items, weight_g


def _box(parcel: Dict, box_slug: str) -> Dict:
    return {
        "slug": box_slug or "default",
        "length": float((parcel or {}).get("length") or 10),
        "width": float((parcel or {}).get("width") or 8),
        "height": float((parcel or {}).get("height") or 4),
        "outer_dimensions_unit": "cm",
    }


def _map_rate(rt: Dict, shipment_id: Optional[str] = None) -> Optional[Dict]:
    """Normalise an Easyship rate into the portal's rate shape."""
    cost = rt.get("total_charge") or rt.get("shipment_charge_total") \
        or rt.get("total_charge_with_markup") or rt.get("total")
    if cost is None:
        return None
    courier_id = rt.get("courier_id") or rt.get("id") or ""
    if not courier_id:
        return None
    return {
        "rate_id": f"es_{courier_id}",
        "provider": rt.get("courier_display_name") or rt.get("courier_name") or "Easyship",
        "servicelevel": rt.get("courier_name") or rt.get("service_name") or "",
        "amount": float(cost),
        "currency": (rt.get("currency") or "GBP").upper(),
        "days": int(rt.get("max_delivery_time") or rt.get("min_delivery_time") or 0) or None,
        "duration_terms": rt.get("description") or "",
        "provider_image": rt.get("courier_logo_url") or "",
        "_es_shipment_id": shipment_id,
    }


def create_shipment(api_key: str, address_from, address_to, parcel: Dict,
                    box_slug: str = "default") -> Dict:
    """Fetch rates via the new Advanced API `/rates` endpoint and return them
    in the Shippo-compatible envelope the portal expects.

    Note: the new API separates rate quoting from shipment creation — no
    shipment is actually created here, so `shipment_id` is None. The label
    purchase step creates the shipment + buys the label in one call.
    """
    if not api_key:
        return {"shipment_id": None, "rates": []}

    items, total_g = _items_and_weight(parcel or {})
    payload = {
        "origin_address": _addr(address_from),
        "destination_address": _addr(address_to),
        "parcels": [{
            "box": _box(parcel or {}, box_slug),
            "items": items,
            "total_actual_weight": total_g,
        }],
        "incoterms": "DDU",
        "insurance": {"is_insured": False},
        "output_currency": "GBP",
    }

    try:
        r = requests.post(f"{BASE}/rates", json=payload,
                          headers=_headers(api_key), timeout=TIMEOUT)
        if r.status_code not in (200, 201):
            log.warning("easyship /rates failed: %s %s",
                        r.status_code, r.text[:300])
            return {"shipment_id": None, "rates": []}
        data = r.json() or {}
        rates_raw = data.get("rates") or data.get("available_rates") or []
        rates: List[Dict] = []
        for rt in rates_raw:
            m = _map_rate(rt)
            if m:
                rates.append(m)
        if not rates:
            log.warning("easyship /rates returned 200 but 0 rates (check couriers "
                        "in dashboard, box slug, address): %s", r.text[:300])
        return {"shipment_id": None, "rates": rates}
    except Exception as e:
        log.warning("easyship /rates exception: %s", e)
        return {"shipment_id": None, "rates": []}


def purchase_label(api_key: str, rate_id: str,
                   *, address_from=None, address_to=None,
                   parcel: Optional[Dict] = None,
                   reference: Optional[str] = None,
                   shipment_id: Optional[str] = None,
                   box_slug: str = "default") -> Dict:
    """Create a shipment + buy the label in one synchronous call via the new
    Advanced API `/shipments` endpoint (shipping_settings.buy_label = true).

    rate_id carries the Easyship courier id prefixed with "es_" (stripped
    back to the raw id before posting).
    """
    if not api_key:
        return {"status": "FAILED", "transaction_id": None,
                "label_url": None, "tracking_number": None,
                "message": "Easyship key not configured"}

    courier_id = rate_id[3:] if rate_id.startswith("es_") else rate_id
    items, total_g = _items_and_weight(parcel or {})
    payload = {
        "origin_address": _addr(address_from),
        "destination_address": _addr(address_to),
        "parcels": [{
            "box": _box(parcel or {}, box_slug),
            "items": items,
            "total_actual_weight": total_g,
        }],
        "incoterms": "DDU",
        "insurance": {"is_insured": False},
        "output_currency": "GBP",
        "platform_name": "pge-returns-portal",
        "platform_order_number": reference or "",
        "courier_selection": {
            "selected_courier_id": courier_id,
            "apply_shipping_rules": False,
        },
        "shipping_settings": {
            "units": {"weight": "g", "dimensions": "cm"},
            "buy_label": True,
            "buy_label_synchronous": True,
            "printing_options": {"format": "pdf", "label": "4x6", "commercial_invoice": "A4"},
        },
    }

    try:
        r = requests.post(f"{BASE}/shipments", json=payload,
                          headers=_headers(api_key), timeout=45)
        if r.status_code not in (200, 201, 202):
            log.warning("easyship /shipments buy failed: %s %s",
                        r.status_code, r.text[:300])
            return {"status": "FAILED", "transaction_id": None,
                    "label_url": None, "tracking_number": None,
                    "message": f"{r.status_code}: {r.text[:200]}"}
        data = r.json() or {}
        shipment = data.get("shipment") or data
        label = shipment.get("label") or {}
        label_url = (label.get("url") or label.get("base64_encoded_strings")
                     or shipment.get("label_url"))
        tracking = (shipment.get("tracking_number")
                    or shipment.get("tracking_page_url") or "")
        tx_id = (shipment.get("easyship_shipment_id")
                 or shipment.get("id") or shipment_id)
        if not label_url:
            warning = shipment.get("warning_message") or shipment.get("error") \
                or "Label URL missing in Easyship response"
            return {"status": "FAILED", "transaction_id": tx_id,
                    "label_url": None, "tracking_number": tracking,
                    "message": str(warning)[:300]}
        return {
            "status": "SUCCESS",
            "transaction_id": tx_id,
            "label_url": label_url,
            "tracking_number": tracking,
        }
    except Exception as e:
        log.warning("easyship label exception: %s", e)
        return {"status": "FAILED", "transaction_id": None,
                "label_url": None, "tracking_number": None,
                "message": f"easyship: {e}"}


def ping(api_key: str) -> Dict:
    """Lightweight connectivity check. /account still lives on the legacy host."""
    if not api_key:
        return {"ok": False, "message": "No key set"}
    try:
        r = requests.get(f"{PING_BASE}/account", headers=_headers(api_key), timeout=10)
        if r.status_code == 200:
            data = r.json() or {}
            email = (data.get("account") or data).get("email") or "ok"
            return {"ok": True, "message": f"Connected · {email}"}
        if r.status_code == 401:
            return {"ok": False, "message": "Unauthorized — check API key"}
        return {"ok": False, "message": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}
