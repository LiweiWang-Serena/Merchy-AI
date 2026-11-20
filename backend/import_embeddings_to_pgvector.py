"""Load the cached KB embeddings into pgvector, reusing retrieval.py's own parsing."""
import os, sys, json
import psycopg
import retrieval

def main():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set")

    index = retrieval._load()
    if not index or index.get("vecs") is None:
        sys.exit("no cached embeddings found; run the eval notebook first")

    rows, vecs = index["rows"], index["vecs"]
    print(f"{len(rows)} rows, {vecs.shape[1]} dims")

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE kb_embeddings")
        for row, vec in zip(rows, vecs):
            cur.execute(
                """INSERT INTO kb_embeddings
                   (doc_id, text, sku, title, link, makes, years, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (doc_id) DO UPDATE SET embedding = EXCLUDED.embedding""",
                (row["text"][:200], row["text"], row.get("sku"), row.get("title"),
                 row.get("link"), sorted(row.get("makes") or []),
                 sorted(row.get("years") or []), list(map(float, vec))),
            )
        conn.commit()
        cur.execute("SELECT count(*) FROM kb_embeddings")
        print("loaded:", cur.fetchone()[0])

if __name__ == "__main__":
    main()
