import { useEffect, useRef, useState } from "react";
import { fetchBestSellers, fetchProductPage } from "./api";
import type { BestSeller, CardData, Product, ProductPage } from "./types";
import { useDebouncedValue } from "./hooks/useDebouncedValue";
import { Header } from "./components/Header";
import { Pager } from "./components/Pager";
import { ProductCard } from "./components/ProductCard";
import { SearchBox } from "./components/SearchBox";

// The shop home page, rewritten from frontend/store.html:
//
//   store.html (plain JavaScript)          here (React + TypeScript)
//   -----------------------------------    --------------------------------
//   card(p) builds an HTML string          <ProductCard> component
//   stockLabel(qty)                        <StockLabel> component
//   loadProducts() + innerHTML = ...       state + useEffect; React redraws
//   setTimeout for the search box          useDebouncedValue hook
//   shop = { q, page } kept by hand        useState
//
// The big difference: the original tells the page *how* to change
// ("set this element's innerHTML"). React describes *what* the page should
// look like for the current state, and updates the page itself.

const fromProduct = (p: Product): CardData => ({
  sku: p.sku, title: p.title, brand: p.brand, price: p.price,
  image: p.images[0] ?? null, stock: p.inventory_qty,
});
const fromBestSeller = (b: BestSeller): CardData => ({
  sku: b.sku, title: b.title, price: b.price, image: b.image, stock: b.inventory_qty,
});

/** Search and page from the address bar, so links like ?q=tube&page=2 work. */
function readAddress() {
  const params = new URLSearchParams(window.location.search);
  return { q: params.get("q") ?? "", page: Number(params.get("page")) || 1 };
}

export default function App() {
  const [initial] = useState(readAddress);
  const [query, setQuery] = useState(initial.q);            // what's typed in the box
  const search = useDebouncedValue(query.trim(), 250);     // what's actually searched for

  // The page number remembers which search it belongs to, so a new search
  // starts at page 1 without any extra code to reset it.
  const [paging, setPaging] = useState({ search: initial.q.trim(), page: initial.page });
  const page = paging.search === search ? paging.page : 1;

  const [data, setData] = useState<ProductPage | null>(null);
  const [best, setBest] = useState<BestSeller[]>([]);
  const [failed, setFailed] = useState(false);
  const listTop = useRef<HTMLHeadingElement>(null);

  // Best sellers: once, when the page opens.
  useEffect(() => {
    fetchBestSellers().then(setBest).catch(() => setBest([]));
  }, []);

  // Products: again whenever the search or the page changes.
  useEffect(() => {
    let stale = false;   // set when a newer search replaces this one
    fetchProductPage(search, page)
      .then((result) => {
        if (!stale) {
          setData(result);
          setFailed(false);
        }
      })
      .catch(() => {
        if (!stale) setFailed(true);
      });

    const address = new URLSearchParams();
    if (search) address.set("q", search);
    if (page > 1) address.set("page", String(page));
    const addressText = address.toString();
    window.history.replaceState(null, "", addressText ? `?${addressText}` : window.location.pathname);

    return () => {
      stale = true;
    };
  }, [search, page]);

  function goToPage(next: number) {
    setPaging({ search, page: next });
    listTop.current?.scrollIntoView();
  }

  const title = !data
    ? "All items"
    : data.q
      ? `${data.total.toLocaleString()} ${data.total === 1 ? "result" : "results"} for “${data.q}”`
      : `All items (${data.total.toLocaleString()})`;

  return (
    <>
      <Header />
      <main>
        <h1 className="page-title">In stock now</h1>
        <p className="page-subtitle">Sample powersports and small-engine parts. A demo store for SkuTrail: nothing is charged or shipped.</p>

        {!search && best.length > 0 && (
          <section>
            <h2 className="section-title">Best sellers</h2>
            <div className="product-grid">
              {best.map((item) => (
                <ProductCard key={item.sku} product={fromBestSeller(item)} />
              ))}
            </div>
          </section>
        )}

        <section>
          <div className="shop-tools">
            <h2 className="section-title" ref={listTop}>{title}</h2>
            <SearchBox value={query} onChange={setQuery} />
          </div>
          <div className="product-grid">
            {data?.items.map((item) => (
              <ProductCard key={item.sku} product={fromProduct(item)} />
            ))}
            {data && data.items.length === 0 && (
              <p className="empty-results">
                Nothing matches that search. Try a part name, a model such as “Kodiak 450”, or a SKU.
              </p>
            )}
          </div>
          {data && <Pager page={data.page} pages={data.pages} onPage={goToPage} />}
          {failed && <p className="load-error">The shop couldn't load. Refresh the page to try again.</p>}
        </section>
      </main>
    </>
  );
}
