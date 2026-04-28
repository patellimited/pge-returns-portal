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


def _tracking_link(cfg: Dict[str, str], rma_number: str) -> str:
    """Build the public tracking URL for a given RMA, or "" if the portal
    URL setting isn't configured."""
    base = (cfg.get("portal_public_url") or "").strip().rstrip("/")
    if not base or not rma_number:
        return ""
    return f"{base}/track?rma={rma_number}"


def _smart_post_arrival_line(method: str, has_deduction: bool) -> str:
    """Method-specific copy for the "we'll process your X once the parcel
    arrives" line. Keeps every email speaking to the customer's actual
    chosen method instead of a generic "refund / store credit"."""
    m = (method or "").lower()
    if m == "store_credit":
        return ("We'll process your <strong>store credit</strong> once the parcel "
                "arrives at our warehouse and passes inspection.")
    if m == "deduct_from_refund" or has_deduction:
        return ("Your <strong>refund</strong> (minus the return shipping label cost) "
                "will be processed to your original payment method once the parcel "
                "arrives at our warehouse and passes inspection.")
    if m == "pay_stripe":
        return ("We'll process your <strong>refund</strong> to your card once the parcel "
                "arrives at our warehouse and passes inspection.")
    if m == "free_label":
        return ("We'll process your <strong>refund</strong> to your original payment method "
                "once the parcel arrives at our warehouse and passes inspection.")
    # Generic fallback (self_ship without explicit refund choice, etc.)
    return ("We'll process your refund or store credit once the parcel arrives at our "
            "warehouse and passes inspection.")


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


def _email_footer_html(cfg: Dict[str, str]) -> str:
    """Single source of truth for the footer block. Pulls store name,
    support email, year, and (optional) website URL from settings — every
    outgoing email rebrands instantly when the admin updates these.
    Renders inside the base template's footer cell."""
    from datetime import datetime
    store_name = (cfg.get("store_name") or "Returns").strip()
    support_email = ((cfg.get("support_email") or "").strip()
                     or (cfg.get("from_email") or "").strip())
    website_url = (cfg.get("portal_public_url") or "").strip()
    year = datetime.utcnow().year
    contact_line = ""
    if support_email:
        contact_line = (f'Questions? Email <a href="mailto:{support_email}" '
                        f'style="color:#111;text-decoration:underline">{support_email}</a>.<br>')
    site_line = ""
    if website_url:
        site_line = (f'<a href="{website_url}" style="color:#111;text-decoration:none">'
                     f'{website_url}</a> · ')
    return (f'{contact_line}'
            f'<span style="color:#9CA3AF">{site_line}© {year} {store_name}. All rights reserved.</span>')


def _base_html(store_name: str, support_email: str, logo_url: str, inner_html: str,
               cfg: Optional[Dict[str, str]] = None) -> str:
    """Base wrapper for every transactional email. The footer is built once
    here from `cfg` (when supplied) — individual templates should NOT add
    their own "Questions? Email …" line, as that would duplicate it."""
    if cfg is not None:
        footer_html = _email_footer_html(cfg)
    else:
        # Legacy callers that haven't been migrated yet — keep the old footer
        # intact so emails still render correctly.
        footer_html = (f'Questions? Email <a href="mailto:{support_email}" '
                       f'style="color:#111">{support_email}</a>.<br>— {store_name}')
    return f"""<!doctype html><html><body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#111;background:#F5F5F0">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F5F0;padding:32px 0">
    <tr><td align="center">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #E5E5E0;padding:32px">
        <tr><td style="padding-bottom:24px;border-bottom:1px solid #E5E5E0">
          {f'<img src="{logo_url}" alt="{store_name}" style="max-height:36px" />' if logo_url else f'<div style="font-weight:600;font-size:18px">{store_name}</div>'}
        </td></tr>
        <tr><td style="padding:28px 0">{inner_html}</td></tr>
        <tr><td style="padding-top:24px;border-top:1px solid #E5E5E0;font-size:12px;color:#78716C">
          {footer_html}
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
                                method_display_label="", method: str = "",
                                refund_amount=0.0,
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

    # Smart copy: tell the customer exactly what they'll get back, in their
    # chosen method's language.
    smart_line = _smart_post_arrival_line(method, refund_deduction and refund_deduction > 0)

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
      <p style="margin:16px 0;line-height:1.6">{smart_line}</p>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Return received ({rma_number})"
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
      <p style="margin:0 0 16px;line-height:1.6">Your return label for <strong>{rma_number}</strong> (order #{order_number}) is ready.</p>
      <p style="margin:16px 0">
        <a href="{label_url}" style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;text-decoration:none;font-weight:500">Download label (PDF)</a>
      </p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">Tracking number</td><td align="right">{tracking_number}</td></tr>
      </table>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Your return label is ready ({rma_number})"
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
      <h1 style="margin:8px 0 16px;font-size:22px;font-weight:500">{rma_number} · Order #{order_number}</h1>

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
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"[New return] {rma_number} · Order #{order_number} · {customer_name}"
    return await send_email(cfg, to_email=admin_to, to_name=f"{store_name} Admin",
                            subject=subject, html=html, tags=["admin_new_return"])


async def send_admin_label_failure(cfg, *, rma_number, return_id, error_message,
                                   method_display_label, amount, currency,
                                   customer_name, customer_email,
                                   rate_provider="", rate_servicelevel="") -> Dict[str, Any]:
    """Alert the store admin that a return label could not be purchased
    (e.g. Shippo 'shipping provider not found', Royal Mail auth error, etc.).
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
      <h1 style="margin:8px 0 16px;font-size:22px;font-weight:500">{rma_number}</h1>
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
        Common causes: carrier not connected in Shippo/Royal Mail dashboard,
        wrong API key (test vs live), insufficient account balance, or an
        expired rate_id. Check the provider dashboard, then process the label
        manually and forward it to the customer.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"[ACTION REQUIRED] Label purchase failed · {rma_number} · {customer_name}"
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
        Your free-label request for <strong>{rma_number}</strong> (order #{order_number}) has been approved.
        The return shipping label is attached to this email — please print it and drop the parcel at your nearest carrier.
      </p>
      {note_block}
      {attach_list_html}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA number</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
      </table>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Free return label approved ({rma_number})"
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
        We've reviewed your return request <strong>{rma_number}</strong> (order #{order_number})
        and unfortunately we're unable to approve it at this time.
      </p>
      {reason_block}
      {attach_list_html}
      <p style="margin:18px 0 0;line-height:1.65;font-size:14px">
        If you believe this decision is a mistake or you'd like to discuss it further,
        please email us at <a href="mailto:{support_email}" style="color:#0A0A0A;text-decoration:underline">{support_email}</a> — we're happy to help.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Return request update ({rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["return_rejected"], attachments=attachments)



async def send_store_credit_issued(cfg, *, to_email, to_name, rma_number, order_number,
                                   coupon_code: str, coupon_amount: float,
                                   currency: str = "GBP", bonus_percent: float = 0.0,
                                   expires_on: Optional[str] = None,
                                   label_deduction: float = 0.0) -> Dict[str, Any]:
    """Email the customer their store-credit coupon code + expiry."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""

    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get((currency or "").upper(), "")
    amount_fmt = f"{symbol}{coupon_amount:.2f}" if symbol else f"{coupon_amount:.2f} {currency}"
    bonus_line = ""
    if bonus_percent and bonus_percent > 0:
        bonus_line = (f"<p style=\"margin:0 0 12px;line-height:1.65\">That's "
                      f"<strong>{bonus_percent:g}% more</strong> than a cash refund — "
                      f"our thank-you for picking store credit.</p>")
    deduction_line = ""
    if label_deduction and label_deduction > 0:
        ded_fmt = f"{symbol}{label_deduction:.2f}" if symbol else f"{label_deduction:.2f} {currency}"
        deduction_line = (f"<tr><td style=\"padding:4px 0;color:#78716C\">Less return label</td>"
                          f"<td align=\"right\">− {ded_fmt}</td></tr>")
    expires_line = ""
    if expires_on:
        expires_line = (f"<tr><td style=\"padding:4px 0;color:#78716C\">Expires</td>"
                        f"<td align=\"right\">{expires_on}</td></tr>")

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#047857">Store credit issued</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Your store credit is ready.</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        For return <strong>{rma_number}</strong> (order #{order_number}) you chose store credit
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
        {deduction_line}
        {expires_line}
      </table>
      <p style="margin:18px 0 0;line-height:1.65;font-size:13px;color:#78716C">
        Single-use, locked to this email address. Apply it at the checkout on our store.
        Please do not reply to this automated message.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Your store credit code {coupon_code}"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["store_credit_issued"])


async def send_store_credit_revoked_to_customer(cfg, *, to_email, to_name,
                                                rma_number, order_number,
                                                coupon_code: str = "",
                                                coupon_amount: float = 0.0,
                                                currency: str = "GBP") -> Dict[str, Any]:
    """Inform the customer that their store credit has been revoked. Asks
    them to open a support ticket if they think this is a mistake. The
    support email is pulled dynamically from settings, so renaming the
    business or changing the support address updates this template instantly."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = ((cfg.get("support_email") or "").strip()
                     or (cfg.get("from_email") or "").strip())
    logo_url = cfg.get("logo_url") or ""

    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get((currency or "").upper(), "")
    amount_fmt = f"{symbol}{coupon_amount:.2f}" if symbol else f"{coupon_amount:.2f} {currency}"

    code_block = ""
    if coupon_code:
        code_block = f"""
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
            <tr><td style="padding:4px 0;color:#78716C">Coupon code</td><td align="right">{coupon_code}</td></tr>
            <tr><td style="padding:4px 0;color:#78716C">Value</td><td align="right">{amount_fmt}</td></tr>
            <tr><td style="padding:4px 0;color:#78716C">RMA</td><td align="right">{rma_number}</td></tr>
            <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
          </table>
        """

    cta_block = ""
    if support_email:
        cta_block = f"""
          <p style="margin:18px 0">
            <a href="mailto:{support_email}?subject=Store%20credit%20revoked%20-%20{rma_number}"
               style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;text-decoration:none;font-weight:500">
              Open a support ticket
            </a>
          </p>
          <p style="margin:0 0 12px;line-height:1.65;font-size:13px;color:#78716C">
            Or email us directly at
            <a href="mailto:{support_email}" style="color:#0A0A0A;text-decoration:underline">{support_email}</a>.
          </p>
        """

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#B91C1C">Store credit revoked</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">An update on your store credit.</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        Your store credit for return <strong>{rma_number}</strong> (order #{order_number})
        has been revoked. The coupon code below is no longer valid and cannot be redeemed
        at checkout.
      </p>
      <p style="margin:0 0 12px;line-height:1.65">
        This usually happens when the returned parcel arrives empty, damaged, or contains
        the wrong item. If you think this is a mistake — or if you'd like more detail on
        why the credit was revoked — please open a support ticket and our team will get back
        to you.
      </p>
      {code_block}
      {cta_block}
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Store credit revoked ({rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["store_credit_revoked"])


# ---- Self-ship (customer uses their own carrier) email templates ---------

# UK carriers commonly used by customers; mapped here to keep wording on-brand.
SELF_SHIP_CARRIER_HINTS = {
    "Royal Mail": "post offices and Royal Mail post-boxes",
    "Evri": "Evri ParcelShops",
    "DPD": "DPD Pickup points or scheduled collection",
    "UPS": "UPS Access Points or scheduled collection",
    "FedEx": "FedEx drop-off points",
}


def _self_ship_panel_html(rma_number: str, order_number: str, requires_admin: bool) -> str:
    """Shared coloured info panel reminding the customer to include a note in
    the parcel when shipping untracked. Renders inside the email body."""
    locked_line = ""
    if requires_admin:
        locked_line = ("<p style=\"margin:0 0 12px;line-height:1.65\">Because of the reason "
                       "selected, our team will review your request first. Please "
                       "<strong>do not ship the parcel yet</strong> — we'll email you the "
                       "moment it's approved.</p>")
    return f"""
      {locked_line}
      <div style="margin:20px 0;padding:14px;border-left:3px solid #B45309;background:#FEF3C7">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#B45309">If you ship without tracking</div>
        <div style="margin-top:6px;line-height:1.55">
          We strongly recommend a <strong>tracked service</strong> so your parcel is protected
          in transit. If you choose untracked anyway, please <strong>include a note inside the
          parcel</strong> with your order number <strong>#{order_number}</strong> and RMA
          <strong>{rma_number}</strong> so we can match it on arrival.
        </div>
      </div>
    """


async def send_self_ship_instructions(cfg, *, to_email, to_name, rma_number,
                                      order_number, requires_admin_first: bool) -> Dict[str, Any]:
    """First email a self-ship customer receives. If `requires_admin_first` is
    True (locked reasons), tells them to wait for approval before posting. Else
    they can ship immediately and add tracking afterwards."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""
    warehouse_name = cfg.get("warehouse_name") or "Returns Center"
    warehouse_addr_lines = "<br />".join([
        x for x in [
            cfg.get("warehouse_street"),
            ", ".join([y for y in [cfg.get("warehouse_city"), cfg.get("warehouse_state")] if y]) +
                (f" {cfg.get('warehouse_zip')}" if cfg.get("warehouse_zip") else ""),
            cfg.get("warehouse_country"),
        ] if x and str(x).strip()
    ]) or "(set in admin settings)"

    # CTA — direct link to the tracking page where the customer can paste
    # their carrier + tracking number. Hidden if the portal URL isn't set.
    tracking_url = _tracking_link(cfg, rma_number)
    cta_block = ""
    if tracking_url and not requires_admin_first:
        cta_block = f"""
          <p style="margin:18px 0">
            <a href="{tracking_url}"
               style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;
                      text-decoration:none;font-weight:500">
              Add tracking number
            </a>
          </p>
          <p style="margin:0 0 8px;font-size:12px;color:#78716C">
            Or open it later: <a href="{tracking_url}" style="color:#0A0A0A;text-decoration:underline">{tracking_url}</a>
          </p>
        """

    if requires_admin_first:
        h1 = "Thanks — we're reviewing your return."
        intro = (f"You've chosen to <strong>send with your own carrier</strong> for return "
                 f"<strong>{rma_number}</strong> (order #{order_number}). Because of the "
                 f"reason you selected, our team will review your request first.")
        next_steps = ("<p style=\"margin:12px 0;line-height:1.65\">"
                      "<strong>Please do not ship anything yet.</strong> Once approved you'll "
                      "receive another email letting you know it's safe to post the parcel and "
                      "asking you to add the tracking number.</p>")
    else:
        h1 = "Ready to ship — here's your address."
        intro = (f"You've chosen to <strong>send with your own carrier</strong> for return "
                 f"<strong>{rma_number}</strong> (order #{order_number}). You can post the "
                 f"parcel today.")
        next_steps = ("<p style=\"margin:12px 0;line-height:1.65\">"
                      "Once you've dropped it off, please add the tracking number using the "
                      "button below — we'll send a couple of reminders if you forget.</p>")

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#0A0A0A">Self-ship return</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">{h1}</h1>
      <p style="margin:0 0 12px;line-height:1.65">{intro}</p>
      {next_steps}
      {cta_block}

      <div style="margin:20px 0;padding:16px;border:1px solid #E5E5E0;background:#FAFAF7">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#78716C;margin-bottom:6px">Send the parcel to</div>
        <div style="font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.6">
          <strong>{warehouse_name}</strong><br />
          {warehouse_addr_lines}
        </div>
      </div>

      {_self_ship_panel_html(rma_number, order_number, requires_admin_first)}

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
      </table>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Self-ship return {rma_number} — " + \
              ("we're reviewing your request" if requires_admin_first else "ship and add tracking")
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["self_ship_instructions"])


async def send_self_ship_approved_to_ship(cfg, *, to_email, to_name, rma_number,
                                          order_number, admin_note: str = "") -> Dict[str, Any]:
    """Sent after admin approves a locked-reason self-ship return — the
    customer can now post the parcel and add tracking."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""
    warehouse_name = cfg.get("warehouse_name") or "Returns Center"
    warehouse_addr_lines = "<br />".join([
        x for x in [
            cfg.get("warehouse_street"),
            ", ".join([y for y in [cfg.get("warehouse_city"), cfg.get("warehouse_state")] if y]) +
                (f" {cfg.get('warehouse_zip')}" if cfg.get("warehouse_zip") else ""),
            cfg.get("warehouse_country"),
        ] if x and str(x).strip()
    ]) or "(set in admin settings)"

    note_block = ""
    if admin_note:
        note_block = f"""
          <div style="margin:20px 0;padding:14px;border-left:3px solid #047857;background:#ECFDF5">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#047857">Note from our returns team</div>
            <div style="margin-top:6px;white-space:pre-wrap;line-height:1.55">{admin_note}</div>
          </div>"""

    tracking_url = _tracking_link(cfg, rma_number)
    cta_block = ""
    if tracking_url:
        cta_block = f"""
          <p style="margin:18px 0">
            <a href="{tracking_url}"
               style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;
                      text-decoration:none;font-weight:500">
              Add tracking number
            </a>
          </p>
        """

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#047857">Approved — please ship</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">You're good to post the parcel.</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        Your self-ship return <strong>{rma_number}</strong> (order #{order_number}) has been
        approved. Please post the parcel using your preferred carrier and then add the tracking
        number using the button below.
      </p>
      {note_block}
      {cta_block}
      <div style="margin:20px 0;padding:16px;border:1px solid #E5E5E0;background:#FAFAF7">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#78716C;margin-bottom:6px">Send the parcel to</div>
        <div style="font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.6">
          <strong>{warehouse_name}</strong><br />
          {warehouse_addr_lines}
        </div>
      </div>
      {_self_ship_panel_html(rma_number, order_number, requires_admin=False)}
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Self-ship approved — please post your parcel ({rma_number})"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["self_ship_approved_to_ship"])


async def send_self_ship_tracking_added(cfg, *, to_email, to_name, rma_number,
                                        order_number, carrier: str,
                                        tracking_number: str, is_tracked: bool,
                                        method: str = "",
                                        has_deduction: bool = False) -> Dict[str, Any]:
    """Sent when the customer submits their carrier + tracking number."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""

    tracking_block = ""
    if is_tracked and tracking_number:
        tracking_block = f"""
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
            <tr><td style="padding:4px 0;color:#78716C">Carrier</td><td align="right">{carrier}</td></tr>
            <tr><td style="padding:4px 0;color:#78716C">Tracking</td><td align="right">{tracking_number}</td></tr>
          </table>
        """
    elif tracking_number:
        tracking_block = f"""
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
            <tr><td style="padding:4px 0;color:#78716C">Carrier</td><td align="right">{carrier} (untracked)</td></tr>
            <tr><td style="padding:4px 0;color:#78716C">Reference</td><td align="right">{tracking_number}</td></tr>
          </table>
        """
    else:
        tracking_block = f"""
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
            <tr><td style="padding:4px 0;color:#78716C">Carrier</td><td align="right">{carrier} (untracked)</td></tr>
          </table>
        """

    untracked_note = ""
    if not is_tracked:
        untracked_note = ("<p style=\"margin:12px 0;line-height:1.65;font-size:13px;color:#78716C\">"
                          "Reminder: because you sent it untracked, please make sure your "
                          "<strong>note inside the parcel</strong> includes the order and RMA "
                          "above — that's how we'll match it on arrival.</p>")

    smart_line = _smart_post_arrival_line(method, has_deduction)

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#047857">Tracking received</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">Got it — thanks!</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        We've recorded your shipping details for return <strong>{rma_number}</strong> (order #{order_number}).
        {smart_line}
      </p>
      {tracking_block}
      {untracked_note}
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Tracking received for {rma_number}"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["self_ship_tracking_added"])


async def send_self_ship_tracking_reminder(cfg, *, to_email, to_name, rma_number,
                                           order_number, attempt: int,
                                           tracking_link: str = "") -> Dict[str, Any]:
    """Periodic reminder when a self-ship customer hasn't added tracking yet."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""

    # Caller may pass an explicit link, but if not we derive one from settings.
    if not tracking_link:
        tracking_link = _tracking_link(cfg, rma_number)

    cta_block = ""
    if tracking_link:
        cta_block = f"""
          <p style="margin:18px 0">
            <a href="{tracking_link}" style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;text-decoration:none;font-weight:500">Add tracking now</a>
          </p>
        """

    severity = "Friendly reminder" if attempt <= 1 else "Reminder"
    if attempt >= 4:
        severity = "Final reminder"

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:#B45309">{severity}</div>
      <h1 style="margin:8px 0 16px;font-size:22px;font-weight:500">Have you posted your return yet?</h1>
      <p style="margin:0 0 12px;line-height:1.65">
        We're holding return <strong>{rma_number}</strong> (order #{order_number}) open for you,
        but we haven't received the carrier + tracking details yet. Please add them so we can
        watch for your parcel and get your refund / store credit moving.
      </p>
      {cta_block}
      <p style="margin:14px 0;line-height:1.65;font-size:13px;color:#78716C">
        Sent it untracked? You can still add the carrier name (and any reference)
        — just tick the "I sent this without tracking" box on the form.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · Reminder: add tracking for return {rma_number}"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["self_ship_tracking_reminder"])


# Friendly copy per status for the customer-subscribed status-change email.
_STATUS_COPY = {
    "in_transit": {
        "kicker": "Your parcel is moving",
        "kicker_color": "#0A0A0A",
        "headline": "On its way to our warehouse.",
        "body": ("The carrier has scanned your parcel — it's now in transit. "
                 "We'll email again the moment it arrives."),
    },
    "delivered": {
        "kicker": "Delivered",
        "kicker_color": "#047857",
        "headline": "We've got your parcel.",
        "body": ("Your return parcel just arrived at our warehouse. Our team "
                 "will inspect the items and process your refund or store "
                 "credit shortly."),
    },
    "refunded": {
        "kicker": "Refund processed",
        "kicker_color": "#047857",
        "headline": "Your refund is on its way.",
        "body": ("All done — your refund has been processed back to the "
                 "original payment method. Depending on your bank it can take "
                 "5–10 business days to appear on your statement."),
    },
    "store_credit_issued": {
        "kicker": "Store credit ready",
        "kicker_color": "#047857",
        "headline": "Your store credit has been issued.",
        "body": ("We've issued your store credit for this return. Check the "
                 "separate email from us with your unique coupon code — it's "
                 "ready to use at checkout."),
    },
    "approved": {
        "kicker": "Return approved",
        "kicker_color": "#047857",
        "headline": "You're all approved.",
        "body": "Your return has been approved by our team and is ready to be shipped or processed.",
    },
    "rejected": {
        "kicker": "Return update",
        "kicker_color": "#B91C1C",
        "headline": "Update on your return.",
        "body": ("There's been an update to your return that needs your "
                 "attention — please review the details below."),
    },
}


async def send_status_update(cfg, *, to_email, to_name, rma_number, order_number,
                             new_status: str, status_label: str = "",
                             tracking_link: str = "") -> Dict[str, Any]:
    """Customer-opt-in email fired when a tracked return changes to one of
    the "interesting" statuses (in_transit / delivered / refunded /
    store_credit_issued / approved / rejected). Customer subscribes to these
    via the toggle on the public tracking page."""
    store_name = cfg.get("store_name") or "Returns"
    support_email = (cfg.get("support_email") or "").strip() or (cfg.get("from_email") or "")
    logo_url = cfg.get("logo_url") or ""

    copy = _STATUS_COPY.get(new_status) or {
        "kicker": "Status update",
        "kicker_color": "#0A0A0A",
        "headline": f"Status: {status_label or new_status}",
        "body": "There's been an update to your return — see the latest status below.",
    }

    if not tracking_link:
        tracking_link = _tracking_link(cfg, rma_number)

    cta_block = ""
    if tracking_link:
        cta_block = f"""
          <p style="margin:18px 0">
            <a href="{tracking_link}"
               style="display:inline-block;background:#0A0A0A;color:#fff;padding:14px 24px;text-decoration:none;font-weight:500">
              View live status
            </a>
          </p>
        """

    inner = f"""
      <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:11px;color:{copy['kicker_color']}">{copy['kicker']}</div>
      <h1 style="margin:8px 0 16px;font-size:24px;font-weight:500">{copy['headline']}</h1>
      <p style="margin:0 0 12px;line-height:1.65">{copy['body']}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5E0;padding:16px;margin:16px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px">
        <tr><td style="padding:4px 0;color:#78716C">RMA</td><td align="right">{rma_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Order</td><td align="right">#{order_number}</td></tr>
        <tr><td style="padding:4px 0;color:#78716C">Status</td><td align="right">{status_label or new_status}</td></tr>
      </table>
      {cta_block}
      <p style="margin:14px 0 0;font-size:12px;color:#78716C">
        You're getting this because you opted in to status updates on the
        tracking page. To stop these emails, open the tracking page and
        toggle "Get email updates" off.
      </p>
    """
    html = _base_html(store_name, support_email, logo_url, inner, cfg)
    subject = f"{store_name} · {copy['kicker']} · {rma_number}"
    return await send_email(cfg, to_email=to_email, to_name=to_name, subject=subject,
                            html=html, tags=["status_update", f"status_{new_status}"])
