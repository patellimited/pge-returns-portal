import React, { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { api } from "../lib/api";
import { useOrder } from "../lib/OrderContext";
import { ArrowLeft, Lock } from "@phosphor-icons/react";
import { toast } from "sonner";

export default function OrderLookup() {
  const nav = useNavigate();
  const { setOrder, reset } = useOrder();
  const [orderId, setOrderId] = useState("");
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);

  const onSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const r = await api.post("/orders/lookup", { order_id: orderId.trim(), email: email.trim() });
      // Fresh lookup: clear any residual selection / returnDoc / method from a
      // previous return. This guarantees a brand-new RMA (and its own admin
      // log + customer email) whenever the customer returns more items from
      // the same order later.
      reset();
      setOrder(r.data);
      nav("/return/select");
    } catch (err) {
      toast.error(err.response?.data?.detail || "Order not found");
    } finally { setLoading(false); }
  };

  return (
    <div className="min-h-screen bg-white fade-in" data-testid="lookup-page">
      <div className="max-w-md mx-auto px-4 sm:px-6 py-10 sm:py-20">
        <Link to="/" className="inline-flex items-center text-sm text-[hsl(var(--ink-muted))] hover:text-[hsl(var(--ink))] mb-10 sm:mb-16" data-testid="back-home-link">
          <ArrowLeft size={14} className="mr-1" /> Back
        </Link>
        <div className="label-caps">Step 01 of 04</div>
        <h1 className="text-3xl sm:text-4xl lg:text-5xl mt-3">Find your order.</h1>
        <p className="mt-3 text-[hsl(var(--ink-muted))]">
          Enter the order number and the email address used at checkout.
        </p>

        <form onSubmit={onSubmit} className="mt-10 space-y-4">
          <div>
            <label className="label-caps block mb-2">Order number</label>
            <input
              className="input-field mono"
              value={orderId}
              onChange={(e) => setOrderId(e.target.value)}
              placeholder="e.g. 10482"
              required
              data-testid="order-id-input"
            />
          </div>
          <div>
            <label className="label-caps block mb-2">Email</label>
            <input
              type="email"
              className="input-field"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@example.com"
              required
              data-testid="email-input"
            />
          </div>
          <button className="btn-primary w-full mt-6" disabled={loading} data-testid="lookup-submit-btn">
            {loading ? "Locating…" : "Find order"}
          </button>
          <div
            data-testid="secure-chip"
            className="mt-4 inline-flex items-center gap-1.5 text-[10px] mono uppercase tracking-[0.22em] text-[hsl(var(--ink-muted))]"
            title="Your order lookup is sent over TLS and never stored."
          >
            <Lock size={11} weight="duotone" style={{ color: "hsl(130 33% 30%)" }} />
            Secure · Easy · Encrypted
          </div>
        </form>
      </div>
    </div>
  );
}
