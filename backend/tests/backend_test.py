"""End-to-end backend tests for the PGE Return Portal."""
import os
import uuid
import pytest
import requests
from pathlib import Path

# Load frontend .env to pull the public backend URL
from dotenv import dotenv_values

FRONTEND_ENV = Path("/app/frontend/.env")
BASE_URL = (dotenv_values(FRONTEND_ENV).get("REACT_APP_BACKEND_URL") or
            os.environ.get("REACT_APP_BACKEND_URL"))
assert BASE_URL, "REACT_APP_BACKEND_URL must be set"
BASE_URL = BASE_URL.rstrip("/")

ADMIN_EMAIL = "admin@pgelimited.com"
ADMIN_PASSWORD = "Admin@12345"


# -------- fixtures --------
@pytest.fixture(scope="session")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(client):
    r = client.post(f"{BASE_URL}/api/auth/login",
                    json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="session")
def admin_client(client, admin_token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {admin_token}"})
    return s


# ------- Health / branding -------
class TestHealth:
    def test_root(self, client):
        r = client.get(f"{BASE_URL}/api/")
        assert r.status_code == 200
        assert r.json().get("status") == "ok"

    def test_branding(self, client):
        r = client.get(f"{BASE_URL}/api/branding")
        assert r.status_code == 200
        d = r.json()
        for k in ("store_name", "support_email", "logo_url",
                  "hero_image_url", "max_return_window_days"):
            assert k in d
        assert d["store_name"]  # non-empty


# ------- Order lookup -------
class TestOrderLookup:
    def test_lookup_mock(self, client, admin_client):
        # NOTE: MOCK order is hardcoded to 2026-01-15; widen window so test is
        # not time-bombed. Main agent should make this date dynamic.
        admin_client.put(f"{BASE_URL}/api/admin/settings",
                         json={"max_return_window_days": "365"})
        try:
            r = client.post(f"{BASE_URL}/api/orders/lookup",
                            json={"order_id": "MOCK-1001", "email": "anything@example.com"})
            assert r.status_code == 200, r.text
            d = r.json()
            assert len(d["line_items"]) == 3
            assert d["order_id"] == "MOCK-1001"
        finally:
            admin_client.put(f"{BASE_URL}/api/admin/settings",
                             json={"max_return_window_days": "30"})

    def test_lookup_invalid(self, client):
        r = client.post(f"{BASE_URL}/api/orders/lookup",
                        json={"order_id": "DOES-NOT-EXIST-9999",
                              "email": "nobody@example.com"})
        assert r.status_code == 404

    def test_return_window_enforcement(self, admin_client, client):
        # Narrow window to 1 day, MOCK order is 2026-01-15 so should now be rejected
        r = admin_client.put(f"{BASE_URL}/api/admin/settings",
                             json={"max_return_window_days": "1"})
        assert r.status_code == 200, r.text
        try:
            r2 = client.post(f"{BASE_URL}/api/orders/lookup",
                             json={"order_id": "MOCK-1001",
                                   "email": "x@example.com"})
            assert r2.status_code == 403, f"expected 403 got {r2.status_code}: {r2.text}"
            assert "window" in r2.json().get("detail", "").lower()
        finally:
            # restore
            r3 = admin_client.put(f"{BASE_URL}/api/admin/settings",
                                  json={"max_return_window_days": "30"})
            assert r3.status_code == 200


# ------- Returns CRUD + rates + tracking -------
@pytest.fixture(scope="session")
def return_address():
    return {
        "name": "Test Customer",
        "street1": "123 Demo Street",
        "city": "Brooklyn",
        "state": "NY",
        "zip": "11201",
        "country": "US",
        "phone": "5551230000",
        "email": "TEST_customer@example.com",
    }


def _mock_items():
    return [
        {"line_item_id": "li-1", "name": "Premium Cotton Tee",
         "quantity": 1, "price": 29.00, "image": "",
         "reason": "size_issue", "notes": ""},
    ]


@pytest.fixture(scope="session")
def stripe_return(client, return_address):
    """Create a pay_stripe return and cache across tests."""
    r = client.post(f"{BASE_URL}/api/returns", json={
        "order_id": "MOCK-1001",
        "email": "TEST_customer@example.com",
        "items": _mock_items(),
        "method": "pay_stripe",
        "customer_note": "TEST",
        "return_address": return_address,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "awaiting_payment"
    assert d["method"] == "pay_stripe"
    assert d["rma_number"].startswith("RMA-")
    return d


@pytest.fixture(scope="session")
def deduct_return(client, return_address):
    r = client.post(f"{BASE_URL}/api/returns", json={
        "order_id": "MOCK-1001",
        "email": "TEST_customer@example.com",
        "items": _mock_items(),
        "method": "deduct_from_refund",
        "return_address": return_address,
    })
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def free_return(client, return_address):
    items = _mock_items()
    items[0]["reason"] = "damaged"
    r = client.post(f"{BASE_URL}/api/returns", json={
        "order_id": "MOCK-1001",
        "email": "TEST_customer@example.com",
        "items": items,
        "method": "free_label",
        "return_address": return_address,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "awaiting_approval"
    return d


class TestReturns:
    def test_create_return_invalid_item(self, client, return_address):
        r = client.post(f"{BASE_URL}/api/returns", json={
            "order_id": "MOCK-1001",
            "email": "TEST_customer@example.com",
            "items": [{"line_item_id": "li-bogus", "name": "x",
                       "quantity": 1, "price": 1.0, "reason": "other"}],
            "method": "pay_stripe",
            "return_address": return_address,
        })
        assert r.status_code == 400

    def test_get_return(self, client, stripe_return):
        r = client.get(f"{BASE_URL}/api/returns/{stripe_return['id']}")
        assert r.status_code == 200
        assert r.json()["rma_number"] == stripe_return["rma_number"]

    def test_rates(self, client, stripe_return):
        r = client.post(f"{BASE_URL}/api/returns/{stripe_return['id']}/rates")
        assert r.status_code == 200, r.text
        d = r.json()
        assert "shipment_id" in d
        assert isinstance(d.get("rates"), list)
        # stash on class for next tests
        TestReturns._rates = d["rates"]
        TestReturns._sid = d["shipment_id"]

    def test_stripe_checkout(self, client, stripe_return):
        rates = getattr(TestReturns, "_rates", [])
        if not rates:
            pytest.skip("No Shippo rates returned - cannot create checkout")
        rate_id = rates[0]["rate_id"]
        r = client.post(
            f"{BASE_URL}/api/returns/{stripe_return['id']}/checkout",
            json={"rate_id": rate_id, "origin_url": BASE_URL},
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["url"].startswith("http")
        assert d["session_id"]
        TestReturns._session_id = d["session_id"]

    def test_payment_status(self, client):
        sid = getattr(TestReturns, "_session_id", None)
        if not sid:
            pytest.skip("No stripe session id - prior test failed")
        r = client.get(f"{BASE_URL}/api/payments/status/{sid}")
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("status", "payment_status", "return_id", "rma_number"):
            assert k in d

    def test_deduct_from_refund(self, client, deduct_return):
        # fetch rates first
        r = client.post(f"{BASE_URL}/api/returns/{deduct_return['id']}/rates")
        assert r.status_code == 200, r.text
        rates = r.json().get("rates") or []
        if not rates:
            pytest.skip("No Shippo rates - cannot exercise deduct-from-refund label buy")
        rate_id = rates[0]["rate_id"]
        r2 = client.post(
            f"{BASE_URL}/api/returns/{deduct_return['id']}/deduct-from-refund",
            json={"rate_id": rate_id, "origin_url": BASE_URL},
        )
        # 200 success or 502 if shippo label buy fails -- both acceptable per spec
        assert r2.status_code in (200, 502), r2.text
        if r2.status_code == 200:
            assert r2.json().get("status") == "label_purchased"

    def test_tracking_pre_shipment(self, client, stripe_return):
        r = client.get(f"{BASE_URL}/api/tracking/{stripe_return['rma_number']}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["rma_number"] == stripe_return["rma_number"]
        assert "status" in d

    def test_tracking_not_found(self, client):
        r = client.get(f"{BASE_URL}/api/tracking/RMA-DOESNOTEXIST")
        assert r.status_code == 404


# ------- Admin endpoints -------
class TestAdmin:
    def test_login_bad(self, client):
        r = client.post(f"{BASE_URL}/api/auth/login",
                        json={"email": ADMIN_EMAIL, "password": "wrongpw"})
        assert r.status_code == 401

    def test_login_ok(self, admin_token):
        assert admin_token

    def test_returns_requires_auth(self, client):
        r = client.get(f"{BASE_URL}/api/admin/returns")
        assert r.status_code in (401, 403)

    def test_list_returns(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/admin/returns")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_stats(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/admin/stats")
        assert r.status_code == 200
        d = r.json()
        assert "total" in d and "by_status" in d

    def test_get_settings_masked(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/admin/settings")
        assert r.status_code == 200
        d = r.json()
        assert d.get("stripe_api_key") == ""  # masked
        assert "stripe_api_key_set" in d
        assert d.get("shippo_api_key") == ""
        assert "shippo_api_key_set" in d

    def test_put_settings_partial(self, admin_client):
        new_name = f"TestStore-{uuid.uuid4().hex[:6]}"
        r = admin_client.put(f"{BASE_URL}/api/admin/settings",
                             json={"store_name": new_name})
        assert r.status_code == 200
        assert r.json().get("store_name") == new_name
        # restore
        admin_client.put(f"{BASE_URL}/api/admin/settings",
                         json={"store_name": "PGE Limited"})

    def test_put_settings_secret_empty_noop(self, admin_client):
        # Sending empty should NOT wipe existing stripe key
        r = admin_client.put(f"{BASE_URL}/api/admin/settings",
                             json={"stripe_api_key": ""})
        assert r.status_code == 200
        assert r.json().get("stripe_api_key_set") is True

    def test_approve_free(self, admin_client, free_return):
        r = admin_client.post(
            f"{BASE_URL}/api/admin/returns/{free_return['id']}/approve-free")
        # 200 success OR 502 if shippo label buy fails (acceptable per spec)
        assert r.status_code in (200, 502), r.text
        if r.status_code == 200:
            assert r.json().get("status") == "label_purchased"

    def test_mark_refunded(self, admin_client, stripe_return):
        r = admin_client.post(
            f"{BASE_URL}/api/admin/returns/{stripe_return['id']}/mark-refunded")
        assert r.status_code == 200
        assert r.json().get("status") == "refunded"
        # verify persistence
        r2 = admin_client.get(f"{BASE_URL}/api/admin/returns")
        assert any(x["id"] == stripe_return["id"] and x["status"] == "refunded"
                   for x in r2.json())
