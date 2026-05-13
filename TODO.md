# TODO

## 1. Design system — color overhaul + dark mode toggle

Current palette is a fixed cream/editorial theme (`--bg: #F4F0E6`).

- Audit CSS variables in `static/app.css` and consolidate all colors under `:root`
- Design a dark mode token set (dark surface, muted text, adjusted bull/bear greens/reds)
- Add a toggle button (sun/moon icon) in the navbar that flips a `data-theme="dark"` attribute on `<html>`
- Persist preference to `localStorage` so it survives reloads

## 2. Keep Edit button on already-resolved tickers

Currently the resolve form only appears for tickers with `name == '(unresolved)'`. If a ticker was resolved incorrectly, there is no way to fix it from the UI.

- In `buildTickerRow()` in `static/app.js`, render the Edit/resolve control for all tickers (not just unresolved ones)
- The `POST /api/tickers/{id}/resolve` endpoint already overwrites any existing resolution, so no backend changes needed
- Style differently from the initial resolution flow (e.g., pencil icon instead of "Resolve" label) to signal it is a correction

## 3. Adjust pane widths — narrower feed, wider detail

The current split (`grid-template-columns: 380px 1fr`) gives the left feed pane too much space relative to the detail pane.

- In `static/app.css`, reduce `.pane-left` fixed width (e.g., `280px` or `300px`) and let `.pane-right` fill remaining space
- Ensure the signal cards in the left pane reflow gracefully at the narrower width (check text truncation on thesis preview)
- Test at common viewport widths (1280px, 1440px, 1920px)

## 4. Manual price validation UI

Some resolved tickers show stale or clearly wrong prices (yfinance data quality issues). Need an in-dashboard way to flag and correct them.

- Add a "flag price" action on each ticker row in the detail pane
- Flagged tickers should show a warning indicator on the leaderboard
- Optionally: allow manually entering a reference price that overrides `current_price` in `ticker_prices` (add a `manual_price` column and a `PATCH /api/tickers/{symbol}/price` endpoint)
- The `fail_count` mechanism already prevents repeated bad fetches; flagging gives human oversight on top

## 5. Scalability architecture plan

At 1000s of tweets and tickers the current design will hit limits. Areas to address:

**Database**
- Add indexes: `tweets(published_at)`, `signals(is_finance, tweet_id)`, `ticker_mentions(resolved_symbol)`, `ticker_prices(resolved_symbol)`
- Consider moving to PostgreSQL if concurrent dashboard + monitor writes cause contention (SQLite WAL mode buys time)
- Archive old signals to a cold table after N days to keep hot queries fast

**Price refresh**
- Current loop refreshes all symbols sequentially in a thread pool. Replace with a batched async approach (e.g., 10 symbols in parallel via `asyncio.gather`)
- Yahoo Finance bulk quote endpoint (`/v7/finance/quote?symbols=A,B,C`) can fetch ~100 symbols in one request — much faster than per-symbol `yf.Ticker` calls

**Ingestion**
- Rate-limit Claude calls: add a semaphore (e.g., `asyncio.Semaphore(5)`) so large backfills don't exhaust API quota
- Consider a task queue (e.g., Redis + RQ, or just a SQLite-backed queue table) so monitor.py and dashboard.py are fully decoupled

**Frontend**
- Current infinite scroll loads all signal data from the DB on each page request; add a `search` / `filter by ticker` query param to the `/api/signals` endpoint
- Leaderboard query does a full table scan on every load — pre-aggregate into a materialized view or a summary table updated on signal insert
