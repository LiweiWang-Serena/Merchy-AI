"""
Simulates a Stripe Checkout flow closely enough that swapping in the real
`stripe` Python SDK later is a small, mechanical change:

    create_session(cart)   -> stripe.checkout.Session.create(...)
    confirm_payment(...)   -> handled by Stripe itself + your webhook
                               listening for `checkout.session.completed`

Card validation below mirrors Stripe's own published test card numbers,
so the "declined" and "insufficient funds" paths behave the same way
they would in Stripe's real test mode:
https://stripe.com/docs/testing
"""

import json
import secrets
from datetime import datetime, timezone

from db import get_connection, ensure_storefront_tables

TEST_CARD_OUTCOMES = {
    "4242424242424242": "success",
    "4000000000000002": "declined",
    "4000000000009995": "insufficient_funds",
    "4000000000000069": "expired_card",
}


def create_session(items: list[dict], customer_email: str):
    """items: [{sku, quantity}]. Looks up live prices/stock server-side --
    never trusts a price the client sends -- and stores a pending order."""
    from storefront import get_product  # local import avoids a circular import

    ensure_storefront_tables()
    line_items = []
    total = 0.0
    for item in items:
        product = get_product(item["sku"])
        if not product:
            raise ValueError(f"Unknown SKU: {item['sku']}")
        if item["quantity"] > product["inventory_qty"]:
            raise ValueError(f"Only {product['inventory_qty']} left of {product['title']}")
        line_total = round(product["price"] * item["quantity"], 2)
        total += line_total
        line_items.append({
            "sku": product["sku"],
            "title": product["title"],
            "unit_price": product["price"],
            "quantity": item["quantity"],
            "line_total": line_total,
        })

    session_id = "cs_test_" + secrets.token_hex(12)
    conn = get_connection()
    conn.execute(
        """INSERT INTO orders (session_id, customer_email, items_json, total, status)
           VALUES (?, ?, ?, ?, 'pending')""",
        (session_id, customer_email, json.dumps(line_items), round(total, 2)),
    )
    conn.commit()
    conn.close()

    return {
        "session_id": session_id,
        "items": line_items,
        "total": round(total, 2),
        "customer_email": customer_email,
    }


def confirm_payment(session_id: str, card_number: str, exp: str, cvc: str):
    """Validates the card against Stripe's published test-card behavior
    and, on success, marks the order paid and decrements inventory."""
    from storefront import get_all_products  # avoids circular import

    conn = get_connection()
    order = conn.execute(
        "SELECT * FROM orders WHERE session_id = ?", (session_id,)
    ).fetchone()

    if not order:
        conn.close()
        return {"status": "error", "message": "Unknown checkout session"}
    if order["status"] == "paid":
        conn.close()
        return {"status": "error", "message": "This order has already been paid"}

    digits = card_number.replace(" ", "")
    if len(digits) != 16 or not digits.isdigit():
        conn.close()
        return {"status": "declined", "message": "Invalid card number"}

    outcome = TEST_CARD_OUTCOMES.get(digits, "success" if _luhn_valid(digits) else "invalid")

    if outcome != "success":
        messages = {
            "declined": "Your card was declined.",
            "insufficient_funds": "Your card has insufficient funds.",
            "expired_card": "Your card has expired.",
            "invalid": "Card number failed validation.",
        }
        conn.close()
        return {"status": "declined", "message": messages.get(outcome, "Payment failed.")}

    from inventory import record_sale_in_ledger, allocate_order_fee  # avoids circular import

    items = json.loads(order["items_json"])
    for item in items:
        # Take the stock only if it's still there. Checked in the UPDATE
        # itself, so two people paying for the last unit at the same moment
        # can't both get it: the second update finds too little and changes
        # nothing, and the whole payment is undone.
        taken = conn.execute(
            "UPDATE products SET inventory_qty = inventory_qty - ? WHERE sku = ? AND inventory_qty >= ?",
            (item["quantity"], item["sku"], item["quantity"]),
        ).rowcount
        if taken != 1:
            conn.rollback()
            conn.close()
            return {"status": "error",
                    "message": f"{item['title']} sold out while you were checking out. Your card wasn't charged."}
        _record_website_sale(conn, item)
        # Same transaction as the stock decrement above, so the ledger and
        # the storefront's cached quantity can't disagree after a sale.
        record_sale_in_ledger(
            conn,
            sku=item["sku"],
            channel="website",
            quantity=item["quantity"],
            unit_price=item["unit_price"],
            fee=allocate_order_fee("website", item["line_total"], order["total"]),
            reference=f"Website order #{order['id']}",
        )

    # Also conditional, so the same order can't be paid twice at once.
    paid = conn.execute(
        """UPDATE orders SET status = 'paid', card_last4 = ?, paid_at = ?
           WHERE session_id = ? AND status != 'paid'""",
        (digits[-4:], datetime.now(timezone.utc).isoformat(), session_id),
    ).rowcount
    if paid != 1:
        conn.rollback()
        conn.close()
        return {"status": "error", "message": "This order has already been paid"}
    conn.commit()

    updated_order = conn.execute(
        "SELECT * FROM orders WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()

    return {
        "status": "success",
        "order_id": updated_order["id"],
        "total": updated_order["total"],
        "card_last4": digits[-4:],
    }


def _record_website_sale(conn, item: dict):
    """Keeps the 'website' channel's row in the unified `listings` table
    in sync with what actually sold on the storefront, so the channel
    ledger and procurement report reflect direct sales alongside Amazon
    and eBay -- not just the two marketplaces."""
    product = conn.execute(
        "SELECT * FROM products WHERE sku = ?", (item["sku"],)
    ).fetchone()

    existing = conn.execute(
        "SELECT * FROM listings WHERE sku = ? AND channel = 'website'", (item["sku"],)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE listings SET quantity_available = ?, units_sold_last_30d = units_sold_last_30d + ?
               WHERE id = ?""",
            (product["inventory_qty"], item["quantity"], existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO listings (sku, channel, title, quantity_available, price, units_sold_last_30d)
               VALUES (?, 'website', ?, ?, ?, ?)""",
            (item["sku"], product["title"], product["inventory_qty"], product["price"], item["quantity"]),
        )


def _luhn_valid(card_number: str) -> bool:
    """Standard Luhn checksum -- the same algorithm card networks use to
    catch typos. Any number that passes this (and isn't one of Stripe's
    special test numbers above) is treated as a generic successful test
    charge, matching how Stripe's test mode behaves."""
    digits = [int(d) for d in card_number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0
