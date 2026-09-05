"""
market_data.py — Alpaca-based daily OHLCV data provider.

Replaces yfinance for all hot-path data fetching (bulk scanner, per-ticker
signal analysis, SPY trend check). This is the fix for the root cause found
in production: yfinance's unofficial Yahoo Finance scraping was failing
constantly with "Invalid Crumb / Unauthorized" errors (Yahoo's anti-bot
cookie auth), silently breaking every scan for days with zero trades placed.

Alpaca's Market Data API is a real, documented, authenticated API (using the
same keys already used for trading) — no cookie-scraping fragility.

yfinance is kept ONLY for one low-frequency, non-blocking, supplementary
lookup that Alpaca's free tier doesn't provide (VIX index level) — it
already fails open (never blocks a trade decision) and is called far less
often than the bulk scan, so residual yfinance flakiness there is a minor,
non-critical inconvenience rather than a silent outage.

News (get_recent_news, below) uses Alpaca's official News API (Benzinga-
sourced) — this replaced an unofficial Finviz scraper (finviz_source.py,
now deleted) for the same "unofficial scraper is a silent failure risk"
reason yfinance was replaced for price data. Earnings-date lookups also
still use yfinance (trader.check_earnings, signals.fetch_earnings_growth)
since Alpaca has no earnings-calendar endpoint — same fail-open treatment.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

import config

logger = logging.getLogger(__name__)

_COLUMN_MAP = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}

# Alpaca's bulk bars endpoint has a practical symbol-count limit per request.
_CHUNK_SIZE = 200


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )


def _period_to_days(period: str) -> int:
    """Convert a yfinance-style period string ('120d') to an integer day count."""
    period = period.strip().lower()
    if period.endswith("d"):
        return int(period[:-1])
    if period.endswith("mo"):
        return int(period[:-2]) * 31
    if period.endswith("y"):
        return int(period[:-1]) * 365
    return 120


def get_daily_bars(
    tickers: List[str],
    period: str = None,
    retries: int = 2,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars for a list of tickers via Alpaca, chunked to stay
    within API limits, with retry on transient failures.

    Returns { "AAPL": DataFrame[Open,High,Low,Close,Volume], ... } — only
    tickers with data are included, matching the old yfinance-based contract
    so callers (scanner.py, signals.py) need minimal changes.
    """
    if not tickers:
        return {}

    days = _period_to_days(period or config.DATA_PERIOD)
    # Fetch a bit of buffer beyond calendar days requested to account for
    # weekends/holidays reducing trading-day count.
    start = datetime.utcnow() - timedelta(days=int(days * 1.6) + 5)
    end = datetime.utcnow()

    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = _data_client()
    result: Dict[str, pd.DataFrame] = {}

    def _fetch_chunk(chunk: List[str], depth: int = 0) -> None:
        """
        Fetch one chunk; on failure (e.g. a single invalid/delisted symbol
        poisoning the whole batch — confirmed with Alpaca: it rejects the
        entire request rather than skipping the bad symbol), bisect and
        retry each half so one bad ticker never costs us the other ~199
        good ones in the same chunk. Bottoms out at single-symbol requests,
        which are simply dropped (logged) if still failing.
        """
        if not chunk:
            return
        attempt = 0
        while attempt <= retries:
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    feed="iex" if config.ALPACA_DATA_FEED == "iex" else "sip",
                )
                bars = client.get_stock_bars(req)
                df_all = bars.df
                if df_all is None or df_all.empty:
                    return
                if isinstance(df_all.index, pd.MultiIndex):
                    for sym in chunk:
                        if sym in df_all.index.get_level_values(0):
                            sub = df_all.xs(sym, level=0).copy()
                            result[sym] = _normalize(sub)
                else:
                    result[chunk[0]] = _normalize(df_all.copy())
                return
            except Exception as e:
                attempt += 1
                if attempt > retries:
                    if len(chunk) == 1:
                        logger.warning(f"Alpaca bars: dropping invalid/unavailable symbol {chunk[0]}: {e}")
                        return
                    mid = len(chunk) // 2
                    logger.info(
                        f"Alpaca bars: chunk of {len(chunk)} failed after {retries} retries "
                        f"({e}) — bisecting to isolate the bad symbol(s)."
                    )
                    _fetch_chunk(chunk[:mid], depth + 1)
                    _fetch_chunk(chunk[mid:], depth + 1)
                    return
                else:
                    time.sleep(1.5 * attempt)

    chunks = [tickers[i:i + _CHUNK_SIZE] for i in range(0, len(tickers), _CHUNK_SIZE)]
    for chunk in chunks:
        _fetch_chunk(chunk)

    logger.info(f"market_data.get_daily_bars: {len(result)}/{len(tickers)} tickers returned data.")
    return result


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=_COLUMN_MAP)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep]
    df.index = pd.to_datetime(df.index.get_level_values(0) if isinstance(df.index, pd.MultiIndex) else df.index)
    df.index.name = "Date"
    df.dropna(how="all", inplace=True)
    return df


def get_single_ticker_bars(ticker: str, period: str = None) -> Optional[pd.DataFrame]:
    """Convenience wrapper for a single ticker (used by signals.py)."""
    data = get_daily_bars([ticker], period=period)
    return data.get(ticker)


def get_latest_price(ticker: str) -> Optional[float]:
    """Real-time (free-tier delayed) latest trade price via Alpaca."""
    try:
        from alpaca.data import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        client = _data_client()
        req = StockLatestTradeRequest(symbol_or_symbols=ticker)
        trade = client.get_stock_latest_trade(req)
        if ticker in trade:
            return round(float(trade[ticker].price), 2)
    except Exception as e:
        logger.warning(f"Alpaca latest price fetch failed for {ticker}: {e}")
    return None


def get_spy_trend() -> tuple:
    """
    Returns (safe, spy_close) — safe=True if SPY is above its 50-day MA.
    Replaces the yfinance-based check in trader.py with the same Alpaca client
    already used for everything else (SPY is a normal tradable equity).
    """
    try:
        data = get_daily_bars(["SPY"], period="90d")
        df = data.get("SPY")
        if df is None or df.empty:
            logger.warning("SPY data unavailable — assuming safe (fail-open).")
            return True, 0.0
        close = df["Close"].dropna()
        ma50 = close.rolling(50).mean().dropna()
        if ma50.empty:
            return True, 0.0
        spy_now = float(close.iloc[-1])
        ma_now = float(ma50.iloc[-1])
        safe = spy_now >= ma_now
        logger.info(f"SPY={spy_now:.2f} 50MA={ma_now:.2f} safe={safe}")
        return safe, round(spy_now, 2)
    except Exception as e:
        logger.error(f"SPY trend check failed: {e}")
        return True, 0.0


def get_recent_news(
    ticker: str,
    end_date: Optional[datetime] = None,
    lookback_days: int = 14,
    limit: int = 10,
) -> List[dict]:
    """
    Fetch recent news headlines for a ticker via Alpaca's News API
    (Benzinga-sourced, official/authenticated — replaces the old Finviz
    scraper entirely, including for backtesting: `end_date` lets us query
    only news that existed as of a given historical date, avoiding the
    lookahead-bias bug we hit with the earnings-calendar check).

    Returns a list of {"title": str, "date": str, "url": str} — same shape
    the AI sentiment layer (ai_signal.py) already expects, so no changes
    needed there beyond the source swap.

    end_date=None means "up to now" (live usage). For backtests, pass the
    simulated date so the AI only ever sees news that existed at that point
    in time.
    """
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        end = end_date or datetime.utcnow()
        start = end - timedelta(days=lookback_days)

        client = NewsClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
        )
        req = NewsRequest(symbols=ticker, start=start, end=end, limit=limit)
        result = client.get_news(req)

        items = result.news if hasattr(result, "news") else result.data.get("news", [])
        headlines = []
        for item in items:
            title = getattr(item, "headline", None) or (item.get("headline") if isinstance(item, dict) else None)
            created = getattr(item, "created_at", None) or (item.get("created_at") if isinstance(item, dict) else None)
            url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
            if not title:
                continue
            headlines.append({
                "title": title,
                "date": str(created)[:10] if created else None,
                "url": url,
            })
        return headlines
    except Exception as e:
        logger.warning(f"Alpaca news fetch failed for {ticker}: {e}")
        return []
