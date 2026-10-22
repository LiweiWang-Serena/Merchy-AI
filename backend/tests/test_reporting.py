"""
Tests for the pure business logic in sync.py and reporting.py --
no database or Flask app required.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import find_stock_discrepancies
from reporting import build_procurement_recommendations


def test_discrepancy_flags_large_quantity_gap():
    amazon = [{"sku": "A1", "title": "Widget", "quantity_available": 20}]
    ebay = [{"sku": "A1", "title": "Widget", "quantity_available": 2}]

    result = find_stock_discrepancies(amazon, ebay)

    assert len(result) == 1
    assert result[0]["difference"] == 18


def test_discrepancy_ignores_small_gap():
    amazon = [{"sku": "A1", "title": "Widget", "quantity_available": 10}]
    ebay = [{"sku": "A1", "title": "Widget", "quantity_available": 9}]

    result = find_stock_discrepancies(amazon, ebay)

    assert result == []


def test_discrepancy_ignores_sku_listed_on_only_one_channel():
    amazon = [{"sku": "A1", "title": "Widget", "quantity_available": 20}]
    ebay = [{"sku": "B2", "title": "Other", "quantity_available": 0}]

    result = find_stock_discrepancies(amazon, ebay)

    assert result == []


def test_discrepancies_sorted_by_severity_descending():
    amazon = [
        {"sku": "A1", "title": "Small gap", "quantity_available": 10},
        {"sku": "A2", "title": "Big gap", "quantity_available": 40},
    ]
    ebay = [
        {"sku": "A1", "title": "Small gap", "quantity_available": 6},
        {"sku": "A2", "title": "Big gap", "quantity_available": 2},
    ]

    result = find_stock_discrepancies(amazon, ebay)

    assert [r["sku"] for r in result] == ["A2", "A1"]


def test_recommends_reorder_when_stock_runs_out_soon():
    skus = [{
        "sku": "A1", "title": "Fast seller",
        "total_quantity": 10, "total_units_sold_30d": 60,  # 2/day -> 5 days left
    }]

    result = build_procurement_recommendations(skus)

    assert len(result) == 1
    assert result[0]["sku"] == "A1"
    assert result[0]["suggested_reorder_qty"] > 0


def test_no_recommendation_for_slow_seller_with_ample_stock():
    skus = [{
        "sku": "A1", "title": "Slow seller",
        "total_quantity": 200, "total_units_sold_30d": 3,
    }]

    result = build_procurement_recommendations(skus)

    assert result == []


def test_no_recommendation_for_zero_velocity():
    skus = [{
        "sku": "A1", "title": "Dead stock",
        "total_quantity": 5, "total_units_sold_30d": 0,
    }]

    result = build_procurement_recommendations(skus)

    assert result == []
