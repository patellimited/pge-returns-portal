import React, { useEffect, useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { api } from "../lib/api";
import { toast } from "sonner";
import { ArrowLeft, CheckCircle, XCircle, Plugs, Trash } from "@phosphor-icons/react";

const FIELD_GROUPS = [
  { title: "🌍 Global branding · used across the entire portal and every email",
    description: "These settings rebrand the whole portal in one go. Change `Store name` to e.g. \"Fleeky Queen\" and every page, every email header, every coupon description switches instantly. No code change, no redeploy.",
    fields: [
      { key: "store_name", label: "Store / brand name (shown on every page + email header)", secret: false, placeholder: "Patel Group" },
      { key: "support_email", label: "Customer service email (every email tells customers to write here)", secret: false, placeholder: "cs@pgelimited.com" },
      { key: "from_name", label: "Sender name on outgoing emails (\"From\")", secret: false, placeholder: "Patel Group Returns" },
      { key: "from_email", label: "Sender address on outgoing emails (must be verified at your email provider)", secret: false, placeholder: "returns@pgelimited.com" },
      { key: "logo_url", label: "Main logo URL (shown on the portal + email headers)", secret: false, placeholder: "https://..." },
      { key: "hero_image_url", label: "Landing page background image URL", secret: false, placeholder: "https://..." },
      { key: "admin_notification_email", label: "Admin notification email (new-return alerts)", secret: false, placeholder: "Returns@pgelimited.com" },
  ]},
  { title: "Return policy", fields: [
    { key: "max_return_window_days", label: "Max return window (days)", secret: false, placeholder: "30" },
  ]},
  { title: "Store credit (WooCommerce coupon instead of cash refund)", fields: [
    { key: "enable_store_credit", label: "Enable store credit option (true / false)", secret: false, placeholder: "true" },
    { key: "store_credit_bonus_percent", label: "Bonus % added on top of refund value", secret: false, placeholder: "5" },
    { key: "store_credit_expiry_days", label: "Coupon expiry (days)", secret: false, placeholder: "365" },
  ]},
  { title: "Weight-based shipping", fields: [
    { key: "default_item_weight_kg", label: "Default item weight (kg) if product has no weight in Woo", secret: false, placeholder: "1.0" },
    { key: "min_parcel_weight_kg", label: "Minimum parcel weight (kg)", secret: false, placeholder: "0.1" },
  ]},
  { title: "WooCommerce", fields: [
    { key: "wc_store_url", label: "Store URL", secret: false, placeholder: "https://yourstore.com" },
    { key: "wc_consumer_key", label: "Consumer key", secret: false, placeholder: "ck_..." },
    { key: "wc_consumer_secret", label: "Consumer secret", secret: true, placeholder: "cs_..." },
  ]},
  { title: "Shippo", fields: [
    { key: "shippo_api_key", label: "API token", secret: true, placeholder: "shippo_live_... (or shippo_test_... for test mode)" },
  ]},
  { title: "Easyship (fallback rate provider)", fields: [
    { key: "easyship_api_key", label: "API key (Easyship dashboard → Connect → API Integrations → Access Token, prod_…)", secret: true, placeholder: "prod_..." },
    { key: "easyship_box_slug", label: "Box slug (Easyship → Settings → Boxes — lowercase slug like 'default' or 'returns')", secret: false, placeholder: "default" },
  ]},
  { title: "Royal Mail Click & Drop (fallback rate provider)", fields: [
    { key: "royal_mail_api_key", label: "API key (Bearer token from Click & Drop → Settings → Integrations → API)", secret: true, placeholder: "rm_..." },
  ]},
  { title: "Stripe", fields: [
    { key: "stripe_publishable_key", label: "Publishable key (pk_live_… or pk_test_…)", secret: false, placeholder: "pk_live_..." },
    { key: "stripe_api_key", label: "Secret key (sk_live_… or sk_test_…)", secret: true, placeholder: "sk_live_..." },
  ]},
  { title: "Email delivery · Brevo (primary)", fields: [
    { key: "brevo_api_key", label: "Brevo API key (leave empty to disable)", secret: true, placeholder: "xkeysib-..." },
  ]},
  { title: "Email fallback · SendGrid", fields: [
    { key: "sendgrid_api_key", label: "SendGrid API key", secret: true, placeholder: "SG.xxxxx" },
  ]},
  { title: "Email fallback · Resend", fields: [
    { key: "resend_api_key", label: "Resend API key", secret: true, placeholder: "re_xxxxx" },
  ]},
  { title: "Email fallback · SMTP (Gmail / custom server)", fields: [
    { key: "smtp_host", label: "SMTP host", secret: false, placeholder: "smtp.gmail.com" },
    { key: "smtp_port", label: "SMTP port (587 TLS / 465 SSL)", secret: false, placeholder: "587" },
    { key: "smtp_user", label: "SMTP username", secret: false, placeholder: "returns@yourstore.com" },
    { key: "smtp_pass", label: "SMTP password / app password", secret: true, placeholder: "•••••" },
  ]},
  { title: "Email routing", fields: [
    { key: "email_provider_order", label: "Provider order (comma-separated, default: brevo,sendgrid,resend,smtp)", secret: false, placeholder: "brevo,sendgrid,resend,smtp" },
  ]},
  { title: "Return warehouse address", fields: [
    { key: "warehouse_name", label: "Name", secret: false },
    { key: "warehouse_street", label: "Street", secret: false },
    { key: "warehouse_city", label: "City", secret: false },
    { key: "warehouse_state", label: "State / County", secret: false },
    { key: "warehouse_zip", label: "ZIP / Postcode", secret: false },
    { key: "warehouse_country", label: "Country (e.g. US, GB)", secret: false },
    { key: "warehouse_phone", label: "Phone", secret: false },
    { key: "warehouse_email", label: "Email", secret: false },
  ]},
];

const CONN_LABELS = {
  woocommerce: "WooCommerce",
  stripe: "Stripe",
  shippo: "Shippo",
  easyship: "Easyship",
  brevo: "Brevo (email)",
  sendgrid: "SendGrid (email fallback)",
  resend: "Resend (email fallback)",
};

export default function AdminSettings() {
  const nav = useNavigate();
  const [data, setData] = useState(null);
  const [form, setForm] = useState({});
  const [initial, setInitial] = useState({}); // snapshot to compare against
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [connections, setConnections] = useState(null);
  const [resetting, setResetting] = useState(false);

  const load = async () => {
    try {
      const r = await api.get("/admin/settings");
      setData(r.data);
      const init = {};
      FIELD_GROUPS.forEach((g) => g.fields.forEach((f) => { init[f.key] = f.secret ? "" : (r.data[f.key] || ""); }));
      setForm(init);
      setInitial(init);
    } catch (e) {
      if (e.response?.status === 401) nav("/admin/login");
      else toast.error("Failed to load settings");
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      // Only send fields whose value changed from what was loaded — everything
      // else (including values the admin never touched) stays untouched in DB.
      const payload = {};
      Object.entries(form).forEach(([k, v]) => {
        const cur = String(v ?? "").trim();
        const prev = String(initial[k] ?? "").trim();
        if (cur !== prev && cur !== "") payload[k] = cur;
      });
      if (Object.keys(payload).length === 0) {
        toast.info("No changes to save.");
        setSaving(false);
        return;
      }
      const r = await api.put("/admin/settings", payload);
      setData(r.data.settings || r.data);
      if (r.data.connections) setConnections(r.data.connections);
      toast.success(`Saved ${Object.keys(payload).length} field(s) · connections re-validated`);
      // Refresh initial snapshot
      const reloaded = r.data.settings || r.data;
      const init = {};
      FIELD_GROUPS.forEach((g) => g.fields.forEach((f) => { init[f.key] = f.secret ? "" : (reloaded[f.key] || ""); }));
      setForm(init);
      setInitial(init);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    } finally { setSaving(false); }
  };

  const testNow = async () => {
    setTesting(true);
    try {
      const r = await api.post("/admin/settings/test");
      setConnections(r.data);
      const anyFail = Object.values(r.data).some((v) => !v.ok);
      anyFail ? toast.error("One or more connections failed — see status below") : toast.success("All integrations connected");
    } catch (e) {
      toast.error("Test failed");
    } finally { setTesting(false); }
  };

  const clearTestData = async () => {
    if (!window.confirm("Delete ALL return requests and payment transactions? This cannot be undone.")) return;
    setResetting(true);
    try {
      const r = await api.post("/admin/reset-test-data");
      toast.success(`Cleared ${r.data.returns_deleted} returns and ${r.data.transactions_deleted} transactions`);
    } catch (e) {
      toast.error("Reset failed");
    } finally { setResetting(false); }
  };

  if (!data) return <div className="min-h-screen grid place-items-center"><div className="label-caps">Loading…</div></div>;

  return (
    <div className="min-h-screen bg-white" data-testid="admin-settings-page">
      <header className="border-b border-[hsl(var(--border))] px-4 sm:px-6 lg:px-10 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-block h-2 w-2 bg-[hsl(var(--ink))]" />
          <span className="font-medium truncate">Settings</span>
        </div>
        <Link to="/admin" className="btn-secondary h-9 sm:h-10 px-3 sm:px-4 text-xs"><ArrowLeft size={14} className="sm:mr-1" /> <span className="hidden sm:inline">Dashboard</span></Link>
      </header>

      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <h1 className="text-2xl sm:text-3xl lg:text-4xl">Integrations &amp; store settings</h1>
        <p className="mt-3 text-[hsl(var(--ink-muted))] text-sm">
          Only the fields you fill in will be updated — every other setting stays exactly as it is.
          Secret fields can be left blank to keep the current value. Every integration call reads
          the latest values — no cache, no redeploy needed.
        </p>

        <div className="mt-6 flex flex-wrap gap-3">
          <button onClick={testNow} className="btn-secondary" disabled={testing} data-testid="test-connections-btn">
            <Plugs size={16} className="mr-2" /> {testing ? "Testing…" : "Test connections"}
          </button>
          <button onClick={clearTestData} className="btn-secondary" disabled={resetting} data-testid="reset-test-data-btn">
            <Trash size={16} className="mr-2" /> {resetting ? "Clearing…" : "Clear all return data"}
          </button>
        </div>

        {connections && (
          <div className="mt-6 border border-[hsl(var(--border))]" data-testid="connections-panel">
            {Object.entries(connections).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between px-4 py-3 border-b border-[hsl(var(--border))] last:border-b-0 text-sm">
                <div className="flex items-center gap-3">
                  {v.ok
                    ? <CheckCircle size={18} weight="fill" className="text-[hsl(var(--success))]" />
                    : <XCircle size={18} weight="fill" className="text-[hsl(var(--destructive))]" />}
                  <span className="font-medium">{CONN_LABELS[k] || k}</span>
                </div>
                <span className="mono text-xs text-[hsl(var(--ink-muted))]">{v.message}</span>
              </div>
            ))}
          </div>
        )}

        <form onSubmit={save} className="mt-10 space-y-10">
          {FIELD_GROUPS.map((g) => (
            <section key={g.title}>
              <div className="label-caps mb-2">{g.title}</div>
              {g.description && (
                <div className="text-xs text-[hsl(var(--ink-muted))] mb-4 max-w-3xl leading-relaxed">
                  {g.description}
                </div>
              )}
              <div className="grid sm:grid-cols-2 gap-3">
                {g.fields.map((f) => (
                  <div key={f.key} className={(f.key === "wc_store_url" || f.key === "warehouse_street" || f.key === "logo_url" || f.key === "hero_image_url") ? "sm:col-span-2" : ""}>
                    <label className="text-xs text-[hsl(var(--ink-muted))] block mb-1">{f.label}</label>
                    <input
                      className="input-field mono text-sm"
                      type={f.secret ? "password" : "text"}
                      placeholder={f.secret && data[`${f.key}_set`] ? `•••• ${data[`${f.key}_preview`] || "set"}` : (f.placeholder || "")}
                      value={form[f.key] || ""}
                      onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                      data-testid={`field-${f.key}`}
                    />
                    {f.secret && data[`${f.key}_set`] && <div className="text-[10px] mono text-[hsl(var(--ink-muted))] mt-1">Current: {data[`${f.key}_preview`]}</div>}
                  </div>
                ))}
              </div>
            </section>
          ))}

          <div className="flex justify-end pt-4 border-t border-[hsl(var(--border))]">
            <button type="submit" className="btn-primary" disabled={saving} data-testid="save-settings-btn">
              {saving ? "Saving & validating…" : "Save & validate"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
