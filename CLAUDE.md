# Signal Monitor — Claude Context

## What this is
A tweet screener dashboard for @aleabitoreddit. Fetches tweets via Apify, extracts stock tickers, prices them via yfinance, summarises the investment thesis via Claude (claude-sonnet-4-6), and surfaces them in a FastAPI/Jinja2 web dashboard.

## Stack
- **Backend**: Python 3.13, FastAPI, psycopg2 (PostgreSQL), yfinance, Anthropic SDK, Apify client
- **Frontend**: Vanilla JS, no framework. Playfair Display + Inter + IBM Plex Mono fonts.
- **DB**: PostgreSQL (migrated from SQLite). Local dev: `postgresql://sudharsanjanardhanan@localhost:5432/signals`
- **Scheduler**: APScheduler refreshes prices every 30 min inside the FastAPI lifespan

## Key files
| File | Purpose |
|---|---|
| `monitor.py` | Ingestion pipeline — fetch tweets → resolve tickers → Claude thesis → save to DB |
| `dashboard.py` | FastAPI web server — all API routes + price refresh scheduler |
| `db.py` | Shared psycopg2 connection manager (`get_db`, `dict_cursor`, `db_now`) |
| `static/app.js` | All frontend logic — feed, leaderboard, overlays, resolve UI |
| `static/app.css` | Cream editorial theme. Mobile breakpoint at `≤768px` + landscape `max-height:500px` |
| `templates/index.html` | Single HTML shell |
| `migrate_to_pg.py` | One-shot SQLite → PostgreSQL migration (idempotent, keep for reference) |

## Running locally
```bash
# Start PostgreSQL
brew services start postgresql@15

# Start dashboard
source .venv/bin/activate
uvicorn dashboard:app --reload --port 8000

# One-shot tweet poll
python monitor.py --source apify --backfill 50

# Backfill historical prices
python monitor.py --fix-prices
```

## Database
- 707 tweets, ~681 finance signals, ~3564 ticker_mentions in DB (full history of @aleabitoreddit — Twitter scraping caps at ~700 tweets)
- Schema: `tweets`, `signals`, `ticker_mentions`, `ticker_prices`
- All timestamps stored as TEXT ISO 8601 strings (not TIMESTAMPTZ) for consistency
- `SERIAL PRIMARY KEY` on signals + ticker_mentions; sequences reset after migration

## Important behaviours
- **OTC deduplication**: `_dedupe_by_primary_exchange()` in monitor.py drops PNK/OTC listings when a primary-exchange listing of the same company exists (e.g. SIVEF vs SIVE.ST)
- **Historical prices**: `_get_historical_price()` fetches closing price on the tweet date (7-day lookback) so `price_at_mention` reflects the price when the tweet was posted, not today
- **"New" badge**: ticker chips show a green "new" superscript on a stock's first-ever mention
- **Ticker overlay**: clicking a symbol in the leaderboard opens a drill-down overlay (not a new tab). Closes on Escape or nav switch.
- **Two-pass resolution**: Pass 1 = `$TICKER` regex → yfinance. Pass 2 = Claude `company_names[]` → Yahoo Finance search → yfinance
- **Apify `toDate`**: pass `--until YYYY-MM-DD` to `monitor.py` and it forwards to Apify's `toDate` param, stopping the scraper before already-seen tweets (saves API credits on re-seeding)

## Next session: Deployment (Railway)
Plan: two Railway services sharing one PostgreSQL plugin.

**Service 1 — Web (`dashboard.py`)**
```
Start command: uvicorn dashboard:app --host 0.0.0.0 --port $PORT
```

**Service 2 — Daemon (`monitor.py`)**
```
Start command: python monitor.py --daemon
```

Both services get `DATABASE_URL` injected automatically by Railway's PostgreSQL plugin.

Env vars needed on Railway:
- `DATABASE_URL` — auto-injected by Railway Postgres plugin
- `ANTHROPIC_API_KEY`
- `APIFY_API_TOKEN`
- `TWEET_SOURCE=apify`
- `X_TARGET_USERNAME=aleabitoreddit`
- `POLL_INTERVAL=3600`
- `POLYGON_API_KEY` (optional)

First deploy steps:
1. Push `dev` branch → merge to `main`
2. Create Railway project → add PostgreSQL plugin
3. Add web service from repo, set start command above
4. Add daemon service from same repo, set start command above
5. Run `python migrate_to_pg.py` once against Railway's DATABASE_URL to seed prod DB, OR just run monitor.py daemon and let it build up from scratch

## Next session: Code cleanup targets
- `monitor.py` `init_db()` migration block (date/html-entity fix) runs on every startup — should be a one-shot or guarded with a version flag
- `dashboard.py` `ensure_schema()` and `monitor.py` `init_db()` both create tables — consolidate into `db.py`
- `migrate_to_pg.py` can be deleted after confirming prod is healthy
- `state/` directory and `Config.STATE_FILE` / `Config.DB_FILE` remnants can be cleaned up (SQLite is gone)
- `Config.TWEET_SOURCE` default is still `"rsshub"` — should default to `"apify"` for prod
- `static/ticker.js` + `templates/ticker.html` are a standalone ticker page that duplicates logic already in the overlay — consider removing or keeping for direct-link sharing
- Unresolved ticker resolve UI in feed detail: works but has no confirmation step
- `logs/` should be excluded from git (add to .gitignore)
