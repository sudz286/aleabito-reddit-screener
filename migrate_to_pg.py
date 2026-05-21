"""
One-shot migration: copies all data from signals.db (SQLite) → PostgreSQL.

Usage:
    python migrate_to_pg.py

Idempotent: uses ON CONFLICT DO NOTHING / DO UPDATE, safe to re-run.
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = Path(__file__).parent / "state" / "signals.db"

import psycopg2
import psycopg2.extras


def pg_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def migrate():
    if not SQLITE_PATH.exists():
        print(f"SQLite DB not found at {SQLITE_PATH}. Nothing to migrate.")
        return

    src = sqlite3.connect(str(SQLITE_PATH))
    src.row_factory = sqlite3.Row
    dst = pg_conn()
    dst.autocommit = False
    cur = dst.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Ensure schema exists ──────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tweets (
            id           TEXT PRIMARY KEY,
            username     TEXT NOT NULL,
            text         TEXT NOT NULL,
            url          TEXT,
            published_at TEXT,
            fetched_at   TEXT,
            source       TEXT,
            is_reply     INTEGER DEFAULT 0,
            parent_text  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id             SERIAL PRIMARY KEY,
            tweet_id       TEXT UNIQUE REFERENCES tweets(id),
            is_finance     INTEGER DEFAULT 0,
            thesis_summary TEXT,
            market_context TEXT,
            sentiment      TEXT,
            confidence     TEXT,
            processed_at   TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticker_mentions (
            id               SERIAL PRIMARY KEY,
            signal_id        INTEGER REFERENCES signals(id),
            symbol           TEXT NOT NULL,
            resolved_symbol  TEXT,
            name             TEXT,
            exchange         TEXT,
            currency         TEXT,
            price_at_mention REAL,
            change_pct       REAL,
            price_fetched_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticker_prices (
            resolved_symbol TEXT PRIMARY KEY,
            current_price   REAL,
            currency        TEXT,
            last_updated    TEXT,
            fail_count      INTEGER DEFAULT 0
        )
    """)
    dst.commit()

    # ── Migrate tweets ────────────────────────────────────────────────────────
    rows = src.execute("SELECT * FROM tweets").fetchall()
    print(f"Migrating {len(rows)} tweets…")
    for r in rows:
        cur.execute("""
            INSERT INTO tweets
                (id, username, text, url, published_at, fetched_at, source, is_reply, parent_text)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, (r["id"], r["username"], r["text"], r["url"],
              r["published_at"], r["fetched_at"], r["source"],
              r["is_reply"], r["parent_text"]))
    dst.commit()
    print(f"  done.")

    # ── Migrate signals ───────────────────────────────────────────────────────
    # We need to preserve signal_ids so ticker_mentions FK stays valid.
    # Strategy: insert with explicit id, then reset the SERIAL sequence.
    rows = src.execute("SELECT * FROM signals").fetchall()
    print(f"Migrating {len(rows)} signals…")
    for r in rows:
        cur.execute("""
            INSERT INTO signals
                (id, tweet_id, is_finance, thesis_summary, market_context,
                 sentiment, confidence, processed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tweet_id) DO UPDATE SET
                is_finance     = EXCLUDED.is_finance,
                thesis_summary = EXCLUDED.thesis_summary,
                market_context = EXCLUDED.market_context,
                sentiment      = EXCLUDED.sentiment,
                confidence     = EXCLUDED.confidence
        """, (r["id"], r["tweet_id"], r["is_finance"], r["thesis_summary"],
              r["market_context"], r["sentiment"], r["confidence"], r["processed_at"]))
    dst.commit()
    # Reset sequence so next INSERT gets a fresh id beyond the migrated max
    cur.execute("SELECT SETVAL('signals_id_seq', (SELECT MAX(id) FROM signals))")
    dst.commit()
    print(f"  done.")

    # ── Migrate ticker_mentions ───────────────────────────────────────────────
    rows = src.execute("SELECT * FROM ticker_mentions").fetchall()
    print(f"Migrating {len(rows)} ticker_mentions…")
    for r in rows:
        cur.execute("""
            INSERT INTO ticker_mentions
                (id, signal_id, symbol, resolved_symbol, name, exchange,
                 currency, price_at_mention, change_pct, price_fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """, (r["id"], r["signal_id"], r["symbol"], r["resolved_symbol"],
              r["name"], r["exchange"], r["currency"],
              r["price_at_mention"], r["change_pct"], r["price_fetched_at"]))
    dst.commit()
    cur.execute("SELECT SETVAL('ticker_mentions_id_seq', (SELECT MAX(id) FROM ticker_mentions))")
    dst.commit()
    print(f"  done.")

    # ── Migrate ticker_prices ─────────────────────────────────────────────────
    rows = src.execute("SELECT * FROM ticker_prices").fetchall()
    print(f"Migrating {len(rows)} ticker_prices…")
    for r in rows:
        fail_count = r["fail_count"] if "fail_count" in r.keys() else 0
        cur.execute("""
            INSERT INTO ticker_prices
                (resolved_symbol, current_price, currency, last_updated, fail_count)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (resolved_symbol) DO UPDATE SET
                current_price = EXCLUDED.current_price,
                last_updated  = EXCLUDED.last_updated,
                fail_count    = EXCLUDED.fail_count
        """, (r["resolved_symbol"], r["current_price"], r["currency"],
              r["last_updated"], fail_count))
    dst.commit()
    print(f"  done.")

    src.close()
    cur.close()
    dst.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    migrate()
