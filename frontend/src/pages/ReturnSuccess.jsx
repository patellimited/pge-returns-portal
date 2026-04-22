import React, { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { api, STATUS_LABELS, formatMoney } from "../lib/api";
import { CheckCircle, Hourglass, DownloadSimple, MapPin, Gift, QrCode, Copy } from "@phosphor-icons/react";

export default function ReturnSuccess() {
  const { returnId } = useParams();
  const [params] = useSearchParams();
  const [doc, setDoc] = useState(null);
  const [polling, setPolling] = useState(false);
  const [err, setErr] = useState(null);
  const finalizedRef = useRef(false);

  const sessionId = params.get("session_id");

  useEffect(() => {
    let stop = false;
    const tick = async (attempt = 0) => {
      try {
        if (sessionId) {
          const s = await api.get(`/payments/status/${sessionId}`);
          if (s.data.payment_status === "paid") {
            const r = await api.get(`/returns/${returnId}`);
            if (!stop) { setDoc(r.data); setPolling(false); }
            return;
          }
          if (attempt < 10 && !stop) {
            setPolling(true);
            setTimeout(() => tick(attempt + 1), 2000);
          } else {
            setPolling(false);
          }
        } else {
          const r = await api.get(`/returns/${returnId}`);
          if (!stop) setDoc(r.data);
        }
      } catch (e) { if (!stop) setErr(e.response?.data?.detail || "Could not load return."); }
    };
    tick();
    return () => { stop = true; };
  }, [returnId, sessionId]);

  // Once the return doc is loaded (and we're not still polling for Stripe),
  // trigger all emails in one batch. Idempotent on the server.
  useEffect(() => {
    if (!doc || polling || finalizedRef.current) return;
    if (doc.emails_finalized) { finalizedRef.current = true; return; }
    finalizedRef.current = true;
    (async () => {
      try {
        await api.post(`/returns/${returnId}/finalize`);
      } catch {
        // fail-safe — the success screen still shows even if email finalization fails
      }
    })();
  }, [doc, polling, returnId]);

  if (err) return (
    <div className="min-h-screen grid place-items-center p-6">
      <div className="card-flat max-w-md text-center space-y-3">
        <div className="label-caps">Something went wrong</div>
        <div className="text-sm">{err}</div>
        <div className="flex flex-wrap gap-2 justify-center pt-2">
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="btn-primary"
            data-testid="retry-load-btn"
          >Retry</button>
          <Link to={`/track?rma=${returnId}`} className="btn-secondary">Track by RMA</Link>
          <Link to="/" className="btn-secondary">Back home</Link>
        </div>
      </div>
    </div>
  );
  if (!doc) return (
    <div className="min-h-screen grid place-items-center">
      <div className="label-caps flex items-center gap-2">
        <span className="inline-block w-2 h-2 rounded-full bg-[hsl(var(--ink))] animate-pulse" />
        Loading your return…
      </div>
    </div>
  );

  const awaiting = doc.status === "awaiting_approval" || doc.status === "awaiting_payment" || polling;
  const hasLabel = !!doc.label_url;
  const hasQr = !!doc.label_qr_url;
  const hasCoupon = !!doc.coupon_code;
  const isStoreCredit = doc.method === "store_credit";

  const copyCoupon = () => {
    if (!doc.coupon_code) return;
    try {
      navigator.clipboard.writeText(doc.coupon_code);
    } catch {
      /* clipboard API may be blocked — fall through silently */
    }
  };

  return (
    <div className="min-h-screen bg-white fade-in" data-testid="success-page">
      <div className="max-w-2xl mx-auto px-4 sm:px-6 py-10 sm:py-16">
        <div className="flex items-start gap-3">
          {awaiting ? <Hourglass size={28} className="shrink-0 mt-1" /> : <CheckCircle size={28} weight="fill" className="text-[hsl(var(--success))] shrink-0 mt-1" />}
          <div className="min-w-0">
            <div className="label-caps">Step 04 of 04</div>
            <h1 className="text-2xl sm:text-3xl lg:text-4xl mt-1 leading-tight">
              {hasCoupon
                ? "Your store credit is ready."
                : awaiting
                  ? "We've got your return."
                  : "All set — here's your label."}
            </h1>
          </div>
        </div>

        {hasCoupon && (
          <div
            className="mt-8 card-flat !p-5 sm:!p-7 border-2 border-[hsl(var(--ink))]"
            data-testid="store-credit-panel"
          >
            <div className="flex items-start gap-3">
              <Gift size={22} className="shrink-0 mt-1" />
              <div className="min-w-0">
                <div className="label-caps">Store credit</div>
                <div className="font-medium text-lg">
                  {formatMoney(doc.coupon_amount || 0, doc.coupon_currency || "GBP")} off your next order
                </div>
                {doc.store_credit_bonus_percent_applied > 0 && (
                  <div className="text-sm text-[hsl(var(--ink-muted))] mt-1">
                    Includes +{doc.store_credit_bonus_percent_applied}% bonus for choosing credit.
                  </div>
                )}
              </div>
            </div>
            <div className="mt-5 flex items-stretch gap-2">
              <div
                className="flex-1 font-mono tracking-widest text-lg sm:text-xl text-center py-3 border border-dashed border-[hsl(var(--ink))] bg-[hsl(var(--surface))] select-all"
                data-testid="coupon-code"
              >
                {doc.coupon_code}
              </div>
              <button
                type="button"
                onClick={copyCoupon}
                className="btn-secondary px-4"
                data-testid="coupon-copy-btn"
                title="Copy code"
              >
                <Copy size={16} />
              </button>
            </div>
            {doc.coupon_expires_at && (
              <div className="mt-3 text-xs text-[hsl(var(--ink-muted))] mono">
                Expires {new Date(doc.coupon_expires_at).toLocaleDateString()}
              </div>
            )}
          </div>
        )}

        <div className="mt-8 sm:mt-10 card-flat !p-4 sm:!p-8">
          <div className="grid grid-cols-2 gap-4 sm:gap-5 text-sm">
            <div>
              <div className="label-caps mb-1">RMA</div>
              <div className="mono text-[hsl(var(--ink))] break-all">{doc.rma_number}</div>
            </div>
            <div>
              <div className="label-caps mb-1">Status</div>
              <div className="mono">{STATUS_LABELS[doc.status] || doc.status}</div>
            </div>
            <div>
              <div className="label-caps mb-1">Order</div>
              <div className="mono">#{doc.order_number}</div>
            </div>
            <div>
              <div className="label-caps mb-1">Method</div>
              <div className="mono text-xs sm:text-sm">{(doc.method_display_label || doc.method || "").replace(/_/g, " ")}</div>
            </div>
            {doc.label_cost > 0 && (
              <div>
                <div className="label-caps mb-1">Label cost</div>
                <div className="mono">{formatMoney(doc.label_cost)}</div>
              </div>
            )}
            {doc.refund_deduction > 0 && (
              <div>
                <div className="label-caps mb-1">Net refund</div>
                <div className="mono">{formatMoney(doc.refund_net || 0)}</div>
              </div>
            )}
            {doc.tracking_number && (
              <div className="col-span-2">
                <div className="label-caps mb-1">Tracking</div>
                <div className="mono break-all">{doc.tracking_number}</div>
              </div>
            )}
          </div>

          <div className="mt-6 sm:mt-8 pt-6 border-t border-[hsl(var(--border))] flex flex-col sm:flex-row flex-wrap gap-3">
            {hasLabel && (
              <a href={doc.label_url} target="_blank" rel="noreferrer" className="btn-primary w-full sm:w-auto" data-testid="download-label-btn">
                <DownloadSimple size={16} className="mr-2" /> Download label
              </a>
            )}
            {hasQr && (
              <a href={doc.label_qr_url} target="_blank" rel="noreferrer" className="btn-secondary w-full sm:w-auto" data-testid="qr-label-btn">
                <QrCode size={16} className="mr-2" /> Scan at drop-off (no printer)
              </a>
            )}
            <Link to={`/track?rma=${doc.rma_number}`} className="btn-secondary w-full sm:w-auto" data-testid="track-this-btn">
              <MapPin size={16} className="mr-2" /> Track this return
            </Link>
            <Link to="/" className="btn-secondary w-full sm:w-auto">Back home</Link>
          </div>

          {hasQr && (
            <p className="mt-4 text-xs text-[hsl(var(--ink-muted))]">
              Tip: open the QR on your phone at the carrier counter — no need to print.
            </p>
          )}

          {isStoreCredit && doc.status === "awaiting_approval" && (
            <p className="mt-6 text-sm text-[hsl(var(--ink-muted))]">
              Our team needs to verify this return before issuing your store credit.
              We'll email <span className="mono text-[hsl(var(--ink))] break-all">{doc.email}</span> with your
              coupon code as soon as it's approved — usually within 24 hours.
            </p>
          )}

          {!hasLabel && !isStoreCredit && doc.label_error && (
            <div
              className="mt-6 p-4 border-l-4 border-[hsl(var(--ink))] bg-[hsl(var(--surface))]"
              data-testid="label-pending-panel"
            >
              <div className="label-caps mb-1">Label is being prepared</div>
              <p className="text-sm">
                {doc.paid
                  ? "We've received your payment "
                  : "We've received your return request "}
                and our team has been notified. Your shipping label will be emailed to{" "}
                <span className="mono text-[hsl(var(--ink))] break-all">{doc.email}</span>{" "}
                within a few hours — no further action needed on your side.
              </p>
              <p className="mt-2 text-xs text-[hsl(var(--ink-muted))]">
                Reference: RMA {doc.rma_number}
              </p>
            </div>
          )}

          {!hasLabel && !isStoreCredit && !doc.label_error && doc.status === "awaiting_approval" && (
            <p className="mt-6 text-sm text-[hsl(var(--ink-muted))]">
              Because you chose a free return label, our team will review your request and send
              the label to <span className="mono text-[hsl(var(--ink))] break-all">{doc.email}</span> within 24 hours.
            </p>
          )}

          <p className="mt-6 text-xs text-[hsl(var(--ink-muted))]">
            A confirmation email has been sent to <span className="mono text-[hsl(var(--ink))] break-all">{doc.email}</span>.
          </p>
        </div>
      </div>
    </div>
  );
}
