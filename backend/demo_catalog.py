"""
A large, realistic demo catalog for trying the system at a real store's
scale: thousands of powersports and small-engine parts with the sales
pattern a multichannel parts seller actually has -- a few fast movers,
a middle group, and a long tail of parts that sell a few times a year.

    python demo_catalog.py              # 10,000 items (backs up inventory.db first)
    python demo_catalog.py --skus 3000

The history covers the last 120 days: an opening stock count, sales on
eBay, the website and Amazon, restocks that arrive after a lead time
(at slowly changing costs), items that ran out and weren't restocked,
overstocked slow sellers, and the occasional damaged unit. All names are
generic aftermarket fitment descriptions; no real listings are copied.
"""

import argparse
import json
import math
import random
import shutil
from datetime import datetime, timedelta

import db
import inventory
from storefront import ensure_storefront_tables

PARTS = [
    # name, SKU code, price range
    ("Carburetor", "CARB", 22, 89), ("Carburetor Rebuild Kit", "CRK", 8, 29),
    ("Inner Tube", "TUBE", 6, 19), ("Tire", "TIRE", 18, 69), ("Drum Brake Assembly", "DRUM", 24, 58),
    ("Brake Pads", "PAD", 7, 26), ("Brake Caliper", "CALP", 19, 64), ("CDI Box", "CDI", 11, 42),
    ("Ignition Coil", "COIL", 9, 34), ("Stator", "STAT", 21, 79), ("Voltage Regulator Rectifier", "REG", 13, 46),
    ("Starter Motor", "STRT", 29, 112), ("Starter Relay", "RLY", 6, 18), ("Fuel Pump", "FPMP", 14, 68),
    ("Fuel Petcock Valve", "PETC", 6, 21), ("Air Filter", "AIRF", 5, 19), ("Oil Filter", "OILF", 4, 11),
    ("Spark Plug", "PLUG", 4, 13), ("Throttle Cable", "THRC", 6, 21), ("Clutch Cable", "CLTC", 6, 19),
    ("Clutch Lever", "CLTL", 6, 18), ("Centrifugal Clutch", "CCLU", 16, 49), ("Drive Chain", "CHN", 12, 39),
    ("Sprocket", "SPRK", 8, 27), ("Piston Kit", "PIST", 19, 72), ("Gasket Set", "GSKT", 8, 27),
    ("Water Pump", "WPMP", 24, 94), ("Rear Shock Absorber", "SHCK", 29, 119), ("Headlight Assembly", "HEAD", 14, 58),
    ("Handlebar Grips", "GRIP", 5, 15), ("Kickstand", "KICK", 9, 32), ("Pull Starter", "PULL", 12, 38),
    ("Muffler", "MUFF", 18, 74), ("Axle", "AXLE", 22, 86), ("Wheel Bearing Kit", "BRG", 9, 31),
]
FITS = [
    "Yamaha Kodiak 450", "Yamaha Grizzly 350", "Yamaha Big Bear 400", "Honda TRX300", "Honda Rancher 350",
    "Honda Foreman 450", "Honda Recon 250", "Polaris Sportsman 500", "Polaris Scrambler 400", "Suzuki LT-Z400",
    "Suzuki King Quad 300", "Kawasaki Bayou 220", "Kawasaki Prairie 360", "Arctic Cat 400", "Can-Am Outlander 500",
    "GY6 150cc Scooter", "GY6 50cc Scooter", "49cc 2-Stroke Pocket Bike", "43cc Mini Chopper", "110cc Pit Bike",
    "125cc Dirt Bike", "Predator 212cc", "Honda GX160", "Honda GX200", "Coolster 125cc ATV",
    "Tao Tao 110cc ATV", "Razor MX350", "Razor Pocket Mod", "Go Kart 6.5HP", "Johnson Evinrude 40HP",
    "Mercury 9.9HP Outboard", "Stihl MS250 Chainsaw", "Troy-Bilt Tiller", "Club Car Golf Cart", "EZGO TXT Golf Cart",
]
OPTIONS = ["", "Heavy Duty", "OEM Style", "Pair", "Set of 2", "Black", "Chrome", "Upgraded", "Complete Kit",
           "Left Side", "Right Side", "Front", "Rear"]
CHANNEL_WEIGHTS = (("ebay", 0.68), ("website", 0.20), ("amazon", 0.12))


def build_catalog(count, rng):
    combos = [(p, f, o) for p in PARTS for f in FITS for o in OPTIONS]
    rng.shuffle(combos)
    catalog = []
    for i, ((name, code, lo, hi), fit, option) in enumerate(combos[:count], start=1):
        title = f"{name}{' ' + option if option else ''} for {fit}"
        price = math.floor(rng.uniform(lo, hi)) + rng.choice((0.95, 0.99))
        catalog.append({
            "sku": f"{code}-{i:05d}", "title": title[:80], "brand": "Aftermarket",
            "description_html": f"<p>Aftermarket {name.lower()} for {fit}.</p>",
            "price": price, "cost": round(price * rng.uniform(0.35, 0.6), 2),
        })
    return catalog


def simulate(item, start, days, rng):
    """One item's ledger rows over `days` days from `start`."""
    tier = rng.random()
    if tier < 0.02:
        lam, restock = rng.uniform(0.2, 1.2), True                 # fast movers
    elif tier < 0.20:
        lam, restock = rng.uniform(0.02, 0.10), True               # middle group
    else:
        lam, restock = rng.uniform(0.0008, 0.008), False           # long tail
    stops_restocking = restock and rng.random() < 0.08            # runs out near the end
    overstocked = rng.random() < 0.05

    if overstocked:
        opening = rng.randint(60, 300)
    elif tier < 0.20:
        opening = max(3, math.ceil(lam * rng.uniform(40, 120)))
    else:
        opening = rng.randint(1, 12)

    cost = item["cost"]
    rows = [{"movement_type": "purchase", "quantity": opening, "unit_cost": cost,
             "reference": "Opening stock count", "occurred_at": start.strftime(inventory.FMT)}]
    balance, pending = opening, None
    for day in range(days):
        date = start + timedelta(days=day)
        if pending and pending[0] == day:
            cost = round(cost * rng.uniform(0.96, 1.07), 2)
            rows.append({"movement_type": "purchase", "quantity": pending[1], "unit_cost": cost,
                         "reference": f"PO {date:%y%m%d}-{rng.randint(10, 99)}",
                         "occurred_at": (date + timedelta(hours=10)).strftime(inventory.FMT)})
            balance += pending[1]
            pending = None
        for _ in range(inventory._poisson(rng, lam)):
            if balance <= 0:
                break
            quantity = min(balance, 2 if rng.random() < 0.1 else 1)
            channel = _pick(rng)
            unit_price = round(item["price"] * rng.uniform(0.93, 1.0), 2)
            moment = date + timedelta(minutes=rng.randint(8 * 60, 22 * 60))
            rows.append({"movement_type": "sale", "channel": channel, "quantity": -quantity,
                         "unit_price": unit_price,
                         "fee": inventory.channel_fee(channel, unit_price * quantity),
                         "occurred_at": moment.strftime(inventory.FMT)})
            balance -= quantity
        can_restock = restock and not (stops_restocking and day > days * 0.5)
        if can_restock and not pending and balance <= lam * 21:
            pending = (day + rng.randint(5, 12), max(2, math.ceil(lam * rng.uniform(45, 75))))
    if balance > 0 and rng.random() < 0.004:
        when = start + timedelta(days=rng.randint(10, days - 1), hours=15)
        rows.append({"movement_type": "adjustment", "quantity": -1, "note": "Damaged in storage",
                     "occurred_at": when.strftime(inventory.FMT)})
        balance -= 1
    rows.sort(key=lambda r: r["occurred_at"])
    return rows, balance


def _pick(rng):
    roll, total = rng.random(), 0.0
    for channel, weight in CHANNEL_WEIGHTS:
        total += weight
        if roll < total:
            return channel
    return CHANNEL_WEIGHTS[-1][0]


def install(count=10_000, days=120, seed=7, backup=True):
    """Replaces the database with a fresh catalog of `count` items."""
    if db.is_postgres():
        conn = db.get_connection()
        try:
            for table in reversed(db.TABLES):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()
        finally:
            conn.close()
    else:
        if backup and db.DB_PATH.exists():
            target = db.DB_PATH.with_name(f"inventory-backup-{datetime.now():%Y%m%d-%H%M%S}.db")
            shutil.copy2(db.DB_PATH, target)
            print(f"Backed up the old database to {target.name}")
        if db.DB_PATH.exists():
            db.DB_PATH.unlink()
    db.forget_schema()

    rng = random.Random(seed)
    start = datetime.combine(datetime.now().date(), datetime.min.time()) - timedelta(days=days)
    catalog = build_catalog(count, rng)
    ensure_storefront_tables()
    conn = db.get_connection()
    try:
        inventory.ensure_ledger_table(conn)
        movements, products = [], []
        for item in catalog:
            rows, on_hand = simulate(item, start, days, rng)
            products.append((item["sku"], item["title"], item["brand"], item["description_html"],
                             json.dumps([]), item["price"], on_hand))
            movements.extend((item["sku"], r["movement_type"], r.get("channel"), r["quantity"],
                              r.get("unit_cost"), r.get("unit_price"), r.get("fee", 0.0),
                              r.get("reference"), r.get("note"), r["occurred_at"]) for r in rows)
        conn.executemany("""INSERT INTO products (sku, title, brand, description_html, images_json,
                            price, inventory_qty) VALUES (?, ?, ?, ?, ?, ?, ?)""", products)
        conn.executemany(inventory.INSERT_SQL, movements)
        db.upsert_meta(conn, "ledger_seeded", datetime.now().strftime(inventory.FMT))
        db.upsert_meta(conn, "demo_catalog", "parts")
        conn.commit()
    finally:
        conn.close()
    return {"items": len(catalog), "movements": len(movements)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replace the database with a large demo parts catalog.")
    parser.add_argument("--skus", type=int, default=10_000, help="number of items (default 10,000)")
    parser.add_argument("--yes", action="store_true", help="don't ask before replacing a PostgreSQL database")
    args = parser.parse_args()
    if db.is_postgres() and not args.yes:
        answer = input(f"This deletes all data in {db.describe()} and replaces it with demo data.\n"
                       "Back it up first with pg_dump if you need it. Type yes to continue: ")
        if answer.strip().lower() != "yes":
            raise SystemExit("Nothing was changed.")
    result = install(args.skus)
    print(f"Created {result['items']:,} items with {result['movements']:,} ledger entries in {db.describe()}.")
