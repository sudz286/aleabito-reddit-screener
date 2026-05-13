# X Signal Monitor

Polls [@aleabitoreddit](https://x.com/aleabitoreddit) for finance tweets, extracts global tickers with prices, and serves a local dashboard with thesis summaries and performance tracking.

## Architecture

```
Apify (tweet scraper)
        |
        v
   Tweet Ingestion ──> SQLite (tweets, signals, ticker_mentions)
        |
        v
  Two-pass Ticker Resolution
    Pass 1: $TICKER regex → yfinance
    Pass 2: Claude company_names[] → Yahoo Finance search → yfinance
        |
        v
   Claude API (thesis summary, sentiment, confidence, company names)
        |
        v
   FastAPI Dashboard  ←──  APScheduler (price refresh every 30 min)
```

## Pipeline per tweet

1. **Ingest** — Apify scrapes tweets from @aleabitoreddit; SQLite deduplication by tweet ID
2. **Resolve (Pass 1)** — Regex for `$TICKER` patterns → yfinance validation
3. **Summarize** — Claude returns thesis summary, market context, sentiment, confidence, and `company_names[]`
4. **Resolve (Pass 2)** — Each company name → Yahoo Finance search → yfinance validation; merged with Pass 1 results
5. **Persist** — Signals and ticker mentions saved to SQLite with price at mention
6. **Dashboard** — FastAPI serves feed + leaderboard views; prices refreshed every 30 min in background

## Setup

```bash
git clone <repo>
cd aleabitoreddit-screener

pip install -r requirements.txt

cp .env.example .env
# Fill in: ANTHROPIC_API_KEY, APIFY_API_TOKEN
```

`.env` keys:

| Key | Required | Description |
|-----|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `APIFY_API_TOKEN` | Yes | Apify account token |
| `APIFY_ACTOR_ID` | No | Default: `apidojo/tweet-scraper` |
| `TWEET_SOURCE` | No | `apify` (default), `rsshub`, `x_api` |
| `TWITTER_USERNAME` | No | Target account (default: `aleabitoreddit`) |

## Usage

### Run the monitor (fetch + process tweets)

```bash
# Latest tweets (default: most recent run)
python monitor.py

# Backfill last N tweets
python monitor.py --backfill 50

# Date-windowed run
python monitor.py --since 2026-01-01 --until 2026-05-01

# Daemon mode (poll every hour)
python monitor.py --daemon
```

### Run the dashboard

```bash
uvicorn dashboard:app --reload --port 8000
```

Open `http://localhost:8000` in your browser.

## Dashboard

### Feed view
- Paginated signal cards (infinite scroll) ordered by recency
- Right-pane detail: full thesis, market context, sentiment badge, confidence
- Ticker chips with price at mention and current price
- Unresolved tickers can be manually resolved via Yahoo Finance search

### Leaderboard view
- KPI strip: total signals, tickers tracked, best and worst performer
- Podium cards for top 3 performers
- Full table sorted by % gain from first mention, with sparklines and mention counts

## Database

SQLite at `state/signals.db`. Tables:

| Table | Purpose |
|-------|---------|
| `tweets` | Raw tweet text, URL, timestamps, parent context |
| `signals` | Claude-generated thesis, sentiment, confidence, `is_finance` flag |
| `ticker_mentions` | Per-signal tickers with price at mention |
| `ticker_prices` | Latest prices per symbol; `fail_count` skips bad tickers after 5 failures |

## Supported Markets

| Region | Exchanges | yfinance Suffix |
|--------|-----------|-----------------|
| US | NYSE, NASDAQ | (none) |
| Korea | KOSPI, KOSDAQ | `.KS`, `.KQ` |
| Japan | TSE | `.T` |
| Taiwan | TWSE, TPEx | `.TW`, `.TWO` |
| China | SSE, SZSE | `.SS`, `.SZ` |
| Hong Kong | HKEX | `.HK` |
| France | Euronext Paris | `.PA` |
| Germany | XETRA | `.DE` |
| UK | LSE | `.L` |
| India | NSE, BSE | `.NS`, `.BO` |
| Australia | ASX | `.AX` |
| Canada | TSX | `.TO` |
| Brazil | B3 | `.SA` |

## Requirements

```
requests, feedparser, yfinance, anthropic, python-dotenv,
apify-client, fastapi, uvicorn, apscheduler, python-multipart
```
