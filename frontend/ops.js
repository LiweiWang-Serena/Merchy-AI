// Shared helpers for inventory.html and inventory-item.html.

const API = "/api";

const CHANNEL_NAMES = { amazon: "Amazon", ebay: "eBay", website: "Website" };
const CHANNEL_COLORS = { amazon: "#c2740c", ebay: "#2a5da8", website: "#1b7f4b" };
const STATUS_NAMES = {
  healthy: "Healthy",
  low: "Low stock",
  out_of_stock: "Out of stock",
  overstock: "Overstock",
  no_sales: "No recent sales",
  new: "Not stocked yet",
};

// Anything that came from user input (notes, references) or the database
// goes through esc() before it touches innerHTML.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function money(v) {
  if (v === null || v === undefined) return "—";
  const text = "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return v < 0 ? "−" + text : text;
}

function pct(v) {
  return v === null || v === undefined ? "—" : v.toFixed(1) + "%";
}

function num(v, digits = 0) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function times(v) {
  return v === null || v === undefined ? "—" : num(v, 1) + "×";
}

function statusTag(status) {
  return `<span class="tag ${esc(status)}">${esc(STATUS_NAMES[status] || status)}</span>`;
}

// Demo photo URLs use the reserved example.com domain, so those are
// skipped and the tile shows the SKU code instead. Real photo URLs (for
// example from Shopify) load normally and fall back to the code if the
// image fails.
function isPlaceholderImage(url) {
  try {
    const host = new URL(url).hostname;
    return host === "example.com" || host.endsWith(".example.com");
  } catch {
    return true;
  }
}

function tile(sku, image, big = false) {
  const parts = sku.split("-");
  const code = parts.length > 2 ? parts.slice(-2).join("-") : sku.slice(0, 10);
  const photo = image && !isPlaceholderImage(image)
    ? `<img src="${esc(image)}" alt="" onerror="this.remove()">`
    : "";
  return `<div class="tile${big ? " big" : ""}"><span>${esc(code)}</span>${photo}</div>`;
}

function readPeriod() {
  const p = parseInt(new URLSearchParams(location.search).get("period"), 10);
  return p === 90 ? 90 : 30;
}

function setupPeriodToggle(current, onChange) {
  const buttons = document.querySelectorAll("#period-toggle button");
  buttons.forEach(btn => {
    const period = parseInt(btn.dataset.period, 10);
    btn.classList.toggle("active", period === current);
    btn.setAttribute("aria-pressed", period === current);
    btn.onclick = () => {
      buttons.forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-pressed", b === btn);
      });
      onChange(period);
    };
  });
}
