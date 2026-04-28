import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, REASON_LABELS, formatMoney } from "../lib/api";
import { toast } from "sonner";
import { ArrowLeft, ArrowsClockwise, ChartBar, Gift, Wallet, Receipt, Truck } from "@phosphor-icons/react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  PieChart, Pie, Cell, LabelList,
} from "recharts";

const PIE_COLORS = [
  "#0A0A0A", "#78716C", "#047857", "#B45309", "#B91C1C",
  "#1D4ED8", "#7C3AED", "#0891B2", "#BE185D", "#A16207",
];

// Tight, neutral Recharts tooltip — matches the portal's ink-on-cream look.
const TooltipBox = ({ active, payload, label, suffix = "" }) => {
  if (!active || !payload || !payload.length) return null;
  return (
    <div className="border border-[hsl(var(--border))] bg-white px-3 py-2 text-xs shadow-sm">
      {label && <div className="label-caps mb-1">{label}</div>}
      {payload.map((p, i) => (
        <div key={i} className="mono">
          {p.name}: <span className="text-[hsl(var(--ink))]">{p.value}{suffix}</span>
        </div>
      ))}
    </div>
  );
};

function Card({ icon, label, value, sub, testid }) {
  return (
    <div className="card-flat !p-5" data-testid={testid}>
      <div className="flex items-center gap-2 label-caps">
        {icon}
        <span>{label}</span>
      </div>
      <div className="mt-3 text-3xl font-medium leading-none">{value}</div>
      {sub && <div className="mt-1 text-xs text-[hsl(var(--ink-muted))]">{sub}</div>}
    </div>
  );
}

function Panel({ title, right, children, testid }) {
  return (
    <div className="card-flat !p-5 sm:!p-6" data-testid={testid}>
      <div className="flex items-center justify-between mb-4">
        <div className="label-caps">{title}</div>
        {right}
      </div>
      {children}
    </div>
  );
}

export default function AdminAnalytics() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [weeks, setWeeks] = useState(12);
  const [reasonDays, setReasonDays] = useState(90);

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.get("/admin/analytics", {
        params: { weeks, reason_days: reasonDays, top_sku_limit: 10 },
      });
      setData(r.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load analytics");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [weeks, reasonDays]);

  const weekly = data?.weekly || [];
  const reasons = useMemo(
    () => (data?.reasons || []).map((r, i) => ({
      ...r,
      label: REASON_LABELS[r.reason] || r.reason,
      fill: PIE_COLORS[i % PIE_COLORS.length],
    })),
    [data]
  );
  const topSkus = data?.top_skus || [];
  const carrierTransit = data?.carrier_transit || [];
  const fin = data?.financials || {};

  // Headline revenue-kept metric: store credit kept in-store instead of cash out.
  const revenueKept = Number(fin.total_store_credit || 0);

  return (
    <div className="min-h-screen bg-white fade-in" data-testid="analytics-page">
      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 py-6 sm:py-10">
        {/* Header */}
        <div className="flex items-center justify-between gap-4 mb-6 sm:mb-8">
          <div className="flex items-center gap-3 min-w-0">
            <Link to="/admin" className="btn-secondary h-9 px-3 text-xs shrink-0" data-testid="back-to-admin">
              <ArrowLeft size={14} className="mr-1" /> Back
            </Link>
            <div className="min-w-0">
              <div className="label-caps flex items-center gap-2">
                <ChartBar size={12} /> Admin
              </div>
              <h1 className="text-2xl sm:text-3xl mt-1 truncate">Return analytics</h1>
            </div>
          </div>
          <button
            className="btn-secondary h-9 px-3 text-xs shrink-0"
            onClick={load}
            disabled={loading}
            data-testid="refresh-btn"
          >
            <ArrowsClockwise size={14} className={`sm:mr-1 ${loading ? "animate-spin" : ""}`} />
            <span className="hidden sm:inline">Refresh</span>
          </button>
        </div>

        {/* Headline cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4" data-testid="headline-cards">
          <Card
            testid="card-refunded"
            icon={<Receipt size={14} />}
            label="Refunded (£)"
            value={formatMoney(fin.total_refunded || 0)}
            sub={`${fin.refunded_count || 0} returns`}
          />
          <Card
            testid="card-deducted"
            icon={<Wallet size={14} />}
            label="Shipping deducted"
            value={formatMoney(fin.total_deducted || 0)}
            sub={`${fin.deducted_count || 0} returns`}
          />
          <Card
            testid="card-store-credit"
            icon={<Gift size={14} />}
            label="Store credit issued"
            value={formatMoney(fin.total_store_credit || 0)}
            sub={`${fin.store_credit_count || 0} coupons · revenue kept`}
          />
          <Card
            testid="card-returns-window"
            icon={<ChartBar size={14} />}
            label={`Returns (${weeks}w)`}
            value={weekly.reduce((a, w) => a + (w.count || 0), 0)}
            sub="opened in window"
          />
        </div>

        {revenueKept > 0 && (
          <div
            className="mt-4 border border-[hsl(var(--ink))] bg-[hsl(var(--surface))] px-4 py-3 text-sm flex items-center gap-3"
            data-testid="revenue-kept-banner"
          >
            <Gift size={16} weight="fill" />
            <div>
              <strong className="mono">{formatMoney(revenueKept)}</strong> kept in-store via coupons
              {fin.total_refunded > 0 && (
                <> — that's <strong className="mono">{((revenueKept / (revenueKept + Number(fin.total_refunded || 0))) * 100).toFixed(1)}%</strong> of refunds avoided as cash out.</>
              )}
            </div>
          </div>
        )}

        {/* Weekly chart */}
        <div className="mt-6 sm:mt-8">
          <Panel
            testid="weekly-panel"
            title={`Returns per week · last ${weeks} weeks`}
            right={
              <select
                value={weeks}
                onChange={(e) => setWeeks(Number(e.target.value))}
                className="text-xs border border-[hsl(var(--border))] bg-white px-2 py-1 mono"
                data-testid="weeks-select"
              >
                {[4, 8, 12, 26, 52].map((n) => <option key={n} value={n}>{n} weeks</option>)}
              </select>
            }
          >
            <div className="h-[260px] sm:h-[320px]" data-testid="weekly-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={weekly} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="2 4" vertical={false} stroke="#E5E5E0" />
                  <XAxis
                    dataKey="week"
                    tickFormatter={(v) => (v || "").split("-W")[1]}
                    tick={{ fontSize: 10, fill: "#78716C" }}
                    axisLine={{ stroke: "#E5E5E0" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fontSize: 10, fill: "#78716C" }}
                    axisLine={false}
                    tickLine={false}
                    allowDecimals={false}
                  />
                  <Tooltip content={<TooltipBox />} cursor={{ fill: "rgba(10,10,10,0.04)" }} />
                  <Bar dataKey="count" name="returns" fill="#0A0A0A" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Panel>
        </div>

        {/* Two columns: reasons pie + top SKUs table */}
        <div className="mt-6 grid lg:grid-cols-2 gap-4 sm:gap-6">
          <Panel
            testid="reasons-panel"
            title={`Reason breakdown · last ${reasonDays} days`}
            right={
              <select
                value={reasonDays}
                onChange={(e) => setReasonDays(Number(e.target.value))}
                className="text-xs border border-[hsl(var(--border))] bg-white px-2 py-1 mono"
                data-testid="reason-days-select"
              >
                {[30, 60, 90, 180, 365].map((n) => <option key={n} value={n}>{n} days</option>)}
              </select>
            }
          >
            {reasons.length === 0 ? (
              <div className="text-sm text-[hsl(var(--ink-muted))] py-10 text-center">
                No return reasons in this window yet.
              </div>
            ) : (
              <div className="grid sm:grid-cols-[220px_1fr] gap-4 items-center">
                <div className="h-[200px]" data-testid="reasons-chart">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={reasons}
                        dataKey="count"
                        nameKey="label"
                        cx="50%"
                        cy="50%"
                        outerRadius={78}
                        innerRadius={44}
                        stroke="#fff"
                        strokeWidth={2}
                      >
                        {reasons.map((entry, i) => (
                          <Cell key={i} fill={entry.fill} />
                        ))}
                      </Pie>
                      <Tooltip content={<TooltipBox />} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
                <div className="text-sm space-y-1.5" data-testid="reasons-legend">
                  {reasons.map((r) => (
                    <div key={r.reason} className="flex items-center gap-2">
                      <span
                        className="inline-block w-2.5 h-2.5 shrink-0"
                        style={{ background: r.fill }}
                      />
                      <span className="min-w-0 truncate flex-1">{r.label}</span>
                      <span className="mono text-[hsl(var(--ink-muted))]">{r.count}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Panel>

          <Panel
            testid="top-skus-panel"
            title="Top 10 most-returned items"
          >
            {topSkus.length === 0 ? (
              <div className="text-sm text-[hsl(var(--ink-muted))] py-10 text-center">
                No returns yet.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="top-skus-table">
                  <thead>
                    <tr className="text-[hsl(var(--ink-muted))] label-caps">
                      <th className="text-left py-2 font-normal">Product</th>
                      <th className="text-left py-2 font-normal hidden sm:table-cell">SKU</th>
                      <th className="text-right py-2 font-normal">Units</th>
                      <th className="text-right py-2 font-normal">Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {topSkus.map((s, i) => (
                      <tr
                        key={`${s.sku}-${i}`}
                        className="border-t border-[hsl(var(--border))]"
                        data-testid={`sku-row-${i}`}
                      >
                        <td className="py-2 pr-2">
                          <div className="truncate max-w-[260px]" title={s.name}>{s.name}</div>
                        </td>
                        <td className="py-2 pr-2 mono text-xs text-[hsl(var(--ink-muted))] hidden sm:table-cell">
                          {s.sku || "—"}
                        </td>
                        <td className="py-2 pr-2 mono text-right">{s.units}</td>
                        <td className="py-2 mono text-right">{s.share_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>

        {/* Carrier transit */}
        <div className="mt-6">
          <Panel
            testid="carrier-panel"
            title="Average transit time · by carrier"
            right={<span className="text-[11px] mono text-[hsl(var(--ink-muted))]"><Truck size={11} className="inline mr-1" />label → delivered</span>}
          >
            {carrierTransit.length === 0 ? (
              <div className="text-sm text-[hsl(var(--ink-muted))] py-10 text-center">
                No delivered returns yet. Data will appear once tracking statuses flip to "delivered".
              </div>
            ) : (
              <div className="h-[240px]" data-testid="carrier-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={carrierTransit}
                    layout="vertical"
                    margin={{ top: 4, right: 32, left: 8, bottom: 0 }}
                  >
                    <CartesianGrid strokeDasharray="2 4" horizontal={false} stroke="#E5E5E0" />
                    <XAxis
                      type="number"
                      tick={{ fontSize: 10, fill: "#78716C" }}
                      axisLine={false}
                      tickLine={false}
                      unit="h"
                    />
                    <YAxis
                      type="category"
                      dataKey="carrier"
                      tick={{ fontSize: 11, fill: "#0A0A0A" }}
                      axisLine={false}
                      tickLine={false}
                      width={110}
                    />
                    <Tooltip content={<TooltipBox suffix="h" />} cursor={{ fill: "rgba(10,10,10,0.04)" }} />
                    <Bar dataKey="avg_hours" name="avg hours" fill="#047857">
                      <LabelList
                        dataKey="avg_hours"
                        position="right"
                        formatter={(v) => `${v}h`}
                        style={{ fontSize: 10, fill: "#78716C" }}
                      />
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </Panel>
        </div>

        <div className="mt-6 text-xs text-[hsl(var(--ink-muted))] mono text-right">
          Generated {data?.generated_at ? new Date(data.generated_at).toLocaleString() : "—"}
        </div>
      </div>
    </div>
  );
}
