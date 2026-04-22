import React, { createContext, useContext, useEffect, useState } from "react";
import { api } from "./api";

const BrandingCtx = createContext({
  store_name: "Returns", support_email: "", logo_url: "", hero_image_url: "", max_return_window_days: null,
});

export function BrandingProvider({ children }) {
  const [branding, setBranding] = useState({
    store_name: "Returns", support_email: "", logo_url: "", hero_image_url: "", max_return_window_days: null,
  });
  useEffect(() => {
    api.get("/branding").then((r) => setBranding(r.data)).catch(() => {});
  }, []);
  return <BrandingCtx.Provider value={branding}>{children}</BrandingCtx.Provider>;
}
export const useBranding = () => useContext(BrandingCtx);
