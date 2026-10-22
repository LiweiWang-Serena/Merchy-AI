// Reads the cart the original pages keep in the browser (frontend/cart.js),
// so both versions of the shop share one cart.
const CART_KEY = "pea_cart";

interface CartLine {
  sku: string;
  quantity: number;
}

export function cartCount(): number {
  try {
    const lines = JSON.parse(localStorage.getItem(CART_KEY) ?? "[]") as CartLine[];
    return lines.reduce((sum, line) => sum + line.quantity, 0);
  } catch {
    return 0;
  }
}
