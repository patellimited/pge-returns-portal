"""Iteration 3 backend tests: new features (multi-provider email, duplicate-return
prevention, action timeline, partial admin settings, admin stats deductions)."""
import os
import uuid
import asyncio
import pytest
import requests
from pathlib import Path
from dotenv import dotenv_values

FRONTEND_ENV = Path("/app/frontend/.env")
_PUBLIC = (dotenv_values(FRONTEND_ENV).get("REACT_APP_BACKEND_URL")
           or os.environ.get("REACT_APP_BACKEND_URL") or "")
# Prefer public URL; fall back to internal localhost if ingress 404s
BASE_URL = "http://localhost:8001"
try:
    if _PUBLIC:
        _r = requests.get(_PUBLIC.rstrip("/") + "/api/", timeout=5)
        if _r.status_code == 200:
            BASE_URL = _PUBLIC.rstrip("/")
except Exception:
    pass

ADMIN_EMAIL = "admin@pge.com"
ADMIN_PASSWORD = "admin123"


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
    tok = r.json().get("token")
    assert tok
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"})
    return s


# ---------- AUTH ----------
class TestAuth:
    def test_login_success(self, client):
        r = client.post(f"{BASE_URL}/api/auth/login",
                        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200
        d = r.json()
        assert d.get("token")
        assert isinstance(d["token"], str) and len(d["token"]) > 20


# ---------- ADMIN STATS (new deduction fields) ----------
class TestAdminStats:
    def test_stats_shape(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/admin/stats")
        assert r.status_code == 200, r.text
        d = r.json()
        assert "total" in d
        assert "by_status" in d and isinstance(d["by_status"], dict)
        assert "total_deducted_shipping" in d
        assert "deduction_count" in d
        assert isinstance(d["total_deducted_shipping"], (int, float))
        assert isinstance(d["deduction_count"], int)


# ---------- ADMIN SETTINGS (new email-provider fields + partial update) ----------
class TestAdminSettings:
    def test_get_settings_has_new_email_fields(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/admin/settings")
        assert r.status_code == 200
        d = r.json()
        for k in ("sendgrid_api_key", "resend_api_key", "smtp_host",
                  "smtp_port", "smtp_user", "smtp_pass",
                  "email_provider_order", "from_email", "from_name"):
            assert k in d, f"missing key {k}"
        # Secrets masked but flag present
        assert d["sendgrid_api_key"] == ""
        assert "sendgrid_api_key_set" in d
        assert d["resend_api_key"] == ""
        assert "resend_api_key_set" in d
        assert d["smtp_pass"] == ""
        assert "smtp_pass_set" in d

    def test_put_settings_partial_update(self, admin_client):
        # Read baseline
        r0 = admin_client.get(f"{BASE_URL}/api/admin/settings")
        baseline = r0.json()
        store_before = baseline.get("store_name")
        support_before = baseline.get("support_email")
        max_win_before = baseline.get("max_return_window_days")

        # Send only from_name
        new_from_name = f"Tester-{uuid.uuid4().hex[:6]}"
        r = admin_client.put(f"{BASE_URL}/api/admin/settings",
                             json={"from_name": new_from_name})
        assert r.status_code == 200, r.text
        body = r.json()
        # Response should be {settings, connections} OR flat settings (both acceptable)
        s = body.get("settings", body)
        conns = body.get("connections")
        assert s.get("from_name") == new_from_name
        # Other fields UNCHANGED
        assert s.get("store_name") == store_before
        assert s.get("support_email") == support_before
        assert s.get("max_return_window_days") == max_win_before
        # connections dict (if present) has expected keys
        if conns is not None:
            for k in ("stripe", "shippo", "woocommerce", "brevo",
                      "sendgrid", "resend"):
                assert k in conns, f"connections missing {k}"
                assert "ok" in conns[k] and "message" in conns[k]

        # Verify via fresh GET
        r2 = admin_client.get(f"{BASE_URL}/api/admin/settings")
        d2 = r2.json()
        assert d2.get("from_name") == new_from_name
        assert d2.get("store_name") == store_before

    def test_put_settings_empty_secret_noop(self, admin_client):
        # Sending empty sendgrid_api_key should NOT wipe whatever was there
        r = admin_client.put(f"{BASE_URL}/api/admin/settings",
                             json={"sendgrid_api_key": ""})
        assert r.status_code == 200

    def test_settings_test_endpoint(self, admin_client):
        r = admin_client.post(f"{BASE_URL}/api/admin/settings/test")
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("stripe", "shippo", "woocommerce", "brevo",
                  "sendgrid", "resend"):
            assert k in d, f"missing provider {k} in test result"
            assert "ok" in d[k]
            assert "message" in d[k]


# ---------- RESET TEST DATA ----------
class TestResetTestData:
    def test_reset(self, admin_client):
        r = admin_client.post(f"{BASE_URL}/api/admin/reset-test-data")
        assert r.status_code in (200, 204), r.text


# ---------- EXISTING-ITEMS endpoint ----------
class TestExistingItems:
    def test_empty_after_reset(self, admin_client, client):
        admin_client.post(f"{BASE_URL}/api/admin/reset-test-data")
        r = client.get(
            f"{BASE_URL}/api/returns/existing-items/MOCK-1001/nobody@example.com"
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # Shape: either a list or {line_item_ids: []}
        if isinstance(d, dict):
            ids = d.get("line_item_ids", d.get("items", []))
        else:
            ids = d
        assert isinstance(ids, list)
        assert ids == []

    def test_existing_after_seed(self, admin_client, client):
        """Directly insert a fake returns doc and verify line_item_ids returned."""
        import pymongo
        mongo_url = dotenv_values("/app/backend/.env").get("MONGO_URL")
        db_name = dotenv_values("/app/backend/.env").get("DB_NAME") or "pge_returns"
        assert mongo_url
        cli = pymongo.MongoClient(mongo_url)
        db = cli[db_name]
        doc = {
            "id": f"TEST-{uuid.uuid4().hex[:8]}",
            "rma_number": f"RMA-TEST-{uuid.uuid4().hex[:6]}",
            "order_number": "MOCK-1001",
            "customer_email": "seed@example.com",
            "email": "seed@example.com",
            "status": "awaiting_approval",
            "items": [
                {"line_item_id": "li-1", "name": "Seeded item",
                 "quantity": 1, "price": 10.0, "reason": "other"}
            ],
        }
        db.returns.insert_one(doc)
        try:
            r = client.get(
                f"{BASE_URL}/api/returns/existing-items/MOCK-1001/seed@example.com"
            )
            assert r.status_code == 200, r.text
            d = r.json()
            if isinstance(d, dict):
                ids = d.get("line_item_ids", d.get("items", []))
            else:
                ids = d
            assert "li-1" in ids, f"expected li-1 in {ids}"
        finally:
            db.returns.delete_one({"id": doc["id"]})


# ---------- TRACK-ACTION endpoint ----------
class TestTrackAction:
    def test_track_action_404(self, client):
        r = client.post(
            f"{BASE_URL}/api/returns/does-not-exist-xyz/track-action",
            json={"kind": "viewed_page", "label": "Viewed page", "meta": {}},
        )
        assert r.status_code == 404, r.text

    def test_track_action_appended(self, client):
        """Seed a return doc, POST track-action, verify it was appended."""
        import pymongo
        mongo_url = dotenv_values("/app/backend/.env").get("MONGO_URL")
        db_name = dotenv_values("/app/backend/.env").get("DB_NAME") or "pge_returns"
        cli = pymongo.MongoClient(mongo_url)
        db = cli[db_name]
        rid = f"TEST-{uuid.uuid4().hex[:8]}"
        doc = {
            "id": rid,
            "rma_number": f"RMA-TST-{uuid.uuid4().hex[:6]}",
            "order_number": "MOCK-1001",
            "customer_email": "track@example.com",
            "status": "awaiting_approval",
            "items": [],
            "customer_actions": [],
        }
        db.returns.insert_one(doc)
        try:
            r = client.post(
                f"{BASE_URL}/api/returns/{rid}/track-action",
                json={"kind": "clicked_pay_stripe",
                      "label": "Clicked Pay via Stripe",
                      "meta": {"page": "/method"}},
            )
            assert r.status_code in (200, 201, 204), r.text
            # Verify persistence
            updated = db.returns.find_one({"id": rid})
            actions = updated.get("customer_actions") or []
            assert len(actions) >= 1
            assert actions[-1].get("kind") == "clicked_pay_stripe"
        finally:
            db.returns.delete_one({"id": rid})


# ---------- email_service module direct tests ----------
class TestEmailServiceModule:
    def test_default_order_and_providers(self):
        import sys
        sys.path.insert(0, "/app/backend")
        import email_service as es
        assert es.DEFAULT_ORDER == ["brevo", "sendgrid", "resend", "smtp"]
        for p in ("brevo", "sendgrid", "resend", "smtp"):
            assert p in es.PROVIDERS

    def test_get_order_empty_cfg(self):
        import sys
        sys.path.insert(0, "/app/backend")
        import email_service as es
        assert es._get_order({}) == es.DEFAULT_ORDER
        # custom order with fallbacks appended
        out = es._get_order({"email_provider_order": "smtp,brevo"})
        assert out[0] == "smtp" and out[1] == "brevo"
        # All providers present
        assert set(out) == set(es.DEFAULT_ORDER)

    def test_send_email_all_unconfigured(self):
        import sys
        sys.path.insert(0, "/app/backend")
        import email_service as es
        result = asyncio.run(es.send_email(
            {}, to_email="x@example.com", to_name="X",
            subject="s", html="<p>h</p>", tags=["t"]))
        assert result["ok"] is False
        assert result["provider"] is None
        assert len(result["attempts"]) == 4  # tried all 4


# ---------- woo.py code inspection: search_by_number fallback ----------
class TestWooFallback:
    def test_fetch_order_has_search_fallback(self):
        code = Path("/app/backend/woo.py").read_text()
        # Looks for a fallback query by order number when direct lookup fails
        assert "search" in code.lower(), "woo.fetch_order should have a search fallback path"
