"""
The store's connected eBay account.

Connecting (see integrations/ebay_oauth.py): the admin creates a consent
link, the account owner signs in on eBay and clicks Agree, and the
one-time code comes back either by pasting eBay's confirmation address
into the admin page or, on an HTTPS deployment, through /ebay/callback.

Once connected:
  sync_listings()  active eBay listings -> the listings table (Listings
                   page and oversell checks), optionally adding listings
                   that aren't in the catalog yet
  import_orders()  paid eBay orders -> sales in the inventory ledger

Rules for imported orders:
  - A line is recorded once. Running the import again skips lines already
    imported, so it's always safe to re-run.
  - Canceled and unpaid orders are skipped.
  - A sale can't take stock below zero at the time it happened. If the
    ledger doesn't show enough stock then, the line is skipped and listed,
    so the stock can be recorded first and the import run again.
  - Buyer names and addresses are never stored.

Nothing on eBay is changed. Tokens are kept in backend/inventory.db, so
keep that file private.
"""

import bisect
import secrets
import time
from datetime import datetime, timedelta, timezone

import inventory
import store_import
from db import get_connection, init_db
from integrations import ebay_oauth
from integrations.ebay_oauth import EbayAuthError
from integrations.ebay_seller import EbaySellerError, SellerClient  # noqa: F401 (re-exported)

PENDING_MINUTES = 30
SAMPLE_LIMIT = 50          # rows of each kind returned to the page; counts stay complete
PAID_STATUSES = ("PAID", "PARTIALLY_REFUNDED")
LISTING_PAGES = {"production": "https://www.ebay.com/itm/", "sandbox": "https://sandbox.ebay.com/itm/"}

class NotConnected(ValueError):
    """No eBay account is connected for the current environment."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def status():
    settings = ebay_oauth.Settings.from_env()
    conn = _connect()
    try:
        account = _account(conn, settings.environment)
    finally:
        conn.close()
    out = {"environment": settings.environment, "missing_settings": settings.missing(),
           "connected": account is not None}
    if account:
        out.update({
            "username": account["username"],
            "connected_at": account["connected_at"],
            "access_until": datetime.fromtimestamp(account["refresh_expires_at"]).strftime("%Y-%m-%d"),
            "last_listing_sync": account["last_listing_sync"],
            "last_order_import": account["last_order_import"],
        })
    return out


def is_connected():
    return status()["connected"]


def create_consent_link():
    settings = _ready_settings()
    state = secrets.token_urlsafe(24)
    conn = _connect()
    try:
        conn.execute("DELETE FROM ebay_pending_consent WHERE created_at < ?", (time.time() - PENDING_MINUTES * 60,))
        conn.execute("INSERT INTO ebay_pending_consent (state, created_at) VALUES (?, ?)", (state, time.time()))
        conn.commit()
    finally:
        conn.close()
    return ebay_oauth.consent_url(settings, state)


def complete(pasted, require_state=False):
    """Finishes connecting, from the address eBay sent the owner to (or
    just its code). The code must belong to a consent link made here in
    the last 30 minutes."""
    code, state = ebay_oauth.read_code(pasted)
    settings = _ready_settings()
    cutoff = time.time() - PENDING_MINUTES * 60
    conn = _connect()
    try:
        if state:
            known = conn.execute("SELECT 1 FROM ebay_pending_consent WHERE state = ? AND created_at >= ?",
                                 (state, cutoff)).fetchone()
            if not known:
                raise ValueError("That address is from an old or unknown connection attempt. Open eBay "
                                 "again from the admin page and use the new address.")
        elif require_state or not conn.execute(
                "SELECT 1 FROM ebay_pending_consent WHERE created_at >= ?", (cutoff,)).fetchone():
            raise ValueError("Open eBay from the admin page first, then paste the address you land on.")

        tokens = ebay_oauth.exchange_code(settings, code)
        try:
            username = SellerClient(settings.api_host, tokens["access_token"]).get_username()
        except EbaySellerError:
            username = None   # still connected; the name is only shown on the page

        conn.execute("DELETE FROM ebay_account WHERE environment = ?", (settings.environment,))
        conn.execute(
            """INSERT INTO ebay_account (environment, username, scopes, refresh_token, refresh_expires_at,
               access_token, access_expires_at, connected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (settings.environment, username, " ".join(settings.scopes), tokens["refresh_token"],
             tokens["refresh_expires_at"], tokens["access_token"], tokens["access_expires_at"], _now()))
        if state:
            conn.execute("DELETE FROM ebay_pending_consent WHERE state = ?", (state,))
        else:
            conn.execute("DELETE FROM ebay_pending_consent")
        conn.commit()
    finally:
        conn.close()
    return status()


def disconnect():
    """Forgets the saved authorization. To withdraw it on eBay's side too,
    the owner removes the app in their eBay account settings."""
    settings = ebay_oauth.Settings.from_env()
    conn = _connect()
    try:
        conn.execute("DELETE FROM ebay_account WHERE environment = ?", (settings.environment,))
        conn.commit()
    finally:
        conn.close()
    return status()


def _client():
    """A SellerClient with a valid access token, refreshing it when it has
    expired (access tokens last two hours)."""
    settings = ebay_oauth.Settings.from_env()
    conn = _connect()
    try:
        account = _account(conn, settings.environment)
        if not account:
            raise NotConnected("The eBay account isn't connected yet.")
        if account["access_token"] and (account["access_expires_at"] or 0) > time.time() + 60:
            return SellerClient(settings.api_host, account["access_token"])
        if account["refresh_expires_at"] <= time.time():
            raise EbayAuthError("The eBay authorization has expired (it lasts about 18 months). "
                                "Connect the account again.")
        fresh = ebay_oauth.refresh(settings, account["refresh_token"], account["scopes"].split())
        conn.execute("UPDATE ebay_account SET access_token = ?, access_expires_at = ? WHERE environment = ?",
                     (fresh["access_token"], fresh["access_expires_at"], settings.environment))
        conn.commit()
        return SellerClient(settings.api_host, fresh["access_token"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def listing_records(listings=None):
    """Active listings in the shape sync.py stores: one record per SKU,
    with the eBay units sold in the last 30 days taken from the ledger."""
    if listings is None:
        listings = _client().get_active_listings()
    by_sku = {}
    for listing in listings:
        record = by_sku.setdefault(listing["sku"], {
            "sku": listing["sku"], "channel": "ebay", "title": listing["title"],
            "quantity_available": 0, "price": listing["price"], "units_sold_last_30d": 0})
        record["quantity_available"] += listing["quantity_available"]
    sold = _ebay_units_sold(days=30)
    for sku, record in by_sku.items():
        record["units_sold_last_30d"] = sold.get(sku, 0)
    return list(by_sku.values())


def sync_listings(add_missing=False):
    settings = ebay_oauth.Settings.from_env()
    listings = _client().get_active_listings()
    records = listing_records(listings)
    init_db()   # makes sure the listings table exists
    conn = _connect()
    try:
        known = {r["sku"] for r in conn.execute("SELECT sku FROM products")}
        missing = [r for r in records if r["sku"] not in known]
        conn.execute("DELETE FROM listings WHERE channel = 'ebay'")
        conn.executemany(
            """INSERT INTO listings (sku, channel, title, quantity_available, price, units_sold_last_30d)
               VALUES (?, 'ebay', ?, ?, ?, ?)""",
            [(r["sku"], r["title"], r["quantity_available"], r["price"], r["units_sold_last_30d"])
             for r in records])
        conn.execute("UPDATE ebay_account SET last_listing_sync = ? WHERE environment = ?",
                     (_now(), settings.environment))
        conn.commit()
    finally:
        conn.close()

    added = []
    if add_missing and missing:
        first = {}
        for listing in listings:
            first.setdefault(listing["sku"], listing)
        added = store_import.save_products([{
            "sku": r["sku"], "sku_from_store": True, "title": r["title"][:200], "brand": None,
            "description_html": "", "price": r["price"],
            "images": [first[r["sku"]]["image"]] if first[r["sku"]].get("image") else [],
            "source_url": LISTING_PAGES[settings.environment] + first[r["sku"]]["item_id"],
        } for r in missing])
    still_missing = [] if add_missing else missing
    return {
        "listings": len(listings),
        "skus": len(records),
        "added": added,
        "not_in_catalog_count": len(still_missing),
        "not_in_catalog": [{"sku": r["sku"], "title": r["title"], "quantity_available": r["quantity_available"]}
                           for r in still_missing[:25]],
    }


def _ebay_units_sold(days):
    cutoff = (datetime.now() - timedelta(days=days)).strftime(inventory.FMT)
    conn = get_connection()
    try:
        inventory.ensure_ledger_table(conn)
        rows = conn.execute(
            """SELECT sku, -SUM(quantity) FROM inventory_movements WHERE movement_type = 'sale'
               AND channel = 'ebay' AND occurred_at >= ? GROUP BY sku""", (cutoff,)).fetchall()
    finally:
        conn.close()
    return {sku: units for sku, units in rows}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def import_orders(days, until_days=0):
    """Orders created between `days` and `until_days` days ago. The page
    asks for one 30-day window at a time, so a two-year import shows
    progress instead of one very long wait."""
    if not 0 <= until_days < days <= 730:
        raise ValueError("Choose a window within the last two years.")
    settings = ebay_oauth.Settings.from_env()
    now = datetime.now(timezone.utc)
    stamp = "%Y-%m-%dT%H:%M:%S.000Z"
    until = None if until_days == 0 else (now - timedelta(days=until_days)).strftime(stamp)
    orders = _client().get_orders((now - timedelta(days=days)).strftime(stamp), until)
    lines, skipped_orders = order_lines(orders)
    result = record_lines(lines)
    conn = _connect()
    try:
        conn.execute("UPDATE ebay_account SET last_order_import = ? WHERE environment = ?",
                     (_now(), settings.environment))
        conn.commit()
    finally:
        conn.close()
    return {"orders": len(orders), "skipped_orders": skipped_orders, **result}


def order_lines(orders):
    """Flattens eBay orders into sale lines. Returns (lines, skipped order
    counts by reason). Only SKUs, quantities, prices and fees are read."""
    lines, skipped = [], {"canceled": 0, "not_paid": 0}
    for order in orders:
        if (order.get("cancelStatus") or {}).get("cancelState") == "CANCELED":
            skipped["canceled"] += 1
            continue
        if order.get("orderPaymentStatus") not in PAID_STATUSES:
            skipped["not_paid"] += 1
            continue
        items = order.get("lineItems") or []
        order_total = sum(_amount(li.get("lineItemCost")) for li in items)
        fee_total = _amount(order["totalMarketplaceFee"]) if order.get("totalMarketplaceFee") else None
        when = _local_time(order.get("creationDate"))
        for li in items:
            quantity = int(li.get("quantity") or 0)
            line_total = _amount(li.get("lineItemCost"))   # unit price x quantity
            if quantity <= 0:
                continue
            if fee_total is not None:
                fee, estimated = (round(fee_total * line_total / order_total, 2) if order_total else 0.0), False
            else:
                fee, estimated = inventory.allocate_order_fee("ebay", line_total, order_total), True
            lines.append({
                "order_id": order["orderId"],
                "line_item_id": li.get("lineItemId") or f"{order['orderId']}-{li.get('legacyItemId')}",
                "sku": (li.get("sku") or "").strip() or f"EBAY-{li.get('legacyItemId')}",
                "title": li.get("title") or "",
                "quantity": quantity,
                "unit_price": round(line_total / quantity, 2),
                "fee": fee,
                "fee_estimated": estimated,
                "occurred_at": when,
            })
    return lines, skipped


def record_lines(lines):
    """Records sale lines in the ledger (see the rules at the top). Returns
    complete counts plus up to SAMPLE_LIMIT example rows of each kind."""
    result = {"recorded": [], "recorded_count": 0, "recorded_units": 0, "already_imported": 0,
              "not_in_catalog": [], "not_in_catalog_count": 0,
              "not_enough_stock": [], "not_enough_stock_count": 0}
    missing = {}
    conn = _connect()
    try:
        inventory.ensure_ledger_table(conn)
        imported = {r[0] for r in conn.execute("SELECT line_item_id FROM ebay_imported_lines")}
        known = {r[0] for r in conn.execute("SELECT sku FROM products")}
        history, movements, line_rows = {}, [], []
        for line in sorted(lines, key=lambda l: (l["occurred_at"], l["line_item_id"])):
            if line["line_item_id"] in imported:
                result["already_imported"] += 1
                continue
            sku, when, quantity = line["sku"], line["occurred_at"], line["quantity"]
            brief = {"sku": sku, "title": line["title"], "quantity": quantity,
                     "order_id": line["order_id"], "date": when[:10]}
            if sku not in known:
                result["not_in_catalog_count"] += 1
                entry = missing.setdefault(sku, {**brief, "lines": 0, "quantity": 0})
                entry["lines"] += 1
                entry["quantity"] += quantity
                entry["date"] = when[:10]
                continue
            rows = history.get(sku)
            if rows is None:
                rows = history[sku] = [(q, at) for q, at in conn.execute(
                    "SELECT quantity, occurred_at FROM inventory_movements WHERE sku = ? "
                    "ORDER BY occurred_at, id", (sku,))]
            available = lowest_balance_from(rows, when)
            if available < quantity:
                result["not_enough_stock_count"] += 1
                if len(result["not_enough_stock"]) < SAMPLE_LIMIT:
                    result["not_enough_stock"].append({**brief, "available": max(available, 0)})
                continue
            position = bisect.bisect_right([at for _, at in rows], when)
            rows.insert(position, (-quantity, when))
            movements.append((sku, "sale", "ebay", -quantity, None, line["unit_price"], line["fee"],
                              f"eBay order {line['order_id']}",
                              "fee estimated" if line["fee_estimated"] else None, when))
            line_rows.append((line["line_item_id"], line["order_id"], sku, _now()))
            imported.add(line["line_item_id"])
            result["recorded_count"] += 1
            result["recorded_units"] += quantity
            if len(result["recorded"]) < SAMPLE_LIMIT:
                result["recorded"].append({**brief, "unit_price": line["unit_price"], "fee": line["fee"]})

        conn.executemany(inventory.INSERT_SQL, movements)
        conn.executemany("INSERT INTO ebay_imported_lines (line_item_id, order_id, sku, imported_at) "
                         "VALUES (?, ?, ?, ?)", line_rows)
        for sku in {m[0] for m in movements}:
            conn.execute("UPDATE products SET inventory_qty = (SELECT COALESCE(SUM(quantity), 0) "
                         "FROM inventory_movements WHERE sku = ?) WHERE sku = ?", (sku, sku))
        conn.commit()
    finally:
        conn.close()
    result["not_in_catalog"] = sorted(missing.values(), key=lambda m: -m["quantity"])[:SAMPLE_LIMIT]
    return result


def lowest_balance_from(rows, when):
    """The lowest stock level from `when` onward. A sale dated `when` must
    fit under it, or stock would dip below zero at some later point."""
    running = sum(q for q, at in rows if at <= when)
    lowest = running
    for quantity, at in rows:
        if at > when:
            running += quantity
            lowest = min(lowest, running)
    return lowest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect():
    return get_connection()


def _account(conn, environment):
    row = conn.execute("SELECT * FROM ebay_account WHERE environment = ?", (environment,)).fetchone()
    return dict(row) if row else None


def _ready_settings():
    settings = ebay_oauth.Settings.from_env()
    if settings.missing():
        raise ValueError(f"Add {', '.join(settings.missing())} to backend/.env, then restart the server.")
    return settings


def _amount(value):
    try:
        return float((value or {}).get("value"))
    except (TypeError, ValueError):
        return 0.0


def _local_time(iso):
    """eBay timestamps are UTC ('2026-09-20T15:04:05.000Z'); the ledger uses local time."""
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().strftime(inventory.FMT)
    return moment.astimezone().strftime(inventory.FMT)


def _now():
    return datetime.now().strftime(inventory.FMT)
