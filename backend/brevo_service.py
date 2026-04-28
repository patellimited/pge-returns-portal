"""Brevo v3 transactional email service (fail-safe).

Designed to NEVER raise — if the API key is missing or the API call fails,
it logs a warning and returns False so the calling return-flow continues.
"""
import logging
import httpx
from typing import Dict, Any, Optional

log = logging.getLogger("brevo")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


async def _send(api_key: str, payload: Dict[str, Any]) -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            r = await client.post(
                BREVO_URL,
                headers={"api-key": api_key, "content-type": "application/json", "accept": "application/json"},
                json=payload,
            )
        if 200 <= r.status_code < 300:
            log.info("Brevo email accepted (%s) messageId=%s", r.status_code, r.json().get("messageId"))
            return True
        log.warning("Brevo API error %s: %s", r.status_code, r.text[:400])
        return False
    except Exception as e:
        log.warning("Brevo send failed: %s", e)
        return False


def _should_send(cfg: Dict[str, str]) -> Optional[Dict[str, str]]:
    api_key = (cfg.get("brevo_api_key") or "").strip()
    from_email = (cfg.get("from_email") or "").strip()
    from_name = (cfg.get("from_name") or cfg.get("store_name") or "Returns").strip()
    if not api_key:
        log.info("Brevo API key not configured — skipping email.")
        return None
    if not from_email:
        log.info("Brevo From email not configured — skipping email.")
        return None
    return {"api_key": api_key, "from_email": from_email, "from_name": from_name}


def _base_html(store_name: str, support_email: str, logo_url: str, inner_html: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#111;background:#F5F5F0">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F5F0;padding:32px 0">
    <tr><td align="center">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #E5E5E0;padding:32px">
        <tr><td style="padding-bottom:24px;border-bottom:1px solid #E5E5E0">
          {f'<img src="{logo_url}" alt="{store_name}" style="max-height:36px" />' if logo_url else f'<div style="font-weight:600;font-size:18px">{store_name}</div>'}
        </td></tr>
        <tr><td style="padding:28px 0">{inner_html}</td></tr>
        <tr><td style="padding-top:24px;border-top:1px solid #E5E5E0;font-size:12px;color:#78716C">
          Questions? Email <a href="mailto:{support_email}" style="color:#111">{support_email}</a>.<br>
          — {store_name}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


async def send_return_initiated(cfg: Dict[str, str], *, to_email: str, to_name: str,
                                 rma_number: str, order_number: str) -> bool:
    """Email #1 — RMA created / return initiated."""
    conn = _should_send(cfg)
    if not conn:
        return False

    store_name = cfg.get("store_name") or "Returns"
    support_email = cfg.get("support_email") or conn["from_email"]
    logo_url = cfg.get("logo_url") or ""

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#78716C">Return received</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">We've got your return, {to_name.split()[0] if to_name else ''}.</h1>
      <p style="margin:0 0 16px;line-height:1.6">Thanks for starting a return with {store_name}. Here are your reference details:</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA number</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
      </table>
      <p style="margin:16px 0;line-height:1.6">
        If you chose to pay for a label or deduct from your refund, your shipping label is
        attached in the next email. If you requested a free label, our team will review and email you shortly.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    payload = {
        "sender": {"name": conn["from_name"], "email": conn["from_email"]},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": f"{store_name} · Return received ({rma_number})",
        "htmlContent": html,
        "tags": ["return_initiated"],
    }
    return await _send(conn["api_key"], payload)


async def send_label_ready(cfg: Dict[str, str], *, to_email: str, to_name: str,
                            rma_number: str, order_number: str,
                            tracking_number: str, label_url: str) -> bool:
    """Email #2 — label ready / shipping instructions."""
    conn = _should_send(cfg)
    if not conn:
        return False

    store_name = cfg.get("store_name") or "Returns"
    support_email = cfg.get("support_email") or conn["from_email"]
    logo_url = cfg.get("logo_url") or ""

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#78716C">Your label is ready</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Drop it off and you're done.</h1>
      <p style="margin:0 0 16px;line-height:1.6">Your return label for <strong>{rma_number}</strong> (order #{order_number}) is ready.</p>
      <p style="margin:16px 0">
        <a href="{label_url}" style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;text-decoration:none;font-weight:500">Download label (PDF)</a>
      </p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">Tracking number</td><td align="right">{tracking_number}</td></tr>
      </table>
      <ol style="line-height:1.8;padding-left:18px;margin:16px 0">
        <li>Print the label above.</li>
        <li>Pack the items securely and attach the label on the outside.</li>
        <li>Drop off at the carrier's nearest location.</li>
      </ol>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    payload = {
        "sender": {"name": conn["from_name"], "email": conn["from_email"]},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": f"{store_name} · Your return label is ready ({rma_number})",
        "htmlContent": html,
        "tags": ["label_ready"],
    }
    return await _send(conn["api_key"], payload)
