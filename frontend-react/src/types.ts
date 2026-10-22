// The shapes of the data the Flask API returns. Writing them down is the
// main thing TypeScript adds: every component below is checked against
// these, so a typo like `product.inventoryQty` is caught before it runs.

/** A product, as GET /api/products returns it (see backend/storefront.py). */
export interface Product {
  sku: string;
  title: string;
  brand: string | null;
  description_html: string | null;
  images: string[];
  price: number;
  inventory_qty: number;
  source_url: string | null;
}

/** One page of the shop: GET /api/products?page=...&per_page=...&q=... */
export interface ProductPage {
  items: Product[];
  total: number;
  page: number;
  pages: number;
  per_page: number;
  q: string;
}

/** A best seller: GET /api/store/best-sellers (see backend/reports.py). */
export interface BestSeller {
  sku: string;
  title: string;
  price: number;
  image: string | null;
  inventory_qty: number;
}

/** What a product card needs, whichever list the product came from. */
export interface CardData {
  sku: string;
  title: string;
  brand?: string | null;
  price: number;
  image: string | null;
  stock: number;
}
