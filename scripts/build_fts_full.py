"""One-off: build documents_fts (FTS5 external-content) on the full corpus db.

Batched by document_id ranges so we get progress logging and small
transactions (the embedding run reads this db concurrently in WAL mode).

NOTE: documents_fts is an EXTERNAL CONTENT table, so COUNT(*)/MAX(rowid)
against it scan the content table (documents), NOT the FTS index. Actual
progress is read from the FTS5 ``documents_fts_docsize`` shadow table. The
sidecar `_fts_build_meta` remains as human-readable operational metadata.

Usage:
    uv run python scripts/build_fts_full.py \
        --corpus-db dof_db/dof_corpus_l3.sqlite [--batch 50000] [--reset]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from corpus_store.db import connect, fetch_document_text  # noqa: E402

FTS_DDL = """CREATE VIRTUAL TABLE documents_fts USING fts5(
    markdown, content='documents', content_rowid='document_id',
    tokenize='unicode61 remove_diacritics 1'
)"""

META_DDL = ("CREATE TABLE IF NOT EXISTS _fts_build_meta"
            "(k TEXT PRIMARY KEY, v INTEGER)")


def set_meta(conn, key: str, value: int) -> None:
    conn.execute(
        "INSERT INTO _fts_build_meta(k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus-db", required=True)
    ap.add_argument("--batch", type=int, default=50_000)
    ap.add_argument("--reset", action="store_true",
                    help="drop documents_fts and start over")
    args = ap.parse_args()

    t0 = time.time()
    conn = connect(args.corpus_db)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-2000000")  # ~2 GiB page cache
    conn.execute("PRAGMA temp_store=MEMORY")

    if args.reset:
        conn.execute("DROP TABLE IF EXISTS documents_fts")
        conn.execute("DROP TABLE IF EXISTS _fts_build_meta")
        conn.commit()
        print("dropped existing documents_fts", flush=True)

    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='documents_fts'"
    ).fetchone()
    if not has_fts:
        conn.execute(FTS_DDL)
        conn.commit()
        print(f"created documents_fts ({time.time() - t0:.0f}s)", flush=True)
    conn.execute(META_DDL)
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    max_id = conn.execute("SELECT MAX(document_id) FROM documents").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM documents_fts_docsize").fetchone()[0]
    first_missing = conn.execute(
        "SELECT MIN(d.document_id) FROM documents AS d"
        " LEFT JOIN documents_fts_docsize AS f ON f.id = d.document_id"
        " WHERE f.id IS NULL"
    ).fetchone()[0]
    lo = first_missing - 1 if first_missing is not None else max_id
    set_meta(conn, "indexed_through", lo)
    set_meta(conn, "indexed_count", done)
    conn.commit()
    print(f"documents: {total:,}  max_id: {max_id:,}  "
          f"already indexed: {done:,} (through id {lo:,})", flush=True)

    while lo < max_id:
        hi = min(lo + args.batch, max_id)
        t = time.time()
        with conn:
            cur = conn.execute(
                "INSERT INTO documents_fts(rowid, markdown) "
                "SELECT document_id, markdown FROM documents "
                "LEFT JOIN documents_fts_docsize AS f ON f.id = document_id "
                "WHERE document_id > ? AND document_id <= ? "
                "AND f.id IS NULL AND markdown != ''",
                (lo, hi),
            )
            done += cur.rowcount
            set_meta(conn, "indexed_through", hi)
            set_meta(conn, "indexed_count", done)
        el = time.time() - t0
        rate = done / el if el else 0
        eta = (total - done) / rate / 3600 if rate else 0
        print(f"  {done:,}/{total:,} docs ({rate:.0f} docs/s, ETA {eta:.1f}h, "
              f"batch {time.time() - t:.1f}s)", flush=True)
        lo = hi

    # Oversized docs are stored as segments (markdown = ''), so reassemble any
    # missing from the actual FTS shadow table, including documents appended
    # after the original full build.
    seg_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT s.document_id FROM document_segments AS s"
        " LEFT JOIN documents_fts_docsize AS f ON f.id = s.document_id"
        " WHERE f.id IS NULL")]
    if seg_ids:
        with conn:
            for doc_id in seg_ids:
                conn.execute(
                    "INSERT INTO documents_fts(rowid, markdown) VALUES (?, ?)",
                    (doc_id, fetch_document_text(conn, doc_id)),
                )
            done += len(seg_ids)
            set_meta(conn, "indexed_count", done)
            set_meta(conn, "segmented_done", 1)
        print(f"indexed {len(seg_ids)} segmented docs", flush=True)

    done = conn.execute("SELECT COUNT(*) FROM documents_fts_docsize").fetchone()[0]
    assert done == total, f"fts count {done} != documents {total}"
    # real index sanity: a MATCH query (COUNT(*) scans the content table!)
    n = conn.execute(
        "SELECT COUNT(*) FROM documents_fts"
        " WHERE documents_fts MATCH 'decreto'").fetchone()[0]
    if total > 10_000:
        assert n > 10_000, f"suspiciously few MATCH results: {n}"
    else:
        # Small test/incremental corpora are guarded by the exact count
        # equality above; 'decreto' frequency is unrepresentative there.
        print(f"small corpus ({total:,} docs): {n:,} 'decreto' matches "
              "(MATCH threshold only applies above 10,000 docs)", flush=True)
    set_meta(conn, "complete", 1)
    conn.commit()
    print(f"FTS5 build complete: {done:,} docs in "
          f"{(time.time() - t0) / 3600:.2f}h ('decreto' matches: {n:,})",
          flush=True)
    conn.close()


if __name__ == "__main__":
    main()
