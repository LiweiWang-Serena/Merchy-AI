"""Copy the fetched eBay listings into the products table, so the support chatbot can search them.
Backs up inventory.db first. Run from backend/:  python import_listings_for_eval.py"""
import html, shutil, sqlite3
from datetime import datetime
import pandas as pd

backup = f"inventory-backup-{datetime.now():%Y%m%d-%H%M}.db"
shutil.copy("inventory.db", backup)
print("Backed up to", backup)

lst = pd.read_csv("data/qa/listings.csv", dtype=str).fillna("")
meta = pd.read_csv("data/qa/listings_meta.csv", dtype=str).fillna("").drop_duplicates("title")
lst = lst.merge(meta[["title", "item_id", "price", "url"]], on="title", how="left")

rows = []
for r in lst.itertuples():
    text = f"Condition: {r.condition}. {r.item_specifics} {r.description}".strip()
    rows.append((
        "EBAY-" + (r.item_id or str(r.Index)).replace("|", "-"),
        r.title,
        "Paul Eagle",
        f"<p>{html.escape(text)}</p>",          # plain text, escaped so no markup is rendered
        "[]",
        float(r.price) if r.price else None,
        5,                                       # placeholder stock: real quantities come from the eBay sync
        r.url,
    ))

conn = sqlite3.connect("inventory.db")
conn.execute("DELETE FROM products WHERE sku LIKE 'EBAY-%'")   # re-running replaces the earlier import
conn.executemany("INSERT INTO products (sku, title, brand, description_html, images_json, price, inventory_qty, source_url) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
conn.commit()
print(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], "products now in inventory.db")
conn.close()