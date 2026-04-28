import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "sonner";
import "@/App.css";

import { OrderProvider } from "./lib/OrderContext";
import { BrandingProvider } from "./lib/BrandingContext";
import Landing from "./pages/Landing";
import OrderLookup from "./pages/OrderLookup";
import SelectItems from "./pages/SelectItems";
import ReturnMethod from "./pages/ReturnMethod";
import ReturnSuccess from "./pages/ReturnSuccess";
import TrackReturn from "./pages/TrackReturn";
import AdminLogin from "./pages/AdminLogin";
import AdminDashboard from "./pages/AdminDashboard";
import AdminSettings from "./pages/AdminSettings";
import AdminAnalytics from "./pages/AdminAnalytics";

function RequireAdmin({ children }) {
  const token = localStorage.getItem("admin_token");
  if (!token) return <Navigate to="/admin/login" replace />;
  return children;
}

export default function App() {
  return (
    <div className="App">
      <Toaster position="top-center" richColors closeButton />
      <BrowserRouter>
        <BrandingProvider>
          <OrderProvider>
            <Routes>
              <Route path="/" element={<Landing />} />
              <Route path="/start" element={<OrderLookup />} />
              <Route path="/return/select" element={<SelectItems />} />
              <Route path="/return/method" element={<ReturnMethod />} />
              <Route path="/return/:returnId/success" element={<ReturnSuccess />} />
              <Route path="/track" element={<TrackReturn />} />
              <Route path="/admin/login" element={<AdminLogin />} />
              <Route path="/admin" element={<RequireAdmin><AdminDashboard /></RequireAdmin>} />
              <Route path="/admin/settings" element={<RequireAdmin><AdminSettings /></RequireAdmin>} />
              <Route path="/admin/analytics" element={<RequireAdmin><AdminAnalytics /></RequireAdmin>} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </OrderProvider>
        </BrandingProvider>
      </BrowserRouter>
    </div>
  );
}
