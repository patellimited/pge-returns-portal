import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { toast } from "sonner";

export default function AdminLogin() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const r = await api.post("/auth/login", { email, password });
      localStorage.setItem("admin_token", r.data.token);
      nav("/admin");
    } catch (err) {
      toast.error(err.response?.data?.detail || "Invalid credentials");
    } finally { setLoading(false); }
  };

  return (
    <div className="min-h-screen relative" data-testid="admin-login-page">
      <img
        src="https://images.unsplash.com/photo-1595246135406-803418233494?crop=entropy&cs=srgb&fm=jpg&q=80&w=1600"
        alt=""
        className="absolute inset-0 w-full h-full object-cover"
      />
      <div className="absolute inset-0 bg-white/40 backdrop-blur-sm" />
      <div className="relative z-10 grid place-items-center min-h-screen p-4 sm:p-6">
        <div className="w-full max-w-md card-flat bg-white/80 backdrop-blur-2xl border-white/60">
          <div className="label-caps">Admin access</div>
          <h1 className="text-2xl sm:text-3xl mt-2">Sign in</h1>
          <form onSubmit={submit} className="mt-6 space-y-3">
            <input type="email" className="input-field" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required data-testid="admin-email" />
            <input type="password" className="input-field" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} required data-testid="admin-password" />
            <button className="btn-primary w-full" disabled={loading} data-testid="admin-login-btn">
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
