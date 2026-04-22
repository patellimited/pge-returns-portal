/* eslint-disable react-hooks/rules-of-hooks, react-hooks/exhaustive-deps */
import React, { useMemo, useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useOrder } from "../lib/OrderContext";
import { api, FREE_LABEL_REASONS, formatMoney } from "../lib/api";
import { CreditCard, Wallet, Truck, Clock, CheckCircle } from "@phosphor-icons/react";
import { toast } from "sonner";

export default function ReturnMethod() {
  const nav = useNavigate();
  const ctx = useOrder();
  const { order, selection, method, setMethod, returnAddress, setReturnAddress, setReturnDoc, returnDoc, proofFiles } = ctx;

  const [rates, setRates] = useState([]);
  const [selectedRate, setSelectedRate] = useState(null);
  const [loadingRates, setLoadingRates] = useState(false);
  const [ratesError, setRatesError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  // Store-credit config pulled from /branding so the admin can tune the
  // bonus % live without a redeploy.
  const [storeCredit, setStoreCredit] = useState({ enabled: false, bonus: 0, expiryDays: 365 });
  useEffect(() => {
    let alive = true;
    api.get("/branding").then((r) => {
      if (!alive) return;
      setStoreCredit({
        enabled: !!r.data?.store_credit_enabled,
        bonus: Number(r.data?.store_credit_bonus_percent || 0),
        expiryDays: Number(r.data?.store_credit_expiry_days || 365),
      });
    }).catch(() => {});
    return () => { alive = false; };
  }, []);

  // Human-readable "valid for …" label (rounds to years / months).
  const expiryLabel = (() => {
    const d = storeCredit.expiryDays || 365;
    if (d >= 350 && d <= 380) return "1 year";
    if (d >= 720 && d <= 750) return "2 years";
    const years = Math.round(d / 365);
    if (years >= 1) return `${years} year${years > 1 ? "s" : ""}`;
    const months = Math.round(d / 30);
    return `${months} month${months === 1 ? "" : "s"}`;
  })();

  const defaultAddr = returnAddress ||
    (order?.shipping_address ? { ...order.shipping_address } : null) || {
      name: "", street1: "", city: "", state: "", zip: "", country: "US", phone: "", email: order?.email || "",
    };
  const [addr, setAddr] = useState(defaultAddr);

  const eligibleForFree = useMemo(
    () => Object.values(selection || {}).some((s) => FREE_LABEL_REASONS.has(s.reason)),
    [selection]
  );

  // If any selected item has a product-issue reason, the return MUST go through
  // manual admin review — paid options are hidden. The customer doesn't see
  // any "free" labelling; the UI simply says the return needs admin review.
  const requiresManualReview = eligibleForFree;

  // Auto-select the review method so the customer can't pick a paid option.
  // We allow either free_label (default) OR store_credit — both are valid
  // for manual-review returns. The customer can toggle between them.
  useEffect(() => {
    if (requiresManualReview && method !== "free_label" && method !== "store_credit") {
      setMethod("free_label");
    }
    // eslint-disable-next-line
  }, [requiresManualReview]);

  useEffect(() => {
    if (!order || !selection || Object.keys(selection).length === 0) {
      nav("/start", { replace: true });
    }
  }, []);

  // State / County is optional — Shippo quotes from postcode + country.
  const validAddr = () => addr.name && addr.street1 && addr.city && addr.zip;

  const itemsPayload = order ? Object.entries(selection || {}).map(([lid, sel]) => {
    const li = order.line_items.find((x) => x.id === lid);
    return {
      line_item_id: lid, name: li.name, quantity: sel.qty, price: li.price,
      image: li.image, reason: sel.reason, notes: sel.notes || "",
    };
  }) : [];
  const refundTotal = itemsPayload.reduce((a, i) => a + i.price * i.quantity, 0);

  // Tag cheapest + fastest rate for a quick glance. Ties broken by the order
  // the provider returns rates in (stable because we use `.reduce` not sort).
  const cheapestRateId = useMemo(() => {
    if (!rates || rates.length === 0) return null;
    return rates.reduce((best, r) =>
      (!best || Number(r.amount) < Number(best.amount)) ? r : best, null)?.rate_id || null;
  }, [rates]);
  const fastestRateId = useMemo(() => {
    if (!rates || rates.length === 0) return null;
    const withDays = rates.filter((r) => Number(r.days) > 0);
    if (!withDays.length) return null;
    return withDays.reduce((best, r) =>
      (!best || Number(r.days) < Number(best.days)) ? r : best, null)?.rate_id || null;
  }, [rates]);

  const ensureReturn = async () => {
    if (returnDoc?.id) return returnDoc;
    const payload = {
      order_id: String(order.order_id), email: order.email,
      items: itemsPayload, method, customer_note: "",
      return_address: addr,
    };
    try {
      const r = await api.post("/returns", payload);
      setReturnDoc(r.data);
      setReturnAddress(addr);
      // Upload proof photos (if any) — non-blocking failure.
      if (proofFiles && proofFiles.length > 0) {
        try {
          const fd = new FormData();
          proofFiles.forEach((f) => fd.append("files", f));
          await api.post(`/returns/${r.data.id}/proof`, fd, {
            headers: { "Content-Type": "multipart/form-data" },
          });
        } catch (upErr) {
          toast.error(
            upErr.response?.data?.detail ||
            "Proof photos could not be uploaded, but your return was created."
          );
        }
      }
      return r.data;
    } catch (err) {
      throw err;
    }
  };

  const trackAction = async (doc, kind, label, meta = {}) => {
    if (!doc?.id) return;
    try { await api.post(`/returns/${doc.id}/track-action`, { kind, label, meta }); } catch {}
  };

  const pickMethod = async (m, labelText) => {
    setMethod(m);
    if (returnDoc?.id) {
      trackAction(returnDoc, "method_selected", `Selected method: ${labelText}`, { method: m });
    }
  };

  const loadRatesPreview = async () => {
    if (!addr.zip || addr.zip.trim().length < 3) return;
    setLoadingRates(true);
    setRatesError(null);
    try {
      const r = await api.post(`/rates/preview`, {
        zip: addr.zip.trim(),
        country: addr.country || "US",
        state: addr.state || "",
        city: addr.city || "",
      });
      setRates(r.data.rates || []);
      // If the previously-selected rate is no longer in the list, clear it.
      setSelectedRate((curr) => {
        if (!curr) return curr;
        const stillThere = (r.data.rates || []).find((x) => x.rate_id === curr.rate_id);
        return stillThere || null;
      });
    } catch (err) {
      setRatesError(err.response?.data?.detail || "Unable to fetch rates right now.");
      setRates([]);
    } finally { setLoadingRates(false); }
  };

  // Auto-fetch rates as soon as the page has a postcode — no button press,
  // no need to pick a method first. Debounced so editing address fields
  // doesn't hammer Shippo.
  useEffect(() => {
    if (!order) return;
    if (!addr.zip || addr.zip.trim().length < 3) return;
    const t = setTimeout(() => { loadRatesPreview(); }, 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line
  }, [addr.zip, addr.country, addr.city, addr.state, addr.street1]);

  const onConfirm = async () => {
    if (!method) return toast.error("Pick a return method");
    if (!validAddr()) return toast.error("Please complete your return address");
    setSubmitting(true);
    try {
      const doc = await ensureReturn();
      if (method === "free_label") {
        nav(`/return/${doc.id}/success?pending=1`);
        return;
      }
      if (method === "store_credit") {
        // Manual-review reasons hold the coupon until admin approves; other
        // reasons get the coupon issued by the backend on submission.
        const pending = requiresManualReview ? "?pending=1" : "";
        nav(`/return/${doc.id}/success${pending}`);
        return;
      }
      if (!selectedRate) { toast.error("Select a shipping rate"); setSubmitting(false); return; }
      if (method === "pay_stripe") {
        const r = await api.post(`/returns/${doc.id}/checkout`, {
          rate_id: selectedRate.rate_id, origin_url: window.location.origin,
          provider: selectedRate.provider,
          servicelevel: selectedRate.servicelevel,
          amount: selectedRate.amount,
        });
        window.location.href = r.data.url;
      } else if (method === "deduct_from_refund") {
        await api.post(`/returns/${doc.id}/deduct-from-refund`, {
          rate_id: selectedRate.rate_id, origin_url: window.location.origin,
          provider: selectedRate.provider,
          servicelevel: selectedRate.servicelevel,
          amount: selectedRate.amount,
        });
        nav(`/return/${doc.id}/success`);
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || "Something went wrong");
      setSubmitting(false);
    }
  };

  if (!order) return null;

  const showRatesPanel = !requiresManualReview && method !== "store_credit" && (method === "pay_stripe" || method === "deduct_from_refund" || rates.length > 0 || loadingRates);

  return (
    <div className="min-h-screen bg-white fade-in" data-testid="method-page">
      <div className="max-w-4xl mx-auto px-4 sm:px-6 py-8 sm:py-14">
        <div className="label-caps">Step 03 of 04</div>
        <h1 className="text-3xl sm:text-4xl lg:text-5xl mt-3">How would you like to return?</h1>

        <div className="mt-6 sm:mt-10 card-flat !p-4 sm:!p-8">
          <div className="label-caps mb-4">Where are you sending from?</div>
          <div className="grid sm:grid-cols-2 gap-3">
            <input className="input-field" placeholder="Full name" value={addr.name} onChange={(e) => setAddr({ ...addr, name: e.target.value })} data-testid="addr-name" />
            <input className="input-field" placeholder="Phone" value={addr.phone} onChange={(e) => setAddr({ ...addr, phone: e.target.value })} data-testid="addr-phone" />
            <input className="input-field sm:col-span-2" placeholder="Street address" value={addr.street1} onChange={(e) => setAddr({ ...addr, street1: e.target.value })} data-testid="addr-street" />
            <input className="input-field" placeholder="City" value={addr.city} onChange={(e) => setAddr({ ...addr, city: e.target.value })} data-testid="addr-city" />
            <div className="grid grid-cols-2 gap-3">
              <input className="input-field" placeholder="State / County (optional)" value={addr.state} onChange={(e) => setAddr({ ...addr, state: e.target.value })} data-testid="addr-state" />
              <input className="input-field mono" placeholder="ZIP / Postcode" value={addr.zip} onChange={(e) => setAddr({ ...addr, zip: e.target.value })} data-testid="addr-zip" />
            </div>
          </div>
          <div className="mt-3 text-xs text-[hsl(var(--ink-muted))]">
            State / County is optional — live rates load automatically from your postcode + country.
          </div>
        </div>

        {/* Shipping rates panel — visible as soon as a postcode is present so
            the customer sees pricing before picking a method. */}
        {showRatesPanel && (
          <div className="mt-8 sm:mt-10" data-testid="rates-panel">
            <div className="flex items-center justify-between mb-4">
              <div className="label-caps flex items-center gap-2">
                <Truck size={14} /> Live shipping rates
              </div>
              {loadingRates ? (
                <span className="text-[11px] uppercase tracking-widest text-[hsl(var(--ink-muted))] mono flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-[hsl(var(--ink))] animate-pulse"></span>
                  Fetching…
                </span>
              ) : rates.length > 0 ? (
                <span className="text-[11px] uppercase tracking-widest text-[hsl(var(--ink-muted))] mono flex items-center gap-1.5">
                  <CheckCircle size={12} weight="fill" className="text-[hsl(var(--success))]" />
                  {rates.length} option{rates.length > 1 ? "s" : ""}
                </span>
              ) : null}
            </div>

            <div className="space-y-3" data-testid="rates-list">
              {loadingRates && rates.length === 0 && (
                <>
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="rate-card-skeleton" data-testid={`rate-skeleton-${i}`}>
                      <div className="skeleton-box w-12 h-8 rate-card-logo-skel" />
                      <div className="rate-card-meta">
                        <div className="skeleton-box h-3 w-40 max-w-full" />
                        <div className="skeleton-box h-2.5 w-24 mt-2" />
                      </div>
                      <div className="skeleton-box h-5 w-16 rate-card-price-skel" />
                      <div className="rate-check" />
                    </div>
                  ))}
                </>
              )}

              {!loadingRates && rates.map((r) => (
                <button
                  key={r.rate_id}
                  type="button"
                  onClick={() => {
                    setSelectedRate(r);
                    trackAction(returnDoc, "rate_selected",
                      `Selected shipping: ${r.provider} ${r.servicelevel} (${formatMoney(r.amount, r.currency)})`,
                      { rate_id: r.rate_id, amount: r.amount, provider: r.provider });
                  }}
                  className={`rate-card w-full ${selectedRate?.rate_id === r.rate_id ? "selected" : ""}`}
                  data-testid={`rate-${r.rate_id}`}
                >
                  <div className="rate-card-logo">
                    {r.provider_image
                      ? <img src={r.provider_image} alt={r.provider} />
                      : <span className="rate-card-logo-fallback">{(r.provider || "").slice(0, 2).toUpperCase()}</span>}
                  </div>
                  <div className="rate-card-meta">
                    <div className="rate-card-service">
                      {r.servicelevel || r.provider}
                      {cheapestRateId === r.rate_id && (
                        <span className="rate-tag rate-tag-cheapest" data-testid="rate-tag-cheapest">Cheapest</span>
                      )}
                      {fastestRateId === r.rate_id && fastestRateId !== cheapestRateId && (
                        <span className="rate-tag rate-tag-fastest" data-testid="rate-tag-fastest">Fastest</span>
                      )}
                    </div>
                    <div className="rate-card-sub">
                      <span className="rate-card-carrier">{r.provider}</span>
                      <span className="rate-card-sep" aria-hidden>·</span>
                      <span className="rate-card-eta">
                        <Clock size={11} />
                        {r.duration_terms || `${r.days || "–"} business days`}
                      </span>
                    </div>
                  </div>
                  <div className="rate-card-price">{formatMoney(r.amount, r.currency)}</div>
                  <div className={`rate-check ${selectedRate?.rate_id === r.rate_id ? "selected" : ""}`}>
                    {selectedRate?.rate_id === r.rate_id && <CheckCircle size={16} weight="fill" />}
                  </div>
                </button>
              ))}

              {!loadingRates && rates.length === 0 && (
                <div className="rate-empty" data-testid="rates-empty">
                  {ratesError
                    ? ratesError
                    : addr.zip && addr.zip.trim().length >= 3
                      ? "No rates returned for that postcode — double-check the postcode / country and try again."
                      : "Enter your postcode above and rates will load automatically."}
                </div>
              )}
            </div>
          </div>
        )}

        <div className="grid md:grid-cols-3 gap-3 sm:gap-4 mt-8 sm:mt-10">
          {requiresManualReview ? (
            <>
              <div
                className="md:col-span-3 border border-[hsl(var(--ink))] bg-[hsl(var(--surface))] p-5 sm:p-6"
                data-testid="manual-review-notice"
              >
                <div className="label-caps mb-2">This return needs admin review</div>
                <div className="text-sm text-[hsl(var(--ink-muted))]">
                  Based on the reason you selected, a team member will review your request
                  and reply by email — usually within 1 business day. Pick how you'd like
                  your refund resolved once it's approved:
                </div>
              </div>
              <button
                type="button"
                onClick={() => pickMethod("free_label", "Free return label (admin approval)")}
                className={`method-card text-left ${method === "free_label" ? "selected" : ""}`}
                data-testid="method-free-label"
              >
                <Truck size={22} weight="duotone" />
                <div className="mt-4 font-medium">Free return label</div>
                <div className="text-sm text-[hsl(var(--ink-muted))] mt-1">
                  We send you a free shipping label. Refund issued after we receive the item.
                </div>
              </button>
              {storeCredit.enabled && (
                <button
                  type="button"
                  onClick={() => pickMethod(
                    "store_credit",
                    `Store credit${storeCredit.bonus > 0 ? ` (+${storeCredit.bonus}% bonus)` : ""}`
                  )}
                  className={`method-card method-card-shine text-left relative md:col-span-2 ${method === "store_credit" ? "selected" : ""}`}
                  data-testid="method-store-credit-review"
                >
                  <span className="method-card-shine-overlay" aria-hidden="true" />
                  {storeCredit.bonus > 0 && (
                    <span className="absolute top-3 right-3 text-[10px] uppercase tracking-widest mono px-2 py-1 border border-[hsl(var(--ink))] bg-[hsl(var(--surface))] store-credit-bonus-chip">
                      +{storeCredit.bonus}% bonus
                    </span>
                  )}
                  <GiftShineIcon />
                  <div className="mt-4 font-medium">Store credit instead</div>
                  <div className="text-sm text-[hsl(var(--ink-muted))] mt-1">
                    Get <span className="mono text-[hsl(var(--ink))]">
                      {formatMoney(refundTotal * (1 + (storeCredit.bonus || 0) / 100))}
                    </span> as a coupon once approved — no waiting for a bank refund.
                  </div>
                  <div
                    className="mt-2 text-[11px] mono text-[hsl(var(--ink-muted))]"
                    data-testid="store-credit-validity-review"
                  >
                    Code valid for {expiryLabel}
                  </div>
                </button>
              )}
            </>
          ) : (
            <>
              <button type="button" onClick={() => pickMethod("pay_stripe", "Pay for label (Stripe)")} className={`method-card text-left ${method === "pay_stripe" ? "selected" : ""}`} data-testid="method-stripe">
                <CreditCard size={22} />
                <div className="mt-4 font-medium">Pay for label</div>
                <div className="text-sm text-[hsl(var(--ink-muted))] mt-1">Pay now via Stripe. Keep your full refund.</div>
              </button>
              <button type="button" onClick={() => pickMethod("deduct_from_refund", "Deduct shipping from refund")} className={`method-card text-left ${method === "deduct_from_refund" ? "selected" : ""}`} data-testid="method-deduct">
                <Wallet size={22} />
                <div className="mt-4 font-medium">Deduct from refund</div>
                <div className="text-sm text-[hsl(var(--ink-muted))] mt-1">We'll subtract the label cost from your refund.</div>
              </button>
              {storeCredit.enabled && (
                <button
                  type="button"
                  onClick={() => pickMethod(
                    "store_credit",
                    `Store credit${storeCredit.bonus > 0 ? ` (+${storeCredit.bonus}% bonus)` : ""}`
                  )}
                  className={`method-card method-card-shine text-left relative ${method === "store_credit" ? "selected" : ""}`}
                  data-testid="method-store-credit"
                >
                  <span className="method-card-shine-overlay" aria-hidden="true" />
                  {storeCredit.bonus > 0 && (
                    <span className="absolute top-3 right-3 text-[10px] uppercase tracking-widest mono px-2 py-1 border border-[hsl(var(--ink))] bg-[hsl(var(--surface))] store-credit-bonus-chip">
                      +{storeCredit.bonus}% bonus
                    </span>
                  )}
                  <GiftShineIcon />
                  <div className="mt-4 font-medium">Store credit</div>
                  <div className="text-sm text-[hsl(var(--ink-muted))] mt-1">
                    Get <span className="mono text-[hsl(var(--ink))]">
                      {formatMoney(refundTotal * (1 + (storeCredit.bonus || 0) / 100))}
                    </span> as a coupon — instead of a cash refund.
                  </div>
                  <div
                    className="mt-2 text-[11px] mono text-[hsl(var(--ink-muted))]"
                    data-testid="store-credit-validity"
                  >
                    Code valid for {expiryLabel}
                  </div>
                </button>
              )}
            </>
          )}
        </div>

        {method === "deduct_from_refund" && (
          <div
            className="mt-6 border border-[hsl(var(--warning))] bg-[hsl(var(--warning-bg))] p-4 text-sm"
            data-testid="deduct-warning"
          >
            <div className="label-caps mb-1" style={{ color: "hsl(var(--warning))" }}>
              Heads up
            </div>
            <div>
              The shipping label cost will be deducted from your refund. Your net refund after
              shipping is shown below once you pick a rate.
            </div>
          </div>
        )}

        <div className="mt-8 sm:mt-10 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="text-sm text-[hsl(var(--ink-muted))]">
            Refund subtotal: <span className="mono text-[hsl(var(--ink))]">{formatMoney(refundTotal)}</span>
            {method === "deduct_from_refund" && selectedRate &&
              <span> · You'll receive <span className="mono text-[hsl(var(--ink))]" data-testid="net-refund">{formatMoney(refundTotal - selectedRate.amount)}</span></span>}
          </div>
          <button className="btn-primary w-full sm:w-auto" disabled={submitting || !method} onClick={onConfirm} data-testid="confirm-return-btn">
            {submitting
              ? "Processing…"
              : method === "pay_stripe"
                ? "Pay & print label"
                : method === "free_label"
                  ? "Submit request"
                  : method === "store_credit"
                    ? `Get store credit${storeCredit.bonus > 0 ? ` + ${storeCredit.bonus}%` : ""}`
                    : "Confirm return"}
          </button>
        </div>
      </div>
      <style>{`
        .method-card-shine {
          background: linear-gradient(135deg,
            hsl(45 60% 97%) 0%,
            hsl(40 40% 99%) 45%,
            hsl(42 55% 96%) 100%);
          border-color: hsl(42 55% 78%);
          box-shadow: 0 1px 0 rgba(255,255,255,0.9) inset,
                      0 10px 24px -16px rgba(180, 130, 20, 0.35);
          overflow: hidden;
          transition: transform 220ms cubic-bezier(0.22,1,0.36,1),
                      box-shadow 220ms ease,
                      border-color 220ms ease;
        }
        .method-card-shine:hover {
          transform: translateY(-2px);
          border-color: hsl(38 65% 55%);
          box-shadow: 0 1px 0 rgba(255,255,255,0.9) inset,
                      0 18px 36px -18px rgba(180, 130, 20, 0.5);
        }
        .method-card-shine.selected {
          border-color: hsl(38 75% 45%);
          box-shadow: 0 0 0 2px hsl(38 75% 45% / 0.25),
                      0 18px 36px -18px rgba(180, 130, 20, 0.55);
        }
        .method-card-shine-overlay {
          pointer-events: none;
          position: absolute;
          top: 0; left: -60%;
          width: 40%; height: 100%;
          background: linear-gradient(115deg,
            transparent 20%,
            rgba(255, 240, 190, 0.65) 45%,
            rgba(255, 255, 255, 0.9) 50%,
            rgba(255, 240, 190, 0.65) 55%,
            transparent 80%);
          transform: skewX(-18deg);
          animation: storeCreditShimmer 4.2s cubic-bezier(0.22,1,0.36,1) 800ms infinite;
        }
        @keyframes storeCreditShimmer {
          0%   { left: -60%; opacity: 0; }
          8%   { opacity: 1; }
          55%  { opacity: 1; }
          70%  { left: 120%; opacity: 0; }
          100% { left: 120%; opacity: 0; }
        }
        .store-credit-bonus-chip {
          background: linear-gradient(135deg,
            hsl(45 100% 85%),
            hsl(40 90% 72%));
          border-color: hsl(35 80% 40%);
          color: hsl(30 60% 20%);
          box-shadow: 0 4px 12px -6px hsl(38 80% 45% / 0.6);
          animation: bonusChipPulse 2.6s ease-in-out infinite;
        }
        @keyframes bonusChipPulse {
          0%, 100% { box-shadow: 0 4px 12px -6px hsl(38 80% 45% / 0.55); }
          50%      { box-shadow: 0 6px 18px -6px hsl(38 95% 55% / 0.9); }
        }
        .gift-shine-svg {
          display: block;
          width: 36px;
          height: 36px;
          color: hsl(35 80% 38%);
          filter: drop-shadow(0 2px 4px hsl(38 80% 45% / 0.35));
        }
        .gift-shine-svg .ribbon  { animation: giftRibbonBob 3.4s ease-in-out infinite; transform-origin: 32px 16px; }
        .gift-shine-svg .sparkle { animation: giftSparkle 2.6s ease-in-out infinite; transform-origin: center; }
        .gift-shine-svg .sparkle-b { animation-delay: 0.9s; }
        .gift-shine-svg .sparkle-c { animation-delay: 1.7s; }
        @keyframes giftRibbonBob {
          0%, 100% { transform: translateY(0) rotate(0deg); }
          50%      { transform: translateY(-1px) rotate(-3deg); }
        }
        @keyframes giftSparkle {
          0%, 100% { opacity: 0.15; transform: scale(0.6); }
          50%      { opacity: 1;    transform: scale(1); }
        }
        @media (prefers-reduced-motion: reduce) {
          .method-card-shine-overlay,
          .store-credit-bonus-chip,
          .gift-shine-svg .ribbon,
          .gift-shine-svg .sparkle { animation: none !important; }
        }
      `}</style>
    </div>
  );
}

function GiftShineIcon() {
  return (
    <svg
      className="gift-shine-svg"
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="giftBody" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor="hsl(45 95% 72%)" />
          <stop offset="55%" stopColor="hsl(38 85% 55%)" />
          <stop offset="100%" stopColor="hsl(30 70% 38%)" />
        </linearGradient>
        <linearGradient id="giftLid" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="hsl(45 100% 82%)" />
          <stop offset="100%" stopColor="hsl(38 90% 55%)" />
        </linearGradient>
        <linearGradient id="giftRibbon" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="hsl(350 80% 55%)" />
          <stop offset="100%" stopColor="hsl(340 80% 42%)" />
        </linearGradient>
      </defs>
      {/* Box body */}
      <rect x="10" y="28" width="44" height="28" rx="3" fill="url(#giftBody)" stroke="hsl(30 60% 25%)" strokeWidth="1.2" />
      {/* Lid */}
      <rect x="8" y="22" width="48" height="9" rx="2" fill="url(#giftLid)" stroke="hsl(30 60% 25%)" strokeWidth="1.2" />
      {/* Vertical ribbon */}
      <rect x="28" y="22" width="8" height="34" fill="url(#giftRibbon)" />
      {/* Ribbon highlight */}
      <rect x="30" y="22" width="1.6" height="34" fill="hsl(0 100% 85%)" opacity="0.55" />
      {/* Bow */}
      <g className="ribbon">
        <path d="M32 22 C24 14, 18 14, 18 20 C18 25, 26 24, 32 22 Z" fill="url(#giftRibbon)" stroke="hsl(340 70% 30%)" strokeWidth="1" />
        <path d="M32 22 C40 14, 46 14, 46 20 C46 25, 38 24, 32 22 Z" fill="url(#giftRibbon)" stroke="hsl(340 70% 30%)" strokeWidth="1" />
        <circle cx="32" cy="22" r="2.6" fill="hsl(350 80% 50%)" stroke="hsl(340 70% 30%)" strokeWidth="0.8" />
      </g>
      {/* Sparkles */}
      <g stroke="hsl(45 100% 70%)" strokeWidth="1.4" strokeLinecap="round">
        <g className="sparkle" transform="translate(5 14)">
          <line x1="0" y1="-3.5" x2="0" y2="3.5" />
          <line x1="-3.5" y1="0" x2="3.5" y2="0" />
        </g>
        <g className="sparkle sparkle-b" transform="translate(56 10)">
          <line x1="0" y1="-3" x2="0" y2="3" />
          <line x1="-3" y1="0" x2="3" y2="0" />
        </g>
        <g className="sparkle sparkle-c" transform="translate(52 48)">
          <line x1="0" y1="-2.5" x2="0" y2="2.5" />
          <line x1="-2.5" y1="0" x2="2.5" y2="0" />
        </g>
      </g>
    </svg>
  );
}
