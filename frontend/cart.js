// Shared cart state, stored in localStorage so it persists across pages
// on this site. This is a real standalone website (not a hosted chat
// artifact), so localStorage is the right tool here.

const CART_KEY = "pea_cart";

function getCart() {
  try {
    return JSON.parse(localStorage.getItem(CART_KEY)) || [];
  } catch {
    return [];
  }
}

function saveCart(cart) {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
  updateCartBadge();
}

function addToCart(sku, title, price, image, quantity = 1) {
  const cart = getCart();
  const existing = cart.find(item => item.sku === sku);
  if (existing) {
    existing.quantity += quantity;
  } else {
    cart.push({ sku, title, price, image, quantity });
  }
  saveCart(cart);
}

function removeFromCart(sku) {
  saveCart(getCart().filter(item => item.sku !== sku));
}

function updateQuantity(sku, quantity) {
  const cart = getCart();
  const item = cart.find(i => i.sku === sku);
  if (item) {
    item.quantity = Math.max(1, quantity);
    saveCart(cart);
  }
}

function cartTotal() {
  return getCart().reduce((sum, item) => sum + item.price * item.quantity, 0);
}

function cartCount() {
  return getCart().reduce((sum, item) => sum + item.quantity, 0);
}

function updateCartBadge() {
  const badge = document.getElementById('cart-badge');
  if (badge) badge.textContent = cartCount();
}

document.addEventListener('DOMContentLoaded', updateCartBadge);

// Escapes text before it goes into innerHTML (titles imported from the
// store can contain quotes and ampersands).
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Demo photo URLs use the reserved example.com domain and aren't real
// images; photos imported from the store (Shopify's CDN) are shown.
function productImage(url, alt) {
  let real = false;
  try {
    const host = new URL(url).hostname;
    real = !(host === "example.com" || host.endsWith(".example.com"));
  } catch {
    real = false;
  }
  return real
    ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy" onerror="this.outerHTML='<span>No photo yet</span>'">`
    : `<span>No photo yet</span>`;
}
