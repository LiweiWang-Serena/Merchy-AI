"""
Populates the customer-facing `products` table from the same catalog
used by the Amazon/eBay mocks, so the storefront, the inventory sync,
and the eBay listing generator are all describing the same underlying
products -- exactly like a real multi-channel seller's data should be.
"""

import json
import random
from mock_apis.amazon_api import CATALOG, PRODUCT_DETAILS
from listing_generator import generate_ebay_description_html
from db import get_connection, ensure_storefront_tables

# Filler detail for catalog items that don't have rich PRODUCT_DETAILS yet,
# so every SKU still renders a reasonable storefront page.
FALLBACK_DETAIL = {
    "brand": "Merchy-AI Demo",
    "bullets": ["Inspected and authenticated by our team", "Ships insured"],
    "images": ["https://images.example.com/placeholder/item.jpg"],
}


def seed_products(force: bool = False):
    ensure_storefront_tables()
    conn = get_connection()

    existing = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    if existing and not force:
        conn.close()
        return

    conn.execute("DELETE FROM products")
    rng = random.Random(42)

    for item in CATALOG:
        detail = PRODUCT_DETAILS.get(item["sku"], FALLBACK_DETAIL)
        product_like = {"title": item["title"], "brand": detail.get("brand"), "bullets": detail["bullets"]}
        description_html = generate_ebay_description_html(product_like)

        conn.execute(
            """INSERT INTO products (sku, title, brand, description_html, images_json, price, inventory_qty)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                item["sku"],
                item["title"],
                detail.get("brand"),
                description_html,
                json.dumps(detail["images"]),
                round(rng.uniform(45, 780), 2),
                rng.randint(1, 25),
            ),
        )
    conn.commit()
    conn.close()


def get_all_products():
    ensure_storefront_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM products ORDER BY title").fetchall()
    conn.close()
    return [_row_to_product(r) for r in rows]


def product_page(q="", page=1, per_page=24):
    """One page of the shop, in-stock items first, searched by words in
    the title or SKU. Runs in SQL, so it stays fast with a large catalog."""
    ensure_storefront_tables()
    per_page = max(6, min(int(per_page), 96))
    clauses, params = [], []
    for word in (q or "").lower().split()[:6]:
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(LOWER(title) LIKE ? ESCAPE '\\' OR LOWER(sku) LIKE ? ESCAPE '\\')")
        params += [pattern, pattern]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM products {where}", params).fetchone()[0]
        pages = max(1, -(-total // per_page))
        page = min(max(1, int(page)), pages)
        rows = conn.execute(
            f"SELECT * FROM products {where} ORDER BY (inventory_qty > 0) DESC, title LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page]).fetchall()
    finally:
        conn.close()
    return {"items": [_row_to_product(r) for r in rows], "total": total, "page": page,
            "pages": pages, "per_page": per_page, "q": q or ""}


def get_product(sku: str):
    ensure_storefront_tables()
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    conn.close()
    return _row_to_product(row) if row else None


def _row_to_product(row):
    d = dict(row)
    d["images"] = json.loads(d.pop("images_json") or "[]")
    return d
