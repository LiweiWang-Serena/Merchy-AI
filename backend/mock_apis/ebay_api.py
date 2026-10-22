"""
Simulates eBay's Trading/Inventory API (GetMyeBaySelling / Inventory API).

Real integration would authenticate via eBay's OAuth flow and call the
Inventory API's `getInventoryItems` endpoint. This mock returns the same
shared catalog as amazon_api.py, but with independent stock levels and
prices -- exactly the kind of drift a real multi-channel seller has to
reconcile by hand (or automate, which is the point of this project).
"""

import random
from .amazon_api import CATALOG


def get_inventory_snapshot(seed: int = 2):
    rng = random.Random(seed)
    snapshot = []
    for item in CATALOG:
        if rng.random() < 0.2:
            continue
        snapshot.append({
            "sku": item["sku"],
            "item_id": str(rng.randint(100000000000, 999999999999)),
            "title": item["title"],
            "quantity_available": rng.randint(0, 40),
            "price": round(rng.uniform(35, 850), 2),
            "units_sold_last_30d": rng.randint(0, 60),
            "channel": "ebay",
        })
    return snapshot
