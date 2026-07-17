"""
Free, key-less market index data (S&P 500, NASDAQ) for the "Combined
Politician Portfolio Performance" comparison chart.

Uses Yahoo Finance's public chart JSON endpoint (query1.finance.yahoo.com) --
an unofficial but long-standing, widely-used, no-signup endpoint (the same
finance.yahoo.com domain the app already links out to for the News button).
No API key, no scraping of rendered HTML -- just a JSON response.

This is intentionally NOT part of the main data_fetch.py refresh pipeline:
it isn't stored permanently and no politician/trade data depends on it.
It's fetched on demand when the portfolio chart is viewed, with a short
local cache (see CACHE_TTL_SECONDS) so switching time ranges back and forth
doesn't repeatedly hit Yahoo.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

from . import db

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

TIMEOUT = 15
CACHE_TTL_SECONDS = 60 * 60  # 1 hour is plenty fresh for a comparison chart

# (yahoo range, interval) per app time-range key. Longer ranges use a
# coarser interval to keep the response (and the aligned chart data) a
# reasonable size.
RANGE_TO_YAHOO = {
    "1m": ("1mo", "1d"),
    "3m": ("3mo", "1d"),
    "6m": ("6mo", "1d"),
    "ytd": ("ytd", "1d"),
    "1y": ("1y", "1d"),
    "5y": ("5y", "1wk"),
}

INDEX_SYMBOLS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
}


def _cache_path(symbol, range_key):
    d = os.path.join(db.get_data_dir(), "cache", "market")
    os.makedirs(d, exist_ok=True)
    safe_symbol = symbol.replace("^", "")
    return os.path.join(d, f"{safe_symbol}_{range_key}.json")


def _read_cache(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("fetched_at", 0) > CACHE_TTL_SECONDS:
            return None
        return data.get("series")
    except Exception:
        return None


def _write_cache(path, series):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "series": series}, f)
    except Exception:
        pass  # non-fatal -- worst case we just re-fetch next time


def _price_history_cache_path(symbol):
    d = os.path.join(db.get_data_dir(), "cache", "market")
    os.makedirs(d, exist_ok=True)
    safe_symbol = symbol.replace("^", "").replace("/", "_")
    return os.path.join(d, f"{safe_symbol}_history.json")


# How far back to pull daily closes for a single ticker's realized P/L
# estimate (see app.py's _annotate_realized_pnl) -- long enough to cover
# this app's ~2012-onward disclosure history in one request.
PRICE_HISTORY_YAHOO_RANGE = "max"
PRICE_HISTORY_CACHE_TTL_SECONDS = 60 * 60 * 12  # 12 hours


def get_price_history(ticker):
    """Returns a dict {date_str (YYYY-MM-DD): close_price} of daily closing
    prices for `ticker` going back as far as Yahoo Finance has data, or None
    if unavailable (network issue, unknown/delisted ticker, unexpected
    response shape, etc). Never raises -- profit/loss estimates are a
    nice-to-have annotation, not core data. Cached on disk (see
    PRICE_HISTORY_CACHE_TTL_SECONDS) since the same ticker's history is
    reused across every politician who's traded it."""
    if not ticker:
        return None
    symbol = ticker.strip().upper()
    if not symbol:
        return None

    path = _price_history_cache_path(symbol)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if time.time() - data.get("fetched_at", 0) <= PRICE_HISTORY_CACHE_TTL_SECONDS:
                return data.get("series")
        except Exception:
            pass

    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        resp = requests.get(
            url,
            params={"range": PRICE_HISTORY_YAHOO_RANGE, "interval": "1d"},
            headers=REQUEST_HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception:
        return None

    series = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        series[date_str] = close

    if series:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": time.time(), "series": series}, f)
        except Exception:
            pass
    return series


def get_index_series(index_key, range_key):
    """Returns a dict {date_str (YYYY-MM-DD): close_price} for the given
    index ('sp500' or 'nasdaq') and range key, or None if unavailable
    (network issue, unrecognized key, unexpected response shape, etc).
    Never raises -- comparison lines are a nice-to-have, not core data."""
    symbol = INDEX_SYMBOLS.get(index_key)
    if not symbol or range_key not in RANGE_TO_YAHOO:
        return None

    path = _cache_path(symbol, range_key)
    cached = _read_cache(path)
    if cached is not None:
        return cached

    yahoo_range, interval = RANGE_TO_YAHOO[range_key]
    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        resp = requests.get(
            url,
            params={"range": yahoo_range, "interval": interval},
            headers=REQUEST_HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception:
        return None

    series = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        series[date_str] = close

    if series:
        _write_cache(path, series)
    return series
