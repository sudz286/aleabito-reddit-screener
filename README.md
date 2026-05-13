# X Signal Monitor

Polls [@aleabitoreddit](https://x.com/aleabitoreddit) for finance tweets, extracts global tickers with prices, and delivers simplified thesis summaries to Telegram.

## Architecture

```
RSSHub (free)              yfinance (free, global)
     |                            |
     v                            v
 Tweet Ingestion ──> Ticker Extraction ──> Price Resolution
                                                |
                                                v
                                    Claude API (thesis summary)
                                                |
                                                v
                                        Telegram Delivery
```

## Pipeline per tweet

1. **Ingest** - Pull tweet text, detect if it's a reply, grab parent context
2. **Extract** - Regex for `$TICKER` patterns, filter false positives
3. **Resolve** - Scan tweet for country/exchange hints (e.g. "Korea", "KOSPI"), try yfinance with appropriate suffixes (`.KS`, `.T`, `.PA`, etc.)
4. **Summarize** - Send tweet + ticker data to Claude, get back plain-English thesis and market context
5. **Deliver** - Format and push to Telegram (falls back to stdout if not configured)

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

## Setup

```bash
# Clone / copy files
cd x_signal_monitor

# Install deps
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Load env vars (or use python-dotenv)
export $(grep -v '^#' .env | xargs)
```

## Self-hosting RSSHub (recommended)

Public RSSHub instances can be unreliable. Self-host for stability:

```bash
# Docker (simplest)
docker run -d --name rsshub -p 1200:1200 diygod/rsshub

# Then set RSSHUB_BASE=http://localhost:1200
```

## Usage

```bash
# One-shot: fetch and process latest tweets
python monitor.py

# Backfill last 50 tweets
python monitor.py --backfill 50

# Daemon mode: poll every hour continuously
python monitor.py --daemon

# Override tweet source
python monitor.py --source x_api
```

## Example Output (Telegram)

```
📡 New Signal

Tickers:
  $005930 Samsung Electronics — KRW 82400 (+1.23%)
  $ASML ASML Holding — EUR 723.50 (-0.45%)

Market: Korean/European semiconductor equipment supply chain

Thesis: Samsung is ramping up its advanced chip manufacturing
and increasing orders from ASML for their latest lithography
machines. The bet is that Samsung will close the gap with TSMC,
which would be good for both companies' stock prices.

View tweet: https://x.com/aleabitoreddit/status/...
```

## Extending

- **Add WhatsApp**: swap `send_telegram()` for Twilio WhatsApp API
- **Add more accounts**: pass `--username` flag or loop over a list
- **Store signals**: write Signal objects to SQLite/Postgres for backtesting
- **Dashboard**: feed JSON signals to a simple React frontend
