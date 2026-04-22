import React, { useMemo, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useOrder } from "../lib/OrderContext";
import { api, REASON_OPTIONS, FREE_LABEL_REASONS, formatMoney } from "../lib/api";
import { Check, Camera, X } from "@phosphor-icons/react";
import { toast } from "sonner";

export default function SelectItems() {
  const nav = useNavigate();
  const { order, selection, setSelection, proofFiles, setProofFiles } = useOrder();
  const [blockedIds, setBlockedIds] = useState(new Set());
  const [compressing, setCompressing] = useState(false);

  const total = useMemo(
    () => Object.entries(selection).reduce((a, [id, sel]) => {
      const li = order?.line_items.find((x) => x.id === id);
      return a + (li ? li.price * sel.qty : 0);
    }, 0), [selection, order]);

  useEffect(() => {
    if (!order) { nav("/start", { replace: true }); return; }
    (async () => {
      try {
        const r = await api.get(
          `/returns/existing-items/${encodeURIComponent(order.order_number)}/${encodeURIComponent(order.email)}`
        );
        const ids = new Set((r.data?.line_item_ids || []).map(String));
        setBlockedIds(ids);
        setSelection((s) => {
          const next = { ...s };
          Object.keys(next).forEach((k) => { if (ids.has(String(k))) delete next[k]; });
          return next;
        });
      } catch {
        /* non-blocking */
      }
    })();
    // eslint-disable-next-line
  }, []);

  // Photos are only required when at least one selected item has a reason that
  // signals a product issue. For "changed my mind" style reasons, no photo
  // is asked for. The customer never sees this rule explicitly — the uploader
  // itself only appears when needed.
  const proofRequired = Object.values(selection).some((s) => FREE_LABEL_REASONS.has(s.reason));

  // If the customer removes the last product-issue reason, drop any photos
  // they already attached so the payload stays clean.
  useEffect(() => {
    if (!proofRequired && proofFiles.length > 0) {
      setProofFiles([]);
    }
    // eslint-disable-next-line
  }, [proofRequired]);

  if (!order) return null;

  const toggle = (li) => {
    if (blockedIds.has(String(li.id))) return;
    setSelection((s) => {
      const next = { ...s };
      if (next[li.id]) delete next[li.id];
      else next[li.id] = { qty: li.quantity, reason: "no_longer_needed", notes: "" };
      return next;
    });
  };
  const setReason = (id, reason) => setSelection((s) => ({ ...s, [id]: { ...s[id], reason } }));
  const setNotes = (id, notes) => setSelection((s) => ({ ...s, [id]: { ...s[id], notes } }));

  const addProofFiles = async (fileList) => {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    setCompressing(true);
    try {
      const next = [...proofFiles];
      for (const raw of incoming) {
        if (next.length >= 3) {
          toast.error("Max 3 photos. Remove one to add another.");
          break;
        }
        if (!raw.type?.startsWith("image/")) {
          toast.error(`"${raw.name}" is not an image.`);
          continue;
        }
        let f = raw;
        // Auto-compress anything over ~1.9MB so phone photos fit the 2MB limit.
        if (f.size > 1.9 * 1024 * 1024) {
          try {
            f = await compressImage(raw, { maxSide: 1800, quality: 0.82, target: 1.9 * 1024 * 1024 });
            if (f.size > 2 * 1024 * 1024) {
              toast.error(`"${raw.name}" is still over 2 MB after compression. Try a smaller image.`);
              continue;
            }
            toast.success(`"${raw.name}" was compressed to fit under 2 MB.`);
          } catch {
            toast.error(`"${raw.name}" could not be compressed — please pick a smaller image.`);
            continue;
          }
        }
        next.push(f);
      }
      setProofFiles(next);
    } finally {
      setCompressing(false);
    }
  };

  const removeProof = (idx) => {
    setProofFiles(proofFiles.filter((_, i) => i !== idx));
  };

  const count = Object.keys(selection).length;
  const anyBlocked = blockedIds.size > 0;
  const proofMissing = proofRequired && proofFiles.length === 0;

  return (
    <div className="min-h-screen bg-white fade-in pb-24 md:pb-0" data-testid="select-items-page">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-14">
        <div className="label-caps">Step 02 of 04 · Order #{order.order_number}</div>
        <h1 className="text-2xl sm:text-4xl lg:text-5xl mt-2 sm:mt-3 leading-tight">
          What are you returning?
        </h1>

        {anyBlocked && (
          <div
            className="mt-5 border border-[hsl(var(--border))] bg-[hsl(var(--surface))] p-4 text-sm"
            data-testid="already-returned-notice"
          >
            Some items from this order already have an active return. You can still return other
            items below.
          </div>
        )}

        <div className="grid md:grid-cols-12 gap-5 md:gap-10 mt-6 md:mt-12">
          <div className="md:col-span-8 space-y-3">
            {order.line_items.map((li) => {
              const blocked = blockedIds.has(String(li.id));
              const selected = !!selection[li.id];
              return (
                <div
                  key={li.id}
                  onClick={() => !blocked && toggle(li)}
                  className={`card-flat !p-4 sm:!p-6 cursor-pointer transition-colors
                    ${selected ? "border-[hsl(var(--ink))] bg-[hsl(var(--surface))]" : "hover:border-[hsl(var(--ink))]"}
                    ${blocked ? "opacity-60 cursor-not-allowed" : ""}`}
                  data-testid={`item-row-${li.id}`}
                >
                  <div className="flex gap-3 sm:gap-5">
                    <div className="w-16 h-16 sm:w-20 sm:h-20 bg-white border border-[hsl(var(--border))] overflow-hidden shrink-0">
                      {li.image && <img src={li.image} alt={li.name} className="w-full h-full object-cover" />}
                    </div>

                    <div className="flex-1 min-w-0">
                      <div className="flex gap-3">
                        <div className="min-w-0 flex-1">
                          <h3 className="text-sm sm:text-base font-medium leading-snug break-words">{li.name}</h3>
                          <div className="text-[11px] text-[hsl(var(--ink-muted))] mono mt-1">
                            {li.sku ? `${li.sku} · ` : ""}Qty {li.quantity}
                          </div>
                          <div className="mono text-sm mt-1 sm:hidden">{formatMoney(li.price)}</div>
                          {blocked && (
                            <span
                              className="inline-block mt-2 px-2 py-0.5 text-[10px] uppercase tracking-widest bg-[hsl(var(--surface))] border border-[hsl(var(--border))]"
                              data-testid={`already-returned-${li.id}`}
                            >
                              Already being returned
                            </span>
                          )}
                        </div>

                        <div className="hidden sm:flex flex-col items-end shrink-0 gap-2">
                          <div className="mono text-sm">{formatMoney(li.price)}</div>
                        </div>

                        {!blocked && (
                          <div
                            className={`self-start shrink-0 w-8 h-8 sm:w-7 sm:h-7 border-2 flex items-center justify-center transition-colors
                              ${selected ? "bg-[hsl(var(--ink))] border-[hsl(var(--ink))]" : "border-[hsl(var(--border))]"}`}
                            aria-checked={selected}
                            role="checkbox"
                            data-testid={`select-item-${li.id}`}
                          >
                            {selected && <Check size={16} weight="bold" className="text-white" />}
                          </div>
                        )}
                      </div>

                      {selected && !blocked && (
                        <div
                          onClick={(e) => e.stopPropagation()}
                          className="mt-4 pt-4 border-t border-[hsl(var(--border))] grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4"
                        >
                          <div>
                            <label className="label-caps block mb-2">Reason</label>
                            <select
                              className="input-field h-11 !text-sm"
                              value={selection[li.id].reason}
                              onChange={(e) => setReason(li.id, e.target.value)}
                              data-testid={`reason-select-${li.id}`}
                            >
                              {REASON_OPTIONS.map(([k, v]) => (
                                <option key={k} value={k}>{v}</option>
                              ))}
                            </select>
                          </div>
                          <div>
                            <label className="label-caps block mb-2">Notes (optional)</label>
                            <input
                              className="input-field h-11 !text-sm"
                              value={selection[li.id].notes}
                              onChange={(e) => setNotes(li.id, e.target.value)}
                              placeholder="Any details…"
                            />
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Desktop summary sidebar */}
          <div className="md:col-span-4 hidden md:block">
            <div className="card-flat md:sticky md:top-6">
              <div className="label-caps">Summary</div>
              <div className="mt-4 flex justify-between text-sm">
                <span>Items selected</span>
                <span className="mono">{count}</span>
              </div>
              <div className="mt-2 flex justify-between text-sm">
                <span>Refundable total</span>
                <span className="mono">{formatMoney(total)}</span>
              </div>

              {proofRequired && (
                <ProofUploader
                  files={proofFiles}
                  onAdd={addProofFiles}
                  onRemove={removeProof}
                  required={true}
                  compressing={compressing}
                />
              )}

              <button
                className="btn-primary w-full mt-6"
                disabled={count === 0 || proofMissing || compressing}
                onClick={() => nav("/return/method")}
                data-testid="continue-method-btn"
              >
                {compressing ? "Preparing photos…" : "Continue"}
              </button>
              {proofMissing && (
                <div className="mt-3 text-[11px] text-[hsl(var(--ink-muted))] text-center">
                  At least one proof photo is required to continue.
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Mobile sticky footer summary */}
      <div
        className="md:hidden fixed bottom-0 inset-x-0 bg-white border-t border-[hsl(var(--border))] px-4 py-3 z-30 shadow-[0_-4px_12px_rgba(0,0,0,0.04)]"
        data-testid="mobile-summary-bar"
      >
        {proofRequired && (
          <div className="mb-3">
            <ProofUploader
              files={proofFiles}
              onAdd={addProofFiles}
              onRemove={removeProof}
              required={true}
              compact
              compressing={compressing}
            />
          </div>
        )}
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-widest text-[hsl(var(--ink-muted))]">
              {count} {count === 1 ? "item" : "items"} · {formatMoney(total)}
            </div>
            <div className="text-xs text-[hsl(var(--ink-muted))] mt-0.5 truncate">
              {count === 0
                ? "Tap an item to select it"
                : proofMissing
                  ? "Add at least 1 photo to continue"
                  : "Ready to continue"}
            </div>
          </div>
          <button
            className="btn-primary h-11 px-5 text-sm shrink-0"
            disabled={count === 0 || proofMissing || compressing}
            onClick={() => nav("/return/method")}
            data-testid="continue-method-btn-mobile"
          >
            {compressing ? "Preparing…" : "Continue"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ProofUploader({ files, onAdd, onRemove, required, compact, compressing }) {
  const inputRef = React.useRef(null);
  return (
    <div className={compact ? "" : "mt-6 pt-6 border-t border-[hsl(var(--border))]"} data-testid="proof-uploader">
      <div className="flex items-center justify-between mb-2">
        <span className="label-caps">
          Proof photos {required && <span className="text-[hsl(var(--destructive))]">*</span>}
        </span>
        <span className="text-[10px] mono text-[hsl(var(--ink-muted))]">{files.length}/3 · ≤2 MB each</span>
      </div>
      <p className="text-[11px] text-[hsl(var(--ink-muted))] mb-3 leading-snug">
        Attach up to 3 clear photos showing the issue. Each image must be <span className="mono">≤ 2 MB</span> —
        larger photos are automatically resized for you.
      </p>
      <div className="grid grid-cols-3 gap-2">
        {files.map((f, i) => {
          const url = URL.createObjectURL(f);
          return (
            <div
              key={i}
              className="relative aspect-square border border-[hsl(var(--border))] overflow-hidden group"
              data-testid={`proof-thumb-${i}`}
            >
              <img src={url} alt={f.name} className="w-full h-full object-cover" />
              <button
                type="button"
                onClick={() => onRemove(i)}
                className="absolute top-1 right-1 w-6 h-6 bg-black/70 text-white flex items-center justify-center hover:bg-black transition-colors"
                data-testid={`proof-remove-${i}`}
                aria-label="Remove photo"
              >
                <X size={12} weight="bold" />
              </button>
            </div>
          );
        })}
        {files.length < 3 && (
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={compressing}
            className="aspect-square border border-dashed border-[hsl(var(--border))] hover:border-[hsl(var(--ink))] flex flex-col items-center justify-center gap-1 text-[hsl(var(--ink-muted))] hover:text-[hsl(var(--ink))] transition-colors disabled:opacity-50 disabled:cursor-wait"
            data-testid="proof-add-btn"
          >
            <Camera size={18} />
            <span className="text-[10px] mono uppercase tracking-widest">
              {compressing ? "Resizing…" : "Add"}
            </span>
          </button>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        onChange={(e) => {
          onAdd(e.target.files);
          e.target.value = "";
        }}
        data-testid="proof-file-input"
      />
    </div>
  );
}

/**
 * Down-scale + re-encode an image in the browser so it fits the 2MB server
 * cap. Tries decreasing qualities until the blob is under `target` bytes.
 * Returns a File so FormData treats it the same as the original upload.
 */
async function compressImage(file, { maxSide = 1800, quality = 0.82, target = 1.9 * 1024 * 1024 } = {}) {
  const dataUrl = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
  const img = await new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => resolve(im);
    im.onerror = reject;
    im.src = dataUrl;
  });
  const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(img.width * scale);
  canvas.height = Math.round(img.height * scale);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  const tryEncode = (q) => new Promise((resolve) => {
    canvas.toBlob((b) => resolve(b), "image/jpeg", q);
  });
  let q = quality;
  let blob = await tryEncode(q);
  while (blob && blob.size > target && q > 0.35) {
    q -= 0.12;
    blob = await tryEncode(q);
  }
  if (!blob) throw new Error("encode failed");
  const name = (file.name || "photo").replace(/\.(heic|heif|png|webp|jpg|jpeg)$/i, "") + ".jpg";
  return new File([blob], name, { type: "image/jpeg", lastModified: Date.now() });
}
