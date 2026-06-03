import logging
import pickle
from datetime import date
from pathlib import Path
from typing import Optional, List, Dict
import pandas as pd
import yfinance as yf
import ta as ta_lib

import config

logger = logging.getLogger(__name__)

# ── Daily data cache (reused within the same calendar day) ───────────────────

_DAILY_CACHE_FILE = Path(__file__).parent / "daily_data_cache.pkl"

def _load_daily_cache() -> Optional[Dict[str, pd.DataFrame]]:
    """Return today's cached data if it exists, else None."""
    try:
        if _DAILY_CACHE_FILE.exists():
            with open(_DAILY_CACHE_FILE, "rb") as f:
                cached_date, data = pickle.load(f)
            if cached_date == date.today():
                logger.info(f"Daily cache hit — {len(data)} tickers loaded from disk.")
                return data
    except Exception as e:
        logger.warning(f"Could not read daily cache: {e}")
    return None

def _save_daily_cache(data: Dict[str, pd.DataFrame]) -> None:
    try:
        with open(_DAILY_CACHE_FILE, "wb") as f:
            pickle.dump((date.today(), data), f)
        logger.info(f"Daily cache saved — {len(data)} tickers.")
    except Exception as e:
        logger.warning(f"Could not save daily cache: {e}")

# ── S&P 500 ticker list ───────────────────────────────────────────────────────

_sp500_cache: Optional[List[str]] = None
_sp500_names: Dict[str, str] = {}  # { "NVDA": "NVIDIA Corporation", ... }


def get_company_name(ticker: str) -> str:
    """Return the full company name for a ticker, or just the ticker if not found."""
    return _sp500_names.get(ticker, ticker)


def get_sp500_tickers() -> List[str]:
    """
    Fetch the current S&P 500 ticker list from Wikipedia.
    Also populates the company name cache.
    Result is cached in memory so we only hit Wikipedia once per run.
    """
    global _sp500_cache, _sp500_names
    if _sp500_cache is not None:
        return _sp500_cache

    logger.info("Fetching S&P 500 ticker list from Wikipedia...")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; stock-signal-bot/1.0)"}
        import requests, io
        response = requests.get(config.SP500_WIKIPEDIA_URL, headers=headers, timeout=15)
        response.raise_for_status()
        tables = pd.read_html(io.StringIO(response.text))
        table = tables[0]
        tickers = table["Symbol"].tolist()
        names   = table["Security"].tolist()

        # Some tickers on Wikipedia use a dot (e.g. BRK.B) but yfinance needs a dash (BRK-B)
        tickers = [t.replace(".", "-") for t in tickers]

        _sp500_names = {t: n for t, n in zip(tickers, names)}
        _sp500_cache = tickers
        logger.info(f"Loaded {len(tickers)} S&P 500 tickers.")
        return tickers
    except Exception as e:
        logger.error(f"Failed to fetch S&P 500 list: {e}")
        return []


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_data(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """
    Download daily OHLCV data for all tickers in one bulk request.
    Results are cached to disk for the current calendar day — subsequent
    calls (e.g. /testrun) reuse the cache instead of re-downloading.
    Returns a dict: { "AAPL": DataFrame, "MSFT": DataFrame, ... }
    Only includes tickers that have sufficient data.
    """
    cached = _load_daily_cache()
    if cached is not None:
        return cached

    logger.info(f"Downloading daily data for {len(tickers)} tickers...")
    try:
        raw = yf.download(
            tickers=tickers,
            period=config.DATA_PERIOD,
            interval=config.DATA_INTERVAL,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"yfinance download failed: {e}")
        return {}

    result = {}
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                df = raw[ticker].copy()

            # Flatten MultiIndex columns if present (newer yfinance versions)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df.dropna(how="all", inplace=True)

            # Need at least 55 rows for a reliable 50-day MA
            if len(df) >= 55:
                result[ticker] = df
        except Exception:
            pass

    logger.info(f"Usable data for {len(result)} tickers.")
    _save_daily_cache(result)
    return result


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_stock(ticker: str, df: pd.DataFrame) -> Optional[dict]:
    """
    Score a single stock on three dimensions:
      - Volume: today's volume vs 20-day average
      - RSI:    how close RSI is to the buy (30) or sell (70) zone
      - Momentum: % price change over the last 5 days
    Returns a dict with the score and raw values, or None if data is invalid.
    """
    try:
        # RSI
        rsi_series = ta_lib.momentum.RSIIndicator(df["Close"], window=config.RSI_PERIOD).rsi()
        if rsi_series is None or rsi_series.dropna().empty:
            return None
        rsi = float(rsi_series.dropna().iloc[-1])

        # Volume ratio
        vol_avg = df["Volume"].iloc[-config.VOLUME_AVG_DAYS:].mean()
        vol_today = float(df["Volume"].iloc[-1])
        if vol_avg == 0:
            return None
        volume_ratio = vol_today / vol_avg

        # Momentum (5-day % change)
        if len(df) < config.MOMENTUM_DAYS + 1:
            return None
        price_now  = float(df["Close"].iloc[-1])
        price_then = float(df["Close"].iloc[-config.MOMENTUM_DAYS - 1])
        if price_then == 0:
            return None
        momentum_pct = ((price_now - price_then) / price_then) * 100

        # ── Score each dimension (all normalised to 0–1) ──────────────────────

        # Volume score: cap at 5x average → maps to 1.0
        volume_score = min(volume_ratio / 5.0, 1.0)

        # RSI score: distance from the neutral midpoint (50)
        # RSI of 30 → distance = 20 → score = 1.0
        # RSI of 70 → distance = 20 → score = 1.0
        # RSI of 50 → distance = 0  → score = 0.0
        rsi_distance = abs(rsi - 50)
        rsi_score = min(rsi_distance / 20.0, 1.0)

        # Momentum score: absolute % move, cap at 10% → 1.0
        momentum_score = min(abs(momentum_pct) / 10.0, 1.0)

        # ── Weighted composite score ──────────────────────────────────────────
        composite = (
            config.WEIGHT_VOLUME   * volume_score +
            config.WEIGHT_RSI      * rsi_score    +
            config.WEIGHT_MOMENTUM * momentum_score
        )

        return {
            "ticker":        ticker,
            "company_name":  get_company_name(ticker),
            "score":         round(composite, 4),
            "rsi":           round(rsi, 1),
            "volume_ratio":  round(volume_ratio, 2),
            "momentum_pct":  round(momentum_pct, 2),
            "price":         round(price_now, 2),
        }

    except Exception as e:
        logger.debug(f"Scoring failed for {ticker}: {e}")
        return None


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_morning_scan() -> List[dict]:
    """
    Full morning scan:
      1. Fetch S&P 500 tickers
      2. Download daily data for all of them
      3. Score each stock
      4. Return the top N ranked stocks
    """
    tickers = get_sp500_tickers()
    if not tickers:
        logger.error("No tickers available — aborting scan.")
        return []

    data = fetch_data(tickers)
    if not data:
        logger.error("No data returned from yfinance — aborting scan.")
        return []

    scores = []
    for ticker, df in data.items():
        result = _score_stock(ticker, df)
        if result:
            scores.append(result)

    # Sort descending by composite score
    scores.sort(key=lambda x: x["score"], reverse=True)

    top = scores[:config.TOP_N_STOCKS]
    logger.info(f"Top {len(top)} stocks: {[s['ticker'] for s in top]}")
    return top


# ── Auto-trading scan (all 503 tickers, used at 11:15 PM) ────────────────────

def run_auto_scan() -> list:
    """
    Scan ALL S&P 500 stocks for BUY signals using the confirmed swing parameters:
      - RSI < RSI_BUY_THRESHOLD (38)
      - Volume > VOLUME_CONFIRMATION_RATIO × 20-day avg (1.2×)
      - ATR computed for position sizing

    Called from job_signal_check at 11:15 PM Finnish after market close.
    Returns list of signal dicts — one per qualifying stock.
    """
    import ta as ta_lib

    tickers = get_sp500_tickers()
    if not tickers:
        logger.error("Auto-scan: no tickers available.")
        return []

    logger.info(f"Auto-scan: downloading data for {len(tickers)} tickers...")
    data = fetch_data(tickers)
    if not data:
        logger.error("Auto-scan: no data returned.")
        return []

    signals = []
    for ticker, df in data.items():
        try:
            # ── RSI ───────────────────────────────────────────────────────────
            rsi_series = ta_lib.momentum.RSIIndicator(
                df["Close"], window=config.RSI_PERIOD
            ).rsi().dropna()
            if rsi_series.empty:
                continue
            rsi = float(rsi_series.iloc[-1])
            if rsi >= config.RSI_BUY_THRESHOLD:
                continue   # not oversold

            # ── Volume confirmation ───────────────────────────────────────────
            vol_avg = df["Volume"].iloc[-config.VOLUME_AVG_DAYS:].mean()
            if vol_avg == 0:
                continue
            vol_ratio = float(df["Volume"].iloc[-1]) / vol_avg
            if vol_ratio < config.VOLUME_CONFIRMATION_RATIO:
                continue   # low volume — skip

            # ── ATR ───────────────────────────────────────────────────────────
            atr_series = ta_lib.volatility.AverageTrueRange(
                df["High"], df["Low"], df["Close"], window=config.ATR_PERIOD
            ).average_true_range().dropna()
            if atr_series.empty:
                continue
            atr = float(atr_series.iloc[-1])
            if atr <= 0:
                continue

            close_price = float(df["Close"].iloc[-1])
            signal_date = df.index[-1].date()

            signals.append({
                "ticker":       ticker,
                "signal_date":  signal_date,
                "close_price":  round(close_price, 4),
                "rsi":          round(rsi, 1),
                "atr":          round(atr, 4),
                "volume_ratio": round(vol_ratio, 2),
                "stop_est":     round(close_price - atr * config.ATR_STOP_MULTIPLIER, 2),
                "target_est":   round(close_price + atr * config.ATR_TARGET_MULTIPLIER, 2),
            })

        except Exception as e:
            logger.debug(f"Auto-scan: error on {ticker}: {e}")
            continue

    logger.info(f"Auto-scan complete: {len(signals)} BUY signal(s) found.")
    return signals
