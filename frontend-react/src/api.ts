import type { BestSeller, ProductPage } from "./types";

// Relative, like the original pages: the browser sends it to whichever
// server the page came from (Flask on 5050, or Vite's proxy on 5173).
const API = "/api";
export const PER_PAGE = 24;

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
  return (await response.json()) as T;
}

export function fetchProductPage(search: string, page: number): Promise<ProductPage> {
  const params = new URLSearchParams({ q: search, page: String(page), per_page: String(PER_PAGE) });
  return getJson<ProductPage>(`${API}/products?${params}`);
}

export function fetchBestSellers(): Promise<BestSeller[]> {
  return getJson<BestSeller[]>(`${API}/store/best-sellers`);
}
