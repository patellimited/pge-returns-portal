import React, { useEffect, useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { api, STATUS_LABELS, REASON_LABELS, formatMoney } from "../lib/api";
import { toast } from "sonner";
import { SignOut, Gear, ArrowsClockwise, X, Warning, Paperclip, CheckCircle, XCircle, Download, Trash, Archive, ArrowCounterClockwise, Image as ImageIcon, ChartBar } from "@phosphor-icons/react";

const STATUS_FILTERS = ["", "awaiting_approval", "awaiting_payment", "label_purchased", "in_transit", "delivered", "refunded", "store_credit_issued", "rejected"];

const METHOD_LABELS = {
  pay_stripe: "Paid (Stripe)",
  deduct_from_refund: "Deduct from refund",
  free_label: "Free label (admin)",
  store_credit: "Store credit",
  self_ship: "Self-ship (own carrier)",
};

export default function AdminDashboard() {
  const nav = useNavigate();
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [filter, setFilter] = useState("");
  const [viewArchived, setViewArchived] = useState(false);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  
  // NEW: State for the dynamic name
  const [storeName, setStoreName] = useState("");

  const [actionModal, setActionModal] = useState(null);
  const [confirmDlg, setConfirmDlg] = useState(null);

  const buildListUrl = () => {
    const params = [];
    if (filter) params.push(`status=${filter}`);
    if (viewArchived) params.push("archived=true");
    return `/admin/returns${params.length ? `?${params.join("&")}` : ""}`;
  };

  const load = async () => {
    setLoading(true);
    try {
      const [rRows, rStats, rSettings] = await Promise.all([
        api.get(buildListUrl()),
        api.get("/admin/stats"),
        api.get("/admin/settings").catch(() => ({ data: null }))
      ]);
      setRows(rRows.data); 
      setStats(rStats.data);
      
      if (rSettings?.data?.store_name) setStoreName(rSettings.data.store_name);
      else if (rSettings?.data?.brand_name) setStoreName(rSettings.data.brand_name);

    } catch (e) {
      if (e.response?.status === 401) { localStorage.removeItem("admin_token"); nav("/admin/login"); }
      else toast.error("Failed to load");
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [filter, viewArchived]);

  const logout = () => { localStorage.removeItem("admin_token"); nav("/admin/login"); };

  const refreshSelected = async (id) => {
    const fresh = (await api.get(buildListUrl())).data.find((x) => x.id === id);
    if (fresh) setSelected(fresh);
  };

  const submitActionWithAttachment = async ({ mode, returnId, note, file, method }) => {
    const path = mode === "approve" ? "/approve-free" : "/reject";
    const fileField = mode === "approve" ? "label_file" : "evidence_file";
    const fd = new FormData();
    fd.append("note", note || "");
    if (file) fd.append(fileField, file);
    try {
      const r = await api.post(`/admin/returns/${returnId}${path}`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      if (r.data?.email_sent) {
        toast.success(
          mode === "approve"
            ? method === "self_ship"
              ? "Approved — customer notified to ship the parcel"
              : "Approved — email with label sent to customer"
            : "Rejected — email with reason sent to customer"
        );
      } else {
        toast.success(
          mode === "approve" ? "Return approved" : "Return rejected" +
          " (email not sent — check email provider settings)"
        );
      }
      setActionModal(null);
      await load();
      await refreshSelected(returnId);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to submit");
    }
  };

  const doAction = async (id, path, label) => {
    try {
      const r = await api.post(`/admin/returns/${id}${path}`);
      toast.success(label);
      await load();
      const fresh = (await api.get(buildListUrl())).data.find((x) => x.id === id);
      if (fresh) setSelected(fresh);
      else if (r.data && r.data.id) setSelected(r.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const doDelete = async (id) => {
    try {
      await api.delete(`/admin/returns/${id}`);
      toast.success("Return deleted");
      setSelected(null);
      setConfirmDlg(null);
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  const doArchive = async (id, archive) => {
    try {
      await api.post(`/admin/returns/${id}/${archive ? "archive" : "unarchive"}`);
      toast.success(archive ? "Return archived" : "Return restored");
      setSelected(null);
      setConfirmDlg(null);
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Action failed");
    }
  };

  return (
    <div className="min-h-screen bg-white" data-testid="admin-dashboard">
      <header className="border-b border-[hsl(var(--border))] px-4 sm:px-6 lg:px-10 py-4 flex items-center justify-between gap-2">
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-block h-2 w-2 bg-[hsl(var(--ink))]" />
          <span className="font-medium truncate">
            {storeName ? `${storeName} return portal` : ""}
          </span>
          <span className="label-caps ml-2 sm:ml-4 hidden sm:inline">Admin</span>
        </div>
        <div className="flex items-center gap-2">
          <Link to="/admin/analytics" className="btn-secondary h-9 sm:h-10 px-3 sm:px-4 text-xs" data-testid="analytics-link">
            <ChartBar size={14} className="sm:mr-1" /> <span className="hidden sm:inline">Analytics</span>
          </Link>
          <Link to="/admin/settings" className="btn-secondary h-9 sm:h-10 px-3 sm:px-4 text-xs" data-testid="settings-link">
            <Gear size={14} className="sm:mr-1" /> <span className="hidden sm:inline">Settings</span>
          </Link>
          <button onClick={logout} className="btn-secondary h-9 sm:h-10 px-3 sm:px-4 text-xs" data-testid="logout-btn">
            <SignOut size={14} className="sm:mr-1" /> <span className="hidden sm:inline">Sign out</span>
          </button>
        </div>
      </header>

      <div className="px-4 sm:px-6 lg:px-10 py-6 sm:py-8">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-0 border border-[hsl(var(--border))]">
          <Stat label="Total" value={stats?.total ?? stats?.total_returns ?? "—"} />
          <Stat label="Awaiting approval" value={stats?.by_status?.awaiting_approval ?? 0} />
          <Stat label="Awaiting payment" value={stats?.by_status?.awaiting_payment ?? 0} />
          <Stat label="In transit" value={stats?.by_status?.in_transit ?? 0} />
          <Stat label="Refunded" value={stats?.by_status?.refunded ?? 0} />
        </div>

        <div className="mt-6 sm:mt-8 flex items-center justify-between flex-wrap gap-3">
          <div className="flex gap-2 flex-wrap">
            {STATUS_FILTERS.map((s) => (
              <button
                key={s || "all"}
                onClick={() => setFilter(s)}
                className={`px-3 h-9 text-xs border ${filter === s ? "bg-[hsl(var(--ink))] text-white border-[hsl(var(--ink))]" : "border-[hsl(var(--border))] hover:border-[hsl(var(--ink))]"}`}
                data-testid={`filter-${s || "all"}`}
              >
                {s ? STATUS_LABELS[s] : "All"}
              </button>
            ))}
            <button
              onClick={() => setViewArchived((v) => !v)}
              className={`px-3 h-9 text-xs border inline-flex items-center gap-1 ${viewArchived ? "bg-[hsl(var(--ink))] text-white border-[hsl(var(--ink))]" : "border-[hsl(var(--border))] hover:border-[hsl(var(--ink))]"}`}
              data-testid="filter-archived"
            >
              <Archive size={12} /> {viewArchived ? "Archived" : "Show archived"}
            </button>
          </div>
          <button onClick={load} className="btn-secondary h-9 px-4 text-xs"><ArrowsClockwise size={14} className="mr-1" /> Refresh</button>
        </div>

        <div className="mt-6 border border-[hsl(var(--border))] overflow-x-auto hidden sm:block">
          <table className="w-full text-sm">
            <thead className="bg-[hsl(var(--surface))]">
              <tr className="text-left">
                <th className="px-4 py-3 label-caps">RMA</th>
                <th className="px-4 py-3 label-caps">Order</th>
                <th className="px-4 py-3 label-caps">Customer</th>
                <th className="px-4 py-3 label-caps">Method</th>
                <th className="px-4 py-3 label-caps">Status</th>
                <th className="px-4 py-3 label-caps">Created</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} onClick={() => setSelected(r)} className="border-t border-[hsl(var(--border))] hover:bg-[hsl(var(--surface))] cursor-pointer" data-testid={`row-${r.rma_number}`}>
                  <td className="px-4 py-3 mono">{r.rma_number}</td>
                  <td className="px-4 py-3 mono">#{r.order_number}</td>
                  <td className="px-4 py-3">{r.customer_name}<div className="text-xs text-[hsl(var(--ink-muted))]">{r.email}</div></td>
                  <td className="px-4 py-3 text-xs">
                    <MethodBadge method={r.method} />
                    {r.refund_deduction > 0 && (
                      <span
                        className="ml-2 inline-flex items-center gap-1 px-2 py-0.5 text-[10px] uppercase tracking-widest bg-[hsl(var(--warning-bg))] text-[hsl(var(--warning))] border border-[hsl(var(--warning))]"
                        data-testid={`deduct-badge-${r.rma_number}`}
                      >
                        <Warning size={10} /> − {formatMoney(r.refund_deduction)}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3"><span className="status-badge">{STATUS_LABELS[r.status] || r.status}</span></td>
                  <td className="px-4 py-3 mono text-xs">{new Date(r.created_at).toLocaleDateString()}</td>
                </tr>
              ))}
              {!loading && rows.length === 0 && (
                <tr><td colSpan={6} className="p-10 text-center text-[hsl(var(--ink-muted))]">No returns yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="mt-4 sm:hidden space-y-3">
          {rows.map((r) => (
            <div
              key={r.id}
              onClick={() => setSelected(r)}
              className="border border-[hsl(var(--border))] p-4 bg-white cursor-pointer"
              data-testid={`card-${r.rma_number}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="mono text-sm">{r.rma_number}</div>
                  <div className="text-xs text-[hsl(var(--ink-muted))] mono">#{r.order_number}</div>
                </div>
                <span className="status-badge shrink-0">{STATUS_LABELS[r.status] || r.status}</span>
              </div>
              <div className="mt-2 text-sm truncate">{r.customer_name}</div>
              <div className="text-xs text-[hsl(var(--ink-muted))] truncate">{r.email}</div>
              <div className="mt-2 flex flex-wrap gap-1">
                <MethodBadge method={r.method} />
                {r.refund_deduction > 0 && (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] uppercase tracking-widest bg-[hsl(var(--warning-bg))] text-[hsl(var(--warning))] border border-[hsl(var(--warning))]">
                    <Warning size={10} /> − {formatMoney(r.refund_deduction)}
                  </span>
                )}
              </div>
            </div>
          ))}
          {!loading && rows.length === 0 && (
            <div className="border border-[hsl(var(--border))] p-10 text-center text-[hsl(var(--ink-muted))]">No returns yet.</div>
          )}
        </div>
      </div>

      {selected && (
        <div className="fixed inset-0 z-50 flex" data-testid="detail-drawer">
          <div className="flex-1 bg-black/40" onClick={() => setSelected(null)} />
          <div className="w-full sm:max-w-xl bg-white h-full overflow-y-auto border-l border-[hsl(var(--border))]">
            <div className="flex items-center justify-between px-4 sm:px-6 py-4 border-b border-[hsl(var(--border))]">
              <div>
                <div className="label-caps">Return detail</div>
                <div className="mono text-sm mt-1">{selected.rma_number}</div>
              </div>
              <button onClick={() => setSelected(null)} className="p-2 hover:bg-[hsl(var(--surface))]"><X size={18} /></button>
            </div>
            <div className="p-4 sm:p-6 space-y-5">
              {selected.refund_deduction > 0 && (
                <div className="border border-[hsl(var(--warning))] bg-[hsl(var(--warning-bg))] p-4" data-testid="deduct-panel">
                  <div className="flex items-center gap-2 label-caps" style={{ color: "hsl(var(--warning))" }}>
                    <Warning size={14} /> Customer chose: Deduct shipping from refund
                  </div>
                  <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-[hsl(var(--ink-muted))]">Refund</div>
                      <div className="mono">{formatMoney(selected.refund_amount || 0)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-[hsl(var(--ink-muted))]">Deduct</div>
                      <div className="mono">− {formatMoney(selected.refund_deduction || 0)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-[hsl(var(--ink-muted))]">Pay customer</div>
                      <div className="mono font-medium">{formatMoney(selected.refund_net ?? (selected.refund_amount - selected.refund_deduction))}</div>
                    </div>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-4 text-sm">
                <KV label="Order" v={`#${selected.order_number}`} />
                <KV label="Status" v={
                  <span className="inline-flex items-center gap-2">
                    {STATUS_LABELS[selected.status] || selected.status}
                    {selected.closed && (
                      <span data-testid="closed-badge" className="text-[10px] uppercase tracking-wider bg-[hsl(var(--ink))] text-white px-1.5 py-0.5">Closed</span>
                    )}
                  </span>
                } />
                <KV label="Customer" v={selected.customer_name} />
                <KV label="Email" v={selected.email} />
                <KV label="Method" v={selected.method_display_label || METHOD_LABELS[selected.method] || selected.method} />
                <KV label="Label cost" v={formatMoney(selected.label_cost || 0)} />
                {selected.refund_amount > 0 && <KV label="Refund amount" v={formatMoney(selected.refund_amount)} />}
                {selected.tracking_number && <KV label="Tracking" v={selected.tracking_number} />}
                {selected.email_provider_used && <KV label="Email via" v={selected.email_provider_used} />}
              </div>

              <div>
                <div className="label-caps mb-2">Items</div>
                <div className="space-y-2">
                  {selected.items.map((i) => (
                    <div key={i.line_item_id} className="flex justify-between text-sm border-b border-[hsl(var(--border))] pb-2 gap-3">
                      <div className="min-w-0">
                        <div className="truncate">{i.name}</div>
                        <div className="text-xs text-[hsl(var(--ink-muted))]">Qty {i.quantity} · {REASON_LABELS[i.reason]}</div>
                        {i.notes && <div className="text-xs mt-1">"{i.notes}"</div>}
                      </div>
                      <div className="mono shrink-0">{formatMoney(i.price * i.quantity)}</div>
                    </div>
                  ))}
                </div>
              </div>

              {Array.isArray(selected.customer_actions) && selected.customer_actions.length > 0 && (
                <div data-testid="customer-actions-panel">
                  <div className="label-caps mb-2">What the customer pressed</div>
                  <div className="space-y-2">
                    {selected.customer_actions.map((a, idx) => (
                      <div key={idx} className="border-l-2 border-[hsl(var(--ink))] pl-3 py-1">
                        <div className="text-sm">{a.label}</div>
                        <div className="text-[10px] mono text-[hsl(var(--ink-muted))]">
                          {a.kind} · {a.at ? new Date(a.at).toLocaleString() : ""}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {selected.method === "self_ship" && (
                <div data-testid="self-ship-admin-panel" className="border border-[hsl(var(--ink))] bg-[hsl(var(--surface))] p-4">
                  <div className="label-caps mb-2">Self-ship details</div>
                  {selected.self_ship_submitted_at ? (
                    <div className="space-y-2 text-sm">
                      <div className="flex justify-between gap-3">
                        <span className="text-[hsl(var(--ink-muted))]">Carrier</span>
                        <span className="mono text-right">{selected.self_ship_carrier || "—"}</span>
                      </div>
                      {selected.self_ship_tracking_number && (
                        <div className="flex justify-between gap-3">
                          <span className="text-[hsl(var(--ink-muted))]">Tracking</span>
                          <span className="mono text-right break-all">{selected.self_ship_tracking_number}</span>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="text-sm text-[hsl(var(--ink-muted))]">Customer hasn't submitted tracking yet.</div>
                  )}
                </div>
              )}

              {selected.admin_label_attachment && (
                <AttachmentRow
                  label="Approved label"
                  attachment={selected.admin_label_attachment}
                  href={`${api.defaults.baseURL}/admin/returns/${selected.id}/attachment/label`}
                />
              )}

              {Array.isArray(selected.customer_proof_photos) && selected.customer_proof_photos.length > 0 && (
                <div data-testid="customer-proof-panel">
                  <div className="label-caps mb-2 flex items-center gap-1.5">
                    <ImageIcon size={14} /> Customer proof photos ({selected.customer_proof_photos.length})
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    {selected.customer_proof_photos.map((p, i) => (
                      <ProofThumb key={i} idx={i} photo={p} returnId={selected.id} />
                    ))}
                  </div>
                </div>
              )}

              <InternalNotesPanel
                returnId={selected.id}
                notes={selected.internal_notes || []}
                onAdded={(entry) => {
                  setSelected({
                    ...selected,
                    internal_notes: [...(selected.internal_notes || []), entry],
                  });
                }}
              />

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-4 border-t border-[hsl(var(--border))]">
                {selected.status === "awaiting_approval" && (
                  <>
                    <button className="btn-primary" onClick={() => setActionModal({ mode: "approve", returnId: selected.id, rma: selected.rma_number, email: selected.email, method: selected.method, shippingChoice: selected.restricted_shipping_choice })}>
                      <CheckCircle size={16} className="mr-2" weight="fill" /> Approve
                    </button>
                    <button className="btn-secondary border-[hsl(var(--destructive))] text-[hsl(var(--destructive))]" onClick={() => setActionModal({ mode: "reject", returnId: selected.id, rma: selected.rma_number, email: selected.email, method: selected.method })}>
                      <XCircle size={16} className="mr-2" /> Reject with reason
                    </button>
                  </>
                )}
                {selected.status !== "refunded" && selected.status !== "rejected" && (
                  <button className="btn-primary sm:col-span-2" onClick={() => doAction(selected.id, "/mark-refunded", "Marked refunded")}>
                    Mark refunded
                  </button>
                )}
                <button className="btn-secondary" onClick={() => setConfirmDlg({ action: selected.archived ? "unarchive" : "archive", returnId: selected.id, rma: selected.rma_number })}>
                  {selected.archived ? <><ArrowCounterClockwise size={14} className="mr-2" /> Unarchive</> : <><Archive size={14} className="mr-2" /> Archive</>}
                </button>
                <button className="btn-secondary border-[hsl(var(--destructive))] text-[hsl(var(--destructive))]" onClick={() => setConfirmDlg({ action: "delete", returnId: selected.id, rma: selected.rma_number })}>
                  <Trash size={14} className="mr-2" /> Delete
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {confirmDlg && (
        <ConfirmDialog
          dlg={confirmDlg}
          onCancel={() => setConfirmDlg(null)}
          onConfirm={() => {
            if (confirmDlg.action === "delete") doDelete(confirmDlg.returnId);
            else if (confirmDlg.action === "archive") doArchive(confirmDlg.returnId, true);
            else if (confirmDlg.action === "unarchive") doArchive(confirmDlg.returnId, false);
          }}
        />
      )}

      {actionModal && (
        <ActionModal
          mode={actionModal.mode}
          rma={actionModal.rma}
          email={actionModal.email}
          method={actionModal.method}
          shippingChoice={actionModal.shippingChoice}
          financialStage={actionModal.financialStage}
          onClose={() => setActionModal(null)}
          onSubmit={({ note, file }) => submitActionWithAttachment({ ...actionModal, note, file })}
        />
      )}
    </div>
  );
}

// FULL HELPERS - ZERO TRIMMING
function ActionModal({ mode, rma, email, method, shippingChoice, financialStage, onClose, onSubmit }) {
  const [note, setNote] = useState("");
  const [file, setFile] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const isApprove = mode === "approve";
  const hideFileUpload = isApprove && (method === "self_ship" || (method === "store_credit" && shippingChoice === "self_ship") || financialStage === true);

  const handleFile = (e) => {
    const f = e.target.files?.[0] || null;
    if (f && f.size > 5 * 1024 * 1024) { toast.error("File larger than 5 MB"); return; }
    setFile(f);
  };

  const submit = async () => {
    if (!isApprove && !note.trim()) { toast.error("Reason required"); return; }
    setSubmitting(true);
    try { await onSubmit({ note: note.trim(), file }); }
    finally { setSubmitting(false); }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={submitting ? undefined : onClose} />
      <div className="relative bg-white border border-[hsl(var(--border))] w-full max-w-lg shadow-xl">
        <div className="flex items-start justify-between p-5 border-b border-[hsl(var(--border))]">
          <div>
            <div className="label-caps" style={{ color: isApprove ? "hsl(var(--success))" : "hsl(var(--destructive))" }}>
              {isApprove ? "Approve Return" : "Reject Return"}
            </div>
            <div className="mono text-sm mt-1">{rma}</div>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-[hsl(var(--surface))]" disabled={submitting}><X size={18} /></button>
        </div>
        <div className="p-5 space-y-5">
          <div>
            <label className="label-caps block mb-2">{isApprove ? "Note (optional)" : "Reason (required)"}</label>
            <textarea className="w-full min-h-[110px] border p-3 text-sm focus:outline-none focus:border-[hsl(var(--ink))]" value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
          {!hideFileUpload && (
            <div>
              <label className="label-caps block mb-2">{isApprove ? "Attach label" : "Attach evidence (optional)"}</label>
              <label className="flex items-center gap-3 p-3 border border-dashed cursor-pointer hover:border-[hsl(var(--ink))]">
                <Paperclip size={16} />
                <span className="text-sm truncate flex-1">{file ? file.name : "Choose file (max 5 MB)"}</span>
                <input type="file" className="hidden" onChange={handleFile} />
              </label>
            </div>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 p-5 border-t border-[hsl(var(--border))]">
          <button className="btn-secondary h-10 px-5 text-sm" onClick={onClose} disabled={submitting}>Cancel</button>
          <button className={`btn-primary h-10 px-6 text-sm ${!isApprove && "!bg-[hsl(var(--destructive))] !border-[hsl(var(--destructive))]"}`} onClick={submit} disabled={submitting}>
            {submitting ? "Sending…" : isApprove ? "Approve & notify" : "Reject & notify"}
          </button>
        </div>
      </div>
    </div>
  );
}

function InternalNotesPanel({ returnId, notes, onAdded }) {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(notes.length > 0);

  const submit = async () => {
    const text = draft.trim();
    if (!text) return;
    setSaving(true);
    try {
      const r = await api.post(`/admin/returns/${returnId}/internal-notes`, { text });
      setDraft("");
      onAdded?.(r.data?.note || { at: new Date().toISOString(), author: "admin", text });
      toast.success("Note saved");
    } catch { toast.error("Failed"); } finally { setSaving(false); }
  };

  return (
    <div className="border-t pt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="label-caps">Internal notes (admin-only)</div>
        <button onClick={() => setOpen(!open)} className="text-[10px] mono uppercase opacity-50 hover:opacity-100">{open ? "Hide" : `Show (${notes.length})`}</button>
      </div>
      {open && (
        <div className="space-y-3">
          {notes.map((n, idx) => (
            <div key={idx} className="border-l-2 border-[hsl(var(--ink))] pl-3 py-1 bg-[hsl(var(--surface))]">
              <div className="text-sm whitespace-pre-wrap">{n.text}</div>
              <div className="text-[10px] mono opacity-50 mt-1">{n.author} · {new Date(n.at).toLocaleString()}</div>
            </div>
          ))}
          <textarea className="w-full min-h-[80px] border p-2 text-sm" placeholder="Private note..." value={draft} onChange={(e) => setDraft(e.target.value)} disabled={saving} />
          <div className="flex justify-end"><button className="btn-primary h-8 px-4 text-[10px]" onClick={submit} disabled={saving || !draft.trim()}>{saving ? "Saving…" : "Add note"}</button></div>
        </div>
      )}
    </div>
  );
}

function AttachmentRow({ label, attachment, href }) {
  const token = localStorage.getItem("admin_token");
  const handleDownload = async (e) => {
    e.preventDefault();
    try {
      const r = await fetch(href, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = attachment?.filename || "attachment";
      a.click();
    } catch { toast.error("Download failed"); }
  };
  return (
    <div>
      <div className="label-caps mb-2">{label}</div>
      <button onClick={handleDownload} className="w-full flex items-center justify-between p-3 border hover:border-[hsl(var(--ink))] text-left">
        <div className="flex items-center gap-3 truncate">
          <Paperclip size={16} className="shrink-0" />
          <span className="text-sm truncate">{attachment?.filename}</span>
        </div>
        <Download size={16} className="shrink-0 opacity-50" />
      </button>
    </div>
  );
}

function ProofThumb({ idx, photo, returnId }) {
  const [url, setUrl] = useState(null);
  const token = localStorage.getItem("admin_token");
  const href = `${api.defaults.baseURL}/admin/returns/${returnId}/proof/${idx}`;

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const r = await fetch(href, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        const blob = await r.blob();
        if (active) setUrl(URL.createObjectURL(blob));
      } catch {}
    })();
    return () => { active = false; };
  }, [href, token]);

  const openFull = () => window.open(url, "_blank");

  return (
    <button onClick={openFull} className="aspect-square border overflow-hidden bg-[hsl(var(--surface))]">
      {url ? <img src={url} alt="proof" className="w-full h-full object-cover" /> : <div className="flex h-full items-center justify-center opacity-30"><ImageIcon size={20} /></div>}
    </button>
  );
}

function Stat({ label, value }) {
  return (
    <div className="p-3 sm:p-5 border-r border-b border-[hsl(var(--border))] last:border-r-0">
      <div className="label-caps text-[10px] sm:text-xs">{label}</div>
      <div className="mt-2 text-2xl sm:text-3xl font-medium mono">{value}</div>
    </div>
  );
}

function KV({ label, v }) {
  return <div><div className="label-caps mb-1">{label}</div><div className="mono break-words text-sm">{v}</div></div>;
}

function MethodBadge({ method }) {
  return <span className="inline-block px-2 py-0.5 text-[10px] uppercase tracking-widest bg-[hsl(var(--surface))] border border-[hsl(var(--border))]">{METHOD_LABELS[method] || method}</span>;
}

function ConfirmDialog({ dlg, onCancel, onConfirm }) {
  const isDelete = dlg.action === "delete";
  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={onCancel} />
      <div className="relative bg-white border border-[hsl(var(--border))] w-full max-w-md shadow-xl p-5">
        <div className="label-caps mb-1" style={{ color: isDelete ? "hsl(var(--destructive))" : "inherit" }}>{dlg.action} return?</div>
        <div className="mono text-sm mb-4">{dlg.rma}</div>
        <div className="text-sm opacity-70 mb-6">Are you sure you want to {dlg.action} this return request? This action may be permanent.</div>
        <div className="flex justify-end gap-2">
          <button className="btn-secondary h-10 px-5 text-sm" onClick={onCancel}>Cancel</button>
          <button className={`btn-primary h-10 px-6 text-sm ${isDelete && "!bg-[hsl(var(--destructive))] !border-[hsl(var(--destructive))]"}`} onClick={onConfirm}>Confirm</button>
        </div>
      </div>
    </div>
  );
}
