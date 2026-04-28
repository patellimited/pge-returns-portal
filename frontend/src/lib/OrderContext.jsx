import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";

const OrderCtx = createContext(null);

// sessionStorage key — cleared when the browser/tab is closed, so returning
// customers still start fresh next time, but the Back button and accidental
// page refreshes don't wipe their in-progress return.
const STORAGE_KEY = "pge_returns_state_v1";

const safeParse = (raw) => {
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
};

const loadInitial = () => {
  if (typeof window === "undefined") return {};
  return safeParse(window.sessionStorage.getItem(STORAGE_KEY)) || {};
};

export function OrderProvider({ children }) {
  const initial = loadInitial();
  const [order, setOrder] = useState(initial.order || null);
  const [selection, setSelection] = useState(initial.selection || {});
  const [method, setMethod] = useState(initial.method || null);
  const [returnAddress, setReturnAddress] = useState(initial.returnAddress || null);
  const [returnDoc, setReturnDoc] = useState(initial.returnDoc || null);
  // Customer proof photos — File[] held client-side until the return is
  // created, then uploaded via POST /returns/{id}/proof.
  // NOTE: `File` objects cannot be serialised to sessionStorage, so they are
  // intentionally NOT persisted. If the user navigates away and back mid-flow
  // they will need to re-pick their photos — acceptable trade-off.
  const [proofFiles, setProofFiles] = useState([]);

  // Write-through to sessionStorage on every state change. Skipped on the
  // initial render to avoid a redundant write, and guarded against JSON
  // errors (e.g. circular refs — shouldn't happen but safer).
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return; }
    try {
      window.sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ order, selection, method, returnAddress, returnDoc }),
      );
    } catch {
      /* quota exceeded / private mode — fall through silently, state still
         works in-memory for the current navigation. */
    }
  }, [order, selection, method, returnAddress, returnDoc]);

  const reset = useCallback(() => {
    setOrder(null); setSelection({}); setMethod(null);
    setReturnAddress(null); setReturnDoc(null); setProofFiles([]);
    try { window.sessionStorage.removeItem(STORAGE_KEY); } catch {}
  }, []);

  return (
    <OrderCtx.Provider value={{
      order, setOrder, selection, setSelection,
      method, setMethod, returnAddress, setReturnAddress,
      returnDoc, setReturnDoc,
      proofFiles, setProofFiles,
      reset,
    }}>
      {children}
    </OrderCtx.Provider>
  );
}
export const useOrder = () => useContext(OrderCtx);
