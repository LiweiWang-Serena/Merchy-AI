"""
Tests for running at a real store's scale: thousands of items, tens of
thousands of ledger rows. Uses the large demo catalog (demo_catalog.py)
in a throwaway database.
"""

import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import demo_catalog
import ebay_account
import inventory
import reports
import storefront
from mock_apis import catalog_listings
from tests.test_ebay_account import add_product
from tests.test_new_item import expect_error
from tests.test_store_import import temp_database


def big_catalog(count=1500):
    demo_catalog.install(count, backup=False)


def test_the_demo_catalog_is_consistent_and_varied():
    with temp_database():
        big_catalog()
        conn = db.get_connection()
        skus = conn.execute("SELECT COUNT(DISTINCT sku), COUNT(*) FROM products").fetchone()
        mismatched = conn.execute("""SELECT COUNT(*) FROM products p WHERE p.inventory_qty !=
            (SELECT COALESCE(SUM(quantity), 0) FROM inventory_movements m WHERE m.sku = p.sku)""").fetchone()[0]
        negative = conn.execute("SELECT COUNT(*) FROM products WHERE inventory_qty < 0").fetchone()[0]
        conn.close()
        statuses = set(inventory.get_overview(30)["summary"]["status_counts"])
    assert skus[0] == skus[1] == 1500
    assert mismatched == 0 and negative == 0
    assert {"healthy", "low", "out_of_stock", "overstock", "no_sales"} <= statuses


def test_light_and_full_metrics_agree_exactly():
    # Regression: the light path once summed daily values with +=, which
    # differs from sum() in the last decimals (Python 3.12+ compensates
    # for rounding in sum()), and occasionally changed a rounded figure.
    with temp_database():
        big_catalog(600)
        conn = db.get_connection()
        by_sku = inventory.all_movements(conn)
        conn.close()
    now = datetime.now()
    for period in (30, 90):
        for rows in by_sku.values():
            full = inventory.compute_metrics(rows, period, now)
            light = inventory.compute_metrics(rows, period, now, series=False)
            for part in ("stock", "reconciliation", "profit", "turnover", "channels", "raw"):
                assert full[part] == light[part], part


def test_the_list_searches_filters_sorts_and_pages_on_the_server():
    with temp_database():
        big_catalog()
        everything = inventory.list_items(30, per_page=50)
        assert everything["total"] == 1500 and len(everything["items"]) == 50 and everything["pages"] == 30

        found = inventory.list_items(30, q="carburetor kodiak")
        assert found["total"] > 0
        assert all("carburetor" in i["title"].lower() and "kodiak" in i["title"].lower() for i in found["items"])

        out = inventory.list_items(30, status="out_of_stock")
        assert out["total"] == out["summary"]["status_counts"]["out_of_stock"]
        assert all(i["on_hand"] == 0 for i in out["items"])
        attention = inventory.list_items(30, status="attention")
        assert {i["status"] for i in attention["items"]} <= set(inventory.ATTENTION_STATUSES)

        by_value = inventory.list_items(30, sort="inventory_value", direction="desc")["items"]
        assert [i["inventory_value"] for i in by_value] == sorted((i["inventory_value"] for i in by_value), reverse=True)
        days = inventory.list_items(30, sort="days_of_stock_left", direction="asc", per_page=200, page=8)["items"]
        values = [i["days_of_stock_left"] for i in days]
        assert values == sorted(values, key=lambda v: (v is None, v or 0))   # missing values sort last

        last = inventory.list_items(30, page=999)
        assert last["page"] == last["pages"] == 30
        assert inventory.list_items(30, per_page=5000)["per_page"] == 200


def test_cached_figures_refresh_as_soon_as_stock_moves():
    with temp_database():
        big_catalog(300)
        item = inventory.list_items(30, sort="on_hand")["items"][0]
        inventory.record_movement(item["sku"], "adjustment", -1, note="counted one short")
        again = next(i for i in inventory.get_overview(30)["items"] if i["sku"] == item["sku"])
    assert again["on_hand"] == item["on_hand"] - 1


def test_reports_trim_long_lists_but_keep_complete_counts_and_export_everything():
    with temp_database():
        big_catalog()
        full = reports.build_report(30)
        page = reports.for_page(full)
        csv_text = reports.items_csv(30)
    assert page["product_count"] == 1500 and len(page["products"]) == reports.PAGE_LIMITS["products"]
    assert page["procurement"]["reorder_count"] == len(full["procurement"]["reorder_plan"])
    assert len(page["procurement"]["reorder_plan"]) <= reports.PAGE_LIMITS["reorder_plan"]
    assert page["procurement"]["reorder_budget"] == full["procurement"]["reorder_budget"]
    assert page["inventory"]["stockout_count"] == len(full["inventory"]["stockouts"])
    lost = [s["lost_revenue_per_week"] for s in page["inventory"]["stockouts"]]
    assert lost == sorted(lost, reverse=True)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("SKU,Title,ABC class") and len(lines) == 1501


def test_overview_lists_the_most_urgent_items_first_with_complete_counts():
    with temp_database():
        big_catalog()
        overview = reports.build_overview()
    counts = overview["attention_counts"]
    assert len(overview["attention"]) == 12
    assert overview["attention_total"] >= counts["out_of_stock"] + counts["low"] + counts["overstock"]
    ranks = [0 if a["oversell"] else {"out_of_stock": 1, "low": 2, "overstock": 3}[a["status"]]
             for a in overview["attention"]]
    assert ranks == sorted(ranks)


def test_the_shop_pages_and_searches_in_sql():
    with temp_database():
        big_catalog()
        first = storefront.product_page("", 1, 24)
        tubes = storefront.product_page("inner tube", 1, 24)
        literal = storefront.product_page("100%", 1, 24)
    assert first["total"] == 1500 and first["pages"] == 63 and len(first["items"]) == 24
    assert all(p["inventory_qty"] > 0 for p in first["items"])          # in stock first
    assert tubes["total"] > 0 and all("inner tube" in p["title"].lower() for p in tubes["items"])
    assert literal["total"] == 0                                         # % is matched literally


def test_demo_listings_follow_the_catalog_and_include_some_drift():
    from sync import run_sync
    with temp_database():
        assert not catalog_listings.is_active()
        big_catalog()
        assert catalog_listings.is_active()
        ebay = catalog_listings.snapshot("ebay")
        stock = {p["sku"]: p["inventory_qty"] for p in storefront.get_all_products()}
        result = run_sync()
    drifted = sum(1 for r in ebay if r["quantity_available"] != stock[r["sku"]])
    assert 0.75 < len(ebay) / len(stock) < 0.95
    assert 0 < drifted < len(ebay) * 0.1
    assert result["ebay_records"] == len(ebay)


def test_a_large_order_import_is_complete_capped_and_idempotent():
    with temp_database():
        for n in range(200):
            add_product(f"P-{n:03d}", stock=20)
        lines = [{"order_id": f"O-{i}", "line_item_id": f"L-{i}", "sku": f"P-{i % 220:03d}",
                  "title": "Part", "quantity": 1, "unit_price": 12.0, "fee": 1.5, "fee_estimated": False,
                  "occurred_at": f"2026-09-{10 + i % 15:02d} 12:{i % 60:02d}:00"} for i in range(3000)]
        first = ebay_account.record_lines(lines)
        second = ebay_account.record_lines(lines)
        on_hand = db.get_connection().execute("SELECT SUM(inventory_qty) FROM products").fetchone()[0]
    # 200 known SKUs x 20 units = 4,000 units in stock; 20 unknown SKUs; each known SKU gets
    # about 13-14 of the 3,000 lines, all within its 20 units.
    unknown = sum(1 for line in lines if int(line["sku"][2:]) >= 200)
    assert first["not_in_catalog_count"] == unknown and len(first["not_in_catalog"]) == 20
    assert first["recorded_count"] == 3000 - unknown and first["not_enough_stock_count"] == 0
    assert len(first["recorded"]) == ebay_account.SAMPLE_LIMIT
    assert on_hand == 4000 - first["recorded_units"]
    assert second["already_imported"] == first["recorded_count"] and second["recorded_count"] == 0


def test_order_import_windows_stay_within_two_years():
    with temp_database():
        expect_error(ebay_account.import_orders, 30, 30, contains="within the last two years")
        expect_error(ebay_account.import_orders, 900, 0, contains="within the last two years")
