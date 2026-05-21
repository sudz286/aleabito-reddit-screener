"""
Dashboard — FastAPI web UI for X Signal Monitor

Usage:
    uvicorn dashboard:app --reload --port 8000
"""

import logging
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
from db import get_db, dict_cursor, db_now

load_dotenv()

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # suppress yfinance noise

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)


# ── Schema ────────────────────────────────────────────────────────────────────

PRICE_FETCH_TIMEOUT = 8
MAX_TICKER_FAILURES = 5


def ensure_schema():
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_prices (
                    resolved_symbol TEXT PRIMARY KEY,
                    current_price   REAL,
                    currency        TEXT,
                    last_updated    TEXT,
                    fail_count      INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                ALTER TABLE ticker_prices
                ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0
            """)


# ── Price refresh ─────────────────────────────────────────────────────────────

def _fetch_price(symbol: str):
    """Isolated yfinance call — run inside a thread with a timeout."""
    tk = yf.Ticker(symbol)
    return getattr(tk.fast_info, "last_price", None)


def _do_refresh_prices():
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT DISTINCT tm.resolved_symbol, tm.currency,
                       COALESCE(tp.fail_count, 0) AS fail_count
                FROM ticker_mentions tm
                LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
                WHERE tm.resolved_symbol IS NOT NULL
                  AND tm.resolved_symbol != ''
                  AND tm.name != '(unresolved)'
                  AND COALESCE(tp.fail_count, 0) < %s
            """, (MAX_TICKER_FAILURES,))
            rows = cur.fetchall()

    skipped = len(rows)
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
                with dict_cursor(conn) as cur:
                    cur.execute("""
                        INSERT INTO ticker_prices (resolved_symbol, current_price, currency, last_updated, fail_count)
                        VALUES (%s, %s, %s, %s, 0)
                        ON CONFLICT (resolved_symbol) DO UPDATE SET
                            current_price = EXCLUDED.current_price,
                            last_updated  = EXCLUDED.last_updated,
                            fail_count    = 0
                    """, (symbol, round(price, 4), row["currency"] or "", db_now()))
            updated += 1
        else:
            with get_db() as conn:
                with dict_cursor(conn) as cur:
                    cur.execute("""
                        INSERT INTO ticker_prices (resolved_symbol, currency, fail_count, last_updated)
                        VALUES (%s, %s, 1, %s)
                        ON CONFLICT (resolved_symbol) DO UPDATE SET
                            fail_count   = ticker_prices.fail_count + 1,
                            last_updated = EXCLUDED.last_updated
                    """, (symbol, row["currency"] or "", db_now()))
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


@app.get("/tickers/{symbol}", response_class=HTMLResponse)
async def ticker_page(request: Request, symbol: str):
    return templates.TemplateResponse(
        request=request, name="ticker.html",
        context={"symbol": symbol.upper()},
    )


# ── API: signals list ─────────────────────────────────────────────────────────

@app.get("/api/signals")
async def list_signals(page: int = 1, per_page: int = 30):
    offset = (page - 1) * per_page
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT s.id, s.tweet_id, s.thesis_summary, s.market_context,
                       s.sentiment, s.confidence,
                       t.text AS tweet_text, t.url AS tweet_url,
                       t.published_at, t.is_reply
                FROM signals s
                JOIN tweets t ON s.tweet_id = t.id
                WHERE s.is_finance = 1
                ORDER BY t.published_at DESC
                LIMIT %s OFFSET %s
            """, (per_page, offset))
            rows = cur.fetchall()

            cur.execute("SELECT COUNT(*) FROM signals WHERE is_finance = 1")
            total = cur.fetchone()["count"]

            signal_ids = [r["id"] for r in rows]
            ticker_rows = []
            first_mention_map: dict[str, int] = {}
            if signal_ids:
                cur.execute("""
                    SELECT signal_id, resolved_symbol, name
                    FROM ticker_mentions
                    WHERE signal_id = ANY(%s)
                """, (signal_ids,))
                ticker_rows = cur.fetchall()

                cur.execute("""
                    SELECT resolved_symbol, MIN(signal_id) AS first_signal_id
                    FROM ticker_mentions
                    WHERE resolved_symbol IS NOT NULL AND resolved_symbol != ''
                    GROUP BY resolved_symbol
                """)
                first_mention_map = {r["resolved_symbol"]: r["first_signal_id"] for r in cur.fetchall()}

    tickers_by_signal: dict[int, list] = {}
    for tr in ticker_rows:
        tickers_by_signal.setdefault(tr["signal_id"], []).append({
            "symbol": tr["resolved_symbol"],
            "name": tr["name"],
            "is_new": tr["signal_id"] == first_mention_map.get(tr["resolved_symbol"]),
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
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT s.id, s.tweet_id, s.thesis_summary, s.market_context,
                       s.sentiment, s.confidence,
                       t.text AS tweet_text, t.url AS tweet_url,
                       t.published_at, t.is_reply, t.parent_text
                FROM signals s
                JOIN tweets t ON s.tweet_id = t.id
                WHERE s.tweet_id = %s
            """, (tweet_id,))
            sig = cur.fetchone()

            if not sig:
                raise HTTPException(404, "Signal not found")

            cur.execute("""
                SELECT tm.id, tm.symbol, tm.resolved_symbol, tm.name,
                       tm.exchange, tm.currency, tm.price_at_mention, tm.change_pct,
                       tp.current_price, tp.last_updated AS price_updated_at
                FROM ticker_mentions tm
                LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
                WHERE tm.signal_id = %s
            """, (sig["id"],))
            tickers = cur.fetchall()

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
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT tm.resolved_symbol,
                       MAX(tm.name) AS name,
                       MAX(tm.currency) AS currency,
                       MIN(t.published_at) AS first_posted,
                       MAX(t.published_at) AS last_posted,
                       COUNT(DISTINCT s.tweet_id) AS mention_count,
                       MAX(tp.current_price) AS current_price,
                       MAX(tp.last_updated) AS last_updated
                FROM ticker_mentions tm
                JOIN signals s ON tm.signal_id = s.id
                JOIN tweets t ON s.tweet_id = t.id
                LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
                WHERE tm.resolved_symbol IS NOT NULL
                  AND tm.resolved_symbol != ''
                  AND tm.name != '(unresolved)'
                  AND s.is_finance = 1
                GROUP BY tm.resolved_symbol
            """)
            groups = cur.fetchall()

            cur.execute("""
                SELECT tm.resolved_symbol, tm.price_at_mention, t.published_at
                FROM ticker_mentions tm
                JOIN signals s ON tm.signal_id = s.id
                JOIN tweets t ON s.tweet_id = t.id
                WHERE tm.price_at_mention IS NOT NULL
                  AND tm.name != '(unresolved)'
                  AND s.is_finance = 1
                ORDER BY t.published_at ASC
            """)
            all_prices = cur.fetchall()

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
        with dict_cursor(conn) as cur:
            cur.execute("SELECT symbol FROM ticker_mentions WHERE id = %s", (ticker_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Ticker mention not found")

            raw_symbol = row["symbol"]

            cur.execute("""
                UPDATE ticker_mentions
                SET resolved_symbol=%s, name=%s, exchange=%s, currency=%s,
                    price_at_mention=COALESCE(price_at_mention, %s),
                    price_fetched_at=%s
                WHERE symbol=%s AND name='(unresolved)'
            """, (symbol, name, exchange, currency, round(price, 4), db_now(), raw_symbol))
            updated = cur.rowcount

            cur.execute("""
                UPDATE ticker_mentions
                SET resolved_symbol=%s, name=%s, exchange=%s, currency=%s,
                    price_fetched_at=%s
                WHERE id=%s
            """, (symbol, name, exchange, currency, db_now(), ticker_id))

            cur.execute("""
                INSERT INTO ticker_prices (resolved_symbol, current_price, currency, last_updated)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (resolved_symbol) DO UPDATE SET
                    current_price = EXCLUDED.current_price,
                    last_updated  = EXCLUDED.last_updated
            """, (symbol, round(price, 4), currency, db_now()))

            cur.execute("""
                DELETE FROM ticker_mentions
                WHERE resolved_symbol = %s
                  AND id NOT IN (
                      SELECT MIN(id) FROM ticker_mentions
                      WHERE resolved_symbol = %s
                      GROUP BY signal_id
                  )
            """, (symbol, symbol))

    log.info(f"Resolved {raw_symbol!r} → {symbol} across {updated} mention(s)")
    return {"resolved_symbol": symbol, "name": name, "price": round(price, 4), "currency": currency, "mentions_updated": updated}


# ── API: ticker signal history ────────────────────────────────────────────────

@app.get("/api/tickers/{symbol}/signals")
async def ticker_signal_history(symbol: str):
    sym = symbol.upper()
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT s.id, s.tweet_id, s.thesis_summary, s.sentiment, s.confidence,
                       t.url AS tweet_url, t.published_at,
                       tm.price_at_mention, tm.currency, tm.change_pct,
                       tp.current_price
                FROM ticker_mentions tm
                JOIN signals s ON tm.signal_id = s.id
                JOIN tweets t ON s.tweet_id = t.id
                LEFT JOIN ticker_prices tp ON tm.resolved_symbol = tp.resolved_symbol
                WHERE tm.resolved_symbol = %s
                  AND s.is_finance = 1
                ORDER BY t.published_at DESC
            """, (sym,))
            rows = cur.fetchall()

            cur.execute("""
                SELECT name, exchange, currency FROM ticker_mentions
                WHERE resolved_symbol = %s AND name != '(unresolved)'
                LIMIT 1
            """, (sym,))
            info = cur.fetchone()

    current_price = rows[0]["current_price"] if rows else None
    currency = (info["currency"] if info else "") or ""

    return {
        "symbol": sym,
        "name": info["name"] if info else sym,
        "exchange": info["exchange"] if info else "",
        "currency": currency,
        "current_price": current_price,
        "signals": [
            {
                "tweet_id": r["tweet_id"],
                "tweet_url": r["tweet_url"],
                "thesis_summary": r["thesis_summary"],
                "sentiment": r["sentiment"],
                "confidence": r["confidence"],
                "published_at": r["published_at"],
                "price_at_mention": r["price_at_mention"],
                "currency": r["currency"] or currency,
                "change_pct": r["change_pct"],
                "current_price": r["current_price"],
            }
            for r in rows
        ],
    }


# ── API: manual price refresh ─────────────────────────────────────────────────

@app.post("/api/prices/refresh")
async def manual_refresh():
    await refresh_prices()
    return {"status": "ok"}
