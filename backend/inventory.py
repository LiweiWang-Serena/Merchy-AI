"""
Inventory management: a stock movement ledger as the system of record,
plus per-SKU purchase / sales / stock (进销存), profit, and turnover
analytics.

Every change to physical stock is one row in `inventory_movements`:

    purchase    +qty   unit_cost          stock received from a supplier
    sale        -qty   unit_price, fee    sold on amazon / ebay / website
    adjustment  +/-qty note               count corrections, damage, loss

On-hand quantity is the running sum of the ledger -- never a number that
can silently drift. products.inventory_qty is only a cached copy for the
storefront, updated in the same transaction as every ledger write, and
the item detail endpoint reports whether the two still agree.

Costing uses the moving weighted-average method, the usual choice for
small multi-channel sellers: each purchase re-averages the unit cost,
and each sale's cost of goods sold (COGS) is its quantity times the
average cost at the moment it happened.

In production, sale rows would come from each platform's Orders API
(Shopify Orders / eBay Fulfillment / Amazon Orders -- see integrations/)
and purchase rows from supplier invoices; the analytics don't change.
"""

import math
import os
from functools import lru_cache
from time import monotonic
import random
from collections import defaultdict
from datetime import datetime, time, timedelta

import storefront
import store_import
import db
from db import get_connection, ensure_storefront_tables

FMT = "%Y-%m-%d %H:%M:%S"
CHANNELS = ("amazon", "ebay", "website")
CHANNEL_DISPLAY_NAMES = {"amazon": "Amazon", "ebay": "eBay", "website": "Website"}

# Illustrative defaults only -- check each platform's current fee
# schedule for your product category before trusting profit figures.
FEE_RULES = {
    "amazon":  {"pct": 0.15,   "fixed_per_order": 0.00},  # referral fee
    "ebay":    {"pct": 0.1325, "fixed_per_order": 0.40},  # final value fee + per-order fee
    "website": {"pct": 0.029,  "fixed_per_order": 0.30},  # Stripe card processing
}

# Demo only: how prices differ by channel in the generated history.
CHANNEL_PRICE_FACTOR = {"amazon": 1.06, "ebay": 1.00, "website": 0.97}

LOW_STOCK_DAYS = 14      # fewer days of stock than this -> "low"
OVERSTOCK_DAYS = 120     # more days of stock than this -> "overstock"
TARGET_COVER_DAYS = 45   # reorder suggestions aim for this much cover

INSERT_SQL = """
    INSERT INTO inventory_movements
        (sku, movement_type, channel, quantity, unit_cost, unit_price,
         fee, reference, note, occurred_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def ensure_ledger_table(conn=None):
    """Kept for callers; the tables are created by db.ensure_schema()."""
    db.ensure_schema()


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

def channel_fee(channel, amount):
    """Fee for one order worth `amount` dollars on `channel`."""
    rule = FEE_RULES.get(channel)
    if not rule:
        return 0.0
    return round(amount * rule["pct"] + rule["fixed_per_order"], 2)


def allocate_order_fee(channel, line_total, order_total):
    """Splits one order's fee across its line items: the percentage part
    applies to each line directly; the fixed per-order part is shared in
    proportion to each line's share of the order total."""
    rule = FEE_RULES.get(channel)
    if not rule or order_total <= 0:
        return 0.0
    share = line_total / order_total
    return round(line_total * rule["pct"] + rule["fixed_per_order"] * share, 2)


# ---------------------------------------------------------------------------
# Pure analytics (no database) -- covered by tests/test_inventory.py
# ---------------------------------------------------------------------------

def replay(movements):
    """Walks one SKU's movements in time order and returns each movement
    enriched with the running balance, the weighted-average unit cost
    after it, its COGS (sales), and its write-off cost (negative
    adjustments)."""
    on_hand, avg_cost = 0, 0.0
    enriched = []
    for m in movements:
        qty = m["quantity"]
        cogs = writeoff = 0.0
        kind = m["movement_type"]
        if kind == "purchase":
            new_on_hand = on_hand + qty
            if new_on_hand > 0:
                avg_cost = (on_hand * avg_cost + qty * m["unit_cost"]) / new_on_hand
            on_hand = new_on_hand
        elif kind == "sale":
            cogs = -qty * avg_cost
            on_hand += qty
        elif kind == "adjustment":
            if qty < 0:
                writeoff = -qty * avg_cost
            on_hand += qty
        enriched.append({**m, "cogs": cogs, "writeoff": writeoff,
                         "balance": on_hand, "avg_cost": avg_cost})
    return enriched


def compute_metrics(movements, period_days, now, series=True):
    """Everything the inventory pages show for one SKU over the last
    `period_days`, derived from its (time-ordered) ledger rows.

    series=False is the light mode used for lists and reports across
    thousands of items: the same figures, without the daily stock and
    weekly sales series (only the item page draws those), and with only
    the period's sales kept in "rows"."""
    rows = replay(movements)
    period_start = now - timedelta(days=period_days)
    period_start_s = period_start.strftime(FMT)

    opening = 0
    for r in rows:
        if r["occurred_at"] >= period_start_s:
            break
        opening = r["balance"]
    in_period = [r for r in rows if r["occurred_at"] >= period_start_s]
    closing = rows[-1]["balance"] if rows else 0
    avg_cost = rows[-1]["avg_cost"] if rows else 0.0

    purchases = [r for r in in_period if r["movement_type"] == "purchase"]
    sales = [r for r in in_period if r["movement_type"] == "sale"]
    adjustments = [r for r in in_period if r["movement_type"] == "adjustment"]

    purchased = sum(r["quantity"] for r in purchases)
    sold = -sum(r["quantity"] for r in sales)
    adjusted = sum(r["quantity"] for r in adjustments)

    revenue = sum(-r["quantity"] * r["unit_price"] for r in sales)
    fees = sum(r["fee"] or 0.0 for r in sales)
    cogs = sum(r["cogs"] for r in sales)
    writeoffs = sum(r["writeoff"] for r in adjustments)
    gross_profit = revenue - cogs
    net_profit = gross_profit - fees - writeoffs

    if series:
        daily = _daily_snapshots(rows, period_start, now)
        avg_inventory_value = sum(d["value"] for d in daily) / len(daily) if daily else 0.0
    else:
        daily = []
        avg_inventory_value = _average_daily_value(rows, period_start, now)
    turnover = cogs / avg_inventory_value if avg_inventory_value > 0 else None

    last_30_s = (now - timedelta(days=30)).strftime(FMT)
    sold_last_30 = -sum(r["quantity"] for r in rows
                        if r["movement_type"] == "sale" and r["occurred_at"] >= last_30_s)
    in_stock_days = _in_stock_days(rows, now, window=30)
    velocity = sold_last_30 / in_stock_days if in_stock_days else 0.0
    days_left = closing / velocity if velocity > 0 else None

    return {
        "rows": rows if series else sales,
        "stock": {
            "on_hand": closing,
            "avg_cost": _r2(avg_cost),
            "inventory_value": _r2(closing * avg_cost),
        },
        "reconciliation": {
            "opening": opening, "purchased": purchased, "sold": sold,
            "adjusted": adjusted, "closing": closing,
            "reconciles": opening + purchased - sold + adjusted == closing,
        },
        "profit": {
            "revenue": _r2(revenue),
            "cogs": _r2(cogs),
            "fees": _r2(fees),
            "writeoffs": _r2(writeoffs),
            "gross_profit": _r2(gross_profit),
            "net_profit": _r2(net_profit),
            "gross_margin_pct": _pct(gross_profit, revenue),
            "net_margin_pct": _pct(net_profit, revenue),
            "purchase_spend": _r2(sum(r["quantity"] * r["unit_cost"] for r in purchases)),
            "per_unit": {
                "avg_price": _r2(revenue / sold) if sold else None,
                "avg_cost": _r2(cogs / sold) if sold else None,
                "avg_fee": _r2(fees / sold) if sold else None,
                "net_profit": _r2((revenue - cogs - fees) / sold) if sold else None,
            },
        },
        "turnover": {
            "avg_inventory_value": _r2(avg_inventory_value),
            "turnover": _r(turnover, 2),
            "turnover_annualized": _r(turnover * 365 / period_days, 1) if turnover is not None else None,
            "days_of_inventory": _r(period_days / turnover, 1) if turnover else None,
            "sell_through_pct": _pct(sold, opening + purchased),
            "velocity_per_day": _r(velocity, 2),
            "in_stock_days_last_30": in_stock_days,
            "days_of_stock_left": _r(days_left, 1),
        },
        "channels": _channel_breakdown(sales, sold),
        "series": {
            "daily_stock": daily,
            "weekly_sales": _weekly_sales(sales, period_start, now) if series else [],
            "purchases": [{"date": r["occurred_at"][:10], "quantity": r["quantity"],
                           "unit_cost": r["unit_cost"]} for r in purchases],
        },
        # Unrounded figures, for aggregating across SKUs.
        "raw": {"cogs": cogs, "avg_inventory_value": avg_inventory_value,
                "revenue": revenue, "net_profit": net_profit, "sold": sold,
                "velocity": velocity, "days_left": days_left},
    }


def classify(on_hand, velocity, days_left):
    if on_hand <= 0:
        return "out_of_stock"
    if velocity == 0:
        return "no_sales"
    if days_left < LOW_STOCK_DAYS:
        return "low"
    if days_left > OVERSTOCK_DAYS:
        return "overstock"
    return "healthy"


def reorder_suggestion(on_hand, velocity):
    if velocity <= 0:
        return {"suggested": False, "quantity": 0,
                "reason": "No sales in the last 30 days, so no reorder is needed."}
    days_left = on_hand / velocity
    if days_left >= LOW_STOCK_DAYS:
        return {"suggested": False, "quantity": 0,
                "reason": f"About {days_left:.0f} days of stock left at the current sales pace."}
    qty = max(1, math.ceil(velocity * TARGET_COVER_DAYS - on_hand))
    if on_hand <= 0:
        reason = (f"Out of stock. It sold about {velocity:.1f} a day while in stock; "
                  f"ordering {qty} restores about {TARGET_COVER_DAYS} days of stock.")
    else:
        days_text = "1 day" if round(days_left) <= 1 else f"{days_left:.0f} days"
        reason = (f"Selling about {velocity:.1f} a day with {on_hand} on hand "
                  f"(roughly {days_text} left). Ordering {qty} restores "
                  f"about {TARGET_COVER_DAYS} days of stock.")
    return {"suggested": True, "quantity": qty, "reason": reason}


@lru_cache(maxsize=16)
def _day_starts(first_day, window):
    """(ISO date, 'YYYY-MM-DD 00:00:00') for each day of a window."""
    return tuple((d.isoformat(), datetime.combine(d, time()).strftime(FMT))
                 for d in (first_day + timedelta(days=k) for k in range(window)))


@lru_cache(maxsize=16)
def _day_ends(first_day, now):
    """(ISO date, end-of-day cutoff capped at `now`) for each day up to now."""
    out, day = [], first_day
    while day <= now.date():
        out.append((day.isoformat(), min(datetime.combine(day, time(23, 59, 59)), now).strftime(FMT)))
        day += timedelta(days=1)
    return tuple(out)


def _in_stock_days(rows, now, window):
    """Days in the trailing window when the item could actually sell:
    it started the day with stock, or sold something that day. Sales
    pace divides by these rather than the calendar window, so a stockout
    doesn't make demand look lower than it is -- which would otherwise
    shrink the very reorder that fixes the stockout."""
    days = _day_starts(now.date() - timedelta(days=window - 1), window)
    first_day_s = days[0][1]
    if not rows or rows[-1]["occurred_at"] < first_day_s:
        # Nothing happened in the window: stock was the same every day.
        return window if rows and rows[-1]["balance"] > 0 else 0
    sale_days = {r["occurred_at"][:10] for r in rows
                 if r["movement_type"] == "sale" and r["occurred_at"] >= first_day_s}
    count, i, balance = 0, 0, 0
    for iso_day, day_start in days:
        while i < len(rows) and rows[i]["occurred_at"] < day_start:
            balance = rows[i]["balance"]
            i += 1
        if balance > 0 or iso_day in sale_days:
            count += 1
    return count


def _daily_snapshots(rows, period_start, now):
    """End-of-day balance and stock value for each day in the period,
    used for the stock chart and the average-inventory figure behind
    turnover."""
    snapshots = []
    i, balance, avg_cost = 0, 0, 0.0
    for iso_day, cutoff in _day_ends(period_start.date(), now):
        while i < len(rows) and rows[i]["occurred_at"] <= cutoff:
            balance, avg_cost = rows[i]["balance"], rows[i]["avg_cost"]
            i += 1
        snapshots.append({"date": iso_day, "balance": balance,
                          "value": round(balance * avg_cost, 2)})
    return snapshots


def _average_daily_value(rows, period_start, now):
    """The average of the daily end-of-day stock values _daily_snapshots
    would produce, without building the list."""
    days = _day_ends(period_start.date(), now)
    if not days:
        return 0.0
    if not rows or rows[-1]["occurred_at"] <= days[0][1]:
        # No movement after the first day ended: the value never changed.
        # Add it up day by day anyway, exactly as the full calculation does,
        # so both give the same figure to the last decimal.
        value = round(rows[-1]["balance"] * rows[-1]["avg_cost"], 2) if rows else 0.0
        return sum(value for _ in days) / len(days)
    # Same values, same order and the same sum() as _daily_snapshots: since
    # Python 3.12, sum() of floats compensates for rounding, so a manual
    # += loop would differ in the last decimals.
    values, i, balance, avg_cost = [], 0, 0, 0.0
    for _, cutoff in days:
        while i < len(rows) and rows[i]["occurred_at"] <= cutoff:
            balance, avg_cost = rows[i]["balance"], rows[i]["avg_cost"]
            i += 1
        values.append(round(balance * avg_cost, 2))
    return sum(values) / len(values)


def _weekly_sales(sales, period_start, now):
    def week_of(d):
        return d - timedelta(days=d.weekday())

    weeks = {}
    week = week_of(period_start.date())
    while week <= week_of(now.date()):
        weeks[week.isoformat()] = {c: 0 for c in CHANNELS}
        week += timedelta(days=7)
    for r in sales:
        sold_on = datetime.strptime(r["occurred_at"], FMT).date()
        bucket = weeks.get(week_of(sold_on).isoformat())
        if bucket is not None:
            bucket[r["channel"]] = bucket.get(r["channel"], 0) - r["quantity"]
    return [{"week": k, **v} for k, v in weeks.items()]


def _channel_breakdown(sales, total_units):
    stats = {c: {"units": 0, "revenue": 0.0, "fees": 0.0, "cogs": 0.0} for c in CHANNELS}
    for r in sales:
        s = stats.setdefault(r["channel"] or "other",
                             {"units": 0, "revenue": 0.0, "fees": 0.0, "cogs": 0.0})
        units = -r["quantity"]
        s["units"] += units
        s["revenue"] += units * r["unit_price"]
        s["fees"] += r["fee"] or 0.0
        s["cogs"] += r["cogs"]
    breakdown = []
    for channel, s in stats.items():
        net = s["revenue"] - s["cogs"] - s["fees"]
        breakdown.append({
            "channel": channel, "units": s["units"],
            "revenue": _r2(s["revenue"]), "cogs": _r2(s["cogs"]), "fees": _r2(s["fees"]),
            "net_profit": _r2(net), "net_margin_pct": _pct(net, s["revenue"]),
            "unit_share_pct": _pct(s["units"], total_units) or 0.0,
        })
    return breakdown


def _r(x, digits):
    return None if x is None else round(x, digits)


def _r2(x):
    return _r(x, 2)


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


# ---------------------------------------------------------------------------
# Database-backed operations
# ---------------------------------------------------------------------------

def _movements_for(conn, sku):
    rows = conn.execute(
        "SELECT * FROM inventory_movements WHERE sku = ? ORDER BY occurred_at, id", (sku,)
    ).fetchall()
    return [dict(r) for r in rows]


def _listed_quantities(conn, sku):
    """What each marketplace currently advertises as available, from the
    last channel sync -- compared against physical stock to flag
    oversell risk."""
    listed = {"amazon": None, "ebay": None}
    for r in conn.execute(
        "SELECT channel, quantity_available FROM listings "
        "WHERE sku = ? AND channel IN ('amazon', 'ebay')", (sku,)
    ):
        listed[r["channel"]] = r["quantity_available"]
    return listed


def _references_for(conn, sku):
    """Other sellers' prices saved when the item was created from a
    reference link (see new_item.py)."""
    rows = conn.execute(
        "SELECT reference_site, reference_url, reference_price, created_at "
        "FROM product_references WHERE sku = ? ORDER BY id", (sku,)).fetchall()
    return [{"site": r["reference_site"], "url": r["reference_url"],
             "price": r["reference_price"], "saved_at": r["created_at"]} for r in rows]


def record_sale_in_ledger(conn, sku, channel, quantity, unit_price, fee, reference):
    """Called by checkout.py inside its own transaction, so the order, the
    storefront stock cache, and the ledger all commit (or fail) together."""
    ensure_ledger_table(conn)
    conn.execute(INSERT_SQL, (sku, "sale", channel, -abs(int(quantity)), None, unit_price,
                              fee, reference, None, datetime.now().strftime(FMT)))


def record_movement(sku, movement_type, quantity, unit_cost=None, reference=None, note=None):
    """Records a purchase (stock received) or a manual adjustment."""
    if movement_type not in ("purchase", "adjustment"):
        raise ValueError("Type must be 'purchase' or 'adjustment'.")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValueError("Quantity must be a whole number.")

    if movement_type == "purchase":
        if quantity <= 0:
            raise ValueError("A purchase needs a positive quantity.")
        try:
            unit_cost = float(unit_cost)
        except (TypeError, ValueError):
            raise ValueError("A purchase needs a unit cost.")
        if not 0 < unit_cost < 1_000_000:
            raise ValueError("Unit cost must be greater than zero.")
    else:
        if quantity == 0:
            raise ValueError("An adjustment can't be zero.")
        unit_cost = None

    reference = (reference or "").strip()[:120] or None
    note = (note or "").strip()[:120] or None

    conn = get_connection()
    try:
        ensure_ledger_table(conn)
        if not conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone():
            raise ValueError(f"Unknown SKU: {sku}")
        on_hand = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM inventory_movements WHERE sku = ?", (sku,)
        ).fetchone()[0]
        new_on_hand = on_hand + quantity
        if new_on_hand < 0:
            raise ValueError(f"Only {on_hand} on hand, so {-quantity} can't be removed.")

        conn.execute(INSERT_SQL, (sku, movement_type, None, quantity, unit_cost, None, 0.0,
                                  reference, note, datetime.now().strftime(FMT)))
        conn.execute("UPDATE products SET inventory_qty = ? WHERE sku = ?", (new_on_hand, sku))
        conn.commit()
    finally:
        conn.close()
    return {"sku": sku, "on_hand": new_on_hand}


# ---------------------------------------------------------------------------
# All items at once: the inventory list, the overview and the reports.
# Built to stay quick with tens of thousands of SKUs: one query loads the
# whole ledger, the light metrics skip the daily series, and results are
# cached until the ledger changes (any new movement changes its row count
# and last id) or ten minutes pass, so the time windows keep moving.
# ---------------------------------------------------------------------------

CACHE_SECONDS = 600
_metrics_cache = {}
ATTENTION_STATUSES = ("out_of_stock", "low", "overstock")
# Most urgent first, used when sorting by status.
STATUS_RANK = {"out_of_stock": 0, "low": 1, "overstock": 2, "no_sales": 3, "new": 4, "healthy": 5}
SORT_KEYS = ("title", "on_hand", "inventory_value", "units_sold", "revenue", "net_margin_pct",
             "turnover_annualized", "days_of_stock_left", "status")


def all_movements(conn):
    """Every SKU's ledger rows in time order, from a single query."""
    by_sku = defaultdict(list)
    for r in conn.execute("SELECT * FROM inventory_movements ORDER BY sku, occurred_at, id"):
        by_sku[r["sku"]].append(dict(r))
    return by_sku


def all_listed_quantities(conn):
    """What each marketplace advertises for every SKU (last channel sync)."""
    listed = defaultdict(lambda: {"amazon": None, "ebay": None})
    for r in conn.execute("SELECT sku, channel, quantity_available FROM listings "
                          "WHERE channel IN ('amazon', 'ebay')"):
        listed[r["sku"]][r["channel"]] = r["quantity_available"]
    return listed


def metrics_for_all(period_days, now=None):
    """{sku: light metrics} for every SKU that has ledger history. Treat
    the result as read-only: it's shared through the cache."""
    conn = get_connection()
    try:
        ensure_ledger_table(conn)
        count, last_id = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM inventory_movements").fetchone()
        key = (db.identity(), period_days, count, last_id)
        cached = _metrics_cache.get(key)
        if now is None and cached and monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        moment = now or datetime.now()
        by_sku = all_movements(conn)
    finally:
        conn.close()
    metrics = {sku: compute_metrics(rows, period_days, moment, series=False) for sku, rows in by_sku.items()}
    if now is None:
        for old in [k for k in _metrics_cache if k[:2] == key[:2]]:
            del _metrics_cache[old]
        _metrics_cache[key] = (monotonic(), metrics)
    return metrics


def _all_item_rows(period_days):
    now = datetime.now()
    metrics = metrics_for_all(period_days)
    empty = compute_metrics([], period_days, now, series=False)
    items, totals, status_counts = [], defaultdict(float), defaultdict(int)
    for p in storefront.get_all_products():
        m = metrics.get(p["sku"])
        tracked = m is not None
        m = m or empty
        raw = m["raw"]
        status = classify(m["stock"]["on_hand"], raw["velocity"], raw["days_left"]) if tracked else "new"
        status_counts[status] += 1
        items.append({
            "sku": p["sku"],
            "title": p["title"],
            "image": p["images"][0] if p["images"] else None,
            "source_url": p.get("source_url"),
            "on_hand": m["stock"]["on_hand"],
            "inventory_value": m["stock"]["inventory_value"],
            "units_sold": raw["sold"],
            "revenue": m["profit"]["revenue"],
            "net_profit": m["profit"]["net_profit"],
            "gross_margin_pct": m["profit"]["gross_margin_pct"],
            "net_margin_pct": m["profit"]["net_margin_pct"],
            "turnover_annualized": m["turnover"]["turnover_annualized"],
            "days_of_stock_left": m["turnover"]["days_of_stock_left"],
            "status": status,
        })
        for key in ("cogs", "avg_inventory_value", "revenue", "net_profit"):
            totals[key] += raw[key]
        totals["inventory_value"] += m["stock"]["inventory_value"]
        totals["units"] += m["stock"]["on_hand"]

    blended = (totals["cogs"] / totals["avg_inventory_value"] * 365 / period_days
               if totals["avg_inventory_value"] else None)
    summary = {
        "sku_count": len(items),
        "stock_units": int(totals["units"]),
        "status_counts": dict(status_counts),
        "inventory_value": _r2(totals["inventory_value"]),
        "revenue": _r2(totals["revenue"]),
        "net_profit": _r2(totals["net_profit"]),
        "net_margin_pct": _pct(totals["net_profit"], totals["revenue"]),
        "turnover_annualized": _r(blended, 1),
    }
    return items, summary


def get_overview(period_days):
    """Every item with its figures, plus totals (no paging)."""
    items, summary = _all_item_rows(period_days)
    return {"period_days": period_days, "store_domain": store_import.store_domain(),
            "summary": summary, "items": items}


def list_items(period_days, q="", status="", sort="inventory_value", direction="desc",
               page=1, per_page=50):
    """One page of the inventory list, searched, filtered and sorted on
    the server, with totals over all items."""
    items, summary = _all_item_rows(period_days)
    words = (q or "").lower().split()
    if words:
        items = [i for i in items if all(w in f"{i['sku']} {i['title']}".lower() for w in words)]
    if status == "attention":
        items = [i for i in items if i["status"] in ATTENTION_STATUSES]
    elif status:
        items = [i for i in items if i["status"] == status]

    key = sort if sort in SORT_KEYS else "inventory_value"
    descending = direction != "asc"
    if key == "status":
        items.sort(key=lambda i: STATUS_RANK.get(i["status"], 9), reverse=descending)
    else:
        present = [i for i in items if i[key] is not None]
        present.sort(key=lambda i: i[key].lower() if key == "title" else i[key], reverse=descending)
        items = present + [i for i in items if i[key] is None]   # missing values always last

    per_page = max(10, min(int(per_page or 50), 200))
    pages = max(1, math.ceil(len(items) / per_page))
    page = min(max(1, int(page or 1)), pages)
    return {
        "period_days": period_days, "store_domain": store_import.store_domain(),
        "summary": summary, "total": len(items), "page": page, "pages": pages, "per_page": per_page,
        "items": items[(page - 1) * per_page: page * per_page],
    }


def get_item_detail(sku, period_days, recent=30):
    product = storefront.get_product(sku)
    if not product:
        return None

    conn = get_connection()
    try:
        ledger_rows = _movements_for(conn, sku)
        m = compute_metrics(ledger_rows, period_days, datetime.now())
        listed = _listed_quantities(conn, sku)
        references = _references_for(conn, sku)
    finally:
        conn.close()

    raw = m["raw"]
    on_hand = m["stock"]["on_hand"]
    listed["website"] = product["inventory_qty"]
    keys = ("id", "occurred_at", "movement_type", "channel", "quantity", "unit_cost",
            "unit_price", "fee", "reference", "note", "balance")
    movements = [{k: r[k] for k in keys} for r in reversed(m["rows"][-recent:])]

    return {
        "product": {
            "sku": product["sku"], "title": product["title"], "brand": product["brand"],
            "image": product["images"][0] if product["images"] else None,
            "price": product["price"],
            "source_url": product.get("source_url"),
        },
        "period_days": period_days,
        "status": classify(on_hand, raw["velocity"], raw["days_left"]) if ledger_rows else "new",
        "stock": {
            **m["stock"],
            "storefront_qty": product["inventory_qty"],
            "matches_storefront": product["inventory_qty"] == on_hand,
            "listed": listed,
            "oversell_channels": [c for c, q in listed.items() if q is not None and q > on_hand],
        },
        "reconciliation": m["reconciliation"],
        "profit": m["profit"],
        "turnover": m["turnover"],
        "channels": m["channels"],
        "series": m["series"],
        "reorder": reorder_suggestion(on_hand, raw["velocity"]) if ledger_rows else {
            "suggested": False, "quantity": 0,
            "reason": "Not stocked yet. Record the stock you have on hand, with its cost "
                      "per unit, to start tracking this item."},
        "movements": movements,
        "references": references,
        "fee_rules": FEE_RULES,
    }


# ---------------------------------------------------------------------------
# Demo history
# ---------------------------------------------------------------------------

def demo_data_enabled():
    """Set DEMO_DATA=false in backend/.env to start with an empty
    inventory (for example, to track only items imported from the store).
    Delete inventory.db after changing it."""
    return os.environ.get("DEMO_DATA", "true").strip().lower() != "false"


def ensure_seeded():
    if not demo_data_enabled():
        ensure_storefront_tables()
        ensure_ledger_table()
        return
    storefront.seed_products()
    seed_ledger()


def seed_ledger(days=120):
    """Generates about four months of believable demo history per SKU:
    an opening purchase, daily sales across channels, reorders placed at
    a reorder point and received after a lead time, and the occasional
    damaged-unit write-off. Runs once; delete inventory.db to regenerate."""
    ensure_storefront_tables()
    conn = get_connection()
    try:
        ensure_ledger_table(conn)
        if conn.execute("SELECT 1 FROM app_meta WHERE key = 'ledger_seeded'").fetchone():
            return

        today = datetime.combine(datetime.now().date(), time())
        start = today - timedelta(days=days)
        products = conn.execute("SELECT sku, price FROM products ORDER BY sku").fetchall()
        for index, product in enumerate(products):
            scenario = DEMO_SCENARIOS[index % len(DEMO_SCENARIOS)]
            rows, on_hand = _simulate_history(product["sku"], product["price"], start, days, scenario)
            conn.executemany(INSERT_SQL, [
                (product["sku"], r["movement_type"], r.get("channel"), r["quantity"],
                 r.get("unit_cost"), r.get("unit_price"), r.get("fee", 0.0),
                 r.get("reference"), r.get("note"), r["occurred_at"])
                for r in rows
            ])
            conn.execute("UPDATE products SET inventory_qty = ? WHERE sku = ?",
                         (on_hand, product["sku"]))
        db.upsert_meta(conn, "ledger_seeded", datetime.now().strftime(FMT))
        conn.commit()
    finally:
        conn.close()


# Demo only: a mix of situations so every status and the reorder logic
# appear in the generated history. Assigned to SKUs in rotation.
DEMO_SCENARIOS = ["steady", "overstock", "low", "steady", "stalled", "steady", "stockout", "steady"]


def _simulate_history(sku, price, start, days, scenario="steady"):
    rng = random.Random(f"ledger:{sku}")
    base_cost = price * rng.uniform(0.38, 0.55)
    demand = rng.choice([rng.uniform(0.05, 0.3), rng.uniform(0.4, 1.0), rng.uniform(1.0, 2.2)])
    weights = {c: rng.uniform(0.2, 1.0) for c in CHANNELS}
    cover_days = rng.uniform(25, 80)
    lead_time = rng.randint(5, 12)

    # Each scenario changes how the final weeks of history ("the tail") play out.
    tail_start = days  # "steady": no special tail
    if scenario == "overstock":      # an oversized delivery lands; sales stay slow
        demand, tail_start = rng.uniform(0.35, 0.6), days - 20
    elif scenario == "low":          # last delivery arrives, nothing reordered since
        demand, cover_days, tail_start = max(demand, 0.8), 20, days - 22
    elif scenario == "stockout":     # supplier stops shipping; stock runs out
        demand, cover_days, tail_start = max(demand, 0.8), 20, days - 45
    elif scenario == "stalled":      # demand dries up entirely
        demand, tail_start = max(demand, 0.4), days - 40

    reorder_qty = max(6, round(demand * cover_days))
    reorder_point = max(2, round(demand * (8 if scenario in ("low", "stockout") else 12)))

    rows = []
    po_count = 0

    def receive(day, qty, cost_factor):
        nonlocal po_count
        po_count += 1
        rows.append({"movement_type": "purchase", "quantity": qty,
                     "unit_cost": round(base_cost * cost_factor, 2),
                     "reference": f"PO {sku} #{po_count}",
                     "occurred_at": (day + timedelta(hours=9)).strftime(FMT)})
        return qty

    on_hand = receive(start, reorder_qty + rng.randint(2, 12), rng.uniform(0.97, 1.03))
    pending = None  # (arrival day index, quantity, cost factor)

    # Stops at yesterday, so no generated movement is ever in the future.
    # Within a day: deliveries at 9:00, sales 11:00-21:59, write-offs 22:30.
    for d in range(1, days):
        day = start + timedelta(days=d)
        in_tail = d >= tail_start

        if d == tail_start:
            pending = None  # the tail replaces normal replenishment
            if scenario == "overstock":
                on_hand += receive(day, round(demand * 170) + 20, rng.uniform(0.90, 0.97))
            elif scenario == "low" and on_hand < round(demand * 29):
                on_hand += receive(day, round(demand * 29) - on_hand, rng.uniform(0.95, 1.05))
            elif scenario == "stockout" and on_hand < round(demand * 20):
                on_hand += receive(day, round(demand * 20) - on_hand, rng.uniform(0.95, 1.05))
            elif scenario == "stalled" and on_hand < 8:
                on_hand += receive(day, 8 - on_hand, rng.uniform(0.95, 1.05))
        elif pending and pending[0] == d:
            on_hand += receive(day, pending[1], pending[2])
            pending = None

        todays_demand = 0 if (scenario == "stalled" and in_tail) else demand
        units = min(_poisson(rng, todays_demand), on_hand)
        by_channel = defaultdict(int)
        for _ in range(units):
            by_channel[_pick_channel(rng, weights)] += 1
        for channel, qty in by_channel.items():
            unit_price = round(price * CHANNEL_PRICE_FACTOR[channel] * rng.uniform(0.97, 1.03), 2)
            sold_at = day + timedelta(hours=rng.randint(11, 21), minutes=rng.randint(0, 59))
            rows.append({"movement_type": "sale", "channel": channel, "quantity": -qty,
                         "unit_price": unit_price, "fee": channel_fee(channel, qty * unit_price),
                         "reference": f"{CHANNEL_DISPLAY_NAMES[channel]} orders",
                         "occurred_at": sold_at.strftime(FMT)})
        on_hand -= units

        if on_hand > 0 and rng.random() < 0.012:
            rows.append({"movement_type": "adjustment", "quantity": -1,
                         "note": "Damaged in storage", "reference": "Cycle count",
                         "occurred_at": (day + timedelta(hours=22, minutes=30)).strftime(FMT)})
            on_hand -= 1

        replenishment_stopped = in_tail and scenario in ("low", "stockout", "stalled")
        if pending is None and not replenishment_stopped and on_hand <= reorder_point:
            pending = (d + lead_time, reorder_qty, rng.uniform(0.94, 1.07))

    rows.sort(key=lambda r: r["occurred_at"])
    return rows, on_hand


def _poisson(rng, lam):
    """Knuth's method; fine for the small daily sales rates used here."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _pick_channel(rng, weights):
    x = rng.random() * sum(weights.values())
    for channel, w in weights.items():
        x -= w
        if x <= 0:
            return channel
    return channel
