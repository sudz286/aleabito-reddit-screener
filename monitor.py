"""
X Signal Monitor - @aleabitoreddit Tweet Scraper & Analyzer

Polls a target X account for new tweets via RSSHub (free) or X API,
extracts $TICKER symbols across global markets, fetches prices via
yfinance, and uses Claude to summarize the investment thesis.

Pushes formatted signals to Telegram.

Usage:
    python monitor.py                # one-shot poll
    python monitor.py --daemon       # continuous polling (1hr default)
    python monitor.py --backfill 20  # process last N tweets
"""

import os
import re
import json
import time
import html
import logging
import argparse
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

import requests
import feedparser
import yfinance as yf
from anthropic import Anthropic
from apify_client import ApifyClient
from dotenv import load_dotenv
from db import get_db, dict_cursor, db_now

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "monitor.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("x_signal_monitor")

# ── Config ────────────────────────────────────────────────────────────────────

class Config:
    # Tweet source: "rsshub" (free) or "x_api" (needs Bearer token)
    TWEET_SOURCE = os.getenv("TWEET_SOURCE", "rsshub")

    # RSSHub instance URL (self-host or use a public instance)
    # Public instances: https://docs.rsshub.app/guide/instances
    RSSHUB_BASE = os.getenv("RSSHUB_BASE", "https://rsshub.app")
    X_TARGET_USERNAME = os.getenv("X_TARGET_USERNAME", "aleabitoreddit")

    # X API (only if TWEET_SOURCE=x_api)
    X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")

    # Apify (only if TWEET_SOURCE=apify)
    APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
    # Actor: apidojo/tweet-scraper (reliable, works on free tier)
    APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "apidojo/tweet-scraper")

    # Price data
    POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")  # optional, US fallback

    # Claude API for thesis summarization
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # Telegram delivery
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Polling
    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL", "3600"))  # 1 hour

    # State
    STATE_FILE = Path(__file__).parent / "state" / "last_seen.json"


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as conn:
        with dict_cursor(conn) as cur:
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

    # Migrate: convert Twitter-format dates and decode HTML entities in existing rows
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute(
                "SELECT id, published_at FROM tweets WHERE published_at != '' AND published_at NOT LIKE '20%'"
            )
            for row in cur.fetchall():
                iso = _parse_tweet_date(row["published_at"])
                if iso != row["published_at"]:
                    cur.execute("UPDATE tweets SET published_at = %s WHERE id = %s", (iso, row["id"]))

            cur.execute(
                "SELECT id, text FROM tweets WHERE text LIKE '%&amp;%' OR text LIKE '%&gt;%' OR text LIKE '%&lt;%'"
            )
            for row in cur.fetchall():
                decoded = html.unescape(row["text"])
                if decoded != row["text"]:
                    cur.execute("UPDATE tweets SET text = %s WHERE id = %s", (decoded, row["id"]))

    log.info("Database ready (PostgreSQL)")


def _parse_tweet_date(raw: str) -> str:
    """Normalize a Twitter-format date string to ISO 8601 for correct sorting."""
    if not raw:
        return raw
    if re.match(r'^\d{4}-', raw):
        return raw  # already ISO
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        pass
    return raw


def _get_historical_price(tk: "yf.Ticker", as_of_date: str) -> Optional[float]:
    """Return the closing price on the trading day on or before as_of_date."""
    try:
        date = datetime.fromisoformat(as_of_date[:10])
        start = (date - timedelta(days=7)).strftime("%Y-%m-%d")
        end   = (date + timedelta(days=1)).strftime("%Y-%m-%d")
        hist  = tk.history(start=start, end=end)
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 4)
    except Exception:
        pass
    return None


def is_tweet_seen(tweet_id: str) -> bool:
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT 1 FROM tweets WHERE id = %s", (tweet_id,))
            return cur.fetchone() is not None


def get_last_tweet_id() -> Optional[str]:
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT id FROM tweets ORDER BY fetched_at DESC LIMIT 1")
            row = cur.fetchone()
            return row["id"] if row else None


def save_tweet_to_db(tweet: dict):
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute(
                """INSERT INTO tweets
                   (id, username, text, url, published_at, fetched_at, source, is_reply, parent_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    tweet["id"], Config.X_TARGET_USERNAME, tweet["text"],
                    tweet["url"], tweet["time"], db_now(), Config.TWEET_SOURCE,
                    int(tweet["is_reply"]), tweet.get("parent_text", ""),
                ),
            )


def save_signal_to_db(signal: "Signal"):
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute(
                """INSERT INTO signals
                   (tweet_id, is_finance, thesis_summary, market_context,
                    sentiment, confidence, processed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tweet_id) DO UPDATE SET
                       is_finance     = EXCLUDED.is_finance,
                       thesis_summary = EXCLUDED.thesis_summary,
                       market_context = EXCLUDED.market_context,
                       sentiment      = EXCLUDED.sentiment,
                       confidence     = EXCLUDED.confidence
                   RETURNING id""",
                (
                    signal.tweet_id, int(signal.is_finance),
                    signal.thesis_summary, signal.market_context,
                    signal.sentiment, signal.confidence,
                    db_now(),
                ),
            )
            signal_id = cur.fetchone()["id"]
            for t in signal.tickers:
                cur.execute(
                    """INSERT INTO ticker_mentions
                       (signal_id, symbol, resolved_symbol, name, exchange, currency,
                        price_at_mention, change_pct, price_fetched_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        signal_id, t["symbol"], t["resolved_symbol"], t.get("name"),
                        t.get("market"), t.get("currency"), t.get("price"), t.get("change_pct"),
                        db_now(),
                    ),
                )


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class TickerInfo:
    symbol: str               # raw symbol from tweet, e.g. "005930"
    resolved_symbol: str      # yfinance-compatible, e.g. "005930.KS"
    name: str = ""
    price: Optional[float] = None
    currency: str = ""
    market: str = ""          # e.g. "KOSPI", "NYSE", "Euronext Paris"
    change_pct: Optional[float] = None

@dataclass
class Signal:
    tweet_id: str
    tweet_text: str
    tweet_url: str
    tweet_time: str
    is_finance: bool = False
    tickers: list = field(default_factory=list)
    thesis_summary: str = ""
    market_context: str = ""
    sentiment: str = ""
    confidence: str = ""
    is_reply: bool = False
    reply_context: str = ""
    raw_parent_text: str = ""


# ── Ticker Extraction ─────────────────────────────────────────────────────────

# Common false positives on finance twitter
TICKER_BLOCKLIST = {
    "CEO", "IPO", "ETF", "GDP", "CPI", "PPI", "FOMC", "FED", "SEC",
    "NYSE", "IMO", "LMAO", "LOL", "USA", "USD", "EUR", "GBP", "JPY",
    "API", "EPS", "DCF", "PE", "ROE", "ROI", "YOY", "QOQ", "MOM",
    "P", "A", "I", "S", "U", "AM", "PM", "IT", "AI", "ATH", "DD",
    "ALL", "ARE", "BE", "SO", "OR", "FOR", "NOW", "NEW", "ONE", "TWO",
    "BIG", "OLD", "OUR", "OUT", "HOW", "HAS", "HIS", "HER", "WHO",
    "ETH", "BTC", "NFT", "DCA", "HODL", "FOMO", "TA", "FA",
    "TV", "UK", "EU", "US", "JP", "CN", "KR", "TW", "FR", "DE",
}

# Mapping of tweet context keywords to yfinance exchange suffixes
MARKET_SUFFIXES = {
    # East Asia
    "korea": [".KS", ".KQ"],
    "korean": [".KS", ".KQ"],
    "kospi": [".KS"],
    "kosdaq": [".KQ"],
    "japan": [".T"],
    "japanese": [".T"],
    "tokyo": [".T"],
    "nikkei": [".T"],
    "taiwan": [".TW", ".TWO"],
    "taiwanese": [".TW", ".TWO"],
    "twse": [".TW"],
    "tsmc": [],  # TSMC is listed as TSM in US
    "china": [".SS", ".SZ"],
    "chinese": [".SS", ".SZ"],
    "shanghai": [".SS"],
    "shenzhen": [".SZ"],
    "hong kong": [".HK"],
    "hkex": [".HK"],
    # Europe
    "france": [".PA"],
    "french": [".PA"],
    "paris": [".PA"],
    "euronext": [".PA", ".AS", ".BR"],
    "germany": [".DE"],
    "german": [".DE"],
    "frankfurt": [".DE"],
    "xetra": [".DE"],
    "london": [".L"],
    "lse": [".L"],
    "amsterdam": [".AS"],
    # Other
    "india": [".NS", ".BO"],
    "indian": [".NS", ".BO"],
    "nse": [".NS"],
    "bse": [".BO"],
    "australia": [".AX"],
    "asx": [".AX"],
    "canada": [".TO", ".V"],
    "tsx": [".TO"],
    "brazil": [".SA"],
}


def extract_raw_tickers(text: str) -> list[str]:
    """Pull $TICKER patterns from tweet text."""
    # Match $AAPL, $005930, $7203 (numeric tickers common in Asia)
    pattern = r'\$([A-Za-z]{1,6}|[0-9]{4,6})'
    matches = re.findall(pattern, text)
    tickers = []
    for t in matches:
        t_upper = t.upper()
        if t_upper not in TICKER_BLOCKLIST and len(t_upper) >= 2:
            tickers.append(t)
    return list(dict.fromkeys(tickers))  # dedupe, preserve order


def detect_market_hints(text: str) -> list[str]:
    """Scan tweet text for country/exchange references to help resolve tickers."""
    text_lower = text.lower()
    hints = []
    for keyword, suffixes in MARKET_SUFFIXES.items():
        if keyword in text_lower:
            hints.extend(suffixes)
    return list(dict.fromkeys(hints))


def search_yahoo_symbols(name: str) -> list[str]:
    """Search Yahoo Finance for equity symbols matching a company name."""
    try:
        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": name, "quotesCount": 5, "newsCount": 0, "enableFuzzyQuery": False},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes", [])
        return [
            q["symbol"] for q in quotes
            if q.get("symbol") and q.get("quoteType") in ("EQUITY", "ETF")
        ]
    except Exception as e:
        log.warning(f"Yahoo Finance search failed for '{name}': {e}")
        return []


def resolve_known_symbol(symbol: str, source_name: str,
                         as_of_date: str = None) -> Optional[TickerInfo]:
    """Validate and fetch price for an already-known yfinance symbol."""
    try:
        tk = yf.Ticker(symbol)
        current = getattr(tk.fast_info, "last_price", None)
        if current is None or current <= 0:
            return None
        price = (_get_historical_price(tk, as_of_date) or current) if as_of_date else current
        full_info = tk.info
        return TickerInfo(
            symbol=source_name,
            resolved_symbol=symbol,
            name=full_info.get("shortName", full_info.get("longName", symbol)),
            price=round(price, 4),
            currency=full_info.get("currency", ""),
            market=full_info.get("exchange", ""),
            change_pct=_calc_change(tk),
        )
    except Exception:
        pass
    return None


def resolve_ticker(raw: str, market_hints: list[str],
                   as_of_date: str = None) -> Optional[TickerInfo]:
    """
    Try to resolve a raw ticker symbol to a valid yfinance instrument.

    Strategy:
    1. If purely numeric (Asian markets), try market_hints first
    2. If alphanumeric, try as-is (US), then with market_hints
    3. Validate by checking yfinance returns price data
    """
    is_numeric = raw.isdigit()
    candidates = []

    if is_numeric:
        for suffix in market_hints:
            candidates.append(f"{raw}{suffix}")
        if not market_hints:
            candidates.extend([
                f"{raw}.KS", f"{raw}.T", f"{raw}.TW",
                f"{raw}.SS", f"{raw}.SZ", f"{raw}.HK",
            ])
    else:
        candidates.append(raw.upper())
        for suffix in market_hints:
            candidates.append(f"{raw.upper()}{suffix}")

    for candidate in candidates:
        try:
            tk = yf.Ticker(candidate)
            current = getattr(tk.fast_info, "last_price", None)
            if current is None or current <= 0:
                continue
            price = (_get_historical_price(tk, as_of_date) or current) if as_of_date else current
            full_info = tk.info
            return TickerInfo(
                symbol=raw,
                resolved_symbol=candidate,
                name=full_info.get("shortName", full_info.get("longName", candidate)),
                price=round(price, 4),
                currency=full_info.get("currency", ""),
                market=full_info.get("exchange", ""),
                change_pct=_calc_change(tk),
            )
        except Exception:
            continue

    log.warning(f"Could not resolve ticker: {raw} (tried: {candidates})")
    return None


def _calc_change(tk) -> Optional[float]:
    """Calculate daily % change from yfinance Ticker."""
    try:
        info = tk.fast_info
        prev = getattr(info, "previous_close", None)
        last = getattr(info, "last_price", None)
        if prev and last and prev > 0:
            return round(((last - prev) / prev) * 100, 2)
    except Exception:
        pass
    return None


# ── Tweet Fetching ────────────────────────────────────────────────────────────

def fetch_tweets_rsshub(username: str, limit: int = 20) -> list[dict]:
    """
    Fetch tweets via RSSHub's Twitter/X route.
    Returns list of dicts with: id, text, url, time, is_reply, parent_text
    """
    feed_url = f"{Config.RSSHUB_BASE}/twitter/user/{username}"
    log.info(f"Fetching RSS feed: {feed_url}")

    try:
        resp = requests.get(feed_url, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception as e:
        log.error(f"RSS fetch failed: {e}")
        return []

    if feed.bozo and not feed.entries:
        log.error(f"RSS parse error: {feed.bozo_exception}")
        return []

    tweets = []
    for entry in feed.entries[:limit]:
        raw_html = entry.get("summary", entry.get("description", ""))
        plain_text = re.sub(r'<[^>]+>', ' ', raw_html)
        plain_text = re.sub(r'\s+', ' ', plain_text).strip()

        tweet_url = entry.get("link", "")
        tweet_id = tweet_url.split("/")[-1] if "/" in tweet_url else hashlib.md5(
            tweet_url.encode()
        ).hexdigest()[:16]

        # Detect reply context from content structure
        is_reply = False
        parent_text = ""
        reply_match = re.search(
            r'(?:Re\s+@\w+[:\s]|Replying to @\w+[:\s])(.*?)(?=\n|$)', plain_text
        )
        if reply_match:
            is_reply = True
            parent_text = reply_match.group(1).strip()

        tweets.append({
            "id": tweet_id,
            "text": html.unescape(plain_text),
            "url": tweet_url,
            "time": _parse_tweet_date(entry.get("published", "")),
            "is_reply": is_reply,
            "parent_text": html.unescape(parent_text),
        })

    log.info(f"Fetched {len(tweets)} tweets via RSSHub")
    return tweets


def fetch_tweets_x_api(username: str, limit: int = 20, since_id: str = None) -> list[dict]:
    """
    Fetch tweets via X API v2 (requires Bearer token, Basic tier+).
    """
    if not Config.X_BEARER_TOKEN:
        log.error("X_BEARER_TOKEN not set")
        return []

    headers = {"Authorization": f"Bearer {Config.X_BEARER_TOKEN}"}

    # Resolve username -> user ID
    try:
        resp = requests.get(
            f"https://api.twitter.com/2/users/by/username/{username}",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        user_id = resp.json()["data"]["id"]
    except Exception as e:
        log.error(f"Failed to resolve X user ID: {e}")
        return []

    # Fetch timeline
    params = {
        "max_results": min(limit, 100),
        "tweet.fields": "created_at,conversation_id,in_reply_to_user_id,referenced_tweets",
        "expansions": "referenced_tweets.id",
    }
    if since_id:
        params["since_id"] = since_id

    try:
        resp = requests.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            headers=headers, params=params, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"X API timeline fetch failed: {e}")
        return []

    # Build lookup for parent tweets (reply context)
    includes = data.get("includes", {})
    ref_tweets = {t["id"]: t["text"] for t in includes.get("tweets", [])}

    tweets = []
    for t in data.get("data", []):
        is_reply = bool(t.get("in_reply_to_user_id"))
        parent_text = ""
        for ref in t.get("referenced_tweets", []):
            if ref["type"] == "replied_to" and ref["id"] in ref_tweets:
                parent_text = ref_tweets[ref["id"]]
                break

        tweets.append({
            "id": t["id"],
            "text": t["text"],
            "url": f"https://x.com/{username}/status/{t['id']}",
            "time": t.get("created_at", ""),
            "is_reply": is_reply,
            "parent_text": parent_text,
        })

    log.info(f"Fetched {len(tweets)} tweets via X API")
    return tweets


def fetch_tweets_apify(username: str, limit: int = 20, until_date: str = None) -> list[dict]:
    """
    Fetch tweets via Apify's tweet-scraper actor.
    Uses apidojo/tweet-scraper by default (works on free tier).
    until_date: ISO date string (e.g. "2026-04-01") — passed to Apify as toDate to
    avoid fetching tweets already in the DB, saving API credits during seeding.
    """
    if not Config.APIFY_API_TOKEN:
        log.error("APIFY_API_TOKEN not set")
        return []

    log.info(f"Fetching tweets via Apify actor: {Config.APIFY_ACTOR_ID}")
    client = ApifyClient(Config.APIFY_API_TOKEN)

    run_input = {
        "startUrls": [{"url": f"https://twitter.com/{username}"}],
        "maxItems": limit,
    }
    if until_date:
        run_input["toDate"] = until_date[:10]  # YYYY-MM-DD
        log.info(f"Apify toDate set to {run_input['toDate']} (fetching tweets before this date)")

    try:
        run = client.actor(Config.APIFY_ACTOR_ID).call(run_input=run_input)
    except Exception as e:
        log.error(f"Apify actor run failed: {e}")
        return []

    tweets = []
    try:
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            text = item.get("text") or item.get("full_text") or item.get("fullText", "")
            tweet_id = str(item.get("id") or item.get("tweetId") or item.get("tweet_id", ""))
            url = item.get("url") or item.get("tweetUrl") or (
                f"https://x.com/{username}/status/{tweet_id}" if tweet_id else ""
            )
            created_at = (
                item.get("createdAt") or item.get("created_at") or
                item.get("timestamp") or ""
            )
            is_reply = bool(
                item.get("isReply") or item.get("inReplyToStatusId") or
                item.get("in_reply_to_status_id")
            )

            if not text or not tweet_id:
                continue

            tweets.append({
                "id": tweet_id,
                "text": html.unescape(text),
                "url": url,
                "time": _parse_tweet_date(created_at),
                "is_reply": is_reply,
                "parent_text": "",
            })
    except Exception as e:
        log.error(f"Apify dataset read failed: {e}")
        return []

    log.info(f"Fetched {len(tweets)} tweets via Apify")
    return tweets


def fetch_tweets(limit: int = 20, since_id: str = None, until_date: str = None) -> list[dict]:
    """Route to the configured tweet source."""
    username = Config.X_TARGET_USERNAME
    if Config.TWEET_SOURCE == "x_api":
        return fetch_tweets_x_api(username, limit, since_id)
    if Config.TWEET_SOURCE == "apify":
        return fetch_tweets_apify(username, limit, until_date=until_date)
    return fetch_tweets_rsshub(username, limit)


# ── Thesis Summarization via Claude ───────────────────────────────────────────

THESIS_PROMPT = """You are a financial signal analyst. Analyze this tweet from a finance-focused X account.

TWEET:
{tweet_text}

{reply_context}

ALREADY RESOLVED TICKERS (from $ symbols in tweet):
{ticker_data}

Respond in this exact JSON format, nothing else:
{{
    "is_finance_related": true/false,
    "thesis_summary": "2-3 sentence plain English summary. No jargon. Explain like you are talking to a smart friend who does not work in finance.",
    "market_context": "Which market(s)/sector(s) the tickers operate in, e.g. 'Korean semiconductor supply chain' or 'French luxury goods'",
    "sentiment": "bullish | bearish | neutral | mixed",
    "confidence": "high | medium | low",
    "company_names": ["Official Company Name 1", "Official Company Name 2"]
}}

Rules:
- If the tweet is not finance-related, set is_finance_related to false and leave other fields as empty strings
- Simplify ALL jargon. "Multiple expansion" becomes "the market is willing to pay more per dollar of earnings"
- If tickers span multiple countries, mention the geographic angle
- Keep thesis_summary under 80 words
- company_names: list every company or stock mentioned by name in the tweet, even those without a $ symbol. Use the full official name (e.g. "Samsung Electronics" not "Samsung", "LVMH Moet Hennessy" not "LVMH"). Include companies already identified via $ symbols. Empty array [] if none."""


def summarize_thesis(tweet_text: str, tickers: list[TickerInfo],
                     is_reply: bool, parent_text: str) -> dict:
    """Call Claude to extract and simplify the investment thesis."""
    if not Config.ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY, skipping thesis summarization")
        return {"is_finance_related": bool(tickers), "thesis_summary": "", "market_context": ""}

    reply_context = ""
    if is_reply and parent_text:
        reply_context = f"THIS IS A REPLY TO:\n{parent_text}"

    ticker_data = "\n".join(
        f"- ${t.symbol} ({t.resolved_symbol}): {t.name}, "
        f"{t.currency} {t.price}, {t.market}"
        + (f", {t.change_pct:+.2f}% today" if t.change_pct is not None else "")
        for t in tickers
    ) if tickers else "No tickers extracted."

    prompt = THESIS_PROMPT.format(
        tweet_text=tweet_text,
        reply_context=reply_context,
        ticker_data=ticker_data,
    )

    try:
        client = Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return {"is_finance_related": bool(tickers), "thesis_summary": "", "market_context": ""}


# ── Signal Assembly ───────────────────────────────────────────────────────────

_OTC_EXCHANGES = {"PNK", "OTC", "GREY", "PINX"}


def _dedupe_by_primary_exchange(resolved: dict) -> dict:
    """Drop OTC/pink-sheet duplicates when a primary-exchange listing exists for the same company."""
    by_name: dict[str, list[str]] = {}
    for sym, info in resolved.items():
        key = (info.name or "").strip().lower()
        if key and key != "(unresolved)":
            by_name.setdefault(key, []).append(sym)

    to_drop: set[str] = set()
    for syms in by_name.values():
        if len(syms) < 2:
            continue
        primary = [s for s in syms if resolved[s].market not in _OTC_EXCHANGES]
        otc     = [s for s in syms if resolved[s].market in _OTC_EXCHANGES]
        if primary and otc:
            to_drop.update(otc)
            log.info(f"Dropped OTC duplicates {otc} in favour of {primary}")

    return {s: info for s, info in resolved.items() if s not in to_drop}


def process_tweet(tweet: dict) -> Signal:
    """Full pipeline: extract tickers -> resolve prices -> Claude thesis + name extraction -> name resolution."""
    text = html.unescape(tweet["text"])
    as_of_date = tweet.get("time", "")
    full_context = text + " " + html.unescape(tweet.get("parent_text", ""))
    market_hints = detect_market_hints(full_context)

    # Pass 1: resolve $TICKER patterns from tweet text
    resolved: dict[str, TickerInfo] = {}
    for raw in extract_raw_tickers(text):
        info = resolve_ticker(raw, market_hints, as_of_date=as_of_date)
        if info:
            resolved[info.resolved_symbol] = info
        else:
            resolved[raw] = TickerInfo(symbol=raw, resolved_symbol=raw,
                                       name="(unresolved)", market="unknown")

    # Ask Claude: thesis summary + company names mentioned in the tweet
    thesis = summarize_thesis(
        text, list(resolved.values()),
        tweet["is_reply"], tweet.get("parent_text", "")
    )

    # Pass 2: resolve company names Claude identified (catches non-$ mentions)
    for name in thesis.get("company_names", []):
        candidates = search_yahoo_symbols(name)
        for symbol in candidates:
            if symbol in resolved:
                break
            info = resolve_known_symbol(symbol, source_name=name, as_of_date=as_of_date)
            if info:
                resolved[symbol] = info
                log.info(f"Name-resolved '{name}' -> {symbol} ({info.name})")
                break

    # Drop OTC/pink-sheet duplicates when a primary-exchange listing exists
    resolved = _dedupe_by_primary_exchange(resolved)

    all_tickers = list(resolved.values())
    return Signal(
        tweet_id=tweet["id"],
        tweet_text=text,
        tweet_url=tweet["url"],
        tweet_time=tweet["time"],
        is_finance=thesis.get("is_finance_related", bool(all_tickers)),
        tickers=[asdict(t) for t in all_tickers],
        thesis_summary=thesis.get("thesis_summary", ""),
        market_context=thesis.get("market_context", ""),
        sentiment=thesis.get("sentiment", ""),
        confidence=thesis.get("confidence", ""),
        is_reply=tweet["is_reply"],
        reply_context=tweet.get("parent_text", ""),
    )


# ── Telegram Delivery ────────────────────────────────────────────────────────

def format_telegram_message(signal: Signal) -> str:
    """Format a Signal into a clean Telegram message."""
    parts = []
    parts.append("📡 *New Signal*")
    parts.append("")

    if signal.tickers:
        ticker_lines = []
        for t in signal.tickers:
            if t.get("price"):
                chg = f" ({t['change_pct']:+.2f}%)" if t.get("change_pct") is not None else ""
                ticker_lines.append(
                    f"  `${t['symbol']}` {t['name']} — {t['currency']} {t['price']}{chg}"
                )
            else:
                ticker_lines.append(f"  `${t['symbol']}` _(unresolved)_")
        parts.append("*Tickers:*")
        parts.extend(ticker_lines)
        parts.append("")

    if signal.market_context:
        parts.append(f"*Market:* {signal.market_context}")
        parts.append("")

    if signal.thesis_summary:
        parts.append(f"*Thesis:* {signal.thesis_summary}")
        parts.append("")

    if signal.is_reply and signal.reply_context:
        ctx = signal.reply_context[:200] + ("..." if len(signal.reply_context) > 200 else "")
        parts.append(f"_Replying to:_ {ctx}")
        parts.append("")

    parts.append(f"[View tweet]({signal.tweet_url})")
    return "\n".join(parts)


def send_telegram(message: str):
    """Send a message via Telegram Bot API."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured, printing to stdout")
        print("\n" + "=" * 60)
        clean = re.sub(r'[*_`\[\]]', '', message)
        print(clean)
        print("=" * 60 + "\n")
        return

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Telegram message sent")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run_once(backfill: int = 20, since: str = None, until: str = None):
    """Single poll: fetch, process, deliver.

    since / until: ISO date strings e.g. "2026-01-01" or "2026-01-01T00:00:00Z"
    When provided, tweets outside the window are skipped (client-side filter,
    works for all sources). The X API source also passes since/until server-side.
    """
    tweets = fetch_tweets(limit=backfill, since_id=get_last_tweet_id(), until_date=until)
    if not tweets:
        log.info("No new tweets found")
        return

    # Client-side date window filter
    if since or until:
        def in_window(t):
            ts = t.get("time", "")
            if not ts:
                return True  # no timestamp, keep it
            if since and ts < since:
                return False
            if until and ts > until:
                return False
            return True
        before = len(tweets)
        tweets = [t for t in tweets if in_window(t)]
        log.info(f"Date filter [{since} → {until}]: {before} → {len(tweets)} tweets")

    if not tweets:
        log.info("No tweets in the specified date window")
        return

    new_signals = 0
    for tweet in reversed(tweets):  # oldest first
        if is_tweet_seen(tweet["id"]):
            continue

        log.info(f"Processing tweet {tweet['id']}: {tweet['text'][:80]}...")
        signal = process_tweet(tweet)

        save_tweet_to_db(tweet)
        save_signal_to_db(signal)

        if signal.is_finance:
            msg = format_telegram_message(signal)
            send_telegram(msg)
            new_signals += 1
        else:
            log.info(f"Skipping non-finance tweet: {tweet['id']}")

    log.info(f"Processed {len(tweets)} tweets, {new_signals} finance signals sent")


def backfill_historical_prices():
    """Update price_at_mention for all existing ticker_mentions using historical closing prices."""
    with get_db() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT tm.id, tm.resolved_symbol, t.published_at
                FROM ticker_mentions tm
                JOIN signals s ON tm.signal_id = s.id
                JOIN tweets t ON s.tweet_id = t.id
                WHERE tm.resolved_symbol IS NOT NULL
                  AND tm.resolved_symbol != ''
                  AND tm.name != '(unresolved)'
                  AND t.published_at IS NOT NULL
                  AND t.published_at != ''
            """)
            rows = cur.fetchall()

    log.info(f"Backfilling historical prices for {len(rows)} ticker mentions…")
    updated = failed = 0

    for row in rows:
        symbol   = row["resolved_symbol"]
        date_str = row["published_at"]
        try:
            price = _get_historical_price(yf.Ticker(symbol), date_str)
            if price and price > 0:
                with get_db() as conn:
                    with dict_cursor(conn) as cur:
                        cur.execute(
                            "UPDATE ticker_mentions SET price_at_mention = %s WHERE id = %s",
                            (price, row["id"])
                        )
                updated += 1
            else:
                failed += 1
                log.warning(f"No historical price for {symbol} on {date_str}")
        except Exception as e:
            log.warning(f"Historical price fetch failed for {symbol}: {e}")
            failed += 1

    log.info(f"Price backfill complete: {updated} updated, {failed} failed")


def run_daemon():
    """Continuous polling loop."""
    log.info(f"Starting daemon, polling every {Config.POLL_INTERVAL_SECONDS}s")
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"Poll cycle error: {e}", exc_info=True)
        time.sleep(Config.POLL_INTERVAL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="X Signal Monitor")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--backfill", type=int, default=20, help="Tweets to fetch")
    parser.add_argument("--source", choices=["rsshub", "x_api", "apify"], help="Override tweet source")
    parser.add_argument("--since", type=str, default=None,
                        help="Only process tweets on/after this date (ISO format, e.g. 2026-01-01)")
    parser.add_argument("--until", type=str, default=None,
                        help="Only process tweets on/before this date (ISO format, e.g. 2026-05-01)")
    parser.add_argument("--fix-prices", action="store_true",
                        help="Backfill price_at_mention with historical closing prices (slow)")
    args = parser.parse_args()

    if args.source:
        Config.TWEET_SOURCE = args.source

    init_db()

    if args.fix_prices:
        backfill_historical_prices()
    elif args.daemon:
        run_daemon()
    else:
        run_once(backfill=args.backfill, since=args.since, until=args.until)


if __name__ == "__main__":
    main()
