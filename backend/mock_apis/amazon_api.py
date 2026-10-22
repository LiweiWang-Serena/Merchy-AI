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
    {"sku": "MOTO-THR-031", "title": "Throttle Cable 31 inch ATV Quad"},
    {"sku": "MOTO-THB-022", "title": "Thumb Throttle Assembly 22mm"},
    {"sku": "MOTO-INT-110", "title": "Intake Manifold Pipe 110cc"},
    {"sku": "MOTO-BRK-180", "title": "Rear Brake Cable 180cm"},
    {"sku": "MOTO-CAR-019", "title": "Carburetor 19mm PZ19"},
    {"sku": "MOTO-SPK-A7", "title": "Spark Plug A7TC 4 Pack"},
    {"sku": "MOTO-CHN-420", "title": "Drive Chain 420 x 100 Links"},
    {"sku": "MOTO-AIR-035", "title": "Air Filter 35mm Cone"},
]

# Richer per-SKU detail used for the "generate eBay listing from Amazon
# link" flow. In production, this is exactly the shape of data you'd get
# back from your OWN Amazon listing via the SP-API Catalog Items /
# Listings Items endpoints -- title, bullet points, and your own product
# photo URLs -- for products you already sell, not scraped from a
# competitor or another seller's page.
PRODUCT_DETAILS = {
    "MOTO-THR-031": {
        "brand": "PowerSport",
        "bullets": [
            "Fits 50-125cc ATV quads",
            "Pre-lubricated inner cable",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/bf0b9d28b43c4a198f17e7c84f392ab5~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-THB-022": {
        "brand": "PowerSport",
        "bullets": [
            "Fits 50-250cc engines",
            "Includes throttle cable",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/9d58eddd663b4ef984c62c42f91b28b3~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-INT-110": {
        "brand": "PowerSport",
        "bullets": [
            "Fits 110cc horizontal engines",
            "Gasket included",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/bf0b9d28b43c4a198f17e7c84f392ab5~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-BRK-180": {
        "brand": "PowerSport",
        "bullets": [
            "180cm galvanised cable",
            "Fits most mid-size quads",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/9d58eddd663b4ef984c62c42f91b28b3~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-CAR-019": {
        "brand": "PowerSport",
        "bullets": [
            "For 50-110cc engines",
            "Cable-operated choke",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/bf0b9d28b43c4a198f17e7c84f392ab5~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-SPK-A7": {
        "brand": "PowerSport",
        "bullets": [
            "Standard A7TC replacement",
            "Four plugs per pack",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/9d58eddd663b4ef984c62c42f91b28b3~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-CHN-420": {
        "brand": "PowerSport",
        "bullets": [
            "420 pitch, master link included",
            "Heat-treated pins",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/bf0b9d28b43c4a198f17e7c84f392ab5~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
        ],
    },
    "MOTO-AIR-035": {
        "brand": "PowerSport",
        "bullets": [
            "35mm high-flow cone",
            "Washable foam",
        ],
        "images": [
            "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/9d58eddd663b4ef984c62c42f91b28b3~tplv-fhlh96nyum-origin-jpeg.jpeg?dr=12178&from=520841845&idc=useast5&ps=933b5bde&shcp=2c1af732&shp=1f0b6a75&t=555f072d",
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
