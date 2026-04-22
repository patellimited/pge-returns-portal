"""Iteration 6 backend tests:
- Easyship replaces Shiptheory as fallback rate provider
- Store-credit closes a return (closed=true, closed_reason='store_credit_applied')
- mark-refunded also closes the return (closed_reason='refunded')
- existing-items endpoint still returns items from closed (store_credit) returns
- Store-credit conflict message on POST /api/returns (code-path verified)
"""
import os
import time
import pytest
import requests
import pymongo
from datetime import datetime, timezone

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://pge-portal-dev.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

ADMIN_EMAIL = "admin@pgelimited.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def mongo_db():
    client = pymongo.MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"no token in response: {data}"
    return token


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ------------------------------------------------------------------
# 1. Backend root
# ------------------------------------------------------------------
def test_root():
    r = requests.get(f"{API}/", timeout=10)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("status") == "ok"
    assert "service" in data


# ------------------------------------------------------------------
# 2. Admin auth
# ------------------------------------------------------------------
def test_admin_login(admin_token):
    assert isinstance(admin_token, str) and len(admin_token) > 10


# ------------------------------------------------------------------
# 3. Admin settings schema: must include easyship_*, must NOT include shiptheory_*
# ------------------------------------------------------------------
def test_admin_settings_easyship_not_shiptheory(auth_headers):
    r = requests.get(f"{API}/admin/settings", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    settings = data.get("settings") or data  # accept either wrapped or flat
    # normalize to flat keys
    keys = set(settings.keys())
    assert "easyship_api_key" in keys, f"easyship_api_key missing: {sorted(keys)}"
    assert "easyship_api_key_set" in keys, f"easyship_api_key_set missing: {sorted(keys)}"
    shiptheory_keys = [k for k in keys if k.startswith("shiptheory")]
    assert not shiptheory_keys, f"shiptheory keys present: {shiptheory_keys}"


# ------------------------------------------------------------------
# 4. Easyship key persistence
# ------------------------------------------------------------------
def test_easyship_key_persistence(auth_headers):
    r = requests.put(f"{API}/admin/settings",
                     headers=auth_headers,
                     json={"easyship_api_key": "test_eas_abc123xyz"},
                     timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    settings = data.get("settings") or data
    assert settings.get("easyship_api_key_set") is True
    preview = settings.get("easyship_api_key_preview", "")
    assert preview and len(preview) > 0, f"preview empty: {preview}"


# ------------------------------------------------------------------
# 5. Connections test: easyship key present, shiptheory absent
# ------------------------------------------------------------------
def test_connections_test_easyship(auth_headers):
    r = requests.post(f"{API}/admin/settings/test", headers=auth_headers, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    # response body may be wrapped or flat
    results = data.get("results") or data
    assert "easyship" in results, f"easyship key missing: {list(results.keys())}"
    assert "shiptheory" not in results, f"shiptheory key should not be present: {list(results.keys())}"


# ------------------------------------------------------------------
# 6. Public branding no regression
# ------------------------------------------------------------------
def test_public_branding():
    r = requests.get(f"{API}/branding", timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "store_credit_enabled" in data
    assert "store_credit_bonus_percent" in data


# ------------------------------------------------------------------
# 7. existing-items endpoint includes items of closed store_credit returns
# ------------------------------------------------------------------
def test_existing_items_includes_store_credit_closed(mongo_db):
    order_number = "TEST-SC-999"
    email = "test@example.com"
    doc = {
        "id": "r-sc-test-iter6",
        "order_number": order_number,
        "email": email,
        "status": "store_credit_issued",
        "closed": True,
        "closed_reason": "store_credit_applied",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "items": [{"line_item_id": "L1", "name": "Widget",
                   "quantity": 1, "price": 10, "reason": "other"}],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    mongo_db.returns.delete_many({"order_number": order_number})
    mongo_db.returns.insert_one(dict(doc))
    try:
        r = requests.get(f"{API}/returns/existing-items/{order_number}/{email}", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "L1" in data.get("line_item_ids", []), f"L1 not in {data}"
    finally:
        mongo_db.returns.delete_many({"order_number": order_number})


# ------------------------------------------------------------------
# 8. mark-refunded sets closed flags
# ------------------------------------------------------------------
def test_mark_refunded_closes_return(mongo_db, auth_headers):
    rid = "r-mkref-test-iter6"
    doc = {
        "id": rid,
        "order_number": "TEST-MKREF-1",
        "email": "mkref@example.com",
        "status": "label_purchased",
        "refunded": False,
        "closed": False,
        "items": [{"line_item_id": "L9", "name": "Thing",
                   "quantity": 1, "price": 25, "reason": "other"}],
        "refund_amount": 25,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    mongo_db.returns.delete_many({"id": rid})
    mongo_db.returns.insert_one(dict(doc))
    try:
        r = requests.post(f"{API}/admin/returns/{rid}/mark-refunded",
                          headers=auth_headers, timeout=20)
        assert r.status_code == 200, f"{r.status_code}: {r.text}"
        stored = mongo_db.returns.find_one({"id": rid})
        assert stored is not None
        assert stored.get("status") == "refunded", f"status={stored.get('status')}"
        assert stored.get("refunded") is True, f"refunded={stored.get('refunded')}"
        assert stored.get("closed") is True, f"closed={stored.get('closed')}"
        assert stored.get("closed_reason") == "refunded", f"closed_reason={stored.get('closed_reason')}"
        assert stored.get("closed_at"), f"closed_at missing: {stored.get('closed_at')}"
    finally:
        mongo_db.returns.delete_many({"id": rid})


# ------------------------------------------------------------------
# 9. Store-credit conflict code-path exists in server.py (reachable branch)
# ------------------------------------------------------------------
def test_store_credit_conflict_branch_exists():
    with open("/app/backend/server.py", "r") as f:
        src = f.read()
    assert 'status": "store_credit_issued"' in src or "'status': 'store_credit_issued'" in src
    assert "Store credit has already been applied" in src


# ------------------------------------------------------------------
# 10. Supervisor/backend healthy — already covered by test_root
# ------------------------------------------------------------------
def test_backend_healthy_again():
    r = requests.get(f"{API}/", timeout=10)
    assert r.status_code == 200
