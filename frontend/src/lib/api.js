import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

export const api = axios.create({ baseURL: API });

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("admin_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

export const STATUS_LABELS = {
  draft: "Draft",
  awaiting_payment: "Awaiting payment",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  label_purchased: "Label purchased",
  in_transit: "In transit",
  delivered: "Delivered",
  refunded: "Refunded",
  store_credit_issued: "Closed — Store credit applied",
  rejected: "Rejected",
  cancelled: "Cancelled",
};

export const REASON_LABELS = {
  // Seller-responsible (free)
  defective: "Defective or does not work properly",
  damaged_outer_box: "Item and outer box both damaged",
  wrong_item: "Wrong item sent",
  missing_parts: "Missing parts or accessories",
  // Buyer-responsible (paid)
  no_longer_needed: "No longer needed / wanted",
  accidental_order: "Accidental order",
  better_price: "Better price available",
  poor_performance: "Performance or quality not adequate",
  incompatible: "Incompatible or not useful for intended purpose",
  // Legacy
  damaged: "Damaged",
  changed_mind: "Changed my mind",
  size_issue: "Size / fit issue",
  other: "Other",
};

// Reasons that auto-qualify for a free (seller-paid) return label.
export const FREE_LABEL_REASONS = new Set([
  "defective",
  "damaged_outer_box",
  "wrong_item",
  "missing_parts",
  // Legacy alias — keep older returns eligible
  "damaged",
]);

// Flat list of reasons. ORDER MATTERS — mixed so the customer cannot tell
// which options trigger manual review / free shipping.
export const REASON_OPTIONS = [
  ["no_longer_needed", "No longer needed / wanted"],
  ["defective", "Defective or does not work properly"],
  ["accidental_order", "Accidental order"],
  ["damaged_outer_box", "Item and outer box both damaged"],
  ["better_price", "Better price available"],
  ["wrong_item", "Wrong item sent"],
  ["poor_performance", "Performance or quality not adequate"],
  ["missing_parts", "Missing parts or accessories"],
  ["incompatible", "Incompatible or not useful for intended purpose"],
];

export function formatMoney(v, currency = "GBP") {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(v || 0));
}
