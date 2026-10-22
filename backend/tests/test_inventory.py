"""
Tests for the inventory accounting in inventory.py -- the pure functions
only, no database. These numbers drive profit and reorder decisions, so
each test uses figures small enough to check by hand.
"""

import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from inventory import (replay, compute_metrics, classify,
                       reorder_suggestion, allocate_order_fee)


def purchase(qty, unit_cost, when):
    return {"movement_type": "purchase", "channel": None, "quantity": qty,
            "unit_cost": unit_cost, "unit_price": None, "fee": 0.0, "occurred_at": when}


def sale(qty, unit_price, when, channel="website", fee=0.0):
    return {"movement_type": "sale", "channel": channel, "quantity": -qty,
            "unit_cost": None, "unit_price": unit_price, "fee": fee, "occurred_at": when}


def adjustment(qty, when):
    return {"movement_type": "adjustment", "channel": None, "quantity": qty,
            "unit_cost": None, "unit_price": None, "fee": 0.0, "occurred_at": when}


def test_weighted_average_cost_after_two_purchases():
    rows = replay([purchase(10, 5.00, "2026-01-01 09:00:00"),
                   purchase(10, 7.00, "2026-01-02 09:00:00")])
    assert rows[-1]["balance"] == 20
    assert rows[-1]["avg_cost"] == 6.0


def test_sale_cogs_uses_average_cost_at_time_of_sale():
    rows = replay([purchase(10, 5.00, "2026-01-01 09:00:00"),
                   purchase(10, 7.00, "2026-01-02 09:00:00"),
                   sale(5, 12.00, "2026-01-03 12:00:00")])
    assert rows[-1]["cogs"] == 30.0
    assert rows[-1]["balance"] == 15


def test_sales_do_not_change_average_cost_but_new_purchases_do():
    rows = replay([purchase(10, 5.00, "2026-01-01 09:00:00"),
                   purchase(10, 7.00, "2026-01-02 09:00:00"),
                   sale(5, 12.00, "2026-01-03 12:00:00"),
                   purchase(5, 9.00, "2026-01-04 09:00:00")])
    assert rows[2]["avg_cost"] == 6.0
    # (15 units x $6 + 5 units x $9) / 20 units
    assert rows[3]["avg_cost"] == 6.75


def test_negative_adjustment_is_written_off_at_average_cost():
    rows = replay([purchase(10, 4.00, "2026-01-01 09:00:00"),
                   adjustment(-2, "2026-01-02 22:30:00")])
    assert rows[-1]["writeoff"] == 8.0
    assert rows[-1]["balance"] == 8


def test_reconciliation_holds_across_the_period_boundary():
    now = datetime(2026, 1, 31, 12, 0, 0)
    movements = [
        purchase(20, 10.0, "2025-12-15 09:00:00"),   # before the 30-day period
        sale(5, 20.0, "2025-12-20 12:00:00"),        # before the period
        purchase(10, 12.0, "2026-01-10 09:00:00"),
        sale(8, 20.0, "2026-01-15 12:00:00"),
        adjustment(-1, "2026-01-20 22:30:00"),
    ]
    rec = compute_metrics(movements, 30, now)["reconciliation"]
    assert (rec["opening"], rec["purchased"], rec["sold"], rec["adjusted"], rec["closing"]) == (15, 10, 8, -1, 16)
    assert rec["reconciles"] is True


def test_profit_breakdown():
    now = datetime(2026, 1, 31, 12, 0, 0)
    movements = [purchase(10, 10.0, "2026-01-05 09:00:00"),
                 sale(4, 25.0, "2026-01-10 12:00:00", channel="ebay", fee=13.50)]
    p = compute_metrics(movements, 30, now)["profit"]
    assert p["revenue"] == 100.0
    assert p["cogs"] == 40.0
    assert p["gross_profit"] == 60.0
    assert p["net_profit"] == 46.5
    assert p["gross_margin_pct"] == 60.0
    assert p["net_margin_pct"] == 46.5


def test_turnover_is_cogs_over_average_daily_inventory_value():
    now = datetime(2026, 1, 3, 12, 0, 0)
    movements = [purchase(10, 10.0, "2025-12-31 09:00:00"),
                 sale(5, 20.0, "2026-01-02 10:00:00")]
    t = compute_metrics(movements, 2, now)["turnover"]
    # End-of-day stock value: Jan 1 $100, Jan 2 $50, Jan 3 $50 -> average $66.67.
    # COGS is 5 units x $10 = $50, so turnover is 50 / 66.67 = 0.75.
    assert t["avg_inventory_value"] == 66.67
    assert t["turnover"] == 0.75
    assert t["days_of_inventory"] == 2.7  # 2 days / 0.75


def test_days_of_stock_left_uses_last_30_days_of_sales():
    now = datetime(2026, 1, 31, 12, 0, 0)
    movements = [purchase(45, 5.0, "2026-01-01 09:00:00")]
    movements += [sale(1, 10.0, f"2026-01-{day:02d} 12:00:00") for day in range(2, 32)]
    t = compute_metrics(movements, 30, now)["turnover"]
    assert t["velocity_per_day"] == 1.0
    assert t["days_of_stock_left"] == 15.0


def test_stockout_days_do_not_dilute_sales_pace():
    # Sold 1/day for 10 days, then out of stock for the rest of the month.
    # Averaging over 30 calendar days would say 0.33/day and under-order;
    # averaging over the 10 days it could actually sell says 1/day.
    now = datetime(2026, 1, 31, 12, 0, 0)
    movements = [purchase(10, 5.0, "2026-01-01 09:00:00")]
    movements += [sale(1, 10.0, f"2026-01-{day:02d} 12:00:00") for day in range(2, 12)]
    t = compute_metrics(movements, 30, now)["turnover"]
    assert t["in_stock_days_last_30"] == 10
    assert t["velocity_per_day"] == 1.0
    assert reorder_suggestion(on_hand=0, velocity=1.0)["quantity"] == 45


def test_no_sales_gives_no_days_left_estimate():
    now = datetime(2026, 1, 31, 12, 0, 0)
    t = compute_metrics([purchase(10, 5.0, "2026-01-01 09:00:00")], 30, now)["turnover"]
    assert t["days_of_stock_left"] is None


def test_empty_ledger_does_not_crash():
    m = compute_metrics([], 30, datetime(2026, 1, 31, 12, 0, 0))
    assert m["stock"]["on_hand"] == 0
    assert m["turnover"]["turnover"] is None


def test_status_thresholds():
    assert classify(0, 1.0, 0) == "out_of_stock"
    assert classify(10, 0, None) == "no_sales"
    assert classify(5, 1.0, 5) == "low"
    assert classify(200, 1.0, 200) == "overstock"
    assert classify(30, 1.0, 30) == "healthy"


def test_reorder_suggestion_restores_target_cover():
    s = reorder_suggestion(on_hand=5, velocity=1.0)  # 5 days left
    assert s["suggested"] is True
    assert s["quantity"] == 40  # 45 days x 1/day, minus 5 on hand


def test_order_fee_fixed_part_is_split_across_lines():
    # Stripe-style 2.9% + $0.30: each line pays 2.9% of itself, and the
    # $0.30 is split 75/25 by each line's share of the order.
    assert allocate_order_fee("website", 75.0, 100.0) == 2.40
    assert allocate_order_fee("website", 25.0, 100.0) == 0.80
