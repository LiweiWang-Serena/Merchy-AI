"""
Import a product into inventory by pasting its page link from your store.

This reads the product's *public* storefront data -- the JSON every
Shopify store serves at /products/<handle>.json, which is what the
product page itself is built from. No credentials needed. It brings in
the name, each variant's SKU and price, the vendor, the description and
the photos.

It deliberately does NOT bring in stock levels, sales or costs: Shopify
only exposes those through the authenticated Admin API, which needs the
store owner's authorization (see integrations/shopify_client.py).
Imported items start with no stock until you record stock received.

Imports are limited to the store set in STORE_DOMAIN. This is for
bringing your own catalog in, not for copying other sellers' listings.
"""

import os
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from db import get_connection, ensure_storefront_tables

DEFAULT_STORE_DOMAIN = ""   # set STORE_DOMAIN in backend/.env, e.g. yourstore.com
# Shopify handles are lowercase letters, digits and hyphens. Matching
# only those also stops cleanly at anything pasted after the link.
HANDLE_PATTERN = re.compile(r"/products/([a-z0-9][a-z0-9-]*)")
TIMEOUT_SECONDS = 10
USER_AGENT = "ChannelLedger/1.0 (inventory import)"


class StoreError(Exception):
    """The store couldn't be reached or didn't return usable data."""


def store_domain():
    domain = os.environ.get("STORE_DOMAIN", DEFAULT_STORE_DOMAIN).strip().lower()
    return domain.removeprefix("https://").removeprefix("http://").strip("/")


def parse_product_url(url, domain=None):
    """Returns the product handle from a product page link on the store.
    Raises ValueError with a message the user can act on."""
    domain = domain or store_domain()
    if not domain:
        raise ValueError("No store is set up for importing yet. Add STORE_DOMAIN=yourstore.com "
                         "to backend/.env and restart the server.")
    text = (url or "").strip()
    if not text:
        raise ValueError("Paste a product link first.")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Paste the full product link, starting with https://")
    host = (parsed.hostname or "").lower()
    if host not in (domain, f"www.{domain}"):
        raise ValueError(_wrong_site_message(host, parsed.path.lower(), domain))
    match = HANDLE_PATTERN.search(parsed.path.lower())
    if not match:
        raise ValueError(
            "That link isn't a product page. Open the product on the store "
            "and copy the link from the address bar; it contains /products/."
        )
    return match.group(1)


MARKETPLACES = {"ebay.com": "an eBay", "amazon.com": "an Amazon", "walmart.com": "a Walmart"}


def _wrong_site_message(host, path, domain):
    """Says which site the link is from, since the usual mistake is
    copying the right product from the wrong place."""
    fix = f"Open the product on {domain} and copy the link from the address bar."
    if host == "admin.shopify.com" or path.startswith("/admin"):
        return f"That's a link to the Shopify admin, not the store's product page. {fix}"
    if host.endswith(".myshopify.com"):
        return f"That's the store's internal myshopify.com address. {fix}"
    for site, name in MARKETPLACES.items():
        if host == site or host.endswith(f".{site}"):
            return f"That's {name} listing, not the store's product page. {fix}"
    return (f"That link is from {host or 'an unknown site'}, but only products from {domain} "
            "can be imported. To use a different store, set STORE_DOMAIN in backend/.env.")


def fetch_product(handle, domain=None):
    """Fetches the public product JSON for one handle."""
    import requests  # only needed for live imports

    domain = domain or store_domain()
    url = f"https://{domain}/products/{handle}.json"
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS,
                            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except requests.RequestException:
        raise StoreError(f"Couldn't reach {domain}. Check your internet connection and try again.")

    if resp.status_code == 404:
        raise ValueError("That product wasn't found on the store. It may have been removed or renamed.")
    if resp.status_code == 429:
        raise StoreError("The store is limiting requests right now. Wait a minute and try again.")
    if not resp.ok:
        raise StoreError(f"The store returned an error (HTTP {resp.status_code}). Try again later.")
    try:
        product = resp.json()["product"]
    except (ValueError, KeyError, TypeError):
        raise StoreError("The store didn't return product data for that link.")
    return product


def product_to_rows(product, domain):
    """One inventory row per variant, since each variant (size, color,
    fitment) is its own SKU with its own stock."""
    variants = product.get("variants") or []
    if not variants:
        raise StoreError("That product has no variants to import.")

    images = [img["src"] for img in product.get("images") or [] if img.get("src")]
    image_by_id = {img.get("id"): img["src"] for img in product.get("images") or [] if img.get("src")}
    description = _html_to_safe_paragraphs(product.get("body_html") or "")
    page_url = f"https://{domain}/products/{product.get('handle', '')}"
    base_title = (product.get("title") or "Untitled product").strip()

    rows = []
    for v in variants:
        store_sku = (v.get("sku") or "").strip()
        title = base_title
        if v.get("title") and v["title"] != "Default Title":
            title = f"{base_title} ({v['title']})"
        variant_images = images
        if v.get("image_id") in image_by_id:
            first = image_by_id[v["image_id"]]
            variant_images = [first] + [i for i in images if i != first]
        rows.append({
            "sku": (store_sku or f"SHOPIFY-{v.get('id')}")[:64],
            "sku_from_store": bool(store_sku),
            "title": title[:200],
            "brand": (product.get("vendor") or "").strip() or None,
            "description_html": description,
            "images": variant_images[:8],
            "price": round(float(v.get("price") or 0), 2),
            "source_url": f"{page_url}?variant={v['id']}" if len(variants) > 1 and v.get("id") else page_url,
        })
    return rows


def save_products(rows):
    """Adds new items, or refreshes name, price and photos for items
    imported before. Stock is never touched here: it comes only from
    the inventory ledger."""
    ensure_storefront_tables()
    conn = get_connection()
    results = []
    try:
        for r in rows:
            fields = (r["title"], r["brand"], r["description_html"],
                      _json_list(r["images"]), r["price"], r["source_url"])
            exists = conn.execute("SELECT 1 FROM products WHERE sku = ?", (r["sku"],)).fetchone()
            if exists:
                conn.execute(
                    """UPDATE products SET title = ?, brand = ?, description_html = ?,
                       images_json = ?, price = ?, source_url = ? WHERE sku = ?""",
                    fields + (r["sku"],))
            else:
                conn.execute(
                    """INSERT INTO products (title, brand, description_html, images_json,
                       price, source_url, sku, inventory_qty) VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                    fields + (r["sku"],))
            results.append({"sku": r["sku"], "title": r["title"], "price": r["price"],
                            "created": not exists, "sku_from_store": r["sku_from_store"]})
        conn.commit()
    finally:
        conn.close()
    return results


def import_from_url(url):
    domain = store_domain()
    handle = parse_product_url(url, domain)
    product = fetch_product(handle, domain)
    return {"store": domain, "handle": handle,
            "items": save_products(product_to_rows(product, domain))}


def _json_list(values):
    import json
    return json.dumps(list(values))


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()  # convert_charrefs=True decodes &amp; etc.
        self.parts = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skipping += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_lines(html):
    """Plain-text lines of an HTML fragment, with scripts and styles
    dropped and whitespace normalized."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return [line for line in lines if line]


def _html_to_safe_paragraphs(html):
    """The storefront renders descriptions as HTML, so imported markup is
    reduced to plain-text paragraphs and re-escaped: no scripts, styles,
    event handlers or links survive the import."""
    return "".join(f"<p>{escape(line)}</p>" for line in html_to_lines(html))[:20000]
