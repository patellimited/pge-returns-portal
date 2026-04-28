import React, { useEffect, useMemo, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { api, STATUS_LABELS } from "../lib/api";
import {
  ArrowLeft,
  CheckCircle,
  Truck,
  Package,
  Info,
  MagnifyingGlass,
  Clock,
  BellRinging,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import SelfShipPanel from "../components/SelfShipPanel";

const ORDER = ["label_purchased", "in_transit", "delivered", "refunded"];
const DISPLAY = {
  label_purchased: { label: "Label printed", icon: Package },
  in_transit: { label: "In transit", icon: Truck },
  delivered: { label: "Delivered to warehouse", icon: CheckCircle },
  refunded: { label: "Refund processed", icon: CheckCircle },
};

// Friendly "x minutes ago" helper – avoids pulling in date-fns just for this.
function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.max(0, Date.now() - then);
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min${mins === 1 ? "" : "s"} ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  const days = Math.floor(hrs / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export default function TrackReturn() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("rma") || "");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  const run = async (q) => {
    const trimmed = (q || "").trim();
    if (!trimmed) return;
    setLoading(true);
    try {
      const r = await api.get(`/tracking/${encodeURIComponent(trimmed)}`);
      setData(r.data);
    } catch (e) {
      toast.error(
        e.response?.data?.detail ||
          "We couldn't find a return with that reference. Double-check the RMA or order number and try again."
      );
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (params.get("rma")) run(params.get("rma")); // eslint-disable-next-line
  }, []);

  const currentIdx = data ? Math.max(0, ORDER.indexOf(data.status)) : -1;
  const lastUpdated = useMemo(() => timeAgo(data?.updated_at), [data]);
  const itemsCount = data?.items?.length || 0;
  const hasEta = !!data?.eta_label && currentIdx < 2; // hide once delivered/refunded

  // ---- "Get email updates" subscription state -------------------------------
  const [subscribing, setSubscribing] = useState(false);
  const [subscribed, setSubscribed] = useState(false);
  const [subEmail, setSubEmail] = useState("");
  const [showSubEmailInput, setShowSubEmailInput] = useState(false);

  // Sync local toggle state whenever a fresh tracking payload lands.
  useEffect(() => {
    if (data) {
      setSubscribed(!!data.notify_status_email);
      setSubEmail(
        data.notify_status_email_address || data.email || ""
      );
    }
  }, [data]);

  const toggleSubscribe = async (next) => {
    if (!data?.rma_number) return;
    // Turning ON without an email on file → reveal a tiny inline input.
    if (next && !(data.email || subEmail)) {
      setShowSubEmailInput(true);
      return;
    }
    setSubscribing(true);
    try {
      const r = await api.post(
        `/tracking/${encodeURIComponent(data.rma_number)}/subscribe`,
        { enabled: next, email: subEmail || data.email || undefined }
      );
      setSubscribed(!!r.data.notify_status_email);
      setSubEmail(r.data.notify_status_email_address || subEmail);
      setShowSubEmailInput(false);
      toast.success(
        next
          ? "You'll get an email every time the status changes."
          : "Email updates turned off."
      );
    } catch (e) {
      toast.error(e.response?.data?.detail || "Couldn't update preference.");
    } finally {
      setSubscribing(false);
    }
  };

  return (
    <div className="min-h-screen bg-white fade-in" data-testid="track-page">
      <div className="max-w-2xl mx-auto px-4 sm:px-6 py-10 sm:py-16">
        <Link
          to="/"
          className="inline-flex items-center text-sm text-[hsl(var(--ink-muted))] hover:text-[hsl(var(--ink))] mb-8 sm:mb-12"
        >
          <ArrowLeft size={14} className="mr-1" /> Back
        </Link>
        <div className="label-caps">Tracking</div>
        <h1 className="text-3xl sm:text-4xl lg:text-5xl mt-3">Where's my return?</h1>
        <p className="mt-3 text-[hsl(var(--ink-muted))] text-sm sm:text-base">
          Enter your <span className="text-[hsl(var(--ink))] font-medium">RMA number</span>,{" "}
          <span className="text-[hsl(var(--ink))] font-medium">order number</span>, or carrier{" "}
          <span className="text-[hsl(var(--ink))] font-medium">tracking number</span> to see live status.
        </p>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            setParams({ rma: query });
            run(query);
          }}
          className="mt-6 sm:mt-8 flex flex-col sm:flex-row gap-3"
        >
          <div className="relative flex-1">
            <MagnifyingGlass
              size={18}
              className="absolute left-4 top-1/2 -translate-y-1/2 text-[hsl(var(--ink-muted))] pointer-events-none"
            />
            <input
              className="input-field mono w-full"
              style={{ paddingLeft: "2.75rem" }}
              placeholder="e.g. RMA-123456 or order #10482"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              inputMode="text"
              data-testid="track-input"
            />
          </div>
          <button
            className="btn-primary w-full sm:w-auto"
            disabled={loading}
            data-testid="track-submit"
          >
            {loading ? "Searching…" : "Track"}
          </button>
        </form>

        {/* Helper / "where do I find this?" panel */}
        <div className="mt-3" data-testid="track-help">
          <button
            type="button"
            onClick={() => setShowHelp((s) => !s)}
            className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--ink-muted))] hover:text-[hsl(var(--ink))]"
            data-testid="track-help-toggle"
          >
            <Info size={14} />
            Can't find your RMA number?
          </button>
          {showHelp && (
            <div
              className="mt-3 p-4 border border-[hsl(var(--border))] bg-[hsl(var(--surface))] text-sm space-y-2"
              style={{ borderRadius: 2 }}
              data-testid="track-help-panel"
            >
              <p className="text-[hsl(var(--ink))] font-medium">Where to find your RMA number</p>
              <ul className="list-disc pl-5 text-[hsl(var(--ink-muted))] space-y-1">
                <li>Check the <span className="text-[hsl(var(--ink))]">return confirmation email</span> we sent right after you submitted the return — the RMA looks like <span className="mono">RMA-XXXXXX</span>.</li>
                <li>It's also printed at the top of your <span className="text-[hsl(var(--ink))]">prepaid return label</span> PDF.</li>
                <li>No email? Try entering the <span className="text-[hsl(var(--ink))]">order number</span> from your original purchase confirmation — we'll find the latest return for that order.</li>
              </ul>
            </div>
          )}
        </div>

        {data && (
          <div className="mt-12">
            {data.method === "self_ship" && (
              <div className="mb-8">
                <SelfShipPanel
                  doc={data}
                  needsTracking={
                    data.status === "awaiting_tracking" && !data.self_ship_submitted_at
                  }
                  pendingApproval={data.status === "awaiting_approval"}
                  shipped={!!data.self_ship_submitted_at}
                  onSubmitted={(updated) =>
                    setData((prev) => ({ ...prev, ...updated }))
                  }
                />
              </div>
            )}

            {/* Top summary strip */}
            <div className="flex flex-wrap items-center gap-2 mb-4 text-xs">
              {itemsCount > 0 && (
                <span
                  className="inline-flex items-center gap-1 px-2.5 py-1 border border-[hsl(var(--border))] bg-[hsl(var(--surface))]"
                  style={{ borderRadius: 2 }}
                  data-testid="track-items-chip"
                >
                  <Package size={12} /> {itemsCount} item{itemsCount === 1 ? "" : "s"}
                </span>
              )}
              {hasEta && (
                <span
                  className="inline-flex items-center gap-1 px-2.5 py-1 border border-[hsl(var(--ink))] bg-white text-[hsl(var(--ink))]"
                  style={{ borderRadius: 2 }}
                  data-testid="track-eta-chip"
                  title={
                    data.eta_source === "carrier_average"
                      ? "Estimated from real delivery times for this carrier"
                      : "Typical carrier estimate"
                  }
                >
                  <Clock size={12} /> {data.eta_label}
                </span>
              )}
              {lastUpdated && (
                <span
                  className="inline-flex items-center gap-1 px-2.5 py-1 border border-[hsl(var(--border))] bg-[hsl(var(--surface))] text-[hsl(var(--ink-muted))]"
                  style={{ borderRadius: 2 }}
                  data-testid="track-updated-chip"
                >
                  Updated {lastUpdated}
                </span>
              )}
            </div>

            <div className="card-flat">
              <div className="grid grid-cols-2 gap-5 text-sm">
                <div>
                  <div className="label-caps mb-1">RMA</div>
                  <div className="mono">{data.rma_number}</div>
                </div>
                <div>
                  <div className="label-caps mb-1">Status</div>
                  <div className="mono">{STATUS_LABELS[data.status] || data.status}</div>
                </div>
                {data.order_number && (
                  <div>
                    <div className="label-caps mb-1">Order</div>
                    <div className="mono">#{data.order_number}</div>
                  </div>
                )}
                {data.tracking_number && (
                  <>
                    <div>
                      <div className="label-caps mb-1">Carrier</div>
                      <div className="mono">{data.tracking_carrier}</div>
                    </div>
                    <div>
                      <div className="label-caps mb-1">Tracking #</div>
                      <div className="mono">{data.tracking_number}</div>
                    </div>
                  </>
                )}
              </div>
            </div>

            <div className="mt-10" data-testid="tracking-timeline">
              {ORDER.map((key, i) => {
                const active = i <= currentIdx;
                const { label, icon: Icon } = DISPLAY[key];
                return (
                  <div key={key} className="timeline-step">
                    <div className={`timeline-dot ${active ? "active" : ""}`} />
                    <div className="flex items-center gap-2">
                      <Icon
                        size={16}
                        weight={active ? "fill" : "regular"}
                        className={
                          active
                            ? "text-[hsl(var(--ink))]"
                            : "text-[hsl(var(--ink-muted))]"
                        }
                      />
                      <span
                        className={
                          active
                            ? "text-[hsl(var(--ink))] font-medium"
                            : "text-[hsl(var(--ink-muted))]"
                        }
                      >
                        {label}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Get email updates toggle */}
            <div
              className="mt-6 p-4 border border-[hsl(var(--border))] bg-[hsl(var(--surface))]"
              style={{ borderRadius: 2 }}
              data-testid="track-subscribe-card"
            >
              <div className="flex items-start sm:items-center justify-between gap-4 flex-col sm:flex-row">
                <div className="flex items-start gap-3">
                  <BellRinging
                    size={18}
                    weight={subscribed ? "fill" : "regular"}
                    className={
                      subscribed
                        ? "text-[hsl(var(--ink))] mt-0.5 sm:mt-0"
                        : "text-[hsl(var(--ink-muted))] mt-0.5 sm:mt-0"
                    }
                  />
                  <div>
                    <div className="text-sm font-medium text-[hsl(var(--ink))]">
                      Get email updates
                    </div>
                    <div className="text-xs text-[hsl(var(--ink-muted))] mt-0.5">
                      We'll email{" "}
                      <span className="mono">
                        {subEmail || data.email || "you"}
                      </span>{" "}
                      whenever your status changes — no need to check back.
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={subscribed}
                  disabled={subscribing}
                  onClick={() => toggleSubscribe(!subscribed)}
                  className="relative inline-flex h-7 w-12 items-center transition-colors disabled:opacity-50"
                  style={{
                    background: subscribed
                      ? "hsl(var(--ink))"
                      : "hsl(var(--border))",
                    borderRadius: 999,
                  }}
                  data-testid="track-subscribe-toggle"
                >
                  <span
                    className="inline-block h-5 w-5 bg-white transition-transform"
                    style={{
                      borderRadius: 999,
                      transform: subscribed
                        ? "translateX(22px)"
                        : "translateX(4px)",
                    }}
                  />
                </button>
              </div>
              {showSubEmailInput && (
                <div
                  className="mt-3 flex flex-col sm:flex-row gap-2"
                  data-testid="track-subscribe-email-row"
                >
                  <input
                    type="email"
                    className="input-field flex-1"
                    placeholder="you@example.com"
                    value={subEmail}
                    onChange={(e) => setSubEmail(e.target.value)}
                    data-testid="track-subscribe-email-input"
                  />
                  <button
                    className="btn-primary w-full sm:w-auto"
                    disabled={subscribing || !subEmail}
                    onClick={() => toggleSubscribe(true)}
                    data-testid="track-subscribe-confirm"
                  >
                    {subscribing ? "Saving…" : "Subscribe"}
                  </button>
                </div>
              )}
            </div>

            {data.updates?.length > 0 && (
              <div className="mt-8">
                <div className="label-caps mb-3">Carrier updates</div>
                <div className="space-y-2 text-sm">
                  {data.updates.map((u, i) => (
                    <div
                      key={i}
                      className="flex justify-between border-b border-[hsl(var(--border))] pb-2"
                    >
                      <span>{u.status_details || u.status}</span>
                      <span className="mono text-xs text-[hsl(var(--ink-muted))]">
                        {u.status_date || ""}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
