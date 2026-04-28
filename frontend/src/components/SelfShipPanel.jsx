import React, { useState } from "react";
import { api, SELF_SHIP_CARRIERS } from "../lib/api";
import { PaperPlaneTilt } from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * Shared self-ship UI:
 *  - Warehouse address banner (visible once approved / non-locked)
 *  - "If you ship untracked, include a note" tip
 *  - Inline carrier + tracking form (when needsTracking)
 *  - Read-only summary (when shipped)
 *
 * Used on both ReturnSuccess and TrackReturn so customers can add tracking
 * from either entry point.
 */
export default function SelfShipPanel({ doc, needsTracking, pendingApproval, shipped, onSubmitted }) {
  const wh = doc.warehouse_address || {};
  const [carrier, setCarrier] = useState(SELF_SHIP_CARRIERS[0]);
  const [carrierOther, setCarrierOther] = useState("");
  const [trackingNumber, setTrackingNumber] = useState("");
  const [isUntracked, setIsUntracked] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!isUntracked && !trackingNumber.trim()) {
      toast.error("Please enter your tracking number, or tick the untracked option.");
      return;
    }
    if (carrier === "Other" && !carrierOther.trim()) {
      toast.error("Please type your carrier name.");
      return;
    }
    setSubmitting(true);
    try {
      const r = await api.post(`/returns/${doc.id}/self-ship/tracking`, {
        carrier,
        carrier_other: carrierOther.trim(),
        tracking_number: trackingNumber.trim(),
        is_tracked: !isUntracked,
      });
      toast.success("Got it — thanks! We'll watch for your parcel.");
      onSubmitted && onSubmitted(r.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not submit tracking. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="self-ship-panel">
      <div className="card-flat !p-5 sm:!p-6 border border-[hsl(var(--ink))]">
        <div className="flex items-start gap-3">
          <PaperPlaneTilt size={22} className="shrink-0 mt-1" />
          <div className="min-w-0 flex-1">
            <div className="label-caps">Self-ship return</div>
            {pendingApproval ? (
              <>
                <h2 className="font-medium text-lg mt-1">We're reviewing your request</h2>
                <p className="text-sm text-[hsl(var(--ink-muted))] mt-2">
                  Because of the reason you selected, our team will review your request first.
                  <strong className="text-[hsl(var(--ink))]"> Please don't ship the parcel yet.</strong>
                  {doc.email ? <> You'll receive an email at{" "}
                    <span className="mono text-[hsl(var(--ink))] break-all">{doc.email}</span>{" "}</> : " You'll receive an email "}
                  with the warehouse address as soon as it's approved.
                </p>
              </>
            ) : shipped ? (
              <>
                <h2 className="font-medium text-lg mt-1">Tracking saved</h2>
                <p className="text-sm text-[hsl(var(--ink-muted))] mt-2">
                  Thanks — we've got your shipping details and we'll process your refund / store credit
                  the moment your parcel arrives at the warehouse.
                </p>
              </>
            ) : (
              <>
                <h2 className="font-medium text-lg mt-1">Ready to post your parcel</h2>
                <p className="text-sm text-[hsl(var(--ink-muted))] mt-2">
                  Pop your parcel in the post using your preferred carrier. Once you've dropped it off,
                  add the carrier name and tracking number below so we can watch for it.
                </p>
              </>
            )}
          </div>
        </div>

        {!pendingApproval && (
          <div className="mt-5 border border-[hsl(var(--border))] bg-[hsl(var(--surface))] p-4">
            <div className="label-caps mb-2">Send the parcel to</div>
            <div className="text-sm mono leading-relaxed" data-testid="self-ship-warehouse-address">
              {wh.name && <>{wh.name}<br /></>}
              {wh.street1 && <>{wh.street1}<br /></>}
              {wh.street2 && <>{wh.street2}<br /></>}
              {[wh.city, wh.state].filter(Boolean).join(", ")}{wh.zip ? ` ${wh.zip}` : ""}
              {wh.country && <><br />{wh.country}</>}
            </div>
          </div>
        )}

        {!pendingApproval && (
          <div className="mt-4 border-l-4 border-[hsl(var(--warning))] bg-[hsl(var(--warning-bg))] p-3 text-sm">
            <div className="label-caps mb-1" style={{ color: "hsl(var(--warning))" }}>If you ship untracked</div>
            <div>
              We strongly recommend a <strong>tracked service</strong> so your parcel is protected.
              If you choose untracked anyway, please <strong>include a note inside the parcel</strong>{" "}
              with your order number{" "}
              <span className="mono text-[hsl(var(--ink))]">#{doc.order_number}</span>
              {doc.email ? <> and email{" "}
                <span className="mono text-[hsl(var(--ink))] break-all">{doc.email}</span></> : null}
              {" "}so we can match it on arrival.
            </div>
          </div>
        )}
      </div>

      {needsTracking && (
        <div className="card-flat !p-5 sm:!p-6" data-testid="self-ship-tracking-form">
          <div className="label-caps mb-3">Add your tracking</div>
          <div className="grid sm:grid-cols-2 gap-3">
            <select
              className="input-field"
              value={carrier}
              onChange={(e) => setCarrier(e.target.value)}
              data-testid="self-ship-carrier-select"
            >
              {SELF_SHIP_CARRIERS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            {carrier === "Other" ? (
              <input
                className="input-field"
                placeholder="Carrier name (e.g. Yodel)"
                value={carrierOther}
                onChange={(e) => setCarrierOther(e.target.value)}
                data-testid="self-ship-carrier-other"
              />
            ) : <div className="hidden sm:block" />}
            <input
              className="input-field mono sm:col-span-2"
              placeholder={isUntracked ? "Reference (optional)" : "Tracking number"}
              value={trackingNumber}
              onChange={(e) => setTrackingNumber(e.target.value)}
              data-testid="self-ship-tracking-input"
            />
          </div>
          <label className="flex items-center gap-2 mt-3 text-sm cursor-pointer">
            <input
              type="checkbox"
              checked={isUntracked}
              onChange={(e) => setIsUntracked(e.target.checked)}
              data-testid="self-ship-untracked-toggle"
            />
            <span>I sent this without tracking</span>
          </label>
          <div className="mt-5 flex flex-col sm:flex-row gap-3">
            <button
              type="button"
              className="btn-primary w-full sm:w-auto"
              onClick={submit}
              disabled={submitting}
              data-testid="self-ship-submit-tracking"
            >
              {submitting ? "Saving…" : "Save tracking"}
            </button>
          </div>
        </div>
      )}

      {shipped && (
        <div className="card-flat !p-5 sm:!p-6" data-testid="self-ship-summary">
          <div className="label-caps mb-2">You sent it via</div>
          <div className="text-sm mono">
            {doc.self_ship_carrier}
            {doc.self_ship_is_tracked === false ? " (untracked)" : ""}
          </div>
          {doc.self_ship_tracking_number && (
            <div className="mt-3">
              <div className="label-caps mb-1">Tracking</div>
              <div className="text-sm mono break-all">{doc.self_ship_tracking_number}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
