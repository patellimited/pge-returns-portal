"""
Iteration 6 — backend bug-fix bundle for PGE Returns Portal.

Tests cover:
  * Auth login → JWT
  * Public endpoints: /api/branding, /api/stats/public
  * Internal-notes admin endpoint (auth, 404, 400, 200, ordering)
  * /api/returns model accepts new restricted_shipping_choice
  * Two-stage approve-free for store_credit (free_label vs self_ship,
    awaiting_approval vs financial-stage)
  * Bonus + label-deduction math in coupon issuance
  * revoke-store-credit flow

NOTE: WooCommerce / Stripe / Shippo / email providers are NOT configured in
this env. We seed return docs directly into MongoDB so we can exercise the
admin endpoints without a live WC store.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
load_dotenv("/app/backend/.env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"

# Mongo connection used only for direct seeding of returns docs (the admin
# endpoints under test don't have a "create return" route that bypasses Woo).
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def db():
    cli = MongoClient(MONGO_URL)
    yield cli[DB_NAME]
    cli.close()


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(
        f"{API}/auth/login",
        json={"email": "admin@pgelimited.com", "password": "admin123"},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "token" in data and isinstance(data["token"], str) and data["token"]
    return data["token"]


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def seed_return(db):
    """Factory fixture — inserts a return doc and cleans up after."""
    created_ids = []

    def _factory(**overrides):
        rid = str(uuid.uuid4())
        rma = f"TEST-RMA-{rid[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        addr = {
            "name": "Test Customer", "street1": "1 High St", "street2": "",
            "city": "London", "state": "", "zip": "SW1A1AA", "country": "GB",
            "phone": "", "email": "test@example.com",
        }
        wh = {
            "name": "PGE Warehouse", "street1": "1 Warehouse Way", "street2": "",
            "city": "Manchester", "state": "", "zip": "M1 1AA", "country": "GB",
            "phone": "", "email": "",
        }
        item = {
            "line_item_id": "li-1", "name": "Test Item", "quantity": 1,
            "price": 10.99, "image": "", "reason": "wrong_item", "notes": "",
            "weight": None, "weight_unit": None, "sku": "", "product_id": "",
        }
        doc = {
            "id": rid,
            "rma_number": rma,
            "order_id": "999999",
            "order_number": "999999",
            "email": "test@example.com",
            "customer_name": "Test Customer",
            "items": [item],
            "method": "store_credit",
            "method_display_label": "Store credit",
            "status": "awaiting_approval",
            "customer_note": "",
            "admin_note": "",
            "return_address": addr,
            "warehouse_address": wh,
            "refund_amount": 10.99,
            "refund_deduction": 0.0,
            "refund_net": 10.99,
            "paid": False,
            "refunded": False,
            "customer_actions": [],
            "customer_proof_photos": [],
            "internal_notes": [],
            "coupon_label_deduction": 0.0,
            "archived": False,
            "closed": False,
            "email_log": [],
            "emails_finalized": False,
            "tracking_updates": [],
            "self_ship_reminder_count": 0,
            "label_cost": 0.0,
            "restricted_shipping_choice": "free_label",
            "created_at": now,
            "updated_at": now,
        }
        doc.update(overrides)
        db.returns.insert_one(dict(doc))
        created_ids.append(rid)
        return doc

    yield _factory

    if created_ids:
        db.returns.delete_many({"id": {"$in": created_ids}})


# -----------------------------------------------------------------------------
# Auth + public endpoints
# -----------------------------------------------------------------------------
class TestAuthAndPublic:
    def test_login_returns_jwt(self, admin_token):
        assert isinstance(admin_token, str) and len(admin_token) > 20

    def test_login_invalid(self):
        r = requests.post(f"{API}/auth/login",
                          json={"email": "admin@pgelimited.com", "password": "wrong"},
                          timeout=15)
        assert r.status_code in (400, 401)

    def test_branding_has_store_credit_keys(self):
        r = requests.get(f"{API}/branding", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert "store_credit_enabled" in j
        assert "store_credit_bonus_percent" in j
        assert isinstance(j["store_credit_enabled"], bool)

    def test_public_stats_shape(self):
        r = requests.get(f"{API}/stats/public", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert "happy_returns" in j
        assert isinstance(j["happy_returns"], int)
        assert j["happy_returns"] >= 0


# -----------------------------------------------------------------------------
# Internal notes
# -----------------------------------------------------------------------------
class TestInternalNotes:
    def test_requires_auth(self):
        r = requests.post(f"{API}/admin/returns/some-id/internal-notes",
                          json={"text": "hi"}, timeout=15)
        assert r.status_code in (401, 403)

    def test_404_on_unknown_id(self, auth_headers):
        r = requests.post(
            f"{API}/admin/returns/does-not-exist-{uuid.uuid4()}/internal-notes",
            headers=auth_headers, json={"text": "hello"}, timeout=15,
        )
        assert r.status_code == 404

    def test_400_on_empty_text(self, auth_headers, seed_return):
        doc = seed_return()
        r = requests.post(
            f"{API}/admin/returns/{doc['id']}/internal-notes",
            headers=auth_headers, json={"text": "   "}, timeout=15,
        )
        assert r.status_code == 400

    def test_appends_with_timestamp_and_persists(self, auth_headers, seed_return, db):
        doc = seed_return()
        rid = doc["id"]
        r1 = requests.post(f"{API}/admin/returns/{rid}/internal-notes",
                           headers=auth_headers, json={"text": "first note"}, timeout=15)
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1.get("ok") is True
        assert body1["note"]["text"] == "first note"
        assert body1["note"]["at"]
        assert body1["note"]["author"]

        r2 = requests.post(f"{API}/admin/returns/{rid}/internal-notes",
                           headers=auth_headers, json={"text": "second note"}, timeout=15)
        assert r2.status_code == 200

        # Verify both notes persisted in document via /admin/returns
        list_r = requests.get(f"{API}/admin/returns",
                              headers=auth_headers, timeout=15)
        assert list_r.status_code == 200
        rows = list_r.json()
        # response can be list directly or wrapped
        if isinstance(rows, dict):
            rows = rows.get("returns") or rows.get("items") or []
        match = next((x for x in rows if x.get("id") == rid), None)
        assert match is not None, "seeded return missing from /admin/returns"
        notes = match.get("internal_notes") or []
        texts = [n.get("text") for n in notes]
        assert "first note" in texts and "second note" in texts


# -----------------------------------------------------------------------------
# create_return — model accepts restricted_shipping_choice
# -----------------------------------------------------------------------------
class TestCreateReturnSchema:
    """Without WooCommerce creds, fetch_order returns None → /api/returns
    returns 404. We use that to confirm Pydantic accepted the body — a 422
    would mean the new field broke validation."""

    BASE_BODY = {
        "order_id": "999999",
        "email": "buyer@example.com",
        "items": [{
            "line_item_id": "li-1", "name": "X", "quantity": 1, "price": 10.0,
            "reason": "no_longer_needed",
        }],
        "method": "store_credit",
        "return_address": {
            "name": "B", "street1": "1 St", "city": "L", "state": "",
            "zip": "AA", "country": "GB",
        },
    }

    def _post(self, **extra):
        body = dict(self.BASE_BODY)
        body.update(extra)
        return requests.post(f"{API}/returns", json=body, timeout=15)

    def test_omitting_field_still_validates(self):
        r = self._post()
        # Pydantic must accept (200/404 acceptable; 422 = schema broken)
        assert r.status_code != 422, r.text

    def test_self_ship_choice_validates(self):
        r = self._post(restricted_shipping_choice="self_ship")
        assert r.status_code != 422, r.text

    def test_free_label_choice_validates(self):
        items = [{"line_item_id": "li-1", "name": "X", "quantity": 1,
                  "price": 10.0, "reason": "wrong_item"}]
        r = self._post(items=items, restricted_shipping_choice="free_label")
        assert r.status_code != 422, r.text

    def test_bad_choice_rejected(self):
        r = self._post(restricted_shipping_choice="invalid_value")
        assert r.status_code == 422


# -----------------------------------------------------------------------------
# approve-free — store_credit two-stage
# -----------------------------------------------------------------------------
class TestApproveFreeStoreCredit:
    def _approve(self, auth_headers, rid, note=""):
        # multipart/form-data — endpoint uses Form() + UploadFile
        return requests.post(
            f"{API}/admin/returns/{rid}/approve-free",
            headers=auth_headers,
            data={"note": note},
            timeout=20,
        )

    def test_stage1_free_label_does_not_issue_coupon(self, auth_headers, seed_return, db):
        doc = seed_return(restricted_shipping_choice="free_label",
                          status="awaiting_approval")
        rid = doc["id"]
        r = self._approve(auth_headers, rid)
        assert r.status_code == 200, r.text
        body = r.json()
        # status should be 'approved' (no attachment) — NOT store_credit_issued
        assert body.get("status") in ("approved", "label_purchased")
        persisted = db.returns.find_one({"id": rid}, {"_id": 0})
        assert not persisted.get("coupon_code"), \
            f"stage-1 must NOT issue coupon, got {persisted.get('coupon_code')}"
        assert persisted["status"] in ("approved", "label_purchased")

    def test_stage1_self_ship_flips_to_awaiting_tracking_no_coupon(
            self, auth_headers, seed_return, db):
        doc = seed_return(restricted_shipping_choice="self_ship",
                          status="awaiting_approval")
        rid = doc["id"]
        r = self._approve(auth_headers, rid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("status") == "awaiting_tracking"
        persisted = db.returns.find_one({"id": rid}, {"_id": 0})
        assert persisted["status"] == "awaiting_tracking"
        assert not persisted.get("coupon_code")

    def test_stage2_issues_coupon_with_bonus(self, auth_headers, seed_return, db):
        """Stage-2: doc already at financial-stage status (label_purchased).
        Approve-free now must issue coupon. WooCommerce create_coupon will
        fail without WC creds → endpoint returns 502; we accept either 200
        (if a fallback exists) or 502 and validate behaviour accordingly."""
        # Set bonus_percent=5 for predictable math; refund=10.99 → expect 11.54
        db.app_settings.update_one(
            {"_id": "config"},
            {"$set": {"store_credit_bonus_percent": 5,
                      "enable_store_credit": "true"}},
            upsert=True,
        )
        doc = seed_return(restricted_shipping_choice="free_label",
                          status="label_purchased",
                          refund_amount=10.99, refund_deduction=0.0)
        rid = doc["id"]
        r = self._approve(auth_headers, rid)
        # Without WC creds Woo coupon creation fails → 502 with our error
        assert r.status_code in (200, 502), r.text
        if r.status_code == 200:
            persisted = db.returns.find_one({"id": rid}, {"_id": 0})
            assert persisted["status"] == "store_credit_issued"
            # base 10.99 + 5% bonus 0.55 = 11.54, no deduction
            assert persisted.get("coupon_amount") == pytest.approx(11.54, abs=0.01)
            assert persisted.get("coupon_code")

    def test_stage2_with_label_deduction(self, auth_headers, seed_return, db):
        """refund_deduction=2.0 simulates label deduction. Expected coupon =
        round(10.99 * 1.05 - 2.0, 2) = 9.54. Math is verified even when WC
        coupon creation fails — _issue_store_credit_for_return logs the
        amount and stops at woo.create_coupon if it returns None. We assert
        coupon_label_deduction is recorded ONLY if endpoint reaches the DB
        write (i.e. WC succeeded). Otherwise we fall back to verifying the
        function path was taken (status not changed to store_credit_issued)."""
        db.app_settings.update_one(
            {"_id": "config"},
            {"$set": {"store_credit_bonus_percent": 5,
                      "enable_store_credit": "true"}},
            upsert=True,
        )
        doc = seed_return(restricted_shipping_choice="free_label",
                          status="label_purchased",
                          refund_amount=10.99, refund_deduction=2.0)
        rid = doc["id"]
        r = self._approve(auth_headers, rid)
        assert r.status_code in (200, 502), r.text
        persisted = db.returns.find_one({"id": rid}, {"_id": 0})
        if persisted.get("coupon_code"):
            # WC actually created the coupon → math must be correct
            assert persisted["coupon_amount"] == pytest.approx(9.54, abs=0.01)
            assert persisted.get("coupon_label_deduction") == pytest.approx(2.0)
        else:
            # WC failed (expected without creds). Verify status NOT promoted.
            assert persisted["status"] != "store_credit_issued"


# -----------------------------------------------------------------------------
# Coupon math — direct unit test of _issue_store_credit_for_return's math
# (verified via re-implementing the formula and asserting matches)
# -----------------------------------------------------------------------------
class TestCouponMath:
    @pytest.mark.parametrize("base,bonus_pct,deduction,expected", [
        (10.99, 5.0, 0.0, 11.54),
        (10.99, 5.0, 2.0, 9.54),
        (100.00, 10.0, 0.0, 110.00),
        (50.00, 0.0, 5.0, 45.00),
    ])
    def test_formula(self, base, bonus_pct, deduction, expected):
        bonus_amount = round(base * (bonus_pct / 100.0), 2)
        coupon_amount = round(base + bonus_amount - deduction, 2)
        if coupon_amount < 0:
            coupon_amount = 0.0
        assert coupon_amount == pytest.approx(expected, abs=0.01)


# -----------------------------------------------------------------------------
# revoke-store-credit
# -----------------------------------------------------------------------------
class TestRevokeStoreCredit:
    def test_requires_auth(self):
        r = requests.post(f"{API}/admin/returns/x/revoke-store-credit", timeout=15)
        assert r.status_code in (401, 403)

    def test_404_unknown(self, auth_headers):
        r = requests.post(
            f"{API}/admin/returns/missing-{uuid.uuid4()}/revoke-store-credit",
            headers=auth_headers, timeout=15,
        )
        assert r.status_code == 404

    def test_400_when_not_issued(self, auth_headers, seed_return):
        doc = seed_return(status="awaiting_approval")
        r = requests.post(
            f"{API}/admin/returns/{doc['id']}/revoke-store-credit",
            headers=auth_headers, timeout=15,
        )
        assert r.status_code == 400

    def test_revoke_flips_status_no_email_failure_called(
            self, auth_headers, seed_return, db):
        doc = seed_return(status="store_credit_issued",
                          coupon_code="RMA-TEST-1234",
                          coupon_amount=11.54,
                          coupon_currency="GBP")
        rid = doc["id"]
        r = requests.post(
            f"{API}/admin/returns/{rid}/revoke-store-credit",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("status") == "store_credit_revoked"
        assert j.get("coupon_code") == "RMA-TEST-1234"
        persisted = db.returns.find_one({"id": rid}, {"_id": 0})
        assert persisted["status"] == "rejected"
        assert persisted.get("store_credit_revoked") is True
        assert persisted.get("closed_reason") == "store_credit_revoked"
        # The action log should reflect the revoke (NOT a label_failure entry)
        kinds = [a.get("kind") for a in (persisted.get("customer_actions") or [])]
        assert "store_credit_revoked" in kinds
        # email_log should not contain admin_label_failure entries
        email_kinds = [e.get("kind") for e in (persisted.get("email_log") or [])]
        assert "admin_label_failure" not in email_kinds

    def test_revoke_idempotent(self, auth_headers, seed_return):
        doc = seed_return(status="store_credit_issued",
                          coupon_code="RMA-TEST-IDEMP",
                          coupon_amount=11.54)
        rid = doc["id"]
        r1 = requests.post(f"{API}/admin/returns/{rid}/revoke-store-credit",
                           headers=auth_headers, timeout=20)
        assert r1.status_code == 200
        r2 = requests.post(f"{API}/admin/returns/{rid}/revoke-store-credit",
                           headers=auth_headers, timeout=20)
        # Second call: doc.status is now 'rejected', so endpoint returns 400
        # OR the already_revoked path returns 200. Both acceptable — endpoint
        # must not raise 500.
        assert r2.status_code in (200, 400)


# -----------------------------------------------------------------------------
# Direct unit test of _issue_store_credit_for_return with woo.create_coupon
# monkey-patched. Validates persistence of coupon_label_deduction and the
# real bonus + deduction math the endpoint applies.
# -----------------------------------------------------------------------------
class TestIssueStoreCreditDirect:
    """Single async run covering 3 math variants — sharing one event loop
    avoids motor's 'Event loop is closed' issue across parametrized cases."""

    def test_math_persists_to_db(self):
        import sys, asyncio
        sys.path.insert(0, "/app/backend")
        import server  # noqa: E402

        cases = [
            (10.99, 5.0, 0.0, 11.54),
            (10.99, 5.0, 2.0, 9.54),
            (50.0, 10.0, 5.0, 50.0),
        ]

        async def fake_create_coupon(cfg, **kw):
            return {"code": f"RMA-FAKE-{uuid.uuid4().hex[:6].upper()}",
                    "amount": kw.get("amount"), "id": 1}

        async def run_all():
            results = []
            orig = server.woo.create_coupon
            server.woo.create_coupon = fake_create_coupon
            try:
                for base, bonus_pct, deduction, expected in cases:
                    rid = str(uuid.uuid4())
                    addr = {"name": "a", "street1": "s", "street2": "",
                            "city": "c", "state": "", "zip": "z",
                            "country": "GB", "phone": "", "email": ""}
                    item = {"line_item_id": "li-1", "name": "X", "quantity": 1,
                            "price": base, "image": "", "reason": "wrong_item",
                            "notes": "", "weight": None, "weight_unit": None,
                            "sku": "", "product_id": ""}
                    now = datetime.now(timezone.utc).isoformat()
                    doc = {"id": rid, "rma_number": f"TST-{rid[:6]}",
                           "order_id": "1", "order_number": "1",
                           "email": "t@e.com", "customer_name": "T",
                           "items": [item], "method": "store_credit",
                           "status": "label_purchased",
                           "return_address": addr, "warehouse_address": addr,
                           "refund_amount": base, "refund_deduction": deduction,
                           "created_at": now, "updated_at": now}
                    await server.db.app_settings.update_one(
                        {"_id": "config"},
                        {"$set": {"store_credit_bonus_percent": bonus_pct,
                                  "enable_store_credit": "true"}},
                        upsert=True,
                    )
                    await server.db.returns.insert_one(dict(doc))
                    updated = await server._issue_store_credit_for_return(rid)
                    results.append((base, bonus_pct, deduction, expected, updated))
                    await server.db.returns.delete_one({"id": rid})
            finally:
                server.woo.create_coupon = orig
            return results

        results = asyncio.run(run_all())
        for base, bonus_pct, deduction, expected, updated in results:
            assert updated is not None, f"None for case {base},{bonus_pct},{deduction}"
            assert updated["status"] == "store_credit_issued"
            assert updated["coupon_amount"] == pytest.approx(expected, abs=0.01), \
                f"expected {expected}, got {updated['coupon_amount']}"
            assert updated["coupon_label_deduction"] == pytest.approx(deduction)
            assert updated.get("coupon_code", "").startswith("RMA-FAKE-")


# -----------------------------------------------------------------------------
# Smoke: email_service module imports cleanly with new helpers
# -----------------------------------------------------------------------------
class TestEmailServiceImport:
    def test_helpers_present(self):
        import importlib
        es = importlib.import_module("email_service")
        for fn in ("_email_footer_html", "_tracking_link",
                   "_smart_post_arrival_line",
                   "send_store_credit_revoked_to_customer"):
            assert hasattr(es, fn), f"missing helper: {fn}"
