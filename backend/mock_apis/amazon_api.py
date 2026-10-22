"""
Simulates Amazon's Seller Partner API (SP-API) inventory/listings endpoint.

In a real integration this module would call:
  GET /fba/inventory/v1/summaries  (or similar SP-API endpoints)
using OAuth credentials. Here we return realistic sample payloads so the
rest of the pipeline (sync, reporting) can be built and demoed without
needing an approved Amazon Seller Central developer account.
"""

import random
import re

# A shared product catalog used across both mock marketplaces so the sync
# step has real overlaps (and real discrepancies) to reconcile.
CATALOG = [
    {"sku": "AUC-CAM-001", "title": "Vintage Film Camera - Refurbished"},
    {"sku": "AUC-WAT-014", "title": "Automatic Dive Watch, Stainless Steel"},
    {"sku": "AUC-LMP-027", "title": "Mid-Century Brass Table Lamp"},
    {"sku": "AUC-RUG-039", "title": "Hand-Woven Wool Area Rug 5x7"},
    {"sku": "AUC-GTR-052", "title": "Acoustic Guitar, Solid Spruce Top"},
    {"sku": "AUC-CHR-061", "title": "Leather Accent Chair, Cognac"},
    {"sku": "AUC-COI-073", "title": "Coin Collection Lot - Mixed Era"},
    {"sku": "AUC-VIN-088", "title": "Vinyl Record Bundle, Jazz Classics"},
]

# Richer per-SKU detail used for the "generate eBay listing from Amazon
# link" flow. In production, this is exactly the shape of data you'd get
# back from your OWN Amazon listing via the SP-API Catalog Items /
# Listings Items endpoints -- title, bullet points, and your own product
# photo URLs -- for products you already sell, not scraped from a
# competitor or another seller's page.
PRODUCT_DETAILS = {
    "AUC-CAM-001": {
        "brand": "Pentax",
        "bullets": [
            "35mm SLR film camera, fully serviced and light-seal replaced",
            "Includes 50mm f/1.8 lens, tested on real film rolls",
            "Light meter confirmed accurate against a handheld meter",
            "Comes with original leather case",
        ],
        "images": [
            "https://images.example.com/auc-cam-001/front.jpg",
            "https://images.example.com/auc-cam-001/side.jpg",
            "https://images.example.com/auc-cam-001/lens.jpg",
        ],
    },
    "AUC-WAT-014": {
        "brand": "Seiko",
        "bullets": [
            "Automatic movement, no battery required",
            "200m water resistance, screw-down crown",
            "Stainless steel case and bracelet, recently polished",
            "Runs within +/-15 seconds/day on our timegrapher test",
        ],
        "images": [
            "https://images.example.com/auc-wat-014/front.jpg",
            "https://images.example.com/auc-wat-014/wrist.jpg",
        ],
    },
    "AUC-LMP-027": {
        "brand": "Generic (Unbranded Mid-Century Style)",
        "bullets": [
            "Solid brass base, hand-polished",
            "Original linen drum shade, no stains or tears",
            "Rewired with new UL-listed cord and socket",
            "Tested and working, in-line rotary switch",
        ],
        "images": [
            "https://images.example.com/auc-lmp-027/full.jpg",
            "https://images.example.com/auc-lmp-027/base-detail.jpg",
        ],
    },
}


def _extract_asin(url: str) -> str:
    """Pull an ASIN out of a typical Amazon product URL. Falls back to a
    deterministic fake ASIN if the URL doesn't match the usual pattern,
    so the rest of the demo flow still works with any pasted link."""
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if match:
        return match.group(1)
    return "B0" + str(abs(hash(url)) % 10**8).zfill(8)


def fetch_product_by_url(url: str):
    """Simulates fetching your own listing's data from Amazon (e.g. via
    the Selling Partner API's Catalog/Listings endpoints) so it can be
    reused to draft the same product on eBay. Returns None if the URL
    doesn't map to a known SKU in this demo catalog."""
    asin = _extract_asin(url)

    # For the demo, match the pasted URL to one of our sample SKUs so the
    # flow feels real regardless of which sample link is pasted.
    rng = random.Random(url)
    sku = rng.choice(list(PRODUCT_DETAILS.keys()))
    catalog_entry = next(c for c in CATALOG if c["sku"] == sku)
    detail = PRODUCT_DETAILS[sku]

    return {
        "sku": sku,
        "asin": asin,
        "source_url": url,
        "title": catalog_entry["title"],
        "brand": detail.get("brand"),
        "bullets": detail["bullets"],
        "images": detail["images"],
    }


def get_inventory_snapshot(seed: int = 1):
    """Return a list of listing/inventory records as Amazon's API would."""
    rng = random.Random(seed)
    snapshot = []
    for item in CATALOG:
        # Not every SKU is listed on every channel.
        if rng.random() < 0.15:
            continue
        snapshot.append({
            "sku": item["sku"],
            "asin": f"B0{rng.randint(10000000, 99999999)}",
            "title": item["title"],
            "quantity_available": rng.randint(0, 40),
            "price": round(rng.uniform(35, 850), 2),
            "units_sold_last_30d": rng.randint(0, 60),
            "channel": "amazon",
        })
    return snapshot
