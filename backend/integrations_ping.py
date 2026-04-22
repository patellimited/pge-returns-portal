"""Live validation pings against each integration's production API.
Called when admin clicks 'Test connections' or right after saving settings.
Returns a dict of integration -> {ok: bool, message: str}.
"""
import httpx
from typing import Dict, Any


async def _ping_stripe(secret_key: str) -> Dict[str, Any]:
    if not secret_key:
        return {"ok": False, "message": "No key set"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.stripe.com/v1/account",
                            headers={"Authorization": f"Bearer {secret_key}"})
        if r.status_code == 200:
            data = r.json()
            mode = "live" if secret_key.startswith("sk_live_") else "test"
            name = data.get("business_profile", {}).get("name") or data.get("email") or data.get("id")
            return {"ok": True, "message": f"Connected ({mode}) · {name}"}
        return {"ok": False, "message": f"{r.status_code}: {r.json().get('error', {}).get('message', r.text[:120])}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}


async def _ping_shippo(token: str) -> Dict[str, Any]:
    if not token:
        return {"ok": False, "message": "No token set"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.goshippo.com/addresses/?results=1",
                            headers={"Authorization": f"ShippoToken {token}"})
        if r.status_code == 200:
            mode = "live" if token.startswith("shippo_live_") else "test"
            return {"ok": True, "message": f"Connected ({mode})"}
        return {"ok": False, "message": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}


async def _ping_woo(store: str, key: str, secret: str) -> Dict[str, Any]:
    if not (store and key and secret):
        return {"ok": False, "message": "Missing store URL / keys"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{store.rstrip('/')}/wp-json/wc/v3/system_status",
                            auth=(key, secret))
        if r.status_code == 200:
            env = r.json().get("environment", {})
            return {"ok": True, "message": f"Connected · WC {env.get('version','?')}"}
        if r.status_code == 401:
            return {"ok": False, "message": "Unauthorized — check consumer key/secret"}
        return {"ok": False, "message": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}


async def _ping_brevo(api_key: str) -> Dict[str, Any]:
    if not api_key:
        return {"ok": False, "message": "Not configured (emails disabled)"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.brevo.com/v3/account",
                            headers={"api-key": api_key, "accept": "application/json"})
        if r.status_code == 200:
            data = r.json()
            email = data.get("email") or "ok"
            return {"ok": True, "message": f"Connected · {email}"}
        return {"ok": False, "message": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}


async def _ping_sendgrid(api_key: str) -> Dict[str, Any]:
    if not api_key:
        return {"ok": False, "message": "Not configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.sendgrid.com/v3/user/account",
                            headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code == 200:
            return {"ok": True, "message": "Connected"}
        return {"ok": False, "message": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}


async def _ping_resend(api_key: str) -> Dict[str, Any]:
    if not api_key:
        return {"ok": False, "message": "Not configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.resend.com/domains",
                            headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code in (200, 401):
            # 401 = key invalid, 200 = key valid. Any 200 response => ok.
            return ({"ok": True, "message": "Connected"} if r.status_code == 200
                    else {"ok": False, "message": "Unauthorized — check key"})
        return {"ok": False, "message": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Network error: {e}"}


async def test_all(cfg: Dict[str, str]) -> Dict[str, Any]:
    import easyship_service
    stripe_res = await _ping_stripe(cfg.get("stripe_api_key", ""))
    shippo_res = await _ping_shippo(cfg.get("shippo_api_key", ""))
    woo_res = await _ping_woo(
        cfg.get("wc_store_url", ""),
        cfg.get("wc_consumer_key", ""),
        cfg.get("wc_consumer_secret", ""),
    )
    easyship_res = easyship_service.ping(cfg.get("easyship_api_key", ""))
    brevo_res = await _ping_brevo(cfg.get("brevo_api_key", ""))
    sendgrid_res = await _ping_sendgrid(cfg.get("sendgrid_api_key", ""))
    resend_res = await _ping_resend(cfg.get("resend_api_key", ""))
    return {
        "stripe": stripe_res,
        "shippo": shippo_res,
        "easyship": easyship_res,
        "woocommerce": woo_res,
        "brevo": brevo_res,
        "sendgrid": sendgrid_res,
        "resend": resend_res,
    }
