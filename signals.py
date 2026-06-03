import logging
import math
import os
from typing import Optional, List
import pandas as pd
import yfinance as yf
import ta as ta_lib

import config

logger = logging.getLogger(__name__)

# ── In-memory asset cache ─────────────────────────────────────────────────────
_asset_cache: List[dict] = []

def load_asset_cache() -> None:
    """
    Pre-load all tradable US equity assets from Alpaca into memory.
    Call this once at bot startup. All searches are then instant.
    """
    global _asset_cache
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass

        api_key    = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            return

        client = TradingClient(api_key, secret_key, paper=True)
        assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY))
        _asset_cache = [
            {"ticker": a.symbol, "name": a.name}
            for a in assets if a.tradable
        ]
        logger.info(f"Asset cache loaded: {len(_asset_cache)} tradable US equities.")
    except Exception as e:
        logger.warning(f"Could not load asset cache: {e}")


def search_tickers(query: str, max_results: int = 10) -> list:
    """
    Search cached assets by ticker or name. Instant after cache is loaded.
    Falls back to a live Alpaca call if cache is empty.
    """
    global _asset_cache
    if not _asset_cache:
        load_asset_cache()

    q = query.lower()
    results = [
        a for a in _asset_cache
        if q in a["ticker"].lower() or q in a["name"].lower()
    ]
    results.sort(key=lambda x: (x["ticker"].lower() != q, x["ticker"]))
    return results[:max_results]


def get_company_name(ticker: str) -> Optional[str]:
    """Look up a single ticker's company name from Alpaca."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass

        api_key    = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            return None

        client = TradingClient(api_key, secret_key, paper=True)
        asset  = client.get_asset(ticker)
        return asset.name if asset else None
    except Exception as e:
        logger.warning(f"Could not fetch company name for {ticker}: {e}")
        return None


def fetch_realtime_price(ticker: str) -> tuple:
    """
    Fetch price via Alpaca (free tier, 15-min delayed).
    Falls back to IBKR only if Alpaca is unavailable.
    Returns (price, source) where source is 'ibkr', 'alpaca', or None.
    """
    # ── Alpaca first (avoids 10s IBKR timeout when IB Gateway is not running) ─
    try:
        from alpaca.data import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        api_key    = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if api_key and secret_key:
            client  = StockHistoricalDataClient(api_key, secret_key)
            request = StockLatestTradeRequest(symbol_or_symbols=ticker)
            trade   = client.get_stock_latest_trade(request)
            if ticker in trade:
                return (round(float(trade[ticker].price), 2), "alpaca")
    except Exception as e:
        logger.warning(f"Alpaca price fetch failed for {ticker}: {e}")

    # ── IBKR fallback (only if Alpaca failed) ─────────────────────────────────
    try:
        import ibkr as ibkr_module
        price = ibkr_module.get_price(ticker)
        if price:
            return (price, "ibkr")
    except Exception:
        pass

    return (None, None)


def fetch_ticker_data(ticker: str) -> Optional[pd.DataFrame]:
    """
    Download daily data for a single ticker.
    Used for on-demand commands like /signal and /chart.
    """
    try:
        df = yf.download(
            tickers=ticker,
            period=config.DATA_PERIOD,
            interval=config.DATA_INTERVAL,
            auto_adjust=True,
            progress=False,
        )
        # Flatten MultiIndex columns if present (newer yfinance versions)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(how="all", inplace=True)
        if len(df) < 55:
            logger.warning(f"Not enough data for {ticker} ({len(df)} rows).")
            return None
        return df
    except Exception as e:
        logger.error(f"Failed to fetch data for {ticker}: {e}")
        return None


def analyse(ticker: str, df: pd.DataFrame, company_name: Optional[str] = None) -> dict:
    """
    Run RSI + MA analysis on a daily DataFrame.

    Returns a dict:
      {
        "ticker":          str,
        "price":           float,
        "rsi":             float,
        "ma_fast":         float,   # 20-day MA
        "ma_slow":         float,   # 50-day MA
        "signal":          "BUY" | "SELL" | "NONE",
        "rsi_signal":      bool,    # RSI crossed threshold
        "ma_crossover":    bool,    # MA crossover occurred today
        "crossover_dir":   None,  # "UP", "DOWN", or None
        "reason":          str,     # Human-readable explanation
      }
    """
    result = {
        "ticker":        ticker,
        "company_name":  company_name or get_company_name(ticker),
        "price":         None,
        "rsi":           None,
        "ma_fast":       None,
        "ma_slow":       None,
        "signal":        "NONE",
        "rsi_signal":    False,
        "ma_crossover":  False,
        "crossover_dir": None,
        "reason":        "",
        "last_candle":   None,   # Date of the most recent daily candle
    }

    try:
        # ── Price + last candle date ──────────────────────────────────────────
        eod_price = round(float(df["Close"].iloc[-1]), 2)
        realtime_price, price_source = fetch_realtime_price(ticker)
        result["price"]        = realtime_price if realtime_price else eod_price
        result["price_source"] = price_source  # 'ibkr', 'alpaca', or None
        result["realtime"]     = realtime_price is not None
        result["last_candle"] = str(df.index[-1].date())

        # ── RSI ───────────────────────────────────────────────────────────────
        rsi_series = ta_lib.momentum.RSIIndicator(df["Close"], window=config.RSI_PERIOD).rsi()
        if rsi_series is None or rsi_series.dropna().empty:
            result["reason"] = "RSI could not be calculated."
            return result

        rsi = float(rsi_series.dropna().iloc[-1])
        result["rsi"] = round(rsi, 1)

        # ── Moving averages ───────────────────────────────────────────────────
        ma_fast_series = df["Close"].rolling(window=config.MA_FAST).mean()
        ma_slow_series = df["Close"].rolling(window=config.MA_SLOW).mean()

        if ma_fast_series is None or ma_slow_series is None:
            result["reason"] = "MA could not be calculated."
            return result

        ma_fast_clean = ma_fast_series.dropna()
        ma_slow_clean = ma_slow_series.dropna()

        if len(ma_fast_clean) < 2 or len(ma_slow_clean) < 2:
            result["reason"] = "Not enough MA data."
            return result

        ma_fast_now  = float(ma_fast_clean.iloc[-1])
        ma_fast_prev = float(ma_fast_clean.iloc[-2])
        ma_slow_now  = float(ma_slow_clean.iloc[-1])
        ma_slow_prev = float(ma_slow_clean.iloc[-2])

        result["ma_fast"] = round(ma_fast_now, 2)
        result["ma_slow"] = round(ma_slow_now, 2)

        # ── MA crossover detection ────────────────────────────────────────────
        # Golden cross: fast crossed ABOVE slow (was below yesterday)
        golden_cross = (ma_fast_prev < ma_slow_prev) and (ma_fast_now >= ma_slow_now)
        # Death cross:  fast crossed BELOW slow (was above yesterday)
        death_cross  = (ma_fast_prev > ma_slow_prev) and (ma_fast_now <= ma_slow_now)

        if golden_cross:
            result["ma_crossover"]  = True
            result["crossover_dir"] = "UP"
        elif death_cross:
            result["ma_crossover"]  = True
            result["crossover_dir"] = "DOWN"

        # ── Signal logic (RSI + MA must agree) ───────────────────────────────
        rsi_buy  = rsi < config.RSI_BUY_THRESHOLD
        rsi_sell = rsi > config.RSI_SELL_THRESHOLD

        result["rsi_signal"] = rsi_buy or rsi_sell

        if rsi_buy and (golden_cross or ma_fast_now > ma_slow_now):
            result["signal"] = "BUY"
            result["reason"] = (
                f"Oversold (RSI {rsi:.1f}) with "
                + ("golden cross " if golden_cross else "")
                + f"20MA ({ma_fast_now:.2f}) above 50MA ({ma_slow_now:.2f})"
            )

        elif rsi_sell and (death_cross or ma_fast_now < ma_slow_now):
            result["signal"] = "SELL"
            result["reason"] = (
                f"Overbought (RSI {rsi:.1f}) with "
                + ("death cross " if death_cross else "")
                + f"20MA ({ma_fast_now:.2f}) below 50MA ({ma_slow_now:.2f})"
            )

        else:
            parts = []
            if rsi_buy:
                parts.append(f"RSI oversold ({rsi:.1f}) but MA not confirming")
            elif rsi_sell:
                parts.append(f"RSI overbought ({rsi:.1f}) but MA not confirming")
            else:
                parts.append(f"RSI neutral ({rsi:.1f})")
            result["reason"] = ". ".join(parts)

    except Exception as e:
        logger.error(f"Analysis failed for {ticker}: {e}")
        result["reason"] = f"Error: {e}"

    return result


def calculate_position_size(
    entry_price: float,
    atr: float,
    atr_target_mult: float,
    profit_target: float = 40.0,
    portfolio_value: float = 5000.0,
    hard_cap_pct: float = 0.15,
    min_shares: int = 3,
) -> int:
    """
    Return the number of shares to buy so that a winning trade (price moves
    ATR × atr_target_mult) earns exactly profit_target dollars.

    Hard-capped at hard_cap_pct of portfolio_value.
    Returns 0 if the stock is too expensive to buy min_shares within the cap
    (caller should skip the trade — do NOT force min 1 share).

    Example — cheap stock:
        $8 stock, ATR=$0.50, mult=6.0, target=$40
        → atr_dollars = $3.00/share → shares_needed = ceil(40/3.00) = 14
        → cap = $5,000 × 15% = $750 → max_by_cap = 93 shares
        → result = min(14, 93) = 14 shares  ✅

    Example — AZO at $3,000 (hard-filtered in scanner, but belt+suspenders):
        $3,000 stock, ATR=$50, mult=6.0, target=$40
        → atr_dollars = $300/share → shares_needed = ceil(40/300) = 1
        → cap = $5,000 × 15% = $750 → max_by_cap = int(750/3000) = 0
        → 0 < min_shares (3) → return 0  ❌ caller skips this trade
    """
    if atr <= 0 or atr_target_mult <= 0 or entry_price <= 0:
        return 0

    atr_dollars_per_share = atr * atr_target_mult

    # Shares needed to hit profit_target at the ATR-based exit
    shares_needed = math.ceil(profit_target / atr_dollars_per_share)

    # Never exceed hard_cap_pct of portfolio in one position
    max_shares_by_cap = int((portfolio_value * hard_cap_pct) / entry_price)

    shares = min(shares_needed, max_shares_by_cap)

    # If we can't buy at least min_shares, the position is not worth taking.
    if shares < min_shares:
        return 0

    return shares


def run_signal_check(watchlist: List[dict]) -> List[dict]:    """
    Run signal analysis on all stocks in the watchlist.
    Fetches fresh daily data for each stock and returns a list of analysis dicts.
    Only returns stocks with a BUY or SELL signal.
    """
    signals = []
    for stock in watchlist:
        ticker = stock["ticker"]
        df = fetch_ticker_data(ticker)
        if df is None:
            continue
        analysis = analyse(ticker, df)
        if analysis["signal"] in ("BUY", "SELL"):
            signals.append(analysis)
            logger.info(f"Signal: {analysis['signal']} {ticker} — {analysis['reason']}")

    return signals
