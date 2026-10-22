"""
The database for the whole app: SQLite by default, PostgreSQL when
DATABASE_URL is set in backend/.env.

    DATABASE_URL=postgresql://localhost/channel_ledger

SQLite needs nothing installed (the data is the file backend/inventory.db),
which keeps first runs and the test suite simple. PostgreSQL is the
database to use for a deployed, multi-user site.

The rest of the code talks to one interface either way:
    conn = get_connection()
    rows = conn.execute("SELECT sku, title FROM products WHERE price > ?", (10,)).fetchall()
    rows[0]["sku"], rows[0][0], dict(rows[0])      # by name, by position, as a dict
    conn.executemany(sql, many_params); conn.commit(); conn.close()

For PostgreSQL this module translates the few differences:
  - ? placeholders become %s (and a literal % becomes %%).
  - In CREATE TABLE: REAL becomes DOUBLE PRECISION (PostgreSQL's REAL has
    only ~6 significant digits, which would round prices and timestamps),
    AUTOINCREMENT ids become identity columns, and CURRENT_TIMESTAMP
    defaults produce the same "YYYY-MM-DD HH:MM:SS" text as SQLite.
  - NUMERIC results (such as SUM of a bigint column) come back as Python
    ints or floats, not Decimals, so arithmetic and JSON stay the same.
Every other query in the app is standard SQL that runs on both.

The schema lives here, in one place. get_connection() makes sure every
table exists (once per database), so no code path depends on a table
having been created by some other page first.
"""

import os
import re
import sqlite3
from pathlib import Path

# Every script that touches the database imports this module, so reading
# backend/.env here means app.py, demo_catalog.py, sync.py and the rest
# all use the same database. Variables already set in the environment win.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

DB_PATH = Path(__file__).parent / "inventory.db"
PG_SCHEMA = None      # tests point PostgreSQL connections at a throwaway schema

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS products (
        sku TEXT PRIMARY KEY,
        title TEXT,
        brand TEXT,
        description_html TEXT,
        images_json TEXT,
        price REAL,
        inventory_qty INTEGER,
        source_url TEXT)""",
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT UNIQUE,
        customer_email TEXT,
        items_json TEXT,
        total REAL,
        status TEXT DEFAULT 'pending',
        card_last4 TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        paid_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS inventory_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        movement_type TEXT NOT NULL CHECK (movement_type IN ('purchase', 'sale', 'adjustment')),
        channel TEXT,
        quantity INTEGER NOT NULL,
        unit_cost REAL,
        unit_price REAL,
        fee REAL NOT NULL DEFAULT 0,
        reference TEXT,
        note TEXT,
        occurred_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_movements_sku_time ON inventory_movements (sku, occurred_at)",
    "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        channel TEXT NOT NULL,
        title TEXT,
        quantity_available INTEGER,
        price REAL,
        units_sold_last_30d INTEGER,
        synced_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_listings_sku ON listings (sku)",
    """CREATE TABLE IF NOT EXISTS sync_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT DEFAULT CURRENT_TIMESTAMP,
        amazon_records INTEGER,
        ebay_records INTEGER,
        discrepancies_found INTEGER,
        discrepancies_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS draft_listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        source_asin TEXT,
        source_url TEXT,
        ebay_title TEXT,
        ebay_description_html TEXT,
        images_json TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS product_references (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        reference_site TEXT,
        reference_url TEXT,
        reference_price REAL,
        facts_json TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ebay_account (
        environment TEXT PRIMARY KEY,
        username TEXT,
        scopes TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        refresh_expires_at REAL NOT NULL,
        access_token TEXT,
        access_expires_at REAL,
        connected_at TEXT NOT NULL,
        last_listing_sync TEXT,
        last_order_import TEXT)""",
    """CREATE TABLE IF NOT EXISTS ebay_pending_consent (
        state TEXT PRIMARY KEY,
        created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ebay_imported_lines (
        line_item_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        sku TEXT NOT NULL,
        imported_at TEXT NOT NULL)""",
    # AI customer support (support_agent.py)
    """CREATE TABLE IF NOT EXISTS support_knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        is_sample INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS support_conversations (
        id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        last_message_at TEXT NOT NULL,
        customer_email TEXT,
        needs_human INTEGER NOT NULL DEFAULT 0,
        handoff_summary TEXT,
        resolved_at TEXT,
        notified_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('customer', 'assistant')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        rating INTEGER,
        correction TEXT,
        staff_name TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_support_messages_conversation ON support_messages (conversation_id, id)",
]
# In dependency-free order: the migration script copies tables in this order.
TABLES = ["products", "orders", "inventory_movements", "app_meta", "listings", "sync_log",
          "draft_listings", "product_references", "ebay_account", "ebay_pending_consent",
          "ebay_imported_lines", "support_knowledge", "support_conversations", "support_messages"]

# Columns added after their table first shipped: databases created earlier
# get them the next time the app starts. (table, column, type)
ADDED_COLUMNS = [
    ("products", "source_url", "TEXT"),
    ("support_messages", "staff_name", "TEXT"),          # set on replies written by a person
    ("support_conversations", "resolved_at", "TEXT"),
    ("support_conversations", "notified_at", "TEXT"),
]

_schema_ready = set()


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------

def database_url():
    return os.environ.get("DATABASE_URL", "").strip()


def is_postgres():
    return database_url().startswith(("postgres://", "postgresql://"))


def identity():
    """Which database this is, for caches keyed by database."""
    if is_postgres():
        return f"{database_url()}#{PG_SCHEMA or ''}"
    return str(DB_PATH)


def describe():
    """A readable name for the database, without any password."""
    if is_postgres():
        return "PostgreSQL at " + re.sub(r"//[^@/]*@", "//", database_url())
    return f"SQLite file {DB_PATH.name}"


def get_connection():
    ensure_schema()
    return _connect()


def _connect():
    if is_postgres():
        return _pg_connect()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    """Creates every table that doesn't exist yet, once per database."""
    key = identity()
    if key in _schema_ready:
        return
    conn = _connect()
    try:
        for statement in SCHEMA:
            conn.execute(statement)
        for table, column, kind in ADDED_COLUMNS:
            if is_postgres():
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
            elif column not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.commit()
    finally:
        conn.close()
    _schema_ready.add(key)


def forget_schema():
    """Call after deleting or replacing the database (demo_catalog does)."""
    _schema_ready.discard(identity())


def insert_returning_id(conn, sql, params):
    """Runs an INSERT and returns the new row's id on either database."""
    if is_postgres():
        return conn.execute(f"{sql} RETURNING id", params).fetchone()[0]
    return conn.execute(sql, params).lastrowid


def upsert_meta(conn, key, value):
    conn.execute("INSERT INTO app_meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


# Kept for the modules that call them before touching their tables; the
# schema is now created in one place, so these just make sure it exists.
def init_db():
    ensure_schema()


def ensure_draft_table():
    ensure_schema()


def ensure_storefront_tables():
    ensure_schema()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

def translate_sql(sql, has_params):
    """? placeholders -> %s outside quoted text; % -> %% when parameters
    are passed (psycopg reads % as the start of a placeholder)."""
    out, quoted = [], False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
            out.append(ch)
        elif ch == "?" and not quoted:
            out.append("%s")
        elif ch == "%" and has_params:
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


def translate_ddl(sql):
    """SQLite column types -> their PostgreSQL equivalents (CREATE TABLE only)."""
    if not re.match(r"\s*CREATE\s+TABLE", sql, re.IGNORECASE):
        return sql
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return sql.replace("DEFAULT CURRENT_TIMESTAMP",
                       "DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')")


def number_from_text(text):
    """How NUMERIC values are read back: whole numbers as int, others as float."""
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return float(text)


def row_class(names):
    """A tuple that can also be read by column name, like sqlite3.Row."""
    index = {name: i for i, name in enumerate(names)}

    class Row(tuple):
        __slots__ = ()

        def __getitem__(self, key):
            if isinstance(key, str):
                return tuple.__getitem__(self, index[key])
            return tuple.__getitem__(self, key)

        def keys(self):
            return list(names)

    return Row


def _pg_connect():
    try:
        import psycopg
        from psycopg.adapt import Loader
    except ImportError:
        raise RuntimeError("DATABASE_URL points to PostgreSQL, but the PostgreSQL driver isn't installed. "
                           "Run: pip install -r requirements.txt")

    class NumberLoader(Loader):
        def load(self, data):
            return number_from_text(bytes(data).decode())

    def rows(cursor):
        if cursor.description is None:
            return tuple
        return row_class(tuple(c.name for c in cursor.description))

    extra = {"options": f"-c search_path={PG_SCHEMA}"} if PG_SCHEMA else {}
    raw = psycopg.connect(database_url(), row_factory=rows, **extra)
    raw.adapters.register_loader("numeric", NumberLoader)
    return PgConnection(raw)


class PgConnection:
    """The small part of sqlite3.Connection's interface the app uses."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cursor = self._raw.cursor()
        params = tuple(params) if params else None
        cursor.execute(translate_sql(translate_ddl(sql), params is not None), params)
        return cursor

    def executemany(self, sql, seq_of_params):
        cursor = self._raw.cursor()
        batch = [tuple(p) for p in seq_of_params]
        if batch:
            cursor.executemany(translate_sql(sql, True), batch)
        return cursor

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()
