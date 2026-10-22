"""
Tests for reports.assemble_report, using small hand-checkable ledgers
built with the same helpers as tests/test_inventory.py.
"""

import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from inventory import compute_metrics
from reports import assemble_report
from tests.test_inventory import purchase, sale, adjustment

NOW = datetime(2026, 1, 31, 12, 0, 0)


def product(sku, price=20.0):
    return {"sku": sku, "title": f"Item {sku}", "images": [], "price": price, "inventory_qty": 0}


def item(sku, movements, price=20.0):
    return (product(sku, price), movements, compute_metrics(movements, 30, NOW))


def daily_sales(qty, price, days, channel="website"):
    return [sale(qty, price, f"2026-01-{d:02d} 12:00:00", channel=channel) for d in days]


def sample_items():
    return [
        # Fast seller: 20 units at $50, cost $20 each.
        item("FAST", [purchase(40, 20.0, "2025-12-20 09:00:00")] + daily_sales(1, 50.0, range(2, 22))),
        # Medium seller: 5 units at $40, cost $10, restocked at a higher cost.
        item("MID", [purchase(10, 10.0, "2025-12-20 09:00:00"),
                     purchase(10, 12.0, "2026-01-10 09:00:00")] + daily_sales(1, 40.0, range(12, 17))),
        # Slow seller with lots of stock: 1 unit at $30.
        item("SLOW", [purchase(100, 15.0, "2025-12-01 09:00:00"), sale(1, 30.0, "2026-01-20 12:00:00")]),
        # Nothing sold in the period.
        item("DEAD", [purchase(8, 5.0, "2025-11-01 09:00:00")]),
    ]


def test_summary_totals_add_up_across_products():
    s = assemble_report(sample_items(), 30, NOW)["summary"]
    assert s["revenue"] == 20 * 50 + 5 * 40 + 30          # 1230
    assert s["units_sold"] == 26
    assert s["purchase_spend"] == 120.0                   # 10 units at $12 in the period
    assert s["units_received"] == 10
    assert s["sku_count"] == 4


def test_abc_classes_follow_cumulative_revenue_share():
    report = assemble_report(sample_items(), 30, NOW)
    classes = {p["sku"]: p["abc"] for p in report["products"]}
    # FAST is 81% of revenue, so it alone is A; MID starts at 81% (B); SLOW starts at 97.6% (C).
    assert classes == {"FAST": "A", "MID": "B", "SLOW": "C", "DEAD": "C"}
    a = next(c for c in report["abc"] if c["class"] == "A")
    assert a["count"] == 1 and a["revenue_share_pct"] == 81.3


def test_products_are_ranked_by_revenue_and_top_sellers_skip_non_sellers():
    report = assemble_report(sample_items(), 30, NOW)
    assert [p["sku"] for p in report["products"]][:3] == ["FAST", "MID", "SLOW"]
    assert [t["sku"] for t in report["top_sellers"]["by_units"]] == ["FAST", "MID", "SLOW"]


def test_procurement_tracks_cost_changes_and_prices_the_reorder_plan():
    proc = assemble_report(sample_items(), 30, NOW)["procurement"]
    mid = next(p for p in proc["by_product"] if p["sku"] == "MID")
    assert mid["units"] == 10 and mid["spend"] == 120.0
    # FAST sold 20 in 30 in-stock days and has 20 left: about 30 days of cover, so no reorder yet.
    assert all(p["sku"] != "FAST" for p in proc["reorder_plan"])
    assert proc["reorder_budget"] == sum(p["estimated_cost"] for p in proc["reorder_plan"])


def test_excess_stock_counts_units_beyond_45_days_and_all_unsold_stock():
    inv = assemble_report(sample_items(), 30, NOW)["inventory"]
    excess = {e["sku"]: e["excess_units"] for e in inv["excess"]["items"]}
    # SLOW sold 1 unit in 30 in-stock days: 45 days of cover is 2 units, so the other 97 are excess.
    assert excess["SLOW"] == 99 - 2
    assert excess["DEAD"] == 8
    statuses = {s["status"] for s in inv["status"]}
    assert "no_sales" in statuses


def test_stockouts_estimate_weekly_lost_sales():
    items = [item("GONE", [purchase(10, 5.0, "2026-01-01 09:00:00")] + daily_sales(1, 25.0, range(2, 12)))]
    stockouts = assemble_report(items, 30, NOW)["inventory"]["stockouts"]
    assert stockouts[0]["sku"] == "GONE"
    assert stockouts[0]["lost_units_per_week"] == 7.0          # sold 1 a day while in stock
    assert stockouts[0]["lost_revenue_per_week"] == 175.0      # 7 x $25
    plan = assemble_report(items, 30, NOW)["procurement"]
    assert plan["reorder_plan"][0]["quantity"] == 45            # 45 days x 1 a day
    assert plan["reorder_budget"] == 225.0                      # 45 x $5 average cost


def test_weekly_trend_adds_up_to_period_revenue():
    report = assemble_report(sample_items(), 30, NOW)
    assert round(sum(w["revenue"] for w in report["trend"]), 2) == report["summary"]["revenue"]
    assert sum(w["units"] for w in report["trend"]) == report["summary"]["units_sold"]
