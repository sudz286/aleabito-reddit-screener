"""
Dashboard — FastAPI web UI for X Signal Monitor

Usage:
    uvicorn dashboard:app --reload --port 8000
"""

import logging
import sqlite3
import concurrent.futures
from contextlib import asynccontextmanager
from pathlib import Path

import requests
import yfinance as yf
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # suppress yfinance noise

DB_FILE = Path(__file__).parent / "state" / "signals.db"
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


PRICE_FETCH_TIMEOUT = 8    # seconds per ticker before we give up
MAX_TICKER_FAILURES = 5   # stop retrying after this many consecutive failures


def ensure_schema():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticker_prices (
                resolved_symbol TEXT PRIMARY KEY,
                current_price   REAL,
                currency        TEXT,
                last_updated    TEXT DEFAULT (datetime('now')),
                fail_count      INTEGER DEFAULT 0
            )
        """)
        # safe migration: add fail_count if upgrading from older schema
        try:
            conn.execute("ALTER TABLE ticker_prices ADD COLUMN fail_count INTEGER DEFAULT 0")
        except Exception:
            pass


# ── Price refresh ─────────────────────────────────────────────────────────────

def _fetch_price(symbol: str):
    """Isolated yfinance call — run inside a thread with a timeout."""
    tk = yf.Ticker(symbol)
    return getattr(tk.fast_info, "last_price", None)


def _do_refresh_prices():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT tm.resolved_symbol, tm.currency,
                   COALESCE(tp.fail_count, 0) AS fail_count
            FROM ticker_mentions tm
            LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
            WHERE tm.resolved_symbol IS NOT NULL
              AND tm.resolved_symbol != ''
              AND tm.name != '(unresolved)'
              AND COALESCE(tp.fail_count, 0) < ?
        """, (MAX_TICKER_FAILURES,)).fetchall()

    skipped = len(rows)  # will subtract as we process
    updated = 0
    failed  = 0

    for row in rows:
        symbol = row["resolved_symbol"]
        price  = None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_fetch_price, symbol)
                price  = future.result(timeout=PRICE_FETCH_TIMEOUT)
        except concurrent.futures.TimeoutError:
            log.warning(f"Price fetch timed out for {symbol} (>{PRICE_FETCH_TIMEOUT}s)")
        except Exception as e:
            log.warning(f"Price fetch error for {symbol}: {e}")

        if price and price > 0:
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO ticker_prices (resolved_symbol, current_price, currency, last_updated, fail_count)
                    VALUES (?, ?, ?, datetime('now'), 0)
                    ON CONFLICT(resolved_symbol) DO UPDATE SET
                        current_price = excluded.current_price,
                        last_updated  = excluded.last_updated,
                        fail_count    = 0
                """, (symbol, round(price, 4), row["currency"] or ""))
            updated += 1
        else:
            # Increment failure counter; at MAX_TICKER_FAILURES this ticker is silently skipped
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO ticker_prices (resolved_symbol, currency, fail_count, last_updated)
                    VALUES (?, ?, 1, datetime('now'))
                    ON CONFLICT(resolved_symbol) DO UPDATE SET
                        fail_count   = fail_count + 1,
                        last_updated = excluded.last_updated
                """, (symbol, row["currency"] or ""))
            failed += 1
            if row["fail_count"] + 1 >= MAX_TICKER_FAILURES:
                log.warning(f"Ticker {symbol} failed {MAX_TICKER_FAILURES} times — skipping permanently")

    log.info(f"Price refresh: {updated} updated, {failed} failed, {skipped - updated - failed} skipped (exhausted)")


async def refresh_prices():
    import asyncio
    await asyncio.to_thread(_do_refresh_prices)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    ensure_schema()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(refresh_prices, "interval", minutes=30, id="price_refresh")
    scheduler.start()
    # Fire-and-forget initial refresh; keep reference so we can cancel on shutdown
    refresh_task = asyncio.create_task(refresh_prices())
    yield
    refresh_task.cancel()
    scheduler.shutdown(wait=False)


app = FastAPI(title="Signal Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── API: signals list ─────────────────────────────────────────────────────────

@app.get("/api/signals")
async def list_signals(page: int = 1, per_page: int = 30):
    offset = (page - 1) * per_page
    with get_db() as conn:
        rows = conn.execute("""
            SELECT s.id, s.tweet_id, s.thesis_summary, s.market_context,
                   s.sentiment, s.confidence,
                   t.text AS tweet_text, t.url AS tweet_url,
                   t.published_at, t.is_reply
            FROM signals s
            JOIN tweets t ON s.tweet_id = t.id
            WHERE s.is_finance = 1
            ORDER BY t.published_at DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE is_finance = 1"
        ).fetchone()[0]

        signal_ids = [r["id"] for r in rows]
        ticker_rows = []
        if signal_ids:
            placeholders = ",".join("?" * len(signal_ids))
            ticker_rows = conn.execute(f"""
                SELECT signal_id, resolved_symbol, name
                FROM ticker_mentions
                WHERE signal_id IN ({placeholders})
            """, signal_ids).fetchall()

    tickers_by_signal: dict[int, list] = {}
    for tr in ticker_rows:
        tickers_by_signal.setdefault(tr["signal_id"], []).append({
            "symbol": tr["resolved_symbol"],
            "name": tr["name"],
        })

    return {
        "signals": [
            {
                "id": r["id"],
                "tweet_id": r["tweet_id"],
                "tweet_text": r["tweet_text"],
                "tweet_url": r["tweet_url"],
                "thesis_summary": r["thesis_summary"],
                "market_context": r["market_context"],
                "sentiment": r["sentiment"],
                "confidence": r["confidence"],
                "published_at": r["published_at"],
                "is_reply": bool(r["is_reply"]),
                "tickers": tickers_by_signal.get(r["id"], []),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# ── API: signal detail ────────────────────────────────────────────────────────

@app.get("/api/signals/{tweet_id}")
async def get_signal(tweet_id: str):
    with get_db() as conn:
        sig = conn.execute("""
            SELECT s.id, s.tweet_id, s.thesis_summary, s.market_context,
                   s.sentiment, s.confidence,
                   t.text AS tweet_text, t.url AS tweet_url,
                   t.published_at, t.is_reply, t.parent_text
            FROM signals s
            JOIN tweets t ON s.tweet_id = t.id
            WHERE s.tweet_id = ?
        """, (tweet_id,)).fetchone()

        if not sig:
            raise HTTPException(404, "Signal not found")

        tickers = conn.execute("""
            SELECT tm.id, tm.symbol, tm.resolved_symbol, tm.name,
                   tm.exchange, tm.currency, tm.price_at_mention, tm.change_pct,
                   tp.current_price, tp.last_updated AS price_updated_at
            FROM ticker_mentions tm
            LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
            WHERE tm.signal_id = ?
        """, (sig["id"],)).fetchall()

    result_tickers = []
    for t in tickers:
        mp = t["price_at_mention"]
        cp = t["current_price"]
        pct = round(((cp - mp) / mp) * 100, 2) if mp and cp and mp > 0 else None
        result_tickers.append({
            "id": t["id"],
            "symbol": t["symbol"],
            "resolved_symbol": t["resolved_symbol"],
            "name": t["name"],
            "exchange": t["exchange"],
            "currency": t["currency"],
            "price_at_mention": mp,
            "current_price": cp,
            "pct_change": pct,
            "price_updated_at": t["price_updated_at"],
            "is_unresolved": t["name"] == "(unresolved)",
        })

    return {
        "tweet_id": sig["tweet_id"],
        "tweet_text": sig["tweet_text"],
        "tweet_url": sig["tweet_url"],
        "published_at": sig["published_at"],
        "is_reply": bool(sig["is_reply"]),
        "parent_text": sig["parent_text"],
        "thesis_summary": sig["thesis_summary"],
        "market_context": sig["market_context"],
        "sentiment": sig["sentiment"],
        "confidence": sig["confidence"],
        "tickers": result_tickers,
    }


# ── API: leaderboard ──────────────────────────────────────────────────────────

@app.get("/api/leaderboard")
async def get_leaderboard():
    with get_db() as conn:
        groups = conn.execute("""
            SELECT tm.resolved_symbol, tm.name, tm.currency,
                   MIN(t.published_at) AS first_posted,
                   MAX(t.published_at) AS last_posted,
                   COUNT(DISTINCT s.tweet_id) AS mention_count,
                   tp.current_price, tp.last_updated
            FROM ticker_mentions tm
            JOIN signals s ON tm.signal_id = s.id
            JOIN tweets t ON s.tweet_id = t.id
            LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
            WHERE tm.resolved_symbol IS NOT NULL
              AND tm.resolved_symbol != ''
              AND tm.name != '(unresolved)'
              AND s.is_finance = 1
            GROUP BY tm.resolved_symbol
        """).fetchall()

        all_prices = conn.execute("""
            SELECT tm.resolved_symbol, tm.price_at_mention, t.published_at
            FROM ticker_mentions tm
            JOIN signals s ON tm.signal_id = s.id
            JOIN tweets t ON s.tweet_id = t.id
            WHERE tm.price_at_mention IS NOT NULL
              AND tm.name != '(unresolved)'
              AND s.is_finance = 1
            ORDER BY t.published_at ASC
        """).fetchall()

    first_price: dict[str, float] = {}
    last_price: dict[str, float] = {}
    for p in all_prices:
        sym = p["resolved_symbol"]
        if sym not in first_price:
            first_price[sym] = p["price_at_mention"]
        last_price[sym] = p["price_at_mention"]

    entries = []
    for r in groups:
        sym = r["resolved_symbol"]
        fp = first_price.get(sym)
        lp = last_price.get(sym)
        cp = r["current_price"]
        entries.append({
            "resolved_symbol": sym,
            "name": r["name"] or "",
            "currency": r["currency"] or "",
            "first_posted": r["first_posted"],
            "last_posted": r["last_posted"],
            "mention_count": r["mention_count"],
            "price_at_first": fp,
            "price_at_last": lp,
            "current_price": cp,
            "pct_from_first": round(((cp - fp) / fp) * 100, 2) if cp and fp and fp > 0 else None,
            "pct_from_last": round(((cp - lp) / lp) * 100, 2) if cp and lp and lp > 0 else None,
            "last_price_update": r["last_updated"],
        })

    entries.sort(
        key=lambda x: x["pct_from_first"] if x["pct_from_first"] is not None else float("-inf"),
        reverse=True,
    )
    return {"entries": entries}


# ── API: ticker search ────────────────────────────────────────────────────────

@app.get("/api/tickers/search")
async def search_tickers(q: str = Query(..., min_length=1)):
    try:
        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 8, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes", [])
        return {
            "results": [
                {
                    "symbol": item["symbol"],
                    "name": item.get("shortname") or item.get("longname", ""),
                    "exchange": item.get("exchange", ""),
                    "type": item.get("quoteType", ""),
                }
                for item in quotes
                if item.get("symbol") and item.get("quoteType") in ("EQUITY", "ETF")
            ]
        }
    except Exception as e:
        raise HTTPException(502, f"Search failed: {e}")


class ResolveBody(BaseModel):
    resolved_symbol: str


@app.post("/api/tickers/{ticker_id}/resolve")
async def resolve_ticker(ticker_id: int, body: ResolveBody):
    symbol = body.resolved_symbol.upper()
    try:
        tk = yf.Ticker(symbol)
        price = getattr(tk.fast_info, "last_price", None)
        if not price or price <= 0:
            raise HTTPException(422, f"No price data for {symbol}")
        info = tk.info
        name = info.get("shortName") or info.get("longName", symbol)
        exchange = info.get("exchange", "")
        currency = info.get("currency", "")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Validation failed: {e}")

    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM ticker_mentions WHERE id = ?", (ticker_id,)).fetchone():
            raise HTTPException(404, "Ticker mention not found")
        conn.execute("""
            UPDATE ticker_mentions
            SET resolved_symbol=?, name=?, exchange=?, currency=?,
                price_at_mention=?, price_fetched_at=datetime('now')
            WHERE id=?
        """, (symbol, name, exchange, currency, round(price, 4), ticker_id))
        conn.execute("""
            INSERT INTO ticker_prices (resolved_symbol, current_price, currency, last_updated)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(resolved_symbol) DO UPDATE SET
                current_price = excluded.current_price,
                last_updated  = excluded.last_updated
        """, (symbol, round(price, 4), currency))

    return {"resolved_symbol": symbol, "name": name, "price": round(price, 4), "currency": currency}


# ── API: manual price refresh ─────────────────────────────────────────────────

@app.post("/api/prices/refresh")
async def manual_refresh():
    await refresh_prices()
    return {"status": "ok"}
