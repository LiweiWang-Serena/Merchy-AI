"""
Demo channel listings for the large parts catalog (demo_catalog.py): what
eBay and Amazon would show if the store listed that catalog there. Most
items are on eBay and about a third on Amazon. Quantities follow stock on
hand, except for a few listings that drifted between syncs -- the ones
the Listings page exists to catch. Units sold come from the ledger.
"""

import random
from collections import defaultdict
from datetime import datetime, timedelta

import inventory
from db import get_connection

SHARE = {"ebay": 0.85, "amazon": 0.35}
PRICE_FACTOR = {"ebay": 1.0, "amazon": 1.08}   # Amazon's higher fees, priced in


def is_active():
    """True when the database holds the large parts demo catalog."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key = 'demo_catalog'").fetchone()
    except Exception:
        return False
    finally:
        conn.close()
    return bool(row) and row[0] == "parts"


def snapshot(channel):
    rng = random.Random(f"{channel}-listings")
    cutoff = (datetime.now() - timedelta(days=30)).strftime(inventory.FMT)
    conn = get_connection()
    try:
        products = conn.execute("SELECT sku, title, price, inventory_qty FROM products ORDER BY sku").fetchall()
        sold = defaultdict(int, conn.execute(
            """SELECT sku, -SUM(quantity) FROM inventory_movements WHERE movement_type = 'sale'
               AND channel = ? AND occurred_at >= ? GROUP BY sku""", (channel, cutoff)).fetchall())
    finally:
        conn.close()
    records = []
    for p in products:
        if rng.random() > SHARE[channel]:
            continue
        quantity, drift = p["inventory_qty"], rng.random()
        if drift < 0.02:
            quantity += rng.randint(1, 6)                  # not updated after a sale: oversell risk
        elif drift < 0.04:
            quantity = max(0, quantity - rng.randint(1, 4))
        records.append({"sku": p["sku"], "channel": channel, "title": p["title"],
                        "quantity_available": quantity,
                        "price": round(p["price"] * PRICE_FACTOR[channel], 2),
                        "units_sold_last_30d": sold[p["sku"]]})
    return records
