"""
market_data.py — Alpaca-based daily OHLCV data provider.

Replaces yfinance for all hot-path data fetching (bulk scanner, per-ticker
signal analysis, SPY trend check). This is the fix for the root cause found
in production: yfinance's unofficial Yahoo Finance scraping was failing
constantly with "Invalid Crumb / Unauthorized" errors (Yahoo's anti-bot
cookie auth), silently breaking every scan for days with zero trades placed.

Alpaca's Market Data API is a real, documented, authenticated API (using the
same keys already used for trading) — no cookie-scraping fragility.

yfinance is kept ONLY for two low-frequency, non-blocking, supplementary
lookups that Alpaca's free tier doesn't provide (earnings calendar/growth,
VIX index level) — both already fail open (never block a trade decision) and
are called far less often than the bulk scan, so residual yfinance flakiness
there is a minor, non-critical inconvenience rather than a silent outage.
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

    chunks = [tickers[i:i + _CHUNK_SIZE] for i in range(0, len(tickers), _CHUNK_SIZE)]
    for chunk in chunks:
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
                    break
                # df_all is MultiIndex (symbol, timestamp) when multiple symbols
                if isinstance(df_all.index, pd.MultiIndex):
                    for sym in chunk:
                        if sym in df_all.index.get_level_values(0):
                            sub = df_all.xs(sym, level=0).copy()
                            result[sym] = _normalize(sub)
                else:
                    # Single symbol in chunk
                    result[chunk[0]] = _normalize(df_all.copy())
                break
            except Exception as e:
                attempt += 1
                if attempt > retries:
                    logger.error(
                        f"Alpaca bars fetch failed for chunk of {len(chunk)} tickers "
                        f"after {retries} retries: {e}"
                    )
                else:
                    logger.warning(f"Alpaca bars fetch error (retry {attempt}/{retries}): {e}")
                    time.sleep(1.5 * attempt)

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
