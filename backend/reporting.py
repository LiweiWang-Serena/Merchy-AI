"""
Turns the raw synced listings table into the kind of report a procurement
or store-expansion decision actually needs: which SKUs are about to run
out, which are moving fast enough to justify reordering or carrying in a
new location, and which are dead stock tying up capital.
"""

import inventory
from db import get_connection, init_db
import json

FAST_MOVER_THRESHOLD = 25  # units sold in last 30 days, combined across channels


def build_report():
    # A brand-new database has no listings table until the first sync;
    # create it so the page shows an empty report instead of an error.
    init_db()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM listings").fetchall()
    conn.close()

    # Aggregate by SKU across channels.
    by_sku = {}
    for r in rows:
        sku = r["sku"]
        entry = by_sku.setdefault(sku, {
            "sku": sku,
            "title": r["title"],
            "total_quantity": 0,
            "total_units_sold_30d": 0,
            "channels": [],
            "avg_price": [],
        })
        entry["total_quantity"] += r["quantity_available"]
        entry["total_units_sold_30d"] += r["units_sold_last_30d"]
        entry["channels"].append(r["channel"])
        entry["avg_price"].append(r["price"])

    for entry in by_sku.values():
        entry["avg_price"] = round(sum(entry["avg_price"]) / len(entry["avg_price"]), 2)
        entry["channel_count"] = len(set(entry["channels"]))

    skus = list(by_sku.values())

    # The same rule as the Inventory page: low means it runs out within
    # inventory.LOW_STOCK_DAYS at the last 30 days' pace. A fixed unit count
    # would flag most of a long-tail parts catalog, where 3 units of a part
    # that sells once a quarter is plenty.
    low_stock = [s for s in skus if _days_left(s) is not None and _days_left(s) < inventory.LOW_STOCK_DAYS]
    fast_movers = [s for s in skus if s["total_units_sold_30d"] >= FAST_MOVER_THRESHOLD]
    dead_stock = [s for s in skus if s["total_units_sold_30d"] == 0 and s["total_quantity"] > 0]

    procurement_recommendations = build_procurement_recommendations(skus)

    conn = get_connection()
    latest_sync = conn.execute(
        "SELECT discrepancies_json FROM sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    discrepancies = json.loads(latest_sync["discrepancies_json"]) if latest_sync and latest_sync["discrepancies_json"] else []

    return {
        "summary": {
            "total_skus_tracked": len(skus),
            "low_stock_count": len(low_stock),
            "fast_mover_count": len(fast_movers),
            "dead_stock_count": len(dead_stock),
            "total_inventory_value": round(
                sum(s["total_quantity"] * s["avg_price"] for s in skus), 2
            ),
        },
        "low_stock": sorted(low_stock, key=_days_left),
        "fast_movers": sorted(fast_movers, key=lambda s: -s["total_units_sold_30d"]),
        "dead_stock": dead_stock,
        "procurement_recommendations": procurement_recommendations,
        "all_skus": sorted(skus, key=lambda s: s["sku"]),
        "_discrepancies": discrepancies,
    }


def _days_left(s):
    """Days the listed quantity lasts at the last 30 days' sales pace; None if it didn't sell."""
    if s["total_units_sold_30d"] <= 0:
        return None
    return s["total_quantity"] / (s["total_units_sold_30d"] / 30)


def build_procurement_recommendations(skus):
    """A simple, explainable reorder heuristic: sales velocity vs. days
    of stock remaining. This is the kind of logic a real procurement
    planning tool would refine with lead times and seasonality, but the
    shape of the decision is the same."""
    recommendations = []
    for s in skus:
        daily_velocity = s["total_units_sold_30d"] / 30
        days_of_stock_left = (
            s["total_quantity"] / daily_velocity if daily_velocity > 0 else None
        )

        if daily_velocity > 0 and days_of_stock_left is not None and days_of_stock_left <= 20:
            suggested_reorder_qty = max(round(daily_velocity * 30), 5)
            recommendations.append({
                "sku": s["sku"],
                "title": s["title"],
                "days_of_stock_left": round(days_of_stock_left, 1),
                "suggested_reorder_qty": suggested_reorder_qty,
                "reason": (
                    f"Selling ~{daily_velocity:.1f}/day with only "
                    f"{s['total_quantity']} units left across channels."
                ),
            })
    return sorted(recommendations, key=lambda r: r["days_of_stock_left"])


if __name__ == "__main__":
    import json
    print(json.dumps(build_report(), indent=2))
