"""
Real eBay Inventory API client.

Setup (roughly half a day, mostly waiting on eBay's dashboard):
  1. Create an eBay Developer account: https://developer.ebay.com
  2. Create an application under "Application Keys" -- you'll get:
       Client ID (App ID), Client Secret (Cert ID)
     Do this for the *Production* environment once you're ready to sync
     your real listings (use *Sandbox* first to test safely).
  3. eBay's Inventory API needs a **user access token** (not just an app
     token), because it's acting on your seller account. Get one via
     eBay's OAuth consent flow:
       a. Redirect the seller (you) to eBay's consent URL with your
          Client ID, redirect URI, and requested scope
          (https://api.ebay.com/oauth/api_scope/sell.inventory)
       b. eBay redirects back with an authorization `code`
       c. Exchange that code for an access token + refresh token via
          POST https://api.ebay.com/identity/v1/oauth2/token
       d. Store the refresh token securely -- it's long-lived (18
          months) and lets you mint new access tokens without repeating
          the consent flow each time.
     Full walkthrough: https://developer.ebay.com/api-docs/static/oauth-tokens.html
  4. Set as environment variables:
       EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN

Docs: https://developer.ebay.com/api-docs/sell/inventory/resources/methods
"""

import os
import base64
import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
INVENTORY_URL = "https://api.ebay.com/sell/inventory/v1/inventory_item"


def _get_access_token():
    """Exchanges the long-lived refresh token for a short-lived (2hr)
    access token. Call this before each batch of API calls rather than
    caching it indefinitely."""
    credentials = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}"
    basic_auth = base64.b64encode(credentials.encode()).decode()

    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["EBAY_REFRESH_TOKEN"],
            "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_inventory_snapshot():
    """Fetch all inventory items (eBay's term for your SKU-level stock
    records) in the same shape as this project's other channel
    snapshots."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    snapshot = []
    url = f"{INVENTORY_URL}s?limit=100"  # /inventory_items, paginated

    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("inventoryItems", []):
            availability = item.get("availability", {}).get("shipToLocationAvailability", {})
            snapshot.append({
                "sku": item["sku"],
                "title": item.get("product", {}).get("title", ""),
                "quantity_available": availability.get("quantity", 0),
                "price": None,  # price lives on the linked Offer, not the inventory item
                "units_sold_last_30d": None,  # not exposed here; see Sold report note below
                "channel": "ebay",
            })

        url = data.get("next")  # eBay includes the next page URL directly, if any

    return snapshot


"""
Note on `units_sold_last_30d`: eBay doesn't return sales velocity from
the Inventory API. For real sales figures, use the Fulfillment API's
order search (GET /sell/fulfillment/v1/order) filtered by creation
date, and aggregate by SKU from each order's line items.
"""
