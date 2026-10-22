import type { CardData } from "../types";
import { ProductImage } from "./ProductImage";
import { StockLabel } from "./StockLabel";

// Replaces card() in the original store.html. That version builds an HTML
// string and has to escape every title by hand (escapeHtml). Here React
// escapes text automatically, so a title containing "<" is always safe.

interface ProductCardProps {
  product: CardData;
}

export function ProductCard({ product }: ProductCardProps) {
  return (
    <a className="product-card" href={`/product?sku=${encodeURIComponent(product.sku)}`}>
      <div className="thumb">
        <ProductImage url={product.image} alt={product.title} />
      </div>
      <div className="info">
        <div className="p-title">{product.title}</div>
        {product.brand && <div className="p-brand">{product.brand}</div>}
        <div className="p-price">${product.price.toFixed(2)}</div>
        <StockLabel stock={product.stock} />
      </div>
    </a>
  );
}
