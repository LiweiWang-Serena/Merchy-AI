import { useEffect, useState } from "react";
import { cartCount } from "../cart";

export function Header() {
  const [count, setCount] = useState(cartCount);

  // If the cart changes in another tab, the browser fires a "storage" event.
  useEffect(() => {
    const refresh = () => setCount(cartCount());
    window.addEventListener("storage", refresh);
    return () => window.removeEventListener("storage", refresh);
  }, []);

  return (
    <header className="site-header">
      <a className="brand" href="/react/">SkuTrail Demo Store</a>
      <nav>
        <a href="/">Original version</a>
        <a className="cart-link" href="/cart">
          Cart <span id="cart-badge">{count}</span>
        </a>
      </nav>
    </header>
  );
}
