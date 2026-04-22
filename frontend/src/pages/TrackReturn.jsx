import React, { useEffect, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { api, STATUS_LABELS } from "../lib/api";
import { ArrowLeft, CheckCircle, Circle, Truck, Package } from "@phosphor-icons/react";
import { toast } from "sonner";

const ORDER = ["label_purchased", "in_transit", "delivered", "refunded"];
const DISPLAY = {
  label_purchased: { label: "Label printed", icon: Package },
  in_transit: { label: "In transit", icon: Truck },
  delivered: { label: "Delivered to warehouse", icon: CheckCircle },
  refunded: { label: "Refund processed", icon: CheckCircle },
};

export default function TrackReturn() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("rma") || "");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const run = async (q) => {
    if (!q) return;
    setLoading(true);
    try {
      const r = await api.get(`/tracking/${encodeURIComponent(q)}`);
      setData(r.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Not found");
      setData(null);
    } finally { setLoading(false); }
  };

  useEffect(() => {
    if (params.get("rma")) run(params.get("rma")); // eslint-disable-next-line
  }, []);

  const currentIdx = data ? Math.max(0, ORDER.indexOf(data.status)) : -1;

  return (
    <div className="min-h-screen bg-white fade-in" data-testid="track-page">
      <div className="max-w-2xl mx-auto px-4 sm:px-6 py-10 sm:py-16">
        <Link to="/" className="inline-flex items-center text-sm text-[hsl(var(--ink-muted))] hover:text-[hsl(var(--ink))] mb-8 sm:mb-12">
          <ArrowLeft size={14} className="mr-1" /> Back
        </Link>
        <div className="label-caps">Tracking</div>
        <h1 className="text-3xl sm:text-4xl lg:text-5xl mt-3">Where's my return?</h1>

        <form
          onSubmit={(e) => { e.preventDefault(); setParams({ rma: query }); run(query); }}
          className="mt-8 sm:mt-10 flex flex-col sm:flex-row gap-3"
        >
          <input
            className="input-field mono flex-1"
            placeholder="RMA number or tracking #"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            data-testid="track-input"
          />
          <button className="btn-primary w-full sm:w-auto" disabled={loading} data-testid="track-submit">
            {loading ? "…" : "Track"}
          </button>
        </form>

        {data && (
          <div className="mt-12">
            <div className="card-flat">
              <div className="grid grid-cols-2 gap-5 text-sm">
                <div><div className="label-caps mb-1">RMA</div><div className="mono">{data.rma_number}</div></div>
                <div><div className="label-caps mb-1">Status</div><div className="mono">{STATUS_LABELS[data.status] || data.status}</div></div>
                {data.tracking_number && (
                  <>
                    <div><div className="label-caps mb-1">Carrier</div><div className="mono">{data.tracking_carrier}</div></div>
                    <div><div className="label-caps mb-1">Tracking #</div><div className="mono">{data.tracking_number}</div></div>
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
                      <Icon size={16} weight={active ? "fill" : "regular"} className={active ? "text-[hsl(var(--ink))]" : "text-[hsl(var(--ink-muted))]"} />
                      <span className={active ? "text-[hsl(var(--ink))] font-medium" : "text-[hsl(var(--ink-muted))]"}>{label}</span>
                    </div>
                  </div>
                );
              })}
            </div>

            {data.updates?.length > 0 && (
              <div className="mt-8">
                <div className="label-caps mb-3">Carrier updates</div>
                <div className="space-y-2 text-sm">
                  {data.updates.map((u, i) => (
                    <div key={i} className="flex justify-between border-b border-[hsl(var(--border))] pb-2">
                      <span>{u.status_details || u.status}</span>
                      <span className="mono text-xs text-[hsl(var(--ink-muted))]">{u.status_date || ""}</span>
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
