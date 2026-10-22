"""
Copies everything from the SQLite database (backend/inventory.db) into
the PostgreSQL database in DATABASE_URL.

    python migrate_to_postgres.py                   # from backend/inventory.db
    python migrate_to_postgres.py --from other.db   # from another SQLite file
    python migrate_to_postgres.py --replace         # empty PostgreSQL first

It creates the tables, copies every row with its id, moves each id
counter past the highest copied id, and checks that every table ends up
with the same number of rows. It won't copy into a PostgreSQL database
that already has data unless you pass --replace. The SQLite file isn't
changed, so it stays as a backup.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

BATCH = 5000


def main():
    parser = argparse.ArgumentParser(description="Copy the SQLite database into PostgreSQL.")
    parser.add_argument("--from", dest="source", default=str(db.DB_PATH), help="SQLite file to copy")
    parser.add_argument("--replace", action="store_true", help="delete what's in PostgreSQL first")
    args = parser.parse_args()

    if not db.is_postgres():
        sys.exit("Set DATABASE_URL in backend/.env to your PostgreSQL database first, for example:\n"
                 "DATABASE_URL=postgresql://localhost/channel_ledger")
    source_path = Path(args.source)
    if not source_path.exists():
        sys.exit(f"{source_path} doesn't exist, so there's nothing to copy.")

    source = sqlite3.connect(source_path)
    target = db.get_connection()          # creates the tables in PostgreSQL
    try:
        existing = {t: target.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES}
        if any(existing.values()) and not args.replace:
            filled = ", ".join(f"{t} ({n:,})" for t, n in existing.items() if n)
            sys.exit(f"PostgreSQL already has data in: {filled}.\n"
                     "Run again with --replace to delete it and copy from SQLite.")
        if args.replace:
            for table in reversed(db.TABLES):
                target.execute(f"DELETE FROM {table}")

        in_source = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        results = []
        for table in db.TABLES:
            if table not in in_source:
                results.append((table, 0, 0))
                continue
            wanted = {r[0] for r in target.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ?", (table,))}
            columns = [r[1] for r in source.execute(f"PRAGMA table_info({table})") if r[1] in wanted]
            insert = (f"INSERT INTO {table} ({', '.join(columns)}) "
                      f"VALUES ({', '.join('?' for _ in columns)})")
            cursor = source.execute(f"SELECT {', '.join(columns)} FROM {table}")
            copied = 0
            while True:
                batch = cursor.fetchmany(BATCH)
                if not batch:
                    break
                target.executemany(insert, batch)
                copied += len(batch)
            if "id" in columns:
                target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                               f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
            results.append((table, copied, target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
        mismatched = [r for r in results if r[1] != r[2]]
        if mismatched:
            target.rollback()
            sys.exit(f"Row counts didn't match, so nothing was saved: {mismatched}")
        target.commit()
    finally:
        target.close()
        source.close()

    width = max(len(t) for t, _, _ in results)
    for table, copied, _ in results:
        print(f"  {table:<{width}}  {copied:>9,} rows")
    print(f"Copied {sum(r[1] for r in results):,} rows from {source_path.name} into {db.describe()}.")
    print("The SQLite file is unchanged and stays as a backup.")


if __name__ == "__main__":
    main()
