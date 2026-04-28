"""Shippo wrapper. API key is injected per-call so admin can change it at runtime."""
from typing import List, Optional, Dict, Any
import shippo
from shippo.models import components

from models import Address, RateResponse


def _client(api_key: str):
    return shippo.Shippo(api_key_header=api_key)


def _addr_payload(a: Address) -> dict:
    return {
        "name": a.name, "street1": a.street1, "street2": a.street2 or "",
        "city": a.city, "state": a.state, "zip": a.zip,
        "country": a.country or "US", "phone": a.phone or "", "email": a.email or "",
    }


def create_shipment(api_key: str, address_from: Address, address_to: Address,
                    parcel: Dict[str, Any]) -> Dict[str, Any]:
    c = _client(api_key)
    # Our server passes weight in kg (with weight_unit="kg"). Shippo accepts
    # either — we send as-is when the unit is provided and recognised, else
    # fall back to the historical LB default. This keeps US-carrier pricing
    # sensible (grams would give laughably low "rate" estimates).
    raw_weight = parcel.get("weight", "1")
    raw_unit = (parcel.get("weight_unit") or "").strip().lower()
    if raw_unit in ("kg", "kgs", "kilogram", "kilograms"):
        mass_unit = components.WeightUnitEnum.KG
        weight_str = str(raw_weight)
    elif raw_unit in ("g", "gram", "grams"):
        mass_unit = components.WeightUnitEnum.G
        weight_str = str(raw_weight)
    elif raw_unit in ("oz", "ounce", "ounces"):
        mass_unit = components.WeightUnitEnum.OZ
        weight_str = str(raw_weight)
    else:
        mass_unit = components.WeightUnitEnum.LB
        weight_str = str(raw_weight)

    req = components.ShipmentCreateRequest(
        address_from=components.AddressCreateRequest(**_addr_payload(address_from)),
        address_to=components.AddressCreateRequest(**_addr_payload(address_to)),
        parcels=[components.ParcelCreateRequest(
            length=str(parcel.get("length", "10")),
            width=str(parcel.get("width", "8")),
            height=str(parcel.get("height", "4")),
            distance_unit=components.DistanceUnitEnum.IN,
            weight=weight_str,
            mass_unit=mass_unit,
        )],
        async_=False,
    )
    shipment = c.shipments.create(req)

    rates_out: List[RateResponse] = []
    for rate in shipment.rates or []:
        rates_out.append(RateResponse(
            rate_id=rate.object_id,
            provider=rate.provider or "",
            servicelevel=(rate.servicelevel.name if rate.servicelevel else "") or "",
            amount=float(rate.amount or 0),
            currency=rate.currency or "USD",
            days=rate.estimated_days,
            duration_terms=rate.duration_terms or "",
            provider_image=rate.provider_image_75 or "",
        ))
    rates_out.sort(key=lambda r: r.amount)
    return {"shipment_id": shipment.object_id, "rates": [r.model_dump() for r in rates_out]}


def purchase_label(api_key: str, rate_id: str) -> Dict[str, Any]:
    c = _client(api_key)
    req = components.TransactionCreateRequest(
        rate=rate_id,
        label_file_type=components.LabelFileTypeEnum.PDF,
        async_=False,
    )
    tx = c.transactions.create(req)
    # Shippo returns an optional `qr_code_url` on carriers that support
    # scan-at-counter (e.g. USPS, UPS). It's None for carriers that don't.
    qr_url = getattr(tx, "qr_code_url", None) or None
    return {
        "transaction_id": tx.object_id,
        "status": tx.status,
        "label_url": tx.label_url,
        "qr_code_url": qr_url,
        "tracking_number": tx.tracking_number,
        "tracking_url": tx.tracking_url_provider,
        "messages": [m.text for m in (tx.messages or [])] if tx.messages else [],
    }


def track(api_key: str, carrier: str, tracking_number: str) -> Optional[Dict[str, Any]]:
    c = _client(api_key)
    try:
        res = c.tracking_status.get(tracking_number=tracking_number, carrier=carrier)
    except Exception:
        return None
    if not res:
        return None
    status = res.tracking_status.status if res.tracking_status else None
    history = []
    for h in (res.tracking_history or []):
        history.append({
            "status": h.status,
            "status_details": h.status_details,
            "status_date": h.status_date,
            "location": (h.location.city if h.location else None),
        })
    return {
        "status": status,
        "status_details": res.tracking_status.status_details if res.tracking_status else None,
        "eta": res.eta,
        "history": history,
    }
