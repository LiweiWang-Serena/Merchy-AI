"""
Real Amazon Selling Partner API (SP-API) client.

This is the slowest of the three integrations to set up -- budget days
to a couple of weeks, mostly waiting on Amazon, not on writing code.

Setup:
  1. You need an active Amazon Seller Central account (Professional
     selling plan, not Individual -- SP-API isn't available on the
     Individual plan).
  2. Register as a developer in Seller Central:
     Settings > User Permissions > (your account) > "Manage" under
     "Third-party developer and apps" -- or apply for your own private
     SP-API app under Apps & Services > Develop apps for Amazon.
  3. Amazon requires you to specify which SP-API "roles" you need
     (e.g. "Inventory and Order Tracking"). Some roles are
     self-authorized instantly; others require Amazon's review, which
     is the part that can take days to weeks.
  4. Once approved, Amazon gives you:
       - a Client ID / Client Secret (LWA credentials, for
         Login-with-Amazon OAuth)
       - a Refresh Token (generated when you authorize your own app
         against your own seller account)
     You'll also need an AWS IAM user with SP-API permissions for
     request signing (SigV4) -- Amazon's setup guide walks through
     creating this.
  5. Install the community SDK, which handles the LWA + SigV4
     complexity for you:
       pip install python-amazon-sp-api
  6. Set as environment variables:
       SP_API_REFRESH_TOKEN, SP_API_LWA_APP_ID, SP_API_LWA_CLIENT_SECRET,
       SP_API_AWS_ACCESS_KEY, SP_API_AWS_SECRET_KEY, SP_API_ROLE_ARN

Full setup walkthrough: https://developer-docs.amazon.com/sp-api/docs/registering-your-application
SDK docs: https://github.com/saleweaver/python-amazon-sp-api
"""

import os

try:
    from sp_api.api import ListingsItems, Inventories
    from sp_api.base import Marketplaces
except ImportError:
    ListingsItems = Inventories = Marketplaces = None  # SDK not installed yet


def _credentials():
    return {
        "refresh_token": os.environ["SP_API_REFRESH_TOKEN"],
        "lwa_app_id": os.environ["SP_API_LWA_APP_ID"],
        "lwa_client_secret": os.environ["SP_API_LWA_CLIENT_SECRET"],
        "aws_access_key": os.environ["SP_API_AWS_ACCESS_KEY"],
        "aws_secret_key": os.environ["SP_API_AWS_SECRET_KEY"],
        "role_arn": os.environ["SP_API_ROLE_ARN"],
    }


def get_inventory_snapshot():
    """Fetch FBA-managed inventory quantities via the Inventories API,
    in the same shape as this project's other channel snapshots.

    If you fulfill orders yourself (not Fulfilled-by-Amazon), use the
    Listings Items API's `fulfillmentAvailability` field instead --
    that's what tracks merchant-fulfilled stock."""
    if Inventories is None:
        raise RuntimeError(
            "Install the SDK first: pip install python-amazon-sp-api"
        )

    client = Inventories(credentials=_credentials(), marketplace=Marketplaces.US)
    response = client.get_inventory_summary_marketplace(details=True)

    snapshot = []
    for summary in response.payload.get("inventorySummaries", []):
        qty = summary.get("inventoryDetails", {}).get("fulfillableQuantity", 0)
        snapshot.append({
            "sku": summary["sellerSku"],
            "asin": summary.get("asin"),
            "title": summary.get("productName", ""),
            "quantity_available": qty,
            "price": None,  # price comes from the Product Pricing API, not Inventories
            "units_sold_last_30d": None,  # see note below
            "channel": "amazon",
        })
    return snapshot


def update_listing_quantity(sku: str, quantity: int, marketplace_id: str):
    """Updates merchant-fulfilled stock for one SKU via the Listings
    Items API's `patchListingsItem` operation. (FBA inventory isn't
    updated this way -- Amazon manages FBA stock counts itself based on
    what you've shipped in.)"""
    if ListingsItems is None:
        raise RuntimeError(
            "Install the SDK first: pip install python-amazon-sp-api"
        )

    client = ListingsItems(credentials=_credentials(), marketplace=Marketplaces.US)
    return client.patch_listings_item(
        sellerId=os.environ["SP_API_SELLER_ID"],
        sku=sku,
        marketplaceIds=[marketplace_id],
        body={
            "productType": "PRODUCT",
            "patches": [{
                "op": "replace",
                "path": "/attributes/fulfillment_availability",
                "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": quantity}],
            }],
        },
    )


"""
Note on `units_sold_last_30d`: use the Orders API
(GET /orders/v0/orders, filtered by CreatedAfter) and aggregate order
items by SKU, the same pattern as the Shopify and eBay clients' notes
above -- all three platforms push sales-velocity calculation onto a
separate orders endpoint rather than exposing it on the inventory
endpoint directly.
"""
