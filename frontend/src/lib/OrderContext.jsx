import React, { createContext, useContext, useState, useCallback } from "react";

const OrderCtx = createContext(null);

export function OrderProvider({ children }) {
  const [order, setOrder] = useState(null);
  const [selection, setSelection] = useState({}); // { line_item_id: { qty, reason, notes } }
  const [method, setMethod] = useState(null);
  const [returnAddress, setReturnAddress] = useState(null);
  const [returnDoc, setReturnDoc] = useState(null);
  // Customer proof photos — File[] held client-side until the return is
  // created, then uploaded via POST /returns/{id}/proof.
  const [proofFiles, setProofFiles] = useState([]);

  const reset = useCallback(() => {
    setOrder(null); setSelection({}); setMethod(null);
    setReturnAddress(null); setReturnDoc(null); setProofFiles([]);
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
