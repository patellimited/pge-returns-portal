"""Iteration 5: tests for admin delete/archive, customer proof upload,
new return reason values, and refund-block logic.

Bypasses Woo by inserting return docs directly into MongoDB via motor.
"""
import os
import io
import uuid
import asyncio
import base64
import pytest
import requests
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAfbLI3wAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(MONGO_URL)
    try:
        yield client[DB_NAME]
    finally:
        client.close()


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _seed_doc(return_id, *, status="awaiting_payment", refunded=False,
              archived=False, line_item_id="ITEM-1", order_number="999",
              email="buyer@test.com"):
    addr = {"name": "X", "street1": "1 St", "city": "C", "state": "CA",
            "zip": "90001", "country": "US", "phone": "", "email": ""}
    return {
        "id": return_id,
        "rma_number": f"RMA-{return_id[:6].upper()}",
        "order_id": "9001",
        "order_number": order_number,
        "email": email,
        "customer_name": "Buyer",
        "items": [{"line_item_id": line_item_id, "name": "Widget",
                   "quantity": 1, "price": 10.0, "image": "",
                   "reason": "defective", "notes": ""}],
        "method": "deduct_from_refund",
        "method_display_label": "x",
        "status": status,
        "customer_note": "", "admin_note": "",
        "return_address": addr, "warehouse_address": addr,
        "refund_amount": 10.0, "refund_net": 10.0, "refund_deduction": 0.0,
        "label_cost": 0.0, "paid": False, "refunded": refunded,
        "archived": archived,
        "customer_actions": [], "customer_proof_photos": [],
        "tracking_updates": [], "email_log": [], "emails_finalized": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_models_accept_new_reasons(db):
    """ReturnReason literal accepts all new values."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from models import ReturnItem
    new_reasons = ["defective", "damaged_outer_box", "wrong_item",
                   "missing_parts", "no_longer_needed", "accidental_order",
                   "better_price", "poor_performance", "incompatible"]
    for r in new_reasons:
        item = ReturnItem(line_item_id="X", name="N", quantity=1,
                          price=1.0, reason=r)
        assert item.reason == r


@pytest.mark.asyncio
async def test_proof_upload_happy_path(db, admin_headers):
    rid = f"TEST-PROOF-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(rid))
    try:
        files = [("files", (f"p{i}.png", io.BytesIO(PNG_1x1), "image/png"))
                 for i in range(2)]
        r = requests.post(f"{API}/returns/{rid}/proof", files=files)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        # admin can stream proof[0]
        r2 = requests.get(f"{API}/admin/returns/{rid}/proof/0",
                          headers=admin_headers)
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("image/")
        # idx out of range
        r3 = requests.get(f"{API}/admin/returns/{rid}/proof/9",
                          headers=admin_headers)
        assert r3.status_code == 404
        # admin auth required
        r4 = requests.get(f"{API}/admin/returns/{rid}/proof/0")
        assert r4.status_code in (401, 403)
    finally:
        await db.returns.delete_one({"id": rid})


@pytest.mark.asyncio
async def test_proof_rejects_non_image(db):
    rid = f"TEST-PROOF-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(rid))
    try:
        files = [("files", ("note.txt", io.BytesIO(b"hello"), "text/plain"))]
        r = requests.post(f"{API}/returns/{rid}/proof", files=files)
        assert r.status_code == 400, r.text
    finally:
        await db.returns.delete_one({"id": rid})


@pytest.mark.asyncio
async def test_proof_rejects_oversized(db):
    rid = f"TEST-PROOF-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(rid))
    try:
        big = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024 + 10)
        files = [("files", ("big.png", io.BytesIO(big), "image/png"))]
        r = requests.post(f"{API}/returns/{rid}/proof", files=files)
        assert r.status_code == 413, r.text
    finally:
        await db.returns.delete_one({"id": rid})


@pytest.mark.asyncio
async def test_proof_rejects_more_than_3(db):
    rid = f"TEST-PROOF-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(rid))
    try:
        files = [("files", (f"p{i}.png", io.BytesIO(PNG_1x1), "image/png"))
                 for i in range(4)]
        r = requests.post(f"{API}/returns/{rid}/proof", files=files)
        assert r.status_code == 400, r.text
    finally:
        await db.returns.delete_one({"id": rid})


@pytest.mark.asyncio
async def test_archive_unarchive_and_listing(db, admin_headers):
    rid = f"TEST-ARCH-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(rid))
    try:
        r = requests.post(f"{API}/admin/returns/{rid}/archive",
                          headers=admin_headers)
        assert r.status_code == 200 and r.json()["archived"] is True
        # default list (archived=false) should NOT include it
        r2 = requests.get(f"{API}/admin/returns", headers=admin_headers)
        assert r2.status_code == 200
        assert all(x["id"] != rid for x in r2.json())
        # archived=true list MUST include it
        r3 = requests.get(f"{API}/admin/returns?archived=true",
                          headers=admin_headers)
        assert any(x["id"] == rid for x in r3.json())
        # unarchive
        r4 = requests.post(f"{API}/admin/returns/{rid}/unarchive",
                           headers=admin_headers)
        assert r4.status_code == 200 and r4.json()["archived"] is False
        r5 = requests.get(f"{API}/admin/returns", headers=admin_headers)
        assert any(x["id"] == rid for x in r5.json())
    finally:
        await db.returns.delete_one({"id": rid})


@pytest.mark.asyncio
async def test_delete_return(db, admin_headers):
    rid = f"TEST-DEL-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(rid))
    try:
        # Auth required
        r0 = requests.delete(f"{API}/admin/returns/{rid}")
        assert r0.status_code in (401, 403)
        r = requests.delete(f"{API}/admin/returns/{rid}",
                            headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True
        # GET /api/returns/{id} -> 404
        rg = requests.get(f"{API}/returns/{rid}")
        assert rg.status_code == 404
        # delete missing -> 404
        rmiss = requests.delete(f"{API}/admin/returns/does-not-exist",
                                headers=admin_headers)
        assert rmiss.status_code == 404
    finally:
        await db.returns.delete_one({"id": rid})


@pytest.mark.asyncio
async def test_admin_endpoints_require_auth():
    rid = "irrelevant"
    assert requests.post(f"{API}/admin/returns/{rid}/archive").status_code in (401, 403)
    assert requests.post(f"{API}/admin/returns/{rid}/unarchive").status_code in (401, 403)
    assert requests.delete(f"{API}/admin/returns/{rid}").status_code in (401, 403)
    assert requests.get(f"{API}/admin/returns").status_code in (401, 403)


@pytest.mark.asyncio
async def test_refund_block_logic_directly(db):
    """Simulate the refund-block by exercising _already_returned_line_ids
    + refunded_q logic via direct DB seed and a count_documents check.
    Avoids depending on the live Woo lookup."""
    seeded_id = f"TEST-REF-{uuid.uuid4().hex[:8]}"
    await db.returns.insert_one(_seed_doc(
        seeded_id, status="refunded", refunded=True,
        line_item_id="ITEM-1", order_number="999",
        email="buyer@test.com"))
    try:
        # Mirror the server's refunded_q
        refunded_q = {
            "order_number": "999",
            "email": "buyer@test.com",
            "status": "refunded",
            "items.line_item_id": {"$in": ["ITEM-1"]},
        }
        n = await db.returns.count_documents(refunded_q)
        assert n == 1
        # And confirm endpoint also reports the line_item_id as already-returned
        r = requests.get(
            f"{API}/returns/existing-items/999/buyer@test.com")
        assert r.status_code == 200
        assert "ITEM-1" in r.json()["line_item_ids"]
    finally:
        await db.returns.delete_one({"id": seeded_id})
