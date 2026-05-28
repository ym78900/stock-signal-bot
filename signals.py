import logging
from typing import Optional, List
import pandas as pd
import yfinance as yf
import ta as ta_lib

import config

logger = logging.getLogger(__name__)


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


def analyse(ticker: str, df: pd.DataFrame) -> dict:
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
        result["price"] = round(float(df["Close"].iloc[-1]), 2)
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


def run_signal_check(watchlist: List[dict]) -> List[dict]:
    """
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
