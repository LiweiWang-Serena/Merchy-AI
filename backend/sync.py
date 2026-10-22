"""
Pulls listing/inventory snapshots from each marketplace's API, writes them
into a unified SQLite table, and flags SKUs where stock levels disagree
between channels (a common real-world problem: an item sells on Amazon
but the eBay listing hasn't been decremented yet, risking an oversell).
"""

from datetime import datetime, timezone
import json
import os
from mock_apis import amazon_api, ebay_api, catalog_listings
from db import get_connection, init_db

USE_REAL_APIS = os.environ.get("USE_REAL_APIS", "false").lower() == "true"


def _normalize(records):
    """Real API responses can leave price/units_sold as None (those live
    on separate endpoints -- see each integration client's docstring).
    Reporting does arithmetic on these fields, so default them to 0
    rather than making every downstream calculation handle None."""
    for r in records:
        r["price"] = r["price"] if r["price"] is not None else 0.0
        r["units_sold_last_30d"] = r["units_sold_last_30d"] or 0
    return records


def _get_amazon_snapshot():
    if USE_REAL_APIS:
        from integrations import amazon_sp_api_client
        return _normalize(amazon_sp_api_client.get_inventory_snapshot())
    if catalog_listings.is_active():
        return catalog_listings.snapshot("amazon")
    return amazon_api.get_inventory_snapshot()


def _get_ebay_snapshot():
    # A connected seller account (admin > eBay) always wins over the
    # older key-based client and the demo data.
    import ebay_account
    if ebay_account.is_connected():
        return ebay_account.listing_records()
    if USE_REAL_APIS:
        from integrations import ebay_client
        return _normalize(ebay_client.get_inventory_snapshot())
    if catalog_listings.is_active():
        return catalog_listings.snapshot("ebay")
    return ebay_api.get_inventory_snapshot()


def run_sync():
    init_db()
    # Fetch first, write after: fetching can mean dozens of slow API calls,
    # and holding a write transaction open meanwhile would lock the
    # database for the whole app (and leave it locked if a call failed).
    amazon_data = _get_amazon_snapshot()
    ebay_data = _get_ebay_snapshot()
    discrepancies = find_stock_discrepancies(amazon_data, ebay_data)

    conn = get_connection()
    try:
        # Only refresh the marketplace channels here -- the 'website' channel
        # is written directly by checkout.py when a sale happens, and should
        # persist across marketplace syncs, not get wiped by them.
        conn.execute("DELETE FROM listings WHERE channel IN ('amazon', 'ebay')")
        conn.executemany(
            """INSERT INTO listings (sku, channel, title, quantity_available, price, units_sold_last_30d)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(r["sku"], r["channel"], r["title"], r["quantity_available"], r["price"], r["units_sold_last_30d"])
             for r in amazon_data + ebay_data])
        conn.execute(
            """INSERT INTO sync_log (amazon_records, ebay_records, discrepancies_found, discrepancies_json)
               VALUES (?, ?, ?, ?)""",
            (len(amazon_data), len(ebay_data), len(discrepancies), json.dumps(discrepancies)))
        conn.commit()
    finally:
        conn.close()

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "amazon_records": len(amazon_data),
        "ebay_records": len(ebay_data),
        "discrepancies": discrepancies,
    }


def find_stock_discrepancies(amazon_data, ebay_data):
    """Flag SKUs listed on both channels with a meaningfully different
    quantity_available -- these are oversell/undersell risks."""
    amazon_by_sku = {r["sku"]: r for r in amazon_data}
    ebay_by_sku = {r["sku"]: r for r in ebay_data}

    discrepancies = []
    for sku in set(amazon_by_sku) & set(ebay_by_sku):
        a_qty = amazon_by_sku[sku]["quantity_available"]
        e_qty = ebay_by_sku[sku]["quantity_available"]
        if abs(a_qty - e_qty) >= 3:
            discrepancies.append({
                "sku": sku,
                "title": amazon_by_sku[sku]["title"],
                "amazon_qty": a_qty,
                "ebay_qty": e_qty,
                "difference": abs(a_qty - e_qty),
            })
    return sorted(discrepancies, key=lambda d: -d["difference"])


if __name__ == "__main__":
    result = run_sync()
    print(f"Synced {result['amazon_records']} Amazon records, "
          f"{result['ebay_records']} eBay records.")
    print(f"Found {len(result['discrepancies'])} stock discrepancies.")
