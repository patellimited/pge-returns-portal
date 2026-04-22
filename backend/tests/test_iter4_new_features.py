"""Iteration 4 backend tests: rates/preview, finalize (idempotent), create-return
no-email behavior, send_admin_new_return template presence, admin_notification_email
default."""
import os
import sys
import uuid
import asyncio
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

sys.path.insert(0, "/app/backend")

# Use localhost since iteration 3 noted public ingress returned 404
BASE_URL = "http://localhost:8001"

ADMIN_EMAIL = "admin@pge.com"
ADMIN_PASSWORD = "admin123"

_ENV = dotenv_values("/app/backend/.env")
MONGO_URL = _ENV.get("MONGO_URL") or os.environ.get("MONGO_URL")
DB_NAME = _ENV.get("DB_NAME") or os.environ.get("DB_NAME") or "pge_returns"


def _mongo():
    import pymongo
    return pymongo.MongoClient(MONGO_URL)[DB_NAME]


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_client(client):
    r = client.post(f"{BASE_URL}/api/auth/login",
                    json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json()["token"]
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"})
    return s


# ---------- POST /api/rates/preview ----------
class TestRatesPreview:
    def test_missing_zip_returns_error(self, client, admin_client):
        """Empty zip → expected 400 Postcode is required (only reachable when
        shippo is configured). With no shippo configured we'll instead get 500
        'Shippo API key not configured.' — either is acceptable as graceful."""
        r = client.post(f"{BASE_URL}/api/rates/preview",
                        json={"zip": "", "country": "US"})
        assert r.status_code in (400, 422, 500), r.text
        body = r.json()
        detail = (body.get("detail") or "").lower()
        assert any(w in detail for w in ("postcode", "zip", "shippo", "required")) \
            or r.status_code == 422

    def test_shippo_not_configured_returns_500(self, admin_client, client):
        """With no Shippo key configured → clean 500 'Shippo API key not configured.'"""
        # Ensure shippo key is NOT set in DB (by sending no update; env var must also be empty)
        sset = admin_client.get(f"{BASE_URL}/api/admin/settings").json()
        if sset.get("shippo_api_key_set"):
            pytest.skip("shippo_api_key is configured; skipping not-configured assertion")
        r = client.post(f"{BASE_URL}/api/rates/preview",
                        json={"zip": "SW1A 1AA", "country": "GB"})
        assert r.status_code == 500, r.text
        assert "shippo" in (r.json().get("detail") or "").lower()

    def test_zip_only_payload_validates(self, client):
        """Body with just zip+country must pass pydantic validation (no state/city required)."""
        r = client.post(f"{BASE_URL}/api/rates/preview",
                        json={"zip": "90210", "country": "US"})
        # Either 500 (shippo not configured) or 502 (shippo error) or 200 (rates) — NOT 422
        assert r.status_code != 422, (
            "RatePreviewRequest should accept body with only zip+country")

    def test_missing_country_defaults_to_us(self, client):
        r = client.post(f"{BASE_URL}/api/rates/preview", json={"zip": "90210"})
        assert r.status_code != 422

    def test_invalid_body_shape(self, client):
        r = client.post(f"{BASE_URL}/api/rates/preview", json={})
        # zip is required → 422
        assert r.status_code == 422


# ---------- create-return does NOT fire emails ----------
class TestCreateReturnNoEmails:
    def test_server_source_has_no_inline_emails(self):
        """Code inspection: POST /returns must NOT call send_return_initiated or
        send_admin_new_return in-line; those run only from /finalize."""
        code = Path("/app/backend/server.py").read_text()
        # Locate create_return block
        start = code.index("async def create_return")
        end = code.index("async def ", start + 10)
        create_block = code[start:end]
        assert "send_return_initiated" not in create_block, \
            "create_return should not send emails directly"
        assert "send_admin_new_return" not in create_block, \
            "create_return should not send admin email directly"
        assert "send_label_ready" not in create_block

    def test_deduct_from_refund_no_inline_emails(self):
        code = Path("/app/backend/server.py").read_text()
        start = code.index("async def deduct_from_refund")
        end = code.index("async def ", start + 10)
        block = code[start:end]
        assert "send_label_ready" not in block
        assert "send_return_initiated" not in block

    def test_payment_status_no_inline_emails(self):
        code = Path("/app/backend/server.py").read_text()
        start = code.index("async def payment_status")
        end = code.index("async def ", start + 10)
        block = code[start:end]
        assert "send_label_ready" not in block

    def test_seeded_return_starts_with_no_email_log(self):
        """Seed a return-style doc and verify the default shape has no email_log
        entries and emails_finalized==False (matching ReturnRequestDoc defaults)."""
        from models import ReturnRequestDoc, Address, ReturnItem
        addr = Address(name="n", street1="s", city="c", state="s",
                       zip="z", country="US")
        doc = ReturnRequestDoc(
            rma_number="RMA-TESTNEW", order_id="1", order_number="1",
            email="x@x.com", customer_name="X",
            items=[ReturnItem(line_item_id="li", name="n", quantity=1,
                              price=1.0, reason="other")],
            method="free_label",
            return_address=addr, warehouse_address=addr,
        ).model_dump()
        assert doc["email_log"] == []
        assert doc["emails_finalized"] is False


# ---------- POST /api/returns/{id}/finalize ----------
class TestFinalize:
    def test_finalize_404(self, client):
        r = client.post(f"{BASE_URL}/api/returns/does-not-exist-xyz/finalize")
        assert r.status_code == 404, r.text

    def test_finalize_idempotent_and_kinds(self, client):
        """Seed return doc; call finalize twice; first returns sent[], second skipped."""
        db = _mongo()
        rid = f"TEST-{uuid.uuid4().hex[:8]}"
        doc = {
            "id": rid,
            "rma_number": f"RMA-IT4-{uuid.uuid4().hex[:6]}",
            "order_id": "999",
            "order_number": "MOCK-9999",
            "email": "finalize@example.com",
            "customer_name": "Finalize Tester",
            "method": "deduct_from_refund",
            "method_display_label": "Deduct shipping from refund",
            "status": "label_purchased",
            "items": [{"line_item_id": "li-f", "name": "Widget",
                       "quantity": 1, "price": 25.0, "reason": "other"}],
            "return_address": {"name": "X", "street1": "1 Rd", "city": "LDN",
                               "state": "--", "zip": "SW1", "country": "GB"},
            "warehouse_address": {"name": "WH", "street1": "1 WH", "city": "LDN",
                                  "state": "--", "zip": "SW1", "country": "GB"},
            "refund_amount": 25.0,
            "refund_deduction": 5.0,
            "refund_net": 20.0,
            "label_url": "https://example.com/label.pdf",
            "tracking_number": "TRACK123",
            "selected_rate": {"currency": "GBP"},
            "email_log": [],
            "emails_finalized": False,
            "customer_actions": [],
        }
        db.returns.insert_one(doc)
        try:
            # First call
            r1 = client.post(f"{BASE_URL}/api/returns/{rid}/finalize")
            assert r1.status_code == 200, r1.text
            b1 = r1.json()
            assert "ok" in b1
            assert "sent" in b1, f"expected 'sent' in response: {b1}"
            sent = b1["sent"]
            assert isinstance(sent, list)
            kinds = {a.get("kind") for a in sent}
            # Should attempt all 3 kinds since label_url+tracking are set
            assert "return_initiated" in kinds, f"kinds={kinds}"
            assert "admin_new_return" in kinds, f"kinds={kinds}"
            assert "label_ready" in kinds, f"kinds={kinds}"
            # With no providers configured, each attempt.ok should be False
            for a in sent:
                assert "provider" in a and "ok" in a and "message" in a
            # Verify emails_finalized set and email_log pushed
            updated = db.returns.find_one({"id": rid})
            assert updated.get("emails_finalized") is True
            assert len(updated.get("email_log") or []) == len(sent)

            # Second call → skipped
            r2 = client.post(f"{BASE_URL}/api/returns/{rid}/finalize")
            assert r2.status_code == 200, r2.text
            b2 = r2.json()
            assert b2.get("ok") is True
            assert b2.get("skipped") is True
            assert "already" in (b2.get("reason") or "").lower()
        finally:
            db.returns.delete_one({"id": rid})

    def test_finalize_without_label_skips_label_ready(self, client):
        """Return without label_url/tracking → sent[] has no label_ready entry."""
        db = _mongo()
        rid = f"TEST-{uuid.uuid4().hex[:8]}"
        doc = {
            "id": rid,
            "rma_number": f"RMA-IT4L-{uuid.uuid4().hex[:6]}",
            "order_id": "998", "order_number": "MOCK-9998",
            "email": "nolabel@example.com", "customer_name": "NoLabel",
            "method": "free_label", "method_display_label": "Free label",
            "status": "awaiting_approval",
            "items": [{"line_item_id": "li-n", "name": "N",
                       "quantity": 1, "price": 10.0, "reason": "other"}],
            "return_address": {"name": "X", "street1": "1", "city": "C",
                               "state": "S", "zip": "Z", "country": "US"},
            "warehouse_address": {"name": "WH", "street1": "1", "city": "C",
                                  "state": "S", "zip": "Z", "country": "US"},
            "refund_amount": 10.0, "refund_deduction": 0.0, "refund_net": 10.0,
            "email_log": [], "emails_finalized": False, "customer_actions": [],
        }
        db.returns.insert_one(doc)
        try:
            r = client.post(f"{BASE_URL}/api/returns/{rid}/finalize")
            assert r.status_code == 200, r.text
            kinds = {a.get("kind") for a in r.json().get("sent", [])}
            assert "return_initiated" in kinds
            assert "admin_new_return" in kinds
            assert "label_ready" not in kinds, \
                "label_ready should be skipped when no label_url/tracking"
        finally:
            db.returns.delete_one({"id": rid})


# ---------- email_service.send_admin_new_return exists ----------
class TestAdminNewReturnTemplate:
    def test_function_present(self):
        import email_service
        assert hasattr(email_service, "send_admin_new_return")
        assert callable(email_service.send_admin_new_return)

    def test_admin_notify_not_configured_without_admin_email(self):
        """If admin_notification_email is blank in cfg, function returns ok:False
        with a clear 'not configured' attempt (no provider attempted)."""
        import email_service as es
        result = asyncio.run(es.send_admin_new_return(
            cfg={"store_name": "S"},  # NO admin_notification_email
            rma_number="RMA-X", order_number="1", customer_name="C",
            customer_email="c@x.com", method_display_label="m",
            items=[], refund_amount=0.0, refund_deduction=0.0, refund_net=0.0,
        ))
        assert result["ok"] is False
        attempts = result.get("attempts") or []
        assert len(attempts) >= 1
        msg = " ".join(a.get("message", "") for a in attempts).lower()
        assert "not configured" in msg or "admin" in msg


# ---------- settings_service admin_notification_email default ----------
class TestAdminNotificationEmailDefault:
    def test_code_default_value(self):
        import settings_service as ss
        assert "admin_notification_email" in ss.SETTINGS_KEYS
        assert ss.CODE_DEFAULTS.get("admin_notification_email") == \
            "Returns@pgelimited.com"

    def test_get_settings_returns_default_when_unset(self):
        """When DB and env var are both empty, get_settings must fall back
        to the code default 'Returns@pgelimited.com'."""
        import settings_service as ss

        class FakeColl:
            async def find_one(self, q):
                return None
        class FakeDB:
            app_settings = FakeColl()

        # ensure env var isn't accidentally set
        old = os.environ.pop("ADMIN_NOTIFICATION_EMAIL", None)
        try:
            out = asyncio.run(ss.get_settings(FakeDB()))
            assert out.get("admin_notification_email") == \
                "Returns@pgelimited.com"
        finally:
            if old is not None:
                os.environ["ADMIN_NOTIFICATION_EMAIL"] = old


# ---------- RatePreviewRequest model ----------
class TestRatePreviewModel:
    def test_model_fields(self):
        from models import RatePreviewRequest
        m = RatePreviewRequest(zip="90210")
        assert m.zip == "90210"
        assert m.country == "US"  # default
        assert m.state is None
        assert m.city is None
        m2 = RatePreviewRequest(zip="SW1A 1AA", country="GB",
                                state="LND", city="London")
        assert m2.country == "GB"
        assert m2.state == "LND"


# ---------- ReturnRequestDoc.emails_finalized field ----------
class TestEmailsFinalizedField:
    def test_default_false(self):
        from models import ReturnRequestDoc, Address, ReturnItem
        addr = Address(name="n", street1="s", city="c", state="s",
                       zip="z", country="US")
        d = ReturnRequestDoc(
            rma_number="R", order_id="1", order_number="1",
            email="x@x.com", customer_name="X",
            items=[ReturnItem(line_item_id="li", name="n", quantity=1,
                              price=1.0, reason="other")],
            method="free_label",
            return_address=addr, warehouse_address=addr,
        )
        assert d.emails_finalized is False
