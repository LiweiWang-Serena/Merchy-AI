"""
Real Shopify Admin API client.

Setup (takes ~5 minutes since you already have the store):
  1. In your Shopify Admin, go to Settings > Apps and sales channels >
     Develop apps > Create an app.
  2. Under "Configuration", grant Admin API scopes:
       read_products, read_inventory, write_inventory
  3. Click "Install app", then reveal and copy the Admin API access token
     (starts with `shpat_...`). Shopify only shows it once.
  4. Set these two values as environment variables (never hard-code
     them or commit them to git):
       SHOPIFY_SHOP = "yourstore"        (the part before .myshopify.com)
       SHOPIFY_ACCESS_TOKEN = "shpat_..."

Docs: https://shopify.dev/docs/api/admin-rest/latest/resources/product
"""

import os
import requests

API_VERSION = "2024-10"


def _base_url():
    shop = os.environ["SHOPIFY_SHOP"]
    return f"https://{shop}.myshopify.com/admin/api/{API_VERSION}"


def _headers():
    return {
        "X-Shopify-Access-Token": os.environ["SHOPIFY_ACCESS_TOKEN"],
        "Content-Type": "application/json",
    }


def get_inventory_snapshot():
    """Fetch every product variant with its SKU, price, and available
    inventory, in the same shape as this project's other channel
    snapshots so it can drop straight into sync.py."""
    products = _get_all_products()

    snapshot = []
    for product in products:
        for variant in product.get("variants", []):
            if not variant.get("sku"):
                continue  # skip variants without a SKU -- can't reconcile these
            snapshot.append({
                "sku": variant["sku"],
                "title": product["title"],
                "quantity_available": variant.get("inventory_quantity", 0),
                "price": float(variant.get("price", 0)),
                "units_sold_last_30d": None,  # Shopify doesn't expose this
                                               # directly here -- see note below
                "channel": "shopify",
            })
    return snapshot


def _get_all_products():
    """Paginates through /products.json using Shopify's cursor-based
    pagination (Link header), since a real catalog is usually bigger
    than the 250-item single-page limit."""
    url = f"{_base_url()}/products.json?limit=250"
    all_products = []

    while url:
        resp = requests.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        all_products.extend(data.get("products", []))

        # Shopify returns the next page URL in the Link header, not the body.
        url = None
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")

    return all_products


def update_inventory_level(inventory_item_id: int, location_id: int, available: int):
    """Sets an exact inventory count for one variant at one location.
    Needed after a sale on another channel to keep Shopify's count
    correct. Requires the `write_inventory` scope."""
    url = f"{_base_url()}/inventory_levels/set.json"
    resp = requests.post(url, headers=_headers(), json={
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": available,
    })
    resp.raise_for_status()
    return resp.json()


"""
Note on `units_sold_last_30d`: Shopify's Admin API doesn't return sales
velocity on the products endpoint. To get real 30-day sales figures,
query the Orders API instead:

    GET /admin/api/2024-10/orders.json?status=any&created_at_min=<30 days ago>

...and aggregate line_items by SKU. Left as a follow-up since it's a
separate endpoint with its own pagination, and the sync/reporting
pipeline already handles a SKU showing 0 sales gracefully.
"""
