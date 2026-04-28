"""Pydantic models for the Return Portal."""
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime, timezone
import uuid


ReturnStatus = Literal[
    "draft",           # customer creating
    "awaiting_payment",
    "awaiting_approval",  # free label requested
    "awaiting_tracking",  # self-ship: ready to ship, customer needs to add tracking
    "approved",        # free label approved, needs label purchase
    "label_purchased",
    "in_transit",
    "delivered",
    "refunded",
    "store_credit_issued",  # coupon created & emailed to customer
    "rejected",
    "cancelled",
]

ReturnMethod = Literal["pay_stripe", "deduct_from_refund", "free_label", "store_credit", "self_ship"]

ReturnReason = Literal[
    # Seller-responsible (free label eligible)
    "defective",            # Defective or does not work properly
    "damaged",              # (legacy) damaged in transit
    "damaged_outer_box",    # Item and outer box both damaged
    "wrong_item",           # Wrong item sent
    "missing_parts",        # Missing parts or accessories
    # Buyer-responsible (paid)
    "no_longer_needed",     # No longer needed/wanted
    "accidental_order",     # Accidental order
    "better_price",         # Better price available
    "poor_performance",     # Performance or quality not adequate
    "incompatible",         # Incompatible or not useful for intended purpose
    # Legacy / catch-all
    "changed_mind",
    "size_issue",
    "other",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Address(BaseModel):
    name: str
    street1: str
    street2: Optional[str] = ""
    city: str
    state: str
    zip: str
    country: str = "US"
    phone: Optional[str] = ""
    email: Optional[str] = ""


class LineItem(BaseModel):
    id: str
    product_id: Optional[str] = None
    name: str
    sku: Optional[str] = ""
    quantity: int
    price: float
    image: Optional[str] = ""
    # Per-unit weight as reported by WooCommerce. Unit is whatever the store
    # is configured to use (kg, lbs, g, oz). None when the product has no
    # weight set — callers fall back to `default_item_weight_kg` setting.
    weight: Optional[float] = None
    weight_unit: Optional[str] = None


class ReturnItem(BaseModel):
    line_item_id: str
    name: str
    quantity: int
    price: float
    image: Optional[str] = ""
    reason: ReturnReason
    notes: Optional[str] = ""
    # Carried over from LineItem so rate calculation doesn't need to re-hit
    # WooCommerce at rate-fetch time.
    weight: Optional[float] = None
    weight_unit: Optional[str] = None
    # SKU — propagated from the WooCommerce line item so analytics can group
    # returns by product identity without re-hitting WooCommerce.
    sku: Optional[str] = ""
    product_id: Optional[str] = ""


class OrderLookupRequest(BaseModel):
    order_id: str
    email: EmailStr


class OrderResponse(BaseModel):
    order_id: str
    order_number: str
    email: str
    customer_name: str
    billing_address: Optional[Address] = None
    shipping_address: Optional[Address] = None
    line_items: List[LineItem]
    total: float
    currency: str = "USD"
    date_created: Optional[str] = None
    status: Optional[str] = None


class CreateReturnRequest(BaseModel):
    order_id: str
    email: EmailStr
    items: List[ReturnItem]
    method: ReturnMethod
    customer_note: Optional[str] = ""
    return_address: Address  # customer's "from" address for the label
    # When method=store_credit AND the return contains a manual-review reason,
    # the customer must also pick how the parcel will be shipped (free label
    # OR self-ship). Both paths are free for the customer; this field tells
    # the admin which approval action to surface and which email to send.
    # Ignored for any other combination.
    restricted_shipping_choice: Optional[Literal["free_label", "self_ship"]] = None


class CustomerAction(BaseModel):
    """Records a user interaction in the return flow for admin visibility."""
    at: str = Field(default_factory=now_iso)
    kind: str  # e.g. "method_selected", "rate_selected", "address_confirmed", "return_created"
    label: str  # human-readable ("Selected: Deduct from refund")
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReturnRequestDoc(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    rma_number: str
    order_id: str
    order_number: str
    email: str
    customer_name: str
    items: List[ReturnItem]
    method: ReturnMethod
    method_display_label: Optional[str] = ""
    status: ReturnStatus = "draft"
    customer_note: Optional[str] = ""
    admin_note: Optional[str] = ""
    return_address: Address
    warehouse_address: Address
    # Shippo
    shippo_shipment_id: Optional[str] = None
    shippo_rate_id: Optional[str] = None
    shippo_transaction_id: Optional[str] = None
    selected_rate: Optional[Dict[str, Any]] = None
    label_url: Optional[str] = None
    # Carrier-provided QR code (Shippo / Royal Mail returns label) so the
    # customer can scan at drop-off without printing.
    label_qr_url: Optional[str] = None
    tracking_number: Optional[str] = None
    tracking_carrier: Optional[str] = None
    tracking_status: Optional[str] = None
    tracking_updates: List[Dict[str, Any]] = Field(default_factory=list)
    # Stripe
    stripe_session_id: Optional[str] = None
    label_cost: float = 0.0
    refund_amount: float = 0.0
    refund_deduction: float = 0.0
    refund_net: float = 0.0
    paid: bool = False
    refunded: bool = False
    # Store credit / WooCommerce coupon (issued instead of cash refund)
    coupon_code: Optional[str] = None
    coupon_amount: Optional[float] = None
    coupon_currency: Optional[str] = None
    coupon_expires_at: Optional[str] = None
    store_credit_bonus_percent_applied: Optional[float] = None
    store_credit_issued_at: Optional[str] = None
    # Customer interaction log (what the customer pressed on)
    customer_actions: List[CustomerAction] = Field(default_factory=list)
    # Customer-uploaded proof photos (up to 3, ≤2MB each)
    customer_proof_photos: List[Dict[str, Any]] = Field(default_factory=list)
    # When method=store_credit + manual-review reason, this records whether
    # the customer chose a free label or self-ship for the physical return.
    # Drives admin's approval-step labelling and which email is sent.
    restricted_shipping_choice: Optional[Literal["free_label", "self_ship"]] = None
    # Private notes the admin can attach to a return — never shown to the
    # customer, never emailed. Each entry: {at, author, text}.
    internal_notes: List[Dict[str, Any]] = Field(default_factory=list)
    # Label cost subtracted from a store-credit coupon when the customer
    # chose "deduct from store credit". Stored separately from `refund_deduction`
    # so the audit trail is clear (refund vs. coupon adjustment).
    coupon_label_deduction: float = 0.0
    # Admin moderation
    archived: bool = False
    # Return lifecycle
    closed: bool = False
    closed_reason: Optional[str] = None  # e.g. "store_credit_applied", "refunded"
    closed_at: Optional[str] = None
    # Email delivery tracking
    email_provider_used: Optional[str] = None
    email_log: List[Dict[str, Any]] = Field(default_factory=list)
    emails_finalized: bool = False
    # Self-ship (customer uses their own carrier) — populated when the
    # customer picks the "Send with my own carrier" method.
    self_ship_carrier: Optional[str] = None         # e.g. "Royal Mail" / "Evri" / "Other"
    self_ship_carrier_other: Optional[str] = None   # free-text when carrier == "Other"
    self_ship_tracking_number: Optional[str] = None
    self_ship_is_tracked: Optional[bool] = None     # False = customer chose untracked / drop-in-postbox
    self_ship_submitted_at: Optional[str] = None
    self_ship_reminder_count: int = 0
    self_ship_last_reminder_at: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class RateResponse(BaseModel):
    rate_id: str
    provider: str
    servicelevel: str
    amount: float
    currency: str
    days: Optional[int] = None
    duration_terms: Optional[str] = ""
    provider_image: Optional[str] = ""


class CheckoutRequest(BaseModel):
    rate_id: str
    origin_url: str
    # Optional resolver hints — used when the preview rate_id no longer exists
    # on a newly-created shipment (e.g. rates were fetched with postcode-only
    # address, then the customer filled in the full address at confirm).
    provider: Optional[str] = None
    servicelevel: Optional[str] = None
    amount: Optional[float] = None


class CheckoutResponse(BaseModel):
    url: str
    session_id: str


class PaymentStatusResponse(BaseModel):
    status: str
    payment_status: str
    return_id: str
    rma_number: str


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminLoginResponse(BaseModel):
    token: str
    email: str


class AdminNoteRequest(BaseModel):
    note: str


class InternalNoteRequest(BaseModel):
    """Admin-only private note attached to a return. Author is taken from
    the JWT (admin email)."""
    text: str


class CustomerActionRequest(BaseModel):
    kind: str
    label: str
    meta: Optional[Dict[str, Any]] = None


class RatePreviewRequest(BaseModel):
    zip: str
    country: str = "US"
    state: Optional[str] = None
    city: Optional[str] = None


class TrackingResponse(BaseModel):
    rma_number: str
    tracking_number: Optional[str] = None
    tracking_carrier: Optional[str] = None
    tracking_status: Optional[str] = None
    status: ReturnStatus
    updates: List[Dict[str, Any]] = []
    label_url: Optional[str] = None
    items: List[ReturnItem] = []
    created_at: str
    updated_at: str
    # Extra fields needed by the public /track page so customers can add
    # self-ship tracking without leaving the page.
    id: Optional[str] = None
    order_number: Optional[str] = None
    email: Optional[str] = None
    method: Optional[str] = None
    warehouse_address: Optional[Address] = None
    self_ship_carrier: Optional[str] = None
    self_ship_tracking_number: Optional[str] = None
    self_ship_is_tracked: Optional[bool] = None
    self_ship_submitted_at: Optional[str] = None
    # Smart-tracking extras (computed at request time, not stored on the doc)
    eta_min_days: Optional[int] = None
    eta_max_days: Optional[int] = None
    eta_label: Optional[str] = None        # e.g. "Usually arrives in 2–3 business days"
    eta_source: Optional[str] = None       # "carrier_average" | "default" | "delivered"
    notify_status_email: Optional[bool] = None
    notify_status_email_address: Optional[str] = None


class SubscribeStatusRequest(BaseModel):
    """Customer toggle for "email me on status change" on the tracking page."""
    enabled: bool
    email: Optional[str] = None


class SelfShipTrackingRequest(BaseModel):
    """Customer-submitted tracking info for a self-ship return."""
    carrier: str  # one of: Royal Mail / Evri / DPD / UPS / FedEx / Other
    carrier_other: Optional[str] = ""  # free text when carrier == "Other"
    tracking_number: Optional[str] = ""
    is_tracked: bool = True  # set False if customer used an untracked service
