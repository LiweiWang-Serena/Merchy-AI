"""
Business reports across the whole catalog, derived from the inventory
ledger through inventory.compute_metrics, so every figure agrees with
the item pages.

  summary      revenue, profit, units, purchase spend, stock value, turnover
  trend        weekly revenue, profit and units, units split by channel
  products     per-product performance, ranked by revenue, with ABC classes
  top_sellers  best five by units sold and by net profit
  channels     profit by sales channel
  procurement  what was bought, cost changes, and a reorder plan with budget
  inventory    stock by status, how long stock will last, excess stock
               tying up cash, and stockouts losing sales

ABC analysis is the standard way to decide where attention goes: A items
bring the first 80% of revenue, B the next 15%, C the last 5%. Comparing
each class's share of revenue with its share of stock value shows where
cash is tied up in slow sellers.
"""

import math
from collections import defaultdict
from datetime import datetime, timedelta

import inventory
import storefront
from db import get_connection

ABC_A_SHARE = 80.0
ABC_B_SHARE = 95.0
STATUS_ORDER = ["healthy", "low", "out_of_stock", "overstock", "no_sales", "new"]
COVER_BUCKETS = [
    (14, "Under 2 weeks"), (45, "2 to 6 weeks"), (90, "6 weeks to 3 months"),
    (180, "3 to 6 months"), (math.inf, "Over 6 months"),
]


# Long lists are trimmed for the page; the counts stay complete and
# items_csv() exports every item.
PAGE_LIMITS = {"products": 100, "by_product": 50, "reorder_plan": 100, "stockouts": 20}
STATUS_LABELS = {"healthy": "Healthy", "low": "Low stock", "out_of_stock": "Out of stock",
                 "overstock": "Overstock", "no_sales": "No recent sales", "new": "Not stocked yet"}
_report_cache = {}


def build_report(period_days):
    """The full report, cached for as long as the underlying metrics are."""
    metrics = inventory.metrics_for_all(period_days)
    cached = _report_cache.get(period_days)
    if cached and cached[0] is metrics:
        return cached[1]
    now = datetime.now()
    empty = inventory.compute_metrics([], period_days, now, series=False)
    items = [(p, p["sku"] in metrics, metrics.get(p["sku"], empty)) for p in storefront.get_all_products()]
    report = assemble_report(items, period_days, now)
    _report_cache[period_days] = (metrics, report)
    return report


def for_page(report):
    """A copy of the report with its long lists trimmed for the browser."""
    out = dict(report)
    out["product_count"] = len(report["products"])
    out["products"] = report["products"][:PAGE_LIMITS["products"]]
    proc = dict(report["procurement"])
    proc["by_product_count"] = len(proc["by_product"])
    proc["by_product"] = proc["by_product"][:PAGE_LIMITS["by_product"]]
    proc["reorder_count"] = len(proc["reorder_plan"])
    proc["reorder_plan"] = proc["reorder_plan"][:PAGE_LIMITS["reorder_plan"]]
    out["procurement"] = proc
    inv = dict(report["inventory"])
    inv["stockout_count"] = len(inv["stockouts"])
    inv["stockout_lost_revenue_per_week"] = round(sum(s["lost_revenue_per_week"] for s in inv["stockouts"]), 2)
    inv["stockouts"] = inv["stockouts"][:PAGE_LIMITS["stockouts"]]
    out["inventory"] = inv
    return out


def items_csv(period_days):
    """Every item's figures for the period, as CSV text."""
    import csv
    import io
    report = build_report(period_days)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["SKU", "Title", "ABC class", "Status", "Units sold", "Revenue", "Net profit",
                     "Net margin %", "Share of revenue %", "On hand", "Stock value", "Days left",
                     "Turnover per year"])
    for p in report["products"]:
        writer.writerow([p["sku"], p["title"], p["abc"], STATUS_LABELS.get(p["status"], p["status"]),
                         p["units_sold"], p["revenue"], p["net_profit"], _blank(p["net_margin_pct"]),
                         p["revenue_share_pct"], p["on_hand"], p["stock_value"], _blank(p["days_left"]),
                         _blank(p["turnover_annualized"])])
    return buffer.getvalue()


def _blank(value):
    return "" if value is None else value


def assemble_report(items, period_days, now):
    """items: (product, whether it has ledger history, compute_metrics result) per SKU."""
    period_start = now - timedelta(days=period_days)
    period_start_s = period_start.strftime(inventory.FMT)
    rows, purchases = [], []
    trend = _empty_weeks(period_start, now)

    for product, tracked, m in items:
        raw, stock = m["raw"], m["stock"]
        tracked = bool(tracked)
        status = inventory.classify(stock["on_hand"], raw["velocity"], raw["days_left"]) if tracked else "new"
        reorder = inventory.reorder_suggestion(stock["on_hand"], raw["velocity"]) if tracked else {}
        avg_price = raw["revenue"] / raw["sold"] if raw["sold"] else product["price"]
        rows.append({
            "sku": product["sku"], "title": product["title"],
            "image": product["images"][0] if product.get("images") else None,
            "status": status,
            "units_sold": raw["sold"],
            "revenue": raw["revenue"],
            "cogs": raw["cogs"],
            "fees": m["profit"]["fees"],
            "writeoffs": m["profit"]["writeoffs"],
            "net_profit": raw["net_profit"],
            "net_margin_pct": m["profit"]["net_margin_pct"],
            "on_hand": stock["on_hand"],
            "avg_cost": stock["avg_cost"],
            "stock_value": stock["inventory_value"],
            "avg_inventory_value": raw["avg_inventory_value"],
            "opening": m["reconciliation"]["opening"],
            "received": m["reconciliation"]["purchased"],
            "velocity": raw["velocity"],
            "days_left": raw["days_left"],
            "turnover_annualized": m["turnover"]["turnover_annualized"],
            "avg_price": avg_price,
            "reorder_qty": reorder.get("quantity", 0) if reorder.get("suggested") else 0,
            "channels": m["channels"],
        })
        for p in m["series"]["purchases"]:
            purchases.append({"sku": product["sku"], "title": product["title"], **p})
        for r in m["rows"]:
            if r["movement_type"] == "sale" and r["occurred_at"] >= period_start_s:
                week = trend.get(_week_of(r["occurred_at"]))
                if week is not None:
                    units = -r["quantity"]
                    week["units"] += units
                    week["revenue"] += units * r["unit_price"]
                    week["net_profit"] += units * r["unit_price"] - r["cogs"] - (r["fee"] or 0)
                    week[r["channel"]] = week.get(r["channel"], 0) + units

    _assign_abc(rows)
    rows.sort(key=lambda r: (-r["revenue"], r["sku"]))
    revenue = sum(r["revenue"] for r in rows)
    cogs = sum(r["cogs"] for r in rows)
    avg_inventory = sum(r["avg_inventory_value"] for r in rows)
    spend = sum(p["quantity"] * p["unit_cost"] for p in purchases)

    return {
        "period_days": period_days,
        "generated_at": now.strftime(inventory.FMT),
        "summary": _summary(rows, revenue, cogs, avg_inventory, spend, purchases, period_days),
        "trend": [{"week": week, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in data.items()}}
                  for week, data in trend.items()],
        "top_sellers": {
            "by_units": [_brief(r) for r in sorted(rows, key=lambda r: -r["units_sold"]) if r["units_sold"] > 0][:5],
            "by_profit": [_brief(r) for r in sorted(rows, key=lambda r: -r["net_profit"]) if r["net_profit"] > 0][:5],
        },
        "products": [_product_row(r, revenue) for r in rows],
        "abc": _abc_summary(rows, revenue),
        "channels": _channels(rows),
        "procurement": _procurement(rows, purchases, spend, cogs),
        "inventory": _inventory_analysis(rows),
    }


def _summary(rows, revenue, cogs, avg_inventory, spend, purchases, period_days):
    fees = sum(r["fees"] for r in rows)
    writeoffs = sum(r["writeoffs"] for r in rows)
    net = sum(r["net_profit"] for r in rows)
    units = sum(r["units_sold"] for r in rows)
    available = sum(r["opening"] + r["received"] for r in rows)
    return {
        "revenue": _m(revenue), "cogs": _m(cogs), "fees": _m(fees), "writeoffs": _m(writeoffs),
        "gross_profit": _m(revenue - cogs), "net_profit": _m(net),
        "gross_margin_pct": _pct(revenue - cogs, revenue), "net_margin_pct": _pct(net, revenue),
        "units_sold": units,
        "avg_selling_price": _m(revenue / units) if units else None,
        "purchase_spend": _m(spend),
        "units_received": sum(p["quantity"] for p in purchases),
        "stock_value": _m(sum(r["stock_value"] for r in rows)),
        "stock_units": sum(r["on_hand"] for r in rows),
        "turnover_annualized": round(cogs / avg_inventory * 365 / period_days, 1) if avg_inventory else None,
        "sell_through_pct": _pct(units, available),
        "sku_count": len(rows),
    }


def _assign_abc(rows):
    total = sum(r["revenue"] for r in rows)
    cumulative = 0.0
    for r in sorted(rows, key=lambda r: -r["revenue"]):
        if total <= 0 or r["revenue"] <= 0:
            r["abc"] = "C"
            continue
        r["abc"] = "A" if cumulative < ABC_A_SHARE else "B" if cumulative < ABC_B_SHARE else "C"
        cumulative += r["revenue"] / total * 100


def _abc_summary(rows, revenue):
    stock_total = sum(r["stock_value"] for r in rows)
    out = []
    for cls in ("A", "B", "C"):
        members = [r for r in rows if r["abc"] == cls]
        out.append({
            "class": cls, "count": len(members),
            "revenue": _m(sum(r["revenue"] for r in members)),
            "revenue_share_pct": _pct(sum(r["revenue"] for r in members), revenue) or 0.0,
            "stock_value": _m(sum(r["stock_value"] for r in members)),
            "stock_share_pct": _pct(sum(r["stock_value"] for r in members), stock_total) or 0.0,
        })
    return out


def _channels(rows):
    stats = defaultdict(lambda: {"units": 0, "revenue": 0.0, "cogs": 0.0, "fees": 0.0})
    for r in rows:
        for c in r["channels"]:
            s = stats[c["channel"]]
            s["units"] += c["units"]
            s["revenue"] += c["revenue"]
            s["cogs"] += c["cogs"]
            s["fees"] += c["fees"]
    total_revenue = sum(s["revenue"] for s in stats.values())
    out = []
    for channel, s in stats.items():
        net = s["revenue"] - s["cogs"] - s["fees"]
        out.append({"channel": channel, "units": s["units"], "revenue": _m(s["revenue"]),
                    "cogs": _m(s["cogs"]), "fees": _m(s["fees"]), "net_profit": _m(net),
                    "net_margin_pct": _pct(net, s["revenue"]),
                    "revenue_share_pct": _pct(s["revenue"], total_revenue) or 0.0})
    return sorted(out, key=lambda c: -c["revenue"])


def _procurement(rows, purchases, spend, cogs):
    by_sku = defaultdict(list)
    for p in sorted(purchases, key=lambda p: p["date"]):
        by_sku[p["sku"]].append(p)
    by_product = []
    for sku, items in by_sku.items():
        units = sum(p["quantity"] for p in items)
        cost = sum(p["quantity"] * p["unit_cost"] for p in items)
        first, last = items[0]["unit_cost"], items[-1]["unit_cost"]
        by_product.append({
            "sku": sku, "title": items[0]["title"], "orders": len(items), "units": units,
            "spend": _m(cost), "avg_unit_cost": _m(cost / units) if units else None,
            "first_unit_cost": _m(first), "last_unit_cost": _m(last),
            "cost_change_pct": _pct(last - first, first) if len(items) > 1 and first else None,
            "last_received": items[-1]["date"],
        })
    by_product.sort(key=lambda p: -p["spend"])

    plan = []
    for r in rows:
        if r["reorder_qty"] > 0:
            unit_cost = r["avg_cost"] or 0.0
            plan.append({"sku": r["sku"], "title": r["title"], "status": r["status"],
                         "on_hand": r["on_hand"], "velocity": round(r["velocity"], 2),
                         "days_left": round(r["days_left"], 1) if r["days_left"] is not None else None,
                         "quantity": r["reorder_qty"], "unit_cost": _m(unit_cost),
                         "estimated_cost": _m(r["reorder_qty"] * unit_cost)})
    plan.sort(key=lambda p: (p["days_left"] is None, p["days_left"] or 0))

    return {
        "totals": {"spend": _m(spend), "orders": len(purchases),
                   "units": sum(p["quantity"] for p in purchases),
                   "avg_unit_cost": _m(spend / sum(p["quantity"] for p in purchases)) if purchases else None},
        "spend_vs_cogs": {
            "spend": _m(spend), "cogs": _m(cogs),
            # Buying more than you sold (at cost) means stock is building up.
            "direction": "building" if spend > cogs * 1.05 else "drawing_down" if spend < cogs * 0.95 else "steady",
        },
        "by_product": by_product,
        "reorder_plan": plan,
        "reorder_budget": _m(sum(p["estimated_cost"] for p in plan)),
    }


def _inventory_analysis(rows):
    status = []
    for s in STATUS_ORDER:
        members = [r for r in rows if r["status"] == s]
        if members:
            status.append({"status": s, "count": len(members),
                           "units": sum(r["on_hand"] for r in members),
                           "value": _m(sum(r["stock_value"] for r in members))})

    cover = [{"label": label, "count": 0, "value": 0.0} for _, label in COVER_BUCKETS]
    no_sales = {"label": "No recent sales", "count": 0, "value": 0.0}
    excess_items, stockouts = [], []
    for r in rows:
        if r["on_hand"] > 0 and r["velocity"] > 0:
            for i, (limit, _) in enumerate(COVER_BUCKETS):
                if r["days_left"] < limit:
                    cover[i]["count"] += 1
                    cover[i]["value"] += r["stock_value"]
                    break
        elif r["on_hand"] > 0 and r["status"] != "new":
            no_sales["count"] += 1
            no_sales["value"] += r["stock_value"]

        # Stock beyond the target cover ties up cash; with no recent sales, all of it does.
        if r["on_hand"] > 0 and r["status"] != "new":
            target = math.ceil(r["velocity"] * inventory.TARGET_COVER_DAYS)
            excess = r["on_hand"] - target if r["velocity"] > 0 else r["on_hand"]
            if excess > 0:
                excess_items.append({"sku": r["sku"], "title": r["title"], "excess_units": excess,
                                     "excess_value": _m(excess * r["avg_cost"]),
                                     "days_left": round(r["days_left"], 0) if r["days_left"] is not None else None})
        if r["status"] == "out_of_stock" and r["velocity"] > 0:
            stockouts.append({"sku": r["sku"], "title": r["title"], "velocity": round(r["velocity"], 2),
                              "lost_units_per_week": round(r["velocity"] * 7, 1),
                              "lost_revenue_per_week": _m(r["velocity"] * 7 * r["avg_price"])})

    for bucket in cover + [no_sales]:
        bucket["value"] = _m(bucket["value"])
    excess_items.sort(key=lambda e: -e["excess_value"])
    stockouts.sort(key=lambda s: -s["lost_revenue_per_week"])
    return {
        "status": status,
        "cover": [b for b in cover + [no_sales] if b["count"]],
        "excess": {"value": _m(sum(e["excess_value"] for e in excess_items)),
                   "units": sum(e["excess_units"] for e in excess_items),
                   "items": excess_items[:6]},
        "stockouts": stockouts,
    }


def _product_row(r, total_revenue):
    return {
        "sku": r["sku"], "title": r["title"], "image": r["image"], "status": r["status"], "abc": r["abc"],
        "units_sold": r["units_sold"], "revenue": _m(r["revenue"]), "net_profit": _m(r["net_profit"]),
        "net_margin_pct": r["net_margin_pct"], "revenue_share_pct": _pct(r["revenue"], total_revenue) or 0.0,
        "on_hand": r["on_hand"], "stock_value": _m(r["stock_value"]),
        "days_left": round(r["days_left"], 1) if r["days_left"] is not None else None,
        "turnover_annualized": r["turnover_annualized"],
    }


def _brief(r):
    return {"sku": r["sku"], "title": r["title"], "image": r["image"], "units_sold": r["units_sold"],
            "revenue": _m(r["revenue"]), "net_profit": _m(r["net_profit"]), "on_hand": r["on_hand"]}


def _empty_weeks(period_start, now):
    weeks, week = {}, period_start.date() - timedelta(days=period_start.weekday())
    last = now.date() - timedelta(days=now.weekday())
    while week <= last:
        weeks[week.isoformat()] = {"units": 0, "revenue": 0.0, "net_profit": 0.0,
                                   **{c: 0 for c in inventory.CHANNELS}}
        week += timedelta(days=7)
    return weeks


def _week_of(timestamp):
    day = datetime.strptime(timestamp, inventory.FMT).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _m(x):
    return None if x is None else round(x, 2)


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


# ---------------------------------------------------------------------------
# Admin overview and the shop's best sellers
# ---------------------------------------------------------------------------

def build_overview():
    report = build_report(30)
    attention = []
    conn = get_connection()
    try:
        listed_all = inventory.all_listed_quantities(conn)
        for p in report["products"]:
            listed = listed_all.get(p["sku"], {})
            over = [c for c, q in listed.items() if q is not None and q > p["on_hand"]]
            if p["status"] in ("out_of_stock", "low", "overstock") or over:
                attention.append({"sku": p["sku"], "title": p["title"], "status": p["status"],
                                  "on_hand": p["on_hand"], "days_left": p["days_left"],
                                  "revenue": p["revenue"], "stock_value": p["stock_value"],
                                  "oversell": over})
        try:
            orders = [dict(r) for r in conn.execute(
                "SELECT id, total, status, items_json, created_at, paid_at FROM orders "
                "ORDER BY id DESC LIMIT 6").fetchall()]
        except Exception:
            orders = []
    finally:
        conn.close()
    import json
    for o in orders:
        o["item_count"] = sum(i.get("quantity", 0) for i in json.loads(o.pop("items_json") or "[]"))
    # Overselling first (a sale eBay or Amazon can't fill), then stockouts of
    # the best sellers, items about to run out, and the biggest overstocks.
    order_rank = {"out_of_stock": 1, "low": 2, "overstock": 3}
    attention.sort(key=lambda a: (0 if a["oversell"] else order_rank.get(a["status"], 4),
                                  -a["revenue"] if a["status"] != "overstock" else -a["stock_value"]))
    counts = {"oversell": sum(1 for a in attention if a["oversell"])}
    for status in order_rank:
        counts[status] = sum(1 for a in attention if a["status"] == status)
    return {"summary": report["summary"], "top_sellers": report["top_sellers"]["by_units"],
            "attention": attention[:12], "attention_total": len(attention),
            "attention_counts": counts, "recent_orders": orders}


def best_sellers(limit=4):
    """For the shop's home page: the best-selling items that are in stock."""
    report = build_report(30)
    in_stock = [p for p in report["top_sellers"]["by_units"] if p["on_hand"] > 0]
    by_sku = {p["sku"]: p for p in storefront.get_all_products()}
    return [{"sku": p["sku"], "title": p["title"], "price": by_sku[p["sku"]]["price"],
             "image": p["image"], "inventory_qty": by_sku[p["sku"]]["inventory_qty"]}
            for p in in_stock[:limit] if p["sku"] in by_sku]
