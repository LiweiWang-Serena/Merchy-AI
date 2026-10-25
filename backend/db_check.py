"""
Checks that the PostgreSQL database in DATABASE_URL works with Channel
Ledger, without touching your data: it creates every table in a
throwaway schema, writes and reads back a product, a sale and a draft,
and deletes the schema again.

    python db_check.py
"""

import sys
import uuid

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db
import inventory


def hint(error):
    text = str(error)
    if "does not exist" in text and "database" in text:
        return "The database doesn't exist yet. Create it with: createdb channel_ledger"
    if "Connection refused" in text or "could not connect" in text or "No such file" in text:
        return "PostgreSQL isn't running. Open Postgres.app and check that the server is started."
    if "password" in text:
        return "The user name or password in DATABASE_URL isn't right."
    return text


def main():
    if not db.is_postgres():
        print(f"DATABASE_URL isn't set to PostgreSQL, so the app uses the {db.describe()}. Nothing to check.")
        return
    try:
        import psycopg
    except ImportError:
        sys.exit("The PostgreSQL driver isn't installed. Run: pip install -r requirements.txt")

    url, schema = db.database_url(), f"check_{uuid.uuid4().hex[:10]}"
    try:
        with psycopg.connect(url, autocommit=True) as admin:
            version = admin.execute("SHOW server_version").fetchone()[0]
            admin.execute(f"CREATE SCHEMA {schema}")
    except psycopg.OperationalError as e:
        sys.exit(f"Couldn't connect to {db.describe()}.\n{hint(e)}")

    db.PG_SCHEMA = schema
    try:
        conn = db.get_connection()            # creates every table in the throwaway schema
        conn.execute("INSERT INTO products (sku, title, price, inventory_qty) VALUES (?, ?, ?, ?)",
                     ("CHECK-1", "Check item", 12345.67, 3))
        conn.execute(inventory.INSERT_SQL, ("CHECK-1", "sale", "ebay", -1, None, 12345.67, 2.5,
                                            "check", None, "2026-09-29 12:00:00"))
        draft_id = db.insert_returning_id(conn, "INSERT INTO draft_listings (sku) VALUES (?)", ("CHECK-1",))
        db.upsert_meta(conn, "check", "1")
        db.upsert_meta(conn, "check", "2")
        conn.commit()
        row = conn.execute("""SELECT p.price, SUM(m.quantity) AS moved, MAX(d.created_at) AS drafted
                              FROM products p JOIN inventory_movements m ON m.sku = p.sku
                              JOIN draft_listings d ON d.sku = p.sku GROUP BY p.price""").fetchone()
        meta = conn.execute("SELECT value FROM app_meta WHERE key = 'check'").fetchone()[0]
        conn.close()
        problems = []
        if row["price"] != 12345.67:
            problems.append(f"price came back as {row['price']!r}")
        if row["moved"] != -1 or not isinstance(row["moved"], int):
            problems.append(f"SUM came back as {row['moved']!r}")
        if not isinstance(draft_id, int) or len(row["drafted"]) != 19:
            problems.append(f"new id {draft_id!r}, timestamp {row['drafted']!r}")
        if meta != "2":
            problems.append(f"upsert kept {meta!r}")
        if problems:
            sys.exit("PostgreSQL connected, but some values came back wrong: " + "; ".join(problems))
        print(f"PostgreSQL {version} works with Merchy-AI: every table created, and rows written "
              f"and read back exactly. Your data wasn't touched.")
    finally:
        db.PG_SCHEMA = None
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


if __name__ == "__main__":
    main()
