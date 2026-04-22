"""Multi-provider email service with automatic fallback.

Providers (in preferred order, configurable via `email_provider_order`):
    brevo → sendgrid → resend → smtp

Every provider is fail-safe: if unconfigured or fails, we log and try the next.
Returns a tuple (ok: bool, provider_used: str|None, attempts: list[dict]).

Attachments (optional): list of dicts with keys:
    - filename: str
    - content_type: str (e.g. "application/pdf", "image/png")
    - content_base64: str (raw base64 content, no data: prefix)
"""
import base64
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Dict, Any, Tuple, List, Optional
import httpx

log = logging.getLogger("email_service")

DEFAULT_ORDER = ["brevo", "sendgrid", "resend", "smtp"]


def _normalize_attachments(attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Drop any entries missing filename or content; ensure keys are present."""
    if not attachments:
        return []
    out = []
    for a in attachments:
        if not a:
            continue
        fn = (a.get("filename") or "").strip()
        ct = (a.get("content_type") or "application/octet-stream").strip()
        b64 = (a.get("content_base64") or "").strip()
        if not fn or not b64:
            continue
        out.append({"filename": fn, "content_type": ct, "content_base64": b64})
    return out


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


async def _brevo(cfg: Dict[str, str], to_email: str, to_name: str,
                 subject: str, html: str, tags: List[str],
                 attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    key = (cfg.get("brevo_api_key") or "").strip()
    from_email = (cfg.get("from_email") or "").strip()
    from_name = (cfg.get("from_name") or cfg.get("store_name") or "Returns").strip()
    if not key or not from_email:
        return False, "not configured"
    try:
        payload: Dict[str, Any] = {
            "sender": {"name": from_name, "email": from_email},
            "to": [{"email": to_email, "name": to_name or to_email}],
            "subject": subject, "htmlContent": html, "tags": tags,
        }
        if attachments:
            payload["attachment"] = [
                {"name": a["filename"], "content": a["content_base64"]}
                for a in attachments
            ]
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as c:
            r = await c.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": key, "content-type": "application/json",
                         "accept": "application/json"},
                json=payload,
            )
        if 200 <= r.status_code < 300:
            return True, f"ok · id={r.json().get('messageId','?')}"
        return False, f"{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"error: {e}"


async def _sendgrid(cfg: Dict[str, str], to_email: str, to_name: str,
                    subject: str, html: str, tags: List[str],
                    attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    key = (cfg.get("sendgrid_api_key") or "").strip()
    from_email = (cfg.get("from_email") or "").strip()
    from_name = (cfg.get("from_name") or cfg.get("store_name") or "Returns").strip()
    if not key or not from_email:
        return False, "not configured"
    try:
        payload: Dict[str, Any] = {
            "personalizations": [{"to": [{"email": to_email, "name": to_name or to_email}]}],
            "from": {"email": from_email, "name": from_name},
            "subject": subject,
            "content": [{"type": "text/html", "value": html}],
            "categories": tags,
        }
        if attachments:
            payload["attachments"] = [
                {
                    "content": a["content_base64"],
                    "filename": a["filename"],
                    "type": a["content_type"],
                    "disposition": "attachment",
                }
                for a in attachments
            ]
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as c:
            r = await c.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
                json=payload,
            )
        if 200 <= r.status_code < 300:
            return True, f"ok · {r.status_code}"
        return False, f"{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"error: {e}"


async def _resend(cfg: Dict[str, str], to_email: str, to_name: str,
                  subject: str, html: str, tags: List[str],
                  attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    key = (cfg.get("resend_api_key") or "").strip()
    from_email = (cfg.get("from_email") or "").strip()
    from_name = (cfg.get("from_name") or cfg.get("store_name") or "Returns").strip()
    if not key or not from_email:
        return False, "not configured"
    try:
        payload: Dict[str, Any] = {
            "from": f"{from_name} <{from_email}>",
            "to": [to_email],
            "subject": subject,
            "html": html,
            "tags": [{"name": "type", "value": tags[0]}] if tags else [],
        }
        if attachments:
            payload["attachments"] = [
                {"filename": a["filename"], "content": a["content_base64"]}
                for a in attachments
            ]
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as c:
            r = await c.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
                json=payload,
            )
        if 200 <= r.status_code < 300:
            return True, f"ok · id={r.json().get('id','?')}"
        return False, f"{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"error: {e}"


async def _smtp(cfg: Dict[str, str], to_email: str, to_name: str,
                subject: str, html: str, tags: List[str],
                attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    host = (cfg.get("smtp_host") or "").strip()
    user = (cfg.get("smtp_user") or "").strip()
    pwd = (cfg.get("smtp_pass") or "").strip()
    port_raw = (cfg.get("smtp_port") or "587").strip()
    from_email = (cfg.get("from_email") or user).strip()
    from_name = (cfg.get("from_name") or cfg.get("store_name") or "Returns").strip()
    if not host or not user or not pwd or not from_email:
        return False, "not configured"
    try:
        port = int(port_raw or "587")
        # Use "mixed" when we have attachments, "alternative" otherwise.
        outer = MIMEMultipart("mixed" if attachments else "alternative")
        outer["Subject"] = subject
        outer["From"] = f"{from_name} <{from_email}>"
        outer["To"] = to_email
        if attachments:
            body = MIMEMultipart("alternative")
            body.attach(MIMEText(html, "html"))
            outer.attach(body)
            for a in attachments:
                part = MIMEBase(*(a["content_type"].split("/", 1) if "/" in a["content_type"] else ("application", "octet-stream")))
                part.set_payload(base64.b64decode(a["content_base64"]))
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{a["filename"]}"')
                outer.attach(part)
        else:
            outer.attach(MIMEText(html, "html"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=25) as s:
                s.login(user, pwd)
                s.sendmail(from_email, [to_email], outer.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=25) as s:
                s.starttls()
                s.login(user, pwd)
                s.sendmail(from_email, [to_email], outer.as_string())
        return True, f"ok · {host}:{port}"
    except Exception as e:
        return False, f"error: {e}"


PROVIDERS = {
    "brevo": _brevo,
    "sendgrid": _sendgrid,
    "resend": _resend,
    "smtp": _smtp,
}


def _get_order(cfg: Dict[str, str]) -> List[str]:
    raw = (cfg.get("email_provider_order") or "").strip().lower()
    if not raw:
        return DEFAULT_ORDER
    order = [p.strip() for p in raw.split(",") if p.strip() in PROVIDERS]
    # Append any missing providers so everything is still a fallback option
    for p in DEFAULT_ORDER:
        if p not in order:
            order.append(p)
    return order


async def send_email(cfg: Dict[str, str], *, to_email: str, to_name: str,
                     subject: str, html: str, tags: List[str],
                     attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Try providers in order; return summary dict with attempts."""
    order = _get_order(cfg)
    norm_attachments = _normalize_attachments(attachments)
    attempts: List[Dict[str, Any]] = []
    for name in order:
        fn = PROVIDERS[name]
        ok, msg = await fn(cfg, to_email, to_name, subject, html, tags, norm_attachments or None)
        attempts.append({"provider": name, "ok": ok, "message": msg})
        if ok:
            log.info("Email sent via %s to %s (attachments=%d)", name, to_email, len(norm_attachments))
            return {"ok": True, "provider": name, "attempts": attempts}
        log.info("Email provider %s skipped/failed: %s", name, msg)
    return {"ok": False, "provider": None, "attempts": attempts}


# ---- Templated wrappers (drop-in replacements for brevo_service.*) ----

async def send_return_initiated(cfg, *, to_email, to_name, rma_number, order_number,
                                method_display_label="", refund_amount=0.0,
                                refund_deduction=0.0, refund_net=0.0,
                                currency="GBP") -> Dict[str, Any]:
    store_name = cfg.get("store_name") or "Returns"
    support_email = cfg.get("support_email") or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""

    def money(v):
        try:
            return f"{currency} {float(v):,.2f}"
        except Exception:
            return f"{currency} {v}"

    deduction_block = ""
    if refund_deduction and refund_deduction > 0:
        deduction_block = f"""
          <tr><td style="padding:4px 0;color:#78716C">Refund subtotal</td><td align="right">{money(refund_amount)}</td></tr>
          <tr><td style="padding:4px 0;color:#78716C">Label cost (deducted)</td><td align="right">− {money(refund_deduction)}</td></tr>
          <tr><td style="padding:4px 0;color:#78716C"><strong>You'll receive</strong></td><td align="right"><strong>{money(refund_net)}</strong></td></tr>
        """

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#78716C">Return received</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">We've got your return, {(to_name or '').split()[0] if to_name else 'there'}.</h1>
      <p style="margin:0 0 16px;line-height:1.6">Thanks for starting a return with {store_name}. Reference:</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA number</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
        {f'<tr><td style="padding:4px 0;color:#78716C">Return method</td><td align="right">{method_display_label}</td></tr>' if method_display_label else ''}
        {deduction_block}
      </table>
      <p style="margin:16px 0;line-height:1.6">
        If you chose to pay for a label or deduct from your refund, your shipping label is
        attached in the next email. If you requested a free label, our team will review and email you shortly.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"{store_name} · Return received (RMA {rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["return_initiated"])


async def send_label_ready(cfg, *, to_email, to_name, rma_number, order_number,
                           tracking_number, label_url) -> Dict[str, Any]:
    store_name = cfg.get("store_name") or "Returns"
    support_email = cfg.get("support_email") or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""
    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#78716C">Your label is ready</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Drop it off and you're done.</h1>
      <p style="margin:0 0 16px;line-height:1.6">Your return label for RMA <strong>{rma_number}</strong> (order #{order_number}) is ready.</p>
      <p style="margin:16px 0">
        <a href="{label_url}" style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;text-decoration:none;font-weight:500">Download label (PDF)</a>
      </p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">Tracking number</td><td align="right">{tracking_number}</td></tr>
      </table>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"{store_name} · Your return label is ready (RMA {rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["label_ready"])


async def send_admin_new_return(cfg, *, rma_number, order_number, customer_name,
                                customer_email, method_display_label, items,
                                refund_amount, refund_deduction, refund_net,
                                customer_note="", currency="GBP") -> Dict[str, Any]:
    """Notify the store admin that a new return has been opened."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = cfg.get("support_email") or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""
    admin_to = (cfg.get("admin_notification_email") or "").strip()
    if not admin_to:
        return {"ok": False, "provider": None,
                "attempts": [{"provider": "admin_notify", "ok": False,
                              "message": "admin_notification_email not configured"}]}

    def money(v):
        try:
            return f"{currency} {float(v):,.2f}"
        except Exception:
            return f"{currency} {v}"

    items_rows = "".join(
        f"""<tr>
              <td style="padding:6px 0;border-top:1px solid #E5E5E0">{i.get('name','')}</td>
              <td style="padding:6px 0;border-top:1px solid #E5E5E0" align="center">{i.get('quantity',1)}</td>
              <td style="padding:6px 0;border-top:1px solid #E5E5E0">{(i.get('reason') or '').replace('_',' ')}</td>
              <td style="padding:6px 0;border-top:1px solid #E5E5E0" align="right">{money(float(i.get('price') or 0) * int(i.get('quantity') or 1))}</td>
            </tr>"""
        for i in (items or [])
    )

    deduction_row = ""
    if refund_deduction and refund_deduction > 0:
        deduction_row = f"""
          <tr><td style="padding:4px 0;color:#B45309"><strong>Shipping deducted from refund</strong></td>
              <td align="right"><strong>− {money(refund_deduction)}</strong></td></tr>
          <tr><td style="padding:4px 0;color:#78716C">Net refund owed</td>
              <td align="right">{money(refund_net)}</td></tr>
        """

    note_block = ""
    if customer_note:
        note_block = f"""
          <div style="margin-top:16px;padding:12px;border-left:3px solid #0A0A0A;background:#F5F5F0">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#78716C">Customer note</div>
            <div style="margin-top:4px">{customer_note}</div>
          </div>"""

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#78716C">New return opened</div>
      <h1 style="margin:8px 0 16px;font-size:22px;font-weight:500">RMA {rma_number} · Order #{order_number}</h1>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">Customer</td><td align="right">{customer_name}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Email</td><td align="right">{customer_email}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Method chosen</td><td align="right">{method_display_label}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Refund subtotal</td><td align="right">{money(refund_amount)}</td></tr>
        {deduction_row}
      </table>

      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#78716C;margin-bottom:8px">Items</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">
        <thead><tr>
          <th align="left" style="padding:6px 0;color:#78716C;font-weight:500">Product</th>
          <th align="center" style="padding:6px 0;color:#78716C;font-weight:500">Qty</th>
          <th align="left" style="padding:6px 0;color:#78716C;font-weight:500">Reason</th>
          <th align="right" style="padding:6px 0;color:#78716C;font-weight:500">Value</th>
        </tr></thead>
        <tbody>{items_rows}</tbody>
      </table>

      {note_block}

      <p style="margin:24px 0 0;font-size:12px;color:#78716C">Open your admin dashboard to approve, reject, or mark this return refunded.</p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"[New return] RMA {rma_number} · Order #{order_number} · {customer_name}"
    return await send_email(cfg, to_email=admin_to, to_name=f"{store_name} Admin",
                            subject=subject, html=html, tags=["admin_new_return"])


async def send_admin_label_failure(cfg, *, rma_number, return_id, error_message,
                                   method_display_label, amount, currency,
                                   customer_name, customer_email,
                                   rate_provider="", rate_servicelevel="") -> Dict[str, Any]:
    """Alert the store admin that a return label could not be purchased
    (e.g. Shippo 'shipping provider not found', Easyship auth error, etc.).
    The customer has usually already been charged or confirmed at this point,
    so the admin needs to manually generate a label and email it to them.
    """
    store_name = cfg.get("store_name") or "Returns"
    support_email = cfg.get("support_email") or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""
    admin_to = (cfg.get("admin_notification_email") or "").strip()
    if not admin_to:
        return {"ok": False, "provider": None,
                "attempts": [{"provider": "admin_notify", "ok": False,
                              "message": "admin_notification_email not configured"}]}

    def money(v):
        try:
            return f"{currency} {float(v):,.2f}"
        except Exception:
            return f"{currency} {v}"

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#B91C1C">Action required · Label purchase failed</div>
      <h1 style="margin:8px 0 16px;font-size:22px;font-weight:500">RMA {rma_number}</h1>
      <p style="font-size:13px;margin:0 0 16px">
        A return label could not be generated automatically. The customer is
        waiting on a label — please buy one manually from your carrier dashboard
        and email it to them, or reach out via your admin portal.
      </p>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">Customer</td><td align="right">{customer_name}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Email</td><td align="right">{customer_email}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Method</td><td align="right">{method_display_label}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Quoted rate</td><td align="right">{rate_provider} {rate_servicelevel}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Amount</td><td align="right">{money(amount)}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Return ID</td><td align="right"><span style="font-family:monospace">{return_id}</span></td></tr>
      </table>

      <div style="margin-top:16px;padding:12px;border-left:3px solid #B91C1C;background:#FEF2F2">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#B91C1C">Carrier error</div>
        <div style="margin-top:4px;font-family:monospace;font-size:12px;white-space:pre-wrap">{error_message or 'Unknown error'}</div>
      </div>

      <p style="margin:24px 0 0;font-size:12px;color:#78716C">
        Common causes: carrier not connected in Shippo/Easyship dashboard,
        wrong API key (test vs live), insufficient account balance, or an
        expired rate_id. Check the provider dashboard, then process the label
        manually and forward it to the customer.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"[ACTION REQUIRED] Label purchase failed · RMA {rma_number} · {customer_name}"
    return await send_email(cfg, to_email=admin_to, to_name=f"{store_name} Admin",
                            subject=subject, html=html, tags=["admin_label_failure"])





async def send_free_label_approved(cfg, *, to_email, to_name, rma_number, order_number,
                                   admin_note: str = "",
                                   attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Email the customer that their free-label request was approved. The shipping
    label (uploaded by admin) is attached to this email. An optional admin note
    is included in the body."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or "cs@pgelimited.com"
    logo_url = cfg.get("logo_url") or ""

    note_block = ""
    if admin_note:
        note_block = f"""
          <div style="margin:20px 0;padding:14px;border-left:3px solid #0A0A0A;background:#F5F5F0">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#78716C">Note from our returns team</div>
            <div style="margin-top:6px;white-space:pre-wrap;line-height:1.55">{admin_note}</div>
          </div>"""

    attach_list_html = ""
    if attachments:
        names = ", ".join(a.get("filename", "attachment") for a in attachments)
        attach_list_html = f"""<p style="margin:14px 0;font-size:13px;color:#78716C">Attached: {names}</p>"""

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#047857">Return approved</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Good news — your free label is approved.</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        Your free-label request for <strong>RMA {rma_number}</strong> (order #{order_number}) has been approved.
        The return shipping label is attached to this email — please print it and drop the parcel at your nearest carrier.
      </p>
      {note_block}
      {attach_list_html}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA number</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
      </table>
      <p style="margin:18px 0 0;line-height:1.65;font-size:13px;color:#78716C">
        Questions about your label or the return? Email us at
        <a href="mailto:{support_email}" style="color:#0A0A0A;text-decoration:underline">{support_email}</a>.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"{store_name} · Free return label approved (RMA {rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["free_label_approved"], attachments=attachments)


async def send_return_rejected(cfg, *, to_email, to_name, rma_number, order_number,
                               admin_note: str = "",
                               attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Email the customer that their return was rejected. Includes the admin's
    reason (note) and any attached evidence image(s)."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or "cs@pgelimited.com"
    logo_url = cfg.get("logo_url") or ""

    reason_block = ""
    if admin_note:
        reason_block = f"""
          <div style="margin:20px 0;padding:14px;border-left:3px solid #B91C1C;background:#FEF2F2">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#B91C1C">Reason from our returns team</div>
            <div style="margin-top:6px;white-space:pre-wrap;line-height:1.55">{admin_note}</div>
          </div>"""

    attach_list_html = ""
    if attachments:
        names = ", ".join(a.get("filename", "attachment") for a in attachments)
        attach_list_html = f"""<p style="margin:14px 0;font-size:13px;color:#78716C">Attached for your reference: {names}</p>"""

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#B91C1C">Return not approved</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Update on your return, {(to_name or '').split()[0] if to_name else 'there'}.</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        We've reviewed your return request <strong>RMA {rma_number}</strong> (order #{order_number})
        and unfortunately we're unable to approve it at this time.
      </p>
      {reason_block}
      {attach_list_html}
      <p style="margin:18px 0 0;line-height:1.65;font-size:14px">
        If you believe this decision is a mistake or you'd like to discuss it further,
        please email us at <a href="mailto:{support_email}" style="color:#0A0A0A;text-decoration:underline">{support_email}</a> — we're happy to help.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"{store_name} · Return request update (RMA {rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["return_rejected"], attachments=attachments)



async def send_store_credit_issued(cfg, *, to_email, to_name, rma_number, order_number,
                                   coupon_code: str, coupon_amount: float,
                                   currency: str = "GBP", bonus_percent: float = 0.0,
                                   expires_on: Optional[str] = None) -> Dict[str, Any]:
    """Email the customer their store-credit coupon code + expiry."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or "cs@pgelimited.com"
    logo_url = cfg.get("logo_url") or ""

    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get((currency or "").upper(), "")
    amount_fmt = f"{symbol}{coupon_amount:.2f}" if symbol else f"{coupon_amount:.2f} {currency}"
    bonus_line = ""
    if bonus_percent and bonus_percent > 0:
        bonus_line = (f"<p style=\"margin:0 0 12px;line-height:1.65\">That's "
                      f"<strong>{bonus_percent:g}% more</strong> than a cash refund — "
                      f"our thank-you for picking store credit.</p>")
    expires_line = ""
    if expires_on:
        expires_line = (f"<tr><td style=\"padding:4px 0;color:#78716C\">Expires</td>"
                        f"<td align=\"right\">{expires_on}</td></tr>")

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#047857">Store credit issued</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Your store credit is ready.</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        For return <strong>RMA {rma_number}</strong> (order #{order_number}) you chose store credit
        instead of a cash refund. Here's your code — use it at checkout.
      </p>
      {bonus_line}
      <div style="margin:22px 0;padding:18px;border:1px dashed #0A0A0A;background:#F5F5F0;text-align:center">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#78716C;margin-bottom:6px">Your code</div>
        <div style="font-family:ui-monospace,Menlo,monospace;font-size:22px;font-weight:600;letter-spacing:0.08em">{coupon_code}</div>
        <div style="margin-top:10px;font-size:14px;color:#78716C">Value: <strong style="color:#0A0A0A">{amount_fmt}</strong></div>
      </div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Credit amount</td><td align="right">{amount_fmt}</td></tr>
        {expires_line}
      </table>
      <p style="margin:18px 0 0;line-height:1.65;font-size:13px;color:#78716C">
        Single-use, locked to this email address. Apply it at the checkout on our store.
        For any questions about this code or your return, email our customer service team at
        <a href="mailto:{support_email}" style="color:#0A0A0A;text-decoration:underline">{support_email}</a>
        — please do not reply to this automated message.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner)
    subject = f"{store_name} · Your store credit code {coupon_code}"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["store_credit_issued"])
