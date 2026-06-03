"""
backtester.py — Backtest the RSI + MA crossover strategy against 2 years of
historical S&P 500 daily data, with Phase 3 confirmation filters and
Phase 3e+ improvement experiments.

Run directly:
    /Library/Developer/CommandLineTools/usr/bin/python3.9 backtester.py

How it works:
1. Downloads 2 years of daily data for all S&P 500 stocks ONCE — cached to disk
2. Computes all indicators ONCE per ticker per day — cached
3. Precomputes exit outcomes for all ATR combos — cached
4. Optimizer runs each combination in milliseconds

Tested filters (Phase 3):
- Volume filter:    today's volume > N × 20-day avg volume
- SPY trend filter: SPY price above its 50-day MA on signal day
- MACD filter:      ❌ incompatible with oversold RSI — produces 0 trades

Improvement experiments (Phase 3e+):
- RSI rising:        RSI today > RSI yesterday (reversal confirmed, not still falling)
- Price above 200MA: only buy stocks in long-term uptrend (no falling knives)
- Consecutive RSI:   RSI was also oversold yesterday (deeper confirmation)
- Volume 1.5×:       stricter volume threshold
- Min price $10:     exclude thin/cheap stocks
- ATR% filter:       skip boring stocks (ATR/price < 1%) and hyper-volatile (> 8%)
"""

import logging
import sys
import os
import pickle
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import pandas as pd
import yfinance as yf
import ta as ta_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from scanner import get_sp500_tickers, get_extended_tickers, get_full_market_tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

BACKTEST_YEARS         = 2
ATR_PERIOD             = 14
MA_200                 = 200
RSI_PERIOD             = config.RSI_PERIOD
MA_FAST                = config.MA_FAST
MA_SLOW                = config.MA_SLOW
MAX_OPEN_POSITIONS     = 5
CONSECUTIVE_LOSS_LIMIT = 3
STARTING_CAPITAL       = 5000.0
MAX_POSITION_PCT       = 0.10
IBKR_FEE_PER_TRADE     = 1.0

# Bump when precompute_signals() output schema changes → cache regenerates automatically
INDICATORS_VERSION = 4   # v4: added bb_lower_pct, ma200_rising

CACHE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache.pkl")
INDICATORS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_indicators.pkl")
EXITS_CACHE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_exits.pkl")
SPY_CACHE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_spy.pkl")
VIX_CACHE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_vix.pkl")

# Extended universe (S&P 500 + NASDAQ-100) — separate caches to avoid invalidating Round 1-4 results
EXTENDED_CACHE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache_extended.pkl")
EXTENDED_INDICATORS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_indicators_extended.pkl")
EXTENDED_EXITS_CACHE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_exits_extended.pkl")

# Full market universe (NYSE + NASDAQ, ~6,500 raw → ~2,000 after price/vol filter)
FULL_MARKET_CACHE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache_full_market.pkl")
FULL_MARKET_INDICATORS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_indicators_full_market.pkl")
FULL_MARKET_EXITS_CACHE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_exits_full_market.pkl")


# ── Step 1: Download & cache raw price data ────────────────────────────────────

def load_or_download_data(tickers: List[str], cache_file: str = None,
                          batch_size: int = 500) -> Dict[str, pd.DataFrame]:
    """
    Load price data from cache, downloading only missing tickers if cache exists.
    Downloads in batches of batch_size to avoid OS thread-limit errors on large universes.
    Pass cache_file to use an alternate cache (e.g. for extended universe).
    """
    if cache_file is None:
        cache_file = CACHE_FILE

    existing: Dict[str, pd.DataFrame] = {}
    if os.path.exists(cache_file):
        logger.info(f"Loading data from cache ({os.path.basename(cache_file)})...")
        with open(cache_file, "rb") as f:
            existing = pickle.load(f)

    missing = [t for t in tickers if t not in existing]

    if not missing:
        logger.info(f"Cache hit — {len(existing)} tickers, no new downloads needed.")
        return existing

    end_date   = datetime.today()
    start_date = end_date - timedelta(days=BACKTEST_YEARS * 365 + 300)
    min_rows   = MA_200 + ATR_PERIOD + 10
    newly_added = 0

    # Download in batches to avoid hitting OS thread limits on large universes
    batches = [missing[i:i + batch_size] for i in range(0, len(missing), batch_size)]
    logger.info(f"Downloading {len(missing)} new tickers in {len(batches)} batches "
                f"({start_date.date()} → {end_date.date()})...")

    for batch_num, batch in enumerate(batches, 1):
        logger.info(f"  Batch {batch_num}/{len(batches)}: {len(batch)} tickers...")
        try:
            raw = yf.download(
                tickers=batch,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            logger.warning(f"  Batch {batch_num} download error: {e} — skipping batch")
            continue

        for ticker in batch:
            try:
                df = raw[ticker].copy() if len(batch) > 1 else raw.copy()
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.dropna(subset=["Close"], inplace=True)
                if len(df) >= min_rows:
                    existing[ticker] = df
                    newly_added += 1
            except Exception:
                continue

        # Save progress after each batch so a crash doesn't lose everything
        with open(cache_file, "wb") as f:
            pickle.dump(existing, f)

    logger.info(f"Added {newly_added} new tickers. Total cache size: {len(existing)}.")
    return existing


# ── Step 1b: SPY trend data ────────────────────────────────────────────────────

def load_spy_trend() -> Dict[pd.Timestamp, dict]:
    """
    Returns a dict keyed by date.
    Each value: {
        "above_50ma":  bool,
        "above_100ma": bool,
        "above_200ma": bool,
    }
    Backward-compatible: callers that check spy_trend[date] as bool still work
    because the dict is truthy when the stock is above ANY MA.
    """
    if os.path.exists(SPY_CACHE):
        logger.info("Loading SPY trend data from cache...")
        with open(SPY_CACHE, "rb") as f:
            cached = pickle.load(f)
        # If old cache format (values are bools), delete and rebuild
        first_val = next(iter(cached.values()), None)
        if isinstance(first_val, bool):
            logger.info("Old SPY cache detected (bool values) — rebuilding with 50/100/200MA...")
            os.remove(SPY_CACHE)
        else:
            return cached

    logger.info("Downloading SPY data for trend filter (50/100/200MA)...")
    end_date   = datetime.today()
    start_date = end_date - timedelta(days=BACKTEST_YEARS * 365 + 300)

    spy_raw = yf.download(
        tickers=["SPY"],
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=True,
        progress=False,
    )
    if isinstance(spy_raw.columns, pd.MultiIndex):
        spy_raw.columns = spy_raw.columns.get_level_values(0)

    spy_raw = spy_raw.reset_index()
    spy_raw["ma50"]  = spy_raw["Close"].rolling(window=50).mean()
    spy_raw["ma100"] = spy_raw["Close"].rolling(window=100).mean()
    spy_raw["ma200"] = spy_raw["Close"].rolling(window=200).mean()
    spy_raw.dropna(subset=["ma50", "ma100", "ma200"], inplace=True)

    date_col = "Date" if "Date" in spy_raw.columns else spy_raw.columns[0]
    trend = {}
    for _, row in spy_raw.iterrows():
        trend[row[date_col]] = {
            "above_50ma":  bool(row["Close"] >= row["ma50"]),
            "above_100ma": bool(row["Close"] >= row["ma100"]),
            "above_200ma": bool(row["Close"] >= row["ma200"]),
        }

    logger.info(f"SPY trend (50/100/200MA) computed for {len(trend)} days. Saving cache...")
    with open(SPY_CACHE, "wb") as f:
        pickle.dump(trend, f)
    return trend


# ── Step 1c: VIX data ──────────────────────────────────────────────────────────

def load_vix() -> Dict[pd.Timestamp, float]:
    """Returns dict of {date: vix_close} for the backtest window."""
    if os.path.exists(VIX_CACHE):
        logger.info("Loading VIX data from cache...")
        with open(VIX_CACHE, "rb") as f:
            return pickle.load(f)

    logger.info("Downloading VIX data...")
    end_date   = datetime.today()
    start_date = end_date - timedelta(days=BACKTEST_YEARS * 365 + 300)

    vix_raw = yf.download(
        tickers=["^VIX"],
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=True,
        progress=False,
    )
    if isinstance(vix_raw.columns, pd.MultiIndex):
        vix_raw.columns = vix_raw.columns.get_level_values(0)

    vix_raw = vix_raw.reset_index()
    date_col = "Date" if "Date" in vix_raw.columns else vix_raw.columns[0]
    vix_map = {row[date_col]: float(row["Close"]) for _, row in vix_raw.iterrows() if not pd.isna(row["Close"])}

    logger.info(f"VIX data loaded for {len(vix_map)} days. Saving cache...")
    with open(VIX_CACHE, "wb") as f:
        pickle.dump(vix_map, f)
    return vix_map


# ── Step 2: Precompute all indicators ONCE ─────────────────────────────────────

def precompute_signals(data: Dict[str, pd.DataFrame], indicators_cache: str = None) -> Tuple[List[dict], Dict[str, pd.DataFrame]]:
    """
    Precompute RSI, MA, ATR, volume ratio, MACD, 200MA, prev_rsi, ATR% per row.
    Cached with version check — regenerates automatically when INDICATORS_VERSION bumps.
    Pass indicators_cache to use an alternate cache file (e.g. extended universe).
    """
    cache_path = indicators_cache if indicators_cache is not None else INDICATORS_CACHE
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        version = cached[0] if isinstance(cached[0], int) else 1
        if version == INDICATORS_VERSION:
            logger.info("Loading precomputed indicators from cache...")
            _, rows, processed = cached
            return rows, processed
        logger.info(f"Indicator cache v{version} < v{INDICATORS_VERSION} — regenerating...")

    logger.info("Precomputing indicators (RSI, MA, ATR, Volume, MACD, 200MA, BB, 200MA slope)...")
    rows      = []
    processed = {}

    for ticker, df in data.items():
        try:
            df = df.copy()

            # Core indicators
            df["rsi"]     = ta_lib.momentum.RSIIndicator(df["Close"], window=RSI_PERIOD).rsi()
            df["ma_fast"] = df["Close"].rolling(window=MA_FAST).mean()
            df["ma_slow"] = df["Close"].rolling(window=MA_SLOW).mean()
            df["ma_200"]  = df["Close"].rolling(window=MA_200).mean()
            df["atr"]     = ta_lib.volatility.AverageTrueRange(
                df["High"], df["Low"], df["Close"], window=ATR_PERIOD
            ).average_true_range()

            # Volume ratio
            df["vol_avg"]      = df["Volume"].rolling(window=20).mean()
            df["volume_ratio"] = df["Volume"] / df["vol_avg"]

            # MACD
            macd_obj             = ta_lib.trend.MACD(df["Close"])
            df["macd_line"]      = macd_obj.macd()
            df["macd_sig_line"]  = macd_obj.macd_signal()

            # Bollinger Bands (20-period, 2 std)
            bb_obj          = ta_lib.volatility.BollingerBands(df["Close"], window=20, window_dev=2)
            df["bb_lower"]  = bb_obj.bollinger_lband()
            df["bb_upper"]  = bb_obj.bollinger_hband()

            df.dropna(subset=["rsi", "ma_fast", "ma_slow", "ma_200", "atr",
                               "volume_ratio", "macd_line", "macd_sig_line",
                               "bb_lower", "bb_upper"], inplace=True)
            df = df.reset_index()
            processed[ticker] = df

            for i in range(1, len(df) - 1):
                row      = df.iloc[i]
                prev_row = df.iloc[i - 1]
                next_row = df.iloc[i + 1]

                if pd.isna(next_row["Open"]) or next_row["Open"] <= 0:
                    continue

                date_val      = row.get("Date", row.get("index", None))
                date_next_val = next_row.get("Date", next_row.get("index", None))

                close_price = float(row["Close"])
                atr_val     = float(row["atr"])
                atr_pct     = (atr_val / close_price * 100) if close_price > 0 else 0.0

                ma200_today = float(row["ma_200"])
                ma200_prev  = float(prev_row["ma_200"])

                bb_lower_val = float(row["bb_lower"])
                # How far price is above the lower BB as % of lower BB
                # 0% = price exactly on lower band (maximally oversold per BB)
                # Positive = price above lower band
                bb_lower_pct = ((close_price - bb_lower_val) / bb_lower_val * 100) if bb_lower_val > 0 else 100.0

                macd_cross = (
                    float(prev_row["macd_line"]) < float(prev_row["macd_sig_line"])
                    and float(row["macd_line"]) >= float(row["macd_sig_line"])
                )
                macd_above = float(row["macd_line"]) >= float(row["macd_sig_line"])

                rows.append({
                    "ticker":            ticker,
                    "date":              date_val,
                    "date_idx":          i,
                    # Core signal inputs
                    "rsi":               float(row["rsi"]),
                    "prev_rsi":          float(prev_row["rsi"]),
                    "ma_fast":           float(row["ma_fast"]),
                    "ma_slow":           float(row["ma_slow"]),
                    "prev_ma_fast":      float(prev_row["ma_fast"]),
                    "prev_ma_slow":      float(prev_row["ma_slow"]),
                    "atr":               atr_val,
                    "atr_pct":           round(atr_pct, 3),
                    "open_next":         float(next_row["Open"]),
                    "date_next":         date_next_val,
                    "close_price":       close_price,
                    # Filters
                    "volume_ratio":      float(row["volume_ratio"]),
                    "price_above_200ma": close_price >= ma200_today,
                    "ma200_rising":      ma200_today > ma200_prev,   # v4: 200MA pointing up
                    "bb_lower_pct":      round(bb_lower_pct, 3),     # v4: % above BB lower band
                    "macd_cross":        macd_cross,
                    "macd_above":        macd_above,
                })
        except Exception as e:
            logger.debug(f"Precompute failed for {ticker}: {e}")
            continue

    rows.sort(key=lambda x: x["date"])
    logger.info(f"Precomputed {len(rows)} signal candidates. Saving indicator cache...")
    with open(cache_path, "wb") as f:
        pickle.dump((INDICATORS_VERSION, rows, processed), f)
    return rows, processed


# ── Step 3: Precompute exit outcomes ───────────────────────────────────────────

def precompute_exits(
    rows: List[dict],
    processed: Dict[str, pd.DataFrame],
    atr_stop_values: List[float],
    atr_target_values: List[float],
    max_hold_days: int = 60,
    exits_cache: str = None,
) -> List[dict]:
    cache_path = exits_cache if exits_cache is not None else EXITS_CACHE
    if os.path.exists(cache_path):
        logger.info("Loading precomputed exits from cache...")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    logger.info(f"Precomputing exit outcomes for {len(rows)} candidates × "
                f"{len(atr_stop_values) * len(atr_target_values)} ATR combos...")

    atr_combos = [(s, t) for s in atr_stop_values for t in atr_target_values if t / s >= 1.5]

    enriched = []
    for row in rows:
        ticker = row["ticker"]
        df     = processed.get(ticker)
        if df is None:
            continue

        entry_idx   = row["date_idx"] + 1
        entry_price = row["open_next"]
        atr         = row["atr"]

        if entry_idx >= len(df):
            continue

        end_idx = min(entry_idx + max_hold_days, len(df))
        highs = df["High"].iloc[entry_idx:end_idx].values
        lows  = df["Low"].iloc[entry_idx:end_idx].values

        if len(highs) == 0:
            continue

        exit_outcomes = {}
        for (stop_mult, target_mult) in atr_combos:
            stop_price   = entry_price - atr * stop_mult
            target_price = entry_price + atr * target_mult

            exit_price  = None
            exit_reason = "end_of_data"
            days_held   = len(highs)

            for d in range(len(highs)):
                if lows[d] <= stop_price:
                    exit_price  = stop_price
                    exit_reason = "stop_loss"
                    days_held   = d + 1
                    break
                if highs[d] >= target_price:
                    exit_price  = target_price
                    exit_reason = "take_profit"
                    days_held   = d + 1
                    break

            if exit_price is None:
                exit_price = float(df["Close"].iloc[end_idx - 1])

            exit_outcomes[(stop_mult, target_mult)] = {
                "exit_price":    round(exit_price, 2),
                "exit_reason":   exit_reason,
                "days_held":     days_held,
                "pnl_per_share": exit_price - entry_price,
            }

        enriched_row = dict(row)
        enriched_row["exit_outcomes"] = exit_outcomes
        # Time-based exit prices: close at day 20, 30, 45 (for max-hold-days tests)
        for hold_d in (20, 30, 45):
            idx = min(entry_idx + hold_d - 1, len(df) - 1)
            enriched_row[f"close_at_{hold_d}d"] = float(df["Close"].iloc[idx])
        enriched.append(enriched_row)

    logger.info(f"Exit outcomes precomputed for {len(enriched)} rows. Saving cache...")
    with open(cache_path, "wb") as f:
        pickle.dump(enriched, f)
    return enriched


# ── Step 4: Fast simulation ─────────────────────────────────────────────────────

def simulate_fast(
    enriched_rows: List[dict],
    rsi_buy: float,
    rsi_sell: float,
    atr_stop: float,
    atr_target: float,
    # ── Phase 3 filters ──────────────────────────────────────────────────────
    volume_min_ratio: Optional[float] = None,    # e.g. 1.2 → volume must be > 1.2× avg
    spy_trend: Optional[Dict] = None,            # {date: dict} SPY MA data
    spy_ma: str = "above_50ma",                  # which MA key to check: above_50ma / above_100ma / above_200ma
    require_macd_cross: bool = False,
    require_macd_above: bool = False,
    # ── VIX filter ────────────────────────────────────────────────────────────
    vix_data: Optional[Dict] = None,             # {date: float} VIX close
    vix_max: Optional[float] = None,             # only trade when VIX < this threshold
    # ── Phase 3e+ improvement experiments ────────────────────────────────────
    require_rsi_rising: bool = False,            # RSI today > RSI yesterday
    require_price_above_200ma: bool = False,     # price above 200-day MA
    require_consecutive_rsi: bool = False,       # prev_rsi also below rsi_buy
    require_ma200_rising: bool = False,          # 200MA slope pointing upward
    bb_lower_pct_max: Optional[float] = None,   # only buy when price is within X% of BB lower band
    min_price: float = 0.0,                      # skip stocks below this price
    max_price: float = 0.0,                      # skip stocks above this price (0 = no cap)
    sizing_mode: str = "fixed_pct",             # "fixed_pct" or "atr_target_40"
    atr_pct_min: float = 0.0,                    # skip boring stocks (ATR/price too small)
    atr_pct_max: float = 100.0,                  # skip hyper-volatile stocks
    # ── Round 3: time-based exit ──────────────────────────────────────────────
    max_hold_days: Optional[int] = None,         # force exit at day N if ATR exits not hit (20/30/45)
    # ── Round 1: portfolio parameters (previously module-level constants) ────
    max_position_pct: float = MAX_POSITION_PCT,          # fraction of portfolio per trade
    max_open_pos: int = MAX_OPEN_POSITIONS,              # max simultaneous positions
    consec_loss_limit: int = CONSECUTIVE_LOSS_LIMIT,     # stop after N consecutive losses
) -> Tuple[List[dict], dict]:
    trades             = []
    portfolio          = STARTING_CAPITAL
    open_tickers       = set()
    consecutive_losses = 0
    paused_until_date  = None   # date after which trading resumes (simulates /resume)

    for row in enriched_rows:
        ticker = row["ticker"]

        if ticker in open_tickers:
            continue

        # ── Consecutive-loss pause (simulates /pause + auto-resume after 5 trading days) ──
        # In live trading the user manually runs /resume. In backtesting we auto-resume
        # after CONSEC_LOSS_PAUSE_DAYS trading days so the full 2-year window is measured.
        if consecutive_losses >= consec_loss_limit:
            if paused_until_date is None:
                # Set resume date = current signal date + 5 calendar days
                import datetime as _dt
                raw = row["date"]
                if hasattr(raw, "date"):
                    base = raw.date()
                else:
                    try:
                        base = _dt.date.fromisoformat(str(raw)[:10])
                    except Exception:
                        base = _dt.date.today()
                paused_until_date = base + _dt.timedelta(days=7)  # ~5 trading days
            # Compare current row date to resume date
            raw = row["date"]
            if hasattr(raw, "date"):
                cur = raw.date()
            else:
                try:
                    cur = _dt.date.fromisoformat(str(raw)[:10])
                except Exception:
                    cur = paused_until_date  # fallback: stay paused
            if cur <= paused_until_date:
                continue
            else:
                consecutive_losses = 0
                paused_until_date  = None

        if len(open_tickers) >= max_open_pos:
            continue

        # ── Core signal ────────────────────────────────────────────────────────
        golden_cross = row["prev_ma_fast"] <= row["prev_ma_slow"] and row["ma_fast"] > row["ma_slow"]
        if not (row["rsi"] < rsi_buy and (row["ma_fast"] > row["ma_slow"] or golden_cross)):
            continue

        # ── Phase 3 filters ────────────────────────────────────────────────────
        if volume_min_ratio is not None and row.get("volume_ratio", 0) < volume_min_ratio:
            continue

        if spy_trend is not None:
            dk = row["date"]
            spy_day = spy_trend.get(dk) or spy_trend.get(pd.Timestamp(dk))
            if spy_day is None:
                continue
            # Support both old bool format and new dict format
            if isinstance(spy_day, bool):
                if not spy_day:
                    continue
            else:
                if not spy_day.get(spy_ma, True):
                    continue

        if vix_data is not None and vix_max is not None:
            dk = row["date"]
            vix_val = vix_data.get(dk) or vix_data.get(pd.Timestamp(dk))
            if vix_val is not None and vix_val >= vix_max:
                continue

        if require_macd_cross and not row.get("macd_cross", False):
            continue
        if require_macd_above and not row.get("macd_above", False):
            continue

        # ── Phase 3e+ improvement filters ─────────────────────────────────────
        if require_rsi_rising and row.get("rsi", 0) <= row.get("prev_rsi", 0):
            continue

        if require_price_above_200ma and not row.get("price_above_200ma", True):
            continue

        if require_ma200_rising and not row.get("ma200_rising", True):
            continue

        if require_consecutive_rsi and row.get("prev_rsi", rsi_buy + 1) >= rsi_buy:
            continue

        if bb_lower_pct_max is not None and row.get("bb_lower_pct", 999) > bb_lower_pct_max:
            continue

        if min_price > 0 and row.get("open_next", 0) < min_price:
            continue

        # Hard max price cap (fixes AZO-style single-share blowups)
        if max_price > 0 and row.get("open_next", 0) > max_price:
            continue

        atr_pct = row.get("atr_pct", 0)
        if atr_pct < atr_pct_min or atr_pct > atr_pct_max:
            continue

        # ── Exit lookup ────────────────────────────────────────────────────────
        outcome = row["exit_outcomes"].get((atr_stop, atr_target))
        if not outcome:
            continue

        # If max_hold_days set and trade would exceed that, use the time-based close instead
        if max_hold_days is not None and outcome["days_held"] > max_hold_days:
            close_key = f"close_at_{max_hold_days}d"
            time_exit_price = row.get(close_key)
            if time_exit_price is not None:
                outcome = {
                    "exit_price":    round(time_exit_price, 2),
                    "exit_reason":   f"max_hold_{max_hold_days}d",
                    "days_held":     max_hold_days,
                    "pnl_per_share": time_exit_price - row["open_next"],
                }
            continue

        entry_price = row["open_next"]

        # ── Position sizing ────────────────────────────────────────────────────
        if sizing_mode == "atr_target_40":
            import math
            atr_val     = row.get("atr", 0)
            atr_dollars = atr_val * atr_target
            if atr_dollars > 0:
                shares_needed = math.ceil(40.0 / atr_dollars)
            else:
                shares_needed = int((portfolio * max_position_pct) / entry_price)
        else:
            shares_needed = int((portfolio * max_position_pct) / entry_price)

        # Hard cap at max_position_pct of portfolio
        max_by_cap = int((portfolio * max_position_pct) / entry_price)
        qty        = min(shares_needed, max_by_cap)

        # Skip if can't buy at least 3 shares — avoids 1-share $3k positions
        if qty < 3:
            continue
        gross_pnl   = outcome["pnl_per_share"] * qty
        fees        = IBKR_FEE_PER_TRADE * 2
        net_pnl     = round(gross_pnl - fees, 2)
        cost        = entry_price * qty
        pnl_pct     = round((net_pnl / cost) * 100, 2) if cost else 0

        portfolio += net_pnl
        open_tickers.discard(ticker)

        if net_pnl > 0:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        entry_date = row["date_next"]
        if hasattr(entry_date, "date"):
            entry_date = str(entry_date.date())

        trades.append({
            "ticker":      ticker,
            "entry_date":  entry_date,
            "entry_price": entry_price,
            "exit_price":  outcome["exit_price"],
            "stop_loss":   round(entry_price - row["atr"] * atr_stop, 2),
            "take_profit": round(entry_price + row["atr"] * atr_target, 2),
            "qty":         qty,
            "net_pnl":     net_pnl,
            "pnl_pct":     pnl_pct,
            "exit_reason": outcome["exit_reason"],
            "days_held":   outcome["days_held"],
            "win":         net_pnl > 0,
        })

    return trades, _build_summary(trades)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_summary(trades: List[dict]) -> dict:
    if not trades:
        return {}

    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    total_pnl      = sum(t["net_pnl"] for t in trades)
    total_wins_pnl = sum(t["net_pnl"] for t in wins)
    total_loss_pnl = abs(sum(t["net_pnl"] for t in losses))

    profit_factor = round(total_wins_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else 999.0

    equity = STARTING_CAPITAL
    peak   = STARTING_CAPITAL
    max_dd = 0.0
    for t in trades:
        equity += t["net_pnl"]
        peak    = max(peak, equity)
        dd      = (peak - equity) / peak * 100
        max_dd  = max(max_dd, dd)

    return {
        "total_trades":      len(trades),
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate_pct":      round(len(wins) / len(trades) * 100, 1),
        "total_net_pnl":     round(total_pnl, 2),
        "total_return_pct":  round(total_pnl / STARTING_CAPITAL * 100, 2),
        "avg_win":           round(total_wins_pnl / len(wins), 2) if wins else 0,
        "avg_loss":          round(-total_loss_pnl / len(losses), 2) if losses else 0,
        "profit_factor":     profit_factor,
        "max_drawdown_pct":  round(max_dd, 2),
        "final_portfolio":   round(STARTING_CAPITAL + total_pnl, 2),
        "stop_loss_exits":   len([t for t in trades if t["exit_reason"] == "stop_loss"]),
        "take_profit_exits": len([t for t in trades if t["exit_reason"] == "take_profit"]),
        "best_trade":        max(trades, key=lambda x: x["net_pnl"]),
        "worst_trade":       min(trades, key=lambda x: x["net_pnl"]),
        "avg_daily_pnl":     round(total_pnl / max(1, (
            (datetime.strptime(max(str(t["entry_date"])[:10] for t in trades), "%Y-%m-%d") -
             datetime.strptime(min(str(t["entry_date"])[:10] for t in trades), "%Y-%m-%d")).days
            * 5 // 7
        )), 2) if trades else 0,
    }


# ── Report printer ─────────────────────────────────────────────────────────────

def print_report(summary: dict, label: str = "", max_open_pos: int = MAX_OPEN_POSITIONS) -> None:
    if not summary:
        print(f"\n  {label}: No trades generated.")
        return

    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  {label}")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f}  |  Max pos: {max_open_pos}")
    print(sep)
    print(f"  {'Total trades:':<25} {summary['total_trades']}")
    print(f"  {'Wins / Losses:':<25} {summary['wins']} / {summary['losses']}")
    print(f"  {'Win rate:':<25} {summary['win_rate_pct']}%")
    print(f"  {'Avg winner:':<25} ${summary['avg_win']:+,.2f}")
    print(f"  {'Avg loser:':<25} ${summary['avg_loss']:+,.2f}")
    print(f"  {'Profit factor:':<25} {summary['profit_factor']}")
    print(f"  {'Total net P&L:':<25} ${summary['total_net_pnl']:+,.2f}  ({summary['total_return_pct']:+.1f}%)")
    print(f"  {'Avg daily P&L:':<25} ${summary.get('avg_daily_pnl', 0):+,.2f}/day")
    print(f"  {'Final portfolio:':<25} ${summary['final_portfolio']:,.2f}")
    print(f"  {'Max drawdown:':<25} -{summary['max_drawdown_pct']:.1f}%")
    print(f"  {'Stop loss exits:':<25} {summary['stop_loss_exits']}")
    print(f"  {'Take profit exits:':<25} {summary['take_profit_exits']}")

    best  = summary["best_trade"]
    worst = summary["worst_trade"]
    print(f"\n  Best:  {best['ticker']} entry ${best['entry_price']:.2f} → ${best['exit_price']:.2f}  net ${best['net_pnl']:+,.2f}")
    print(f"  Worst: {worst['ticker']} entry ${worst['entry_price']:.2f} → ${worst['exit_price']:.2f}  net ${worst['net_pnl']:+,.2f}")

    verdict = (
        "  ✅ POSITIVE expected value — safe to proceed."
        if summary["total_net_pnl"] > 0 and summary["win_rate_pct"] >= 50 and summary["profit_factor"] >= 1.2
        else "  ⚠️  Marginally profitable — review before going live."
        if summary["total_net_pnl"] > 0
        else "  ❌ NEGATIVE expected value."
    )
    print(f"\n  VERDICT\n{verdict}")
    print(sep)


def print_comparison(results: List[dict], title: str = "") -> None:
    sep = "=" * 108
    print(f"\n\n{sep}")
    print(f"  {title}")
    print(sep)
    print(f"  {'Scenario':<42} {'Trades':>7} {'Win%':>7} {'P&L':>11} {'Return':>9} {'MaxDD':>8} {'PF':>6}  {'Decision'}")
    print(f"  {'-'*103}")
    baseline_pnl = None
    for r in results:
        s = r.get("summary", {})
        if not s:
            print(f"  {r['label']:<42} {'—':>7} {'—':>7} {'—':>11} {'—':>9} {'—':>8} {'—':>6}  ❌ no trades")
            continue
        if baseline_pnl is None:
            baseline_pnl = s["total_net_pnl"]
        delta = s["total_net_pnl"] - baseline_pnl
        arrow = f"▲ +${delta:,.0f}" if delta > 0 else (f"▼ -${abs(delta):,.0f}" if delta < 0 else "— baseline")
        print(
            f"  {r['label']:<42} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
            f" ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
            f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}  {arrow}"
        )
    print(sep)


# ── Optimizer ──────────────────────────────────────────────────────────────────

def run_optimizer(
    enriched_rows: List[dict],
    label: str = "2-year",
    top_n: int = 15,
    **filter_kwargs,
) -> dict:
    rsi_buy_values    = [20, 25, 28, 30, 33, 35, 38, 40, 42, 45]
    rsi_sell_values   = [55, 60, 65, 70, 75]
    atr_stop_values   = [1.5, 2.0, 2.5, 3.0, 3.5]
    atr_target_values = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]

    combos = [
        (rb, rs, st, ta)
        for rb in rsi_buy_values
        for rs in rsi_sell_values
        for st in atr_stop_values
        for ta in atr_target_values
        if ta / st >= 1.5
    ]

    print(f"\nOptimizer ({label}): testing {len(combos)} combinations...")

    results = []
    for rsi_buy, rsi_sell, atr_stop, atr_target in combos:
        trades, summary = simulate_fast(
            enriched_rows, rsi_buy, rsi_sell, atr_stop, atr_target, **filter_kwargs
        )
        if not summary or summary["total_trades"] < 10:
            continue
        results.append({
            "rsi_buy":       rsi_buy,
            "rsi_sell":      rsi_sell,
            "atr_stop":      atr_stop,
            "atr_target":    atr_target,
            "trades":        summary["total_trades"],
            "win_rate":      summary["win_rate_pct"],
            "net_pnl":       summary["total_net_pnl"],
            "return_pct":    summary["total_return_pct"],
            "profit_factor": summary["profit_factor"],
            "max_dd":        summary["max_drawdown_pct"],
        })

    results.sort(key=lambda x: x["net_pnl"], reverse=True)

    sep = "=" * 100
    print(f"\n{sep}")
    print(f"  OPTIMIZER RESULTS ({label}) — Top {top_n} by net P&L")
    print(sep)
    print(f"  {'RSI Buy':<9} {'RSI Sell':<10} {'ATR Stop':<10} {'ATR Target':<12} "
          f"{'Trades':<8} {'Win%':<8} {'P&L':>10} {'Return':>9} {'MaxDD':>8}")
    print(f"  {'-'*93}")
    for r in results[:top_n]:
        print(
            f"  {r['rsi_buy']:<9} {r['rsi_sell']:<10} {r['atr_stop']:<10} {r['atr_target']:<12} "
            f"{r['trades']:<8} {r['win_rate']:<8} ${r['net_pnl']:>+8,.2f}  {r['return_pct']:>+7.1f}%  -{r['max_dd']:.1f}%"
        )
    print(sep)

    if results:
        best = results[0]
        print(f"\n  BEST ({label}): RSI {best['rsi_buy']}/{best['rsi_sell']}  "
              f"ATR ×{best['atr_stop']}/×{best['atr_target']}  "
              f"→  {best['win_rate']}% win  ${best['net_pnl']:+,.2f} ({best['return_pct']:+.1f}%)  "
              f"DD -{best['max_dd']:.1f}%  PF {best['profit_factor']}")
        print(sep)
        return best
    return {}


# ── Round 1 portfolio optimizer ────────────────────────────────────────────────

def run_portfolio_optimizer(
    enriched_rows: List[dict],
    spy_trend: Dict,
    # fixed signal/exit params — best known values
    rsi_buy: float    = 38,
    rsi_sell: float   = 55,
    atr_stop: float   = 3.5,
    atr_target: float = 6.0,
) -> dict:
    """
    Grid search over 4 portfolio parameters:
      - position_pct     (how much capital per trade)
      - max_open_pos     (simultaneous positions allowed)
      - consec_loss_limit(stop trading after N consecutive losses)
      - volume_ratio     (minimum volume vs 20-day avg)

    All 4 are independent of the signal logic and require zero cache changes.
    Total combinations: 6 × 5 × 5 × 6 = 900 — runs in < 1 second.
    """

    position_pcts      = [0.05, 0.07, 0.08, 0.10, 0.12, 0.15]
    max_open_positions = [3, 4, 5, 6, 8]
    consec_limits      = [1, 2, 3, 4, 999]   # 999 = effectively unlimited
    volume_thresholds  = [0.0, 0.8, 1.0, 1.2, 1.5, 2.0]

    # ── PART A: vary each parameter in isolation ──────────────────────────────
    BASELINE = dict(
        volume_min_ratio=1.2,
        spy_trend=spy_trend,
        max_position_pct=0.10,
        max_open_pos=5,
        consec_loss_limit=3,
    )

    def run(**overrides):
        p = dict(BASELINE)
        p.update(overrides)
        _, s = simulate_fast(enriched_rows, rsi_buy, rsi_sell, atr_stop, atr_target, **p)
        return s

    sep80 = "─" * 90

    # A1 — Position size
    print(f"\n{'='*90}")
    print("  PART A1 — Position size per trade  (all others at baseline)")
    print(f"{'='*90}")
    print(f"  {'Size':>6}  {'Trades':>7}  {'Win%':>7}  {'P&L':>11}  {'Return':>9}  {'MaxDD':>8}  {'PF':>6}")
    print(f"  {sep80}")
    a1_results = []
    for pct in position_pcts:
        s = run(max_position_pct=pct)
        tag = " ← current" if pct == 0.10 else ""
        if s:
            print(f"  {pct*100:>5.0f}%  {s['total_trades']:>7}  {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f}  {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>5.1f}%  {s['profit_factor']:>5.2f}{tag}")
            a1_results.append((pct, s))
        else:
            print(f"  {pct*100:>5.0f}%  {'—':>7}{tag}")

    # A2 — Max open positions
    print(f"\n{'='*90}")
    print("  PART A2 — Max simultaneous open positions  (all others at baseline)")
    print(f"{'='*90}")
    print(f"  {'Pos':>5}  {'Trades':>7}  {'Win%':>7}  {'P&L':>11}  {'Return':>9}  {'MaxDD':>8}  {'PF':>6}")
    print(f"  {sep80}")
    for mop in max_open_positions:
        s = run(max_open_pos=mop)
        tag = " ← current" if mop == 5 else ""
        if s:
            print(f"  {mop:>5}  {s['total_trades']:>7}  {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f}  {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>5.1f}%  {s['profit_factor']:>5.2f}{tag}")
        else:
            print(f"  {mop:>5}  {'—':>7}{tag}")

    # A3 — Consecutive loss limit
    print(f"\n{'='*90}")
    print("  PART A3 — Consecutive loss limit  (all others at baseline)")
    print(f"{'='*90}")
    print(f"  {'Limit':>7}  {'Trades':>7}  {'Win%':>7}  {'P&L':>11}  {'Return':>9}  {'MaxDD':>8}  {'PF':>6}")
    print(f"  {sep80}")
    for cl in consec_limits:
        s = run(consec_loss_limit=cl)
        label_str = "unlimited" if cl == 999 else str(cl)
        tag = " ← current" if cl == 3 else ""
        if s:
            print(f"  {label_str:>7}  {s['total_trades']:>7}  {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f}  {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>5.1f}%  {s['profit_factor']:>5.2f}{tag}")
        else:
            print(f"  {label_str:>7}  {'—':>7}{tag}")

    # A4 — Volume threshold
    print(f"\n{'='*90}")
    print("  PART A4 — Volume threshold  (all others at baseline)")
    print(f"{'='*90}")
    print(f"  {'Volume':>8}  {'Trades':>7}  {'Win%':>7}  {'P&L':>11}  {'Return':>9}  {'MaxDD':>8}  {'PF':>6}")
    print(f"  {sep80}")
    for vt in volume_thresholds:
        s = run(volume_min_ratio=vt if vt > 0 else None)
        label_str = "off" if vt == 0.0 else f"{vt:.1f}×"
        tag = " ← current" if vt == 1.2 else ""
        if s:
            print(f"  {label_str:>8}  {s['total_trades']:>7}  {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f}  {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>5.1f}%  {s['profit_factor']:>5.2f}{tag}")
        else:
            print(f"  {label_str:>8}  {'—':>7}{tag}")

    # ── PART B: full grid search over all 4 parameters ────────────────────────
    total_combos = (len(position_pcts) * len(max_open_positions)
                    * len(consec_limits) * len(volume_thresholds))
    print(f"\n\n{'='*90}")
    print(f"  PART B — Full grid search: {total_combos} combinations")
    print(f"{'='*90}")

    results = []
    for pct in position_pcts:
        for mop in max_open_positions:
            for cl in consec_limits:
                for vt in volume_thresholds:
                    vol = vt if vt > 0 else None
                    _, s = simulate_fast(
                        enriched_rows, rsi_buy, rsi_sell, atr_stop, atr_target,
                        volume_min_ratio=vol,
                        spy_trend=spy_trend,
                        max_position_pct=pct,
                        max_open_pos=mop,
                        consec_loss_limit=cl,
                    )
                    if not s or s["total_trades"] < 10:
                        continue
                    results.append({
                        "position_pct":  pct,
                        "max_open_pos":  mop,
                        "consec_limit":  cl,
                        "volume":        vt,
                        "trades":        s["total_trades"],
                        "win_rate":      s["win_rate_pct"],
                        "net_pnl":       s["total_net_pnl"],
                        "return_pct":    s["total_return_pct"],
                        "profit_factor": s["profit_factor"],
                        "max_dd":        s["max_drawdown_pct"],
                        "summary":       s,
                    })

    results.sort(key=lambda x: x["net_pnl"], reverse=True)

    print(f"\n  Top 20 by net P&L  (fixed: RSI {rsi_buy}/{rsi_sell}, ATR ×{atr_stop}/×{atr_target}, SPY 50MA on)")
    print(f"  {'Size':>5}  {'Pos':>4}  {'Loss':>5}  {'Vol':>6}  {'Trd':>5}  {'Win%':>6}"
          f"  {'P&L':>11}  {'Return':>9}  {'DD':>7}  {'PF':>6}")
    print(f"  {'─'*88}")
    for r in results[:20]:
        cl_str  = "∞" if r["consec_limit"] == 999 else str(r["consec_limit"])
        vol_str = "off" if r["volume"] == 0.0 else f"{r['volume']:.1f}×"
        print(
            f"  {r['position_pct']*100:>4.0f}%  {r['max_open_pos']:>4}  {cl_str:>5}  {vol_str:>6}"
            f"  {r['trades']:>5}  {r['win_rate']:>5.1f}%"
            f"  ${r['net_pnl']:>+9,.2f}  {r['return_pct']:>+8.1f}%"
            f"  -{r['max_dd']:>4.1f}%  {r['profit_factor']:>5.2f}"
        )

    best = results[0] if results else {}
    if best:
        print(f"\n{'='*90}")
        cl_str  = "unlimited" if best["consec_limit"] == 999 else str(best["consec_limit"])
        vol_str = "off"       if best["volume"] == 0.0       else f"{best['volume']:.1f}×"
        print(f"  BEST COMBINATION (Round 1):")
        print(f"    Position size:      {best['position_pct']*100:.0f}%  per trade")
        print(f"    Max open positions: {best['max_open_pos']}")
        print(f"    Loss limit:         {cl_str}")
        print(f"    Volume threshold:   {vol_str}")
        print(f"    Trades:             {best['trades']}")
        print(f"    Win rate:           {best['win_rate']}%")
        print(f"    Net P&L:            ${best['net_pnl']:+,.2f}  ({best['return_pct']:+.1f}%)")
        print(f"    Max drawdown:       -{best['max_dd']:.1f}%")
        print(f"    Profit factor:      {best['profit_factor']}")
        print(f"{'='*90}")

    return best


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print("\nStock Signal Bot — Backtester  |  Round 3: New Indicator Confirmation")
    print(f"Capital: ${STARTING_CAPITAL:,.0f}  |  Fixed: RSI 38/55, ATR ×3.5/×6.0, Vol 1.2×, SPY 50MA, 12% pos size\n")

    # ── 1. Load data ──────────────────────────────────────────────────────────
    tickers = get_sp500_tickers()
    print(f"Found {len(tickers)} S&P 500 tickers.")
    data = load_or_download_data(tickers)
    if not data:
        print("ERROR: No data available.")
        sys.exit(1)

    # ── 2. Precompute indicators v4 (rebuilds — includes BB + 200MA slope) ───
    rows_2y, processed = precompute_signals(data)
    if not rows_2y:
        print("ERROR: Could not compute indicators.")
        sys.exit(1)

    # ── 3. Load SPY + VIX ────────────────────────────────────────────────────
    spy_trend = load_spy_trend()
    vix_data  = load_vix()

    # ── 4. Precompute exits (rebuilds — includes close_at_20d/30d/45d) ───────
    atr_stops   = [1.5, 2.0, 2.5, 3.0, 3.5]
    atr_targets = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
    enriched_2y = precompute_exits(rows_2y, processed, atr_stops, atr_targets)
    print(f"2-year rows: {len(enriched_2y)}\n")

    # ── Fixed best params ─────────────────────────────────────────────────────
    RSI_BUY    = 38
    RSI_SELL   = 55
    ATR_STOP   = 3.5
    ATR_TARGET = 6.0
    VOL        = 1.2
    POS_PCT    = 0.12

    BASELINE_KW = dict(
        volume_min_ratio=VOL,
        spy_trend=spy_trend,
        spy_ma="above_50ma",
        max_position_pct=POS_PCT,
    )

    def run(label, **kwargs):
        kw = dict(BASELINE_KW)
        kw.update(kwargs)
        trades, s = simulate_fast(enriched_2y, RSI_BUY, RSI_SELL, ATR_STOP, ATR_TARGET, **kw)
        return label, s, trades

    # ══════════════════════════════════════════════════════════════════════════
    # BASELINE
    # ══════════════════════════════════════════════════════════════════════════
    print("=" * 92)
    print("  BASELINE (Round 1+2 confirmed best)")
    print("=" * 92)
    _, base_s, _ = run("Baseline")
    if base_s:
        print(f"  Trades: {base_s['total_trades']}  |  Win: {base_s['win_rate_pct']}%  "
              f"|  P&L: ${base_s['total_net_pnl']:+,.2f} ({base_s['total_return_pct']:+.1f}%)  "
              f"|  DD: -{base_s['max_drawdown_pct']:.1f}%  |  PF: {base_s['profit_factor']}")
    baseline_pnl = base_s["total_net_pnl"] if base_s else 0

    # ══════════════════════════════════════════════════════════════════════════
    # PART A — Bollinger Band lower band filter
    # Only enter when price is within X% of the lower BB (more oversold vs BB)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*92}")
    print("  PART A — Bollinger Band filter: price within X% of lower BB")
    print(f"  (Lower % = closer to BB lower band = more oversold per BB)")
    print(f"{'='*92}")
    print(f"  {'Filter':<28} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}  {'vs baseline'}")
    print(f"  {'─'*90}")

    bb_results = []
    bb_tests = [
        ("No BB filter (baseline)",    {}),
        ("Price ≤ BB lower +2%",        {"bb_lower_pct_max": 2.0}),
        ("Price ≤ BB lower +4%",        {"bb_lower_pct_max": 4.0}),
        ("Price ≤ BB lower +6%",        {"bb_lower_pct_max": 6.0}),
        ("Price ≤ BB lower +8%",        {"bb_lower_pct_max": 8.0}),
        ("Price ≤ BB lower +10%",       {"bb_lower_pct_max": 10.0}),
        ("Price ≤ BB lower +15%",       {"bb_lower_pct_max": 15.0}),
    ]
    for label, kwargs in bb_tests:
        lbl, s, _ = run(label, **kwargs)
        if s:
            delta = s["total_net_pnl"] - baseline_pnl
            arrow = f"▲ +${delta:,.0f}" if delta > 1 else (f"▼ -${abs(delta):,.0f}" if delta < -1 else "— same")
            bb_results.append((label, s))
            print(f"  {label:<28} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}  {arrow}")
        else:
            print(f"  {label:<28} {'no trades':>7}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART B — 200MA slope filter (is the 200MA itself rising?)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*92}")
    print("  PART B — 200MA slope filter: only buy when 200MA is rising")
    print(f"{'='*92}")
    print(f"  {'Filter':<36} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}  {'vs baseline'}")
    print(f"  {'─'*90}")

    for label, kwargs in [
        ("No 200MA slope (baseline)",          {}),
        ("200MA rising",                       {"require_ma200_rising": True}),
        ("price > 200MA + 200MA rising",       {"require_price_above_200ma": True,
                                                "require_ma200_rising": True}),
    ]:
        lbl, s, _ = run(label, **kwargs)
        if s:
            delta = s["total_net_pnl"] - baseline_pnl
            arrow = f"▲ +${delta:,.0f}" if delta > 1 else (f"▼ -${abs(delta):,.0f}" if delta < -1 else "— same")
            print(f"  {label:<36} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}  {arrow}")
        else:
            print(f"  {label:<36} {'no trades':>7}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART C — Max hold days (force-exit after N days if ATR targets not hit)
    # Median hold in baseline is 35 days — test shorter caps to recycle capital faster
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*92}")
    print("  PART C — Max hold days: force-exit if trade still open after N days")
    print(f"  (Baseline: no cap — trades held up to 60 days, median ~35d)")
    print(f"{'='*92}")
    print(f"  {'Max hold':<20} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}  {'vs baseline'}")
    print(f"  {'─'*90}")

    hold_results = []
    for hold_d in [None, 45, 30, 20]:
        label = f"No cap (baseline)" if hold_d is None else f"Exit at day {hold_d}"
        lbl, s, trades = run(label, max_hold_days=hold_d)
        if s:
            delta = s["total_net_pnl"] - baseline_pnl
            arrow = f"▲ +${delta:,.0f}" if delta > 1 else (f"▼ -${abs(delta):,.0f}" if delta < -1 else "— same")
            hold_results.append((hold_d, label, s))
            print(f"  {label:<20} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}  {arrow}")
        else:
            print(f"  {label:<20} {'no trades':>7}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART D — Best combinations (stack any improvements found above)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*92}")
    print("  PART D — Best combinations stacked")
    print(f"{'='*92}")
    print(f"  {'Combo':<44} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}  {'vs baseline'}")
    print(f"  {'─'*92}")

    combo_results = []
    combos_to_test = []

    # Find best BB threshold (highest PF with > 10 trades and P&L > baseline)
    best_bb = None
    for label, s in bb_results[1:]:   # skip baseline
        if s and s["total_trades"] >= 10 and s["profit_factor"] > (base_s["profit_factor"] if base_s else 0):
            if best_bb is None or s["profit_factor"] > best_bb[1]["profit_factor"]:
                # extract threshold from label
                try:
                    thresh = float(label.split("+")[1].replace("%", "").strip())
                    best_bb = (thresh, s)
                except Exception:
                    pass

    # Find best max_hold_days cap
    best_hold = None
    for hold_d, label, s in hold_results[1:]:   # skip baseline
        if s and s["total_net_pnl"] > baseline_pnl:
            if best_hold is None or s["profit_factor"] > best_hold[1]["profit_factor"]:
                best_hold = (hold_d, s)

    combos_to_test.append(("Baseline (no new filters)", {}))
    combos_to_test.append(("200MA rising only", {"require_ma200_rising": True}))

    if best_bb:
        combos_to_test.append((f"Best BB (≤+{best_bb[0]:.0f}%)", {"bb_lower_pct_max": best_bb[0]}))
        combos_to_test.append((f"BB (≤+{best_bb[0]:.0f}%) + 200MA rising",
                                {"bb_lower_pct_max": best_bb[0], "require_ma200_rising": True}))
    if best_hold:
        combos_to_test.append((f"Max hold {best_hold[0]}d", {"max_hold_days": best_hold[0]}))
        if best_bb:
            combos_to_test.append((f"BB + max hold {best_hold[0]}d",
                                    {"bb_lower_pct_max": best_bb[0], "max_hold_days": best_hold[0]}))

    for label, kwargs in combos_to_test:
        lbl, s, trades = run(label, **kwargs)
        if s:
            delta = s["total_net_pnl"] - baseline_pnl
            arrow = f"▲ +${delta:,.0f}" if delta > 1 else (f"▼ -${abs(delta):,.0f}" if delta < -1 else "— same")
            combo_results.append({"label": label, "summary": s, "trades": trades, "kwargs": kwargs})
            print(f"  {label:<44} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}  {arrow}")
        else:
            print(f"  {label:<44} {'no trades':>7}")

    # ── Verdict ────────────────────────────────────────────────────────────────
    print(f"\n{'='*92}")
    print("  ROUND 3 VERDICT")
    print(f"{'='*92}")

    # Best by profit factor (with ≥ 20 trades)
    valid = [r for r in combo_results
             if r["summary"] and r["summary"]["total_trades"] >= 20
             and r["summary"]["total_net_pnl"] > 0]
    valid.sort(key=lambda x: x["summary"]["profit_factor"], reverse=True)

    if valid:
        best = valid[0]
        s    = best["summary"]
        print(f"  Best combo:    {best['label']}")
        print(f"  Trades:        {s['total_trades']}")
        print(f"  Win rate:      {s['win_rate_pct']}%")
        print(f"  Net P&L:       ${s['total_net_pnl']:+,.2f}  ({s['total_return_pct']:+.1f}%)")
        print(f"  Max drawdown:  -{s['max_drawdown_pct']:.1f}%")
        print(f"  Profit factor: {s['profit_factor']}")
        delta = s["total_net_pnl"] - baseline_pnl
        print(f"  vs baseline:   {'▲ +' if delta >= 0 else '▼ '}${abs(delta):,.0f}")
        print(f"{'='*92}")

        if best["trades"]:
            pd.DataFrame(best["trades"]).to_csv("backtest_round3_best.csv", index=False)
            print(f"\nSaved to backtest_round3_best.csv ({len(best['trades'])} trades)")
    else:
        print("  No improvement found over baseline. All confirmed params stand.")

    print("\nRound 3 complete.")
    print("All backtest rounds finished. Ready to finalise config and proceed to Phase 1 (auto-execution).")

    print("\nStock Signal Bot — Backtester  |  Round 2: Bear Market Protection")
    print(f"Capital: ${STARTING_CAPITAL:,.0f}  |  Fixed: RSI 38/55, ATR ×3.5/×6.0, Vol 1.2×, 12% pos size\n")

    # ── 1. Load tickers and price data ────────────────────────────────────────
    tickers = get_sp500_tickers()
    print(f"Found {len(tickers)} S&P 500 tickers.")
    data = load_or_download_data(tickers)
    if not data:
        print("ERROR: No data available.")
        sys.exit(1)

    # ── 2. Load precomputed indicators ────────────────────────────────────────
    rows_2y, processed = precompute_signals(data)
    if not rows_2y:
        print("ERROR: Could not compute indicators.")
        sys.exit(1)

    # ── 3. Load SPY trend (rebuilds cache with 50/100/200MA if old format) ───
    spy_trend = load_spy_trend()

    # ── 4. Load VIX data ──────────────────────────────────────────────────────
    vix_data = load_vix()

    # ── 5. Load precomputed exits ─────────────────────────────────────────────
    atr_stops   = [1.5, 2.0, 2.5, 3.0, 3.5]
    atr_targets = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
    enriched_2y = precompute_exits(rows_2y, processed, atr_stops, atr_targets)
    print(f"2-year rows: {len(enriched_2y)}\n")

    # ── Fixed best params from Round 1 ────────────────────────────────────────
    RSI_BUY    = 38
    RSI_SELL   = 55
    ATR_STOP   = 3.5
    ATR_TARGET = 6.0
    VOL        = 1.2
    POS_PCT    = 0.12   # confirmed best in Round 1

    # Helper: run one scenario with fixed signal params
    def run(label, **kwargs):
        _, s = simulate_fast(
            enriched_2y, RSI_BUY, RSI_SELL, ATR_STOP, ATR_TARGET,
            volume_min_ratio=VOL,
            max_position_pct=POS_PCT,
            **kwargs
        )
        return label, s

    # ══════════════════════════════════════════════════════════════════════════
    # PART A — SPY MA threshold comparison (50MA vs 100MA vs 200MA vs no filter)
    # ══════════════════════════════════════════════════════════════════════════
    print("=" * 88)
    print("  PART A — SPY trend filter: which MA threshold is best?")
    print("=" * 88)
    print(f"  {'Filter':<28} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}")
    print(f"  {'─'*82}")

    spy_ma_results = []
    spy_ma_tests = [
        ("No SPY filter",     dict()),
        ("SPY above 50MA",    dict(spy_trend=spy_trend, spy_ma="above_50ma")),
        ("SPY above 100MA",   dict(spy_trend=spy_trend, spy_ma="above_100ma")),
        ("SPY above 200MA",   dict(spy_trend=spy_trend, spy_ma="above_200ma")),
    ]
    for label, kwargs in spy_ma_tests:
        lbl, s = run(label, **kwargs)
        if s:
            spy_ma_results.append((label, s, kwargs))
            tag = " ← current" if label == "SPY above 50MA" else ""
            print(f"  {label:<28} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}{tag}")
        else:
            print(f"  {label:<28} {'no trades':>7}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART B — VIX threshold comparison (no filter / <20 / <25 / <30 / <35)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*88}")
    print("  PART B — VIX filter: only trade when VIX is below threshold")
    print(f"{'='*88}")
    print(f"  {'Filter':<28} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}")
    print(f"  {'─'*82}")

    vix_results = []
    vix_tests = [
        ("No VIX filter",   dict(spy_trend=spy_trend, spy_ma="above_50ma")),
        ("VIX < 20",        dict(spy_trend=spy_trend, spy_ma="above_50ma", vix_data=vix_data, vix_max=20)),
        ("VIX < 25",        dict(spy_trend=spy_trend, spy_ma="above_50ma", vix_data=vix_data, vix_max=25)),
        ("VIX < 30",        dict(spy_trend=spy_trend, spy_ma="above_50ma", vix_data=vix_data, vix_max=30)),
        ("VIX < 35",        dict(spy_trend=spy_trend, spy_ma="above_50ma", vix_data=vix_data, vix_max=35)),
    ]
    for label, kwargs in vix_tests:
        lbl, s = run(label, **kwargs)
        if s:
            vix_results.append((label, s, kwargs))
            tag = " ← baseline (no VIX)" if label == "No VIX filter" else ""
            print(f"  {label:<28} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                  f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                  f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}{tag}")
        else:
            print(f"  {label:<28} {'no trades':>7}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART C — Best SPY MA × Best VIX threshold combinations
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*88}")
    print("  PART C — Best combinations: SPY MA × VIX threshold")
    print(f"{'='*88}")
    print(f"  {'Filter':<36} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}")
    print(f"  {'─'*88}")

    combo_results = []
    spy_mas  = ["above_50ma", "above_100ma", "above_200ma"]
    vix_maxs = [None, 20, 25, 30, 35]
    for sma in spy_mas:
        for vmax in vix_maxs:
            vix_kw = dict(vix_data=vix_data, vix_max=vmax) if vmax else {}
            label  = f"SPY {sma.replace('above_','').upper()}"
            label += f" + VIX<{vmax}" if vmax else " (no VIX)"
            lbl, s = run(label, spy_trend=spy_trend, spy_ma=sma, **vix_kw)
            if s:
                combo_results.append({"label": label, "summary": s,
                                       "spy_ma": sma, "vix_max": vmax})
                print(f"  {label:<36} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
                      f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
                      f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}")

    # ── Pick best by profit factor (with ≥ 20 trades and positive P&L) ────────
    valid = [r for r in combo_results
             if r["summary"]["total_net_pnl"] > 0 and r["summary"]["total_trades"] >= 20]
    valid.sort(key=lambda x: x["summary"]["profit_factor"], reverse=True)

    print(f"\n{'='*88}")
    print("  ROUND 2 VERDICT — Best combination by Profit Factor (min 20 trades, P&L > 0)")
    print(f"{'='*88}")
    if valid:
        best = valid[0]
        s    = best["summary"]
        print(f"  Filter:         {best['label']}")
        print(f"  SPY MA:         {best['spy_ma']}")
        print(f"  VIX max:        {'none' if best['vix_max'] is None else '< ' + str(best['vix_max'])}")
        print(f"  Trades:         {s['total_trades']}")
        print(f"  Win rate:       {s['win_rate_pct']}%")
        print(f"  Net P&L:        ${s['total_net_pnl']:+,.2f}  ({s['total_return_pct']:+.1f}%)")
        print(f"  Max drawdown:   -{s['max_drawdown_pct']:.1f}%")
        print(f"  Profit factor:  {s['profit_factor']}")
        print(f"{'='*88}")

        # Save full trade list for best combo
        _, best_trades_s = simulate_fast(
            enriched_2y, RSI_BUY, RSI_SELL, ATR_STOP, ATR_TARGET,
            volume_min_ratio=VOL,
            max_position_pct=POS_PCT,
            spy_trend=spy_trend,
            spy_ma=best["spy_ma"],
            **(dict(vix_data=vix_data, vix_max=best["vix_max"]) if best["vix_max"] else {}),
        )
        # Also save trade-level CSV
        trades_out, _ = simulate_fast(
            enriched_2y, RSI_BUY, RSI_SELL, ATR_STOP, ATR_TARGET,
            volume_min_ratio=VOL,
            max_position_pct=POS_PCT,
            spy_trend=spy_trend,
            spy_ma=best["spy_ma"],
            **(dict(vix_data=vix_data, vix_max=best["vix_max"]) if best["vix_max"] else {}),
        )
        if trades_out:
            pd.DataFrame(trades_out).to_csv("backtest_round2_best.csv", index=False)
            print(f"\nSaved to backtest_round2_best.csv ({len(trades_out)} trades)")
    else:
        print("  No valid combination found.")

    print("\nRound 2 complete.")
    print("Next: Round 3 — Bollinger Bands lower band, 200MA slope, max hold days exit")

    # ── 1. Load tickers and data ───────────────────────────────────────────────
    tickers = get_sp500_tickers()
    print(f"Found {len(tickers)} S&P 500 tickers.")
    data = load_or_download_data(tickers)
    if not data:
        print("ERROR: No data available.")
        sys.exit(1)

    # ── 2. Load precomputed indicators ────────────────────────────────────────
    rows_2y, processed = precompute_signals(data)
    if not rows_2y:
        print("ERROR: Could not compute indicators.")
        sys.exit(1)

    # ── 3. Load SPY trend ─────────────────────────────────────────────────────
    spy_trend = load_spy_trend()

    # ── 4. Load precomputed exits ─────────────────────────────────────────────
    atr_stops   = [1.5, 2.0, 2.5, 3.0, 3.5]
    atr_targets = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
    enriched_2y = precompute_exits(rows_2y, processed, atr_stops, atr_targets)

    cutoff_1y   = datetime.today() - timedelta(days=365)
    enriched_1y = [r for r in enriched_2y if r["date"] >= pd.Timestamp(cutoff_1y)]
    print(f"2-year rows: {len(enriched_2y)}  |  1-year rows: {len(enriched_1y)}\n")

    # ── 5. Run Round 1 grid search ────────────────────────────────────────────
    best = run_portfolio_optimizer(enriched_2y, spy_trend)

    # ── 6. Full detail report for best combination ────────────────────────────
    if best:
        print("\n\n--- Full report for best combination ---")
        cl  = best["consec_limit"]
        vol = best["volume"] if best["volume"] > 0 else None
        trades, s = simulate_fast(
            enriched_2y, 38, 55, 3.5, 6.0,
            volume_min_ratio=vol,
            spy_trend=spy_trend,
            max_position_pct=best["position_pct"],
            max_open_pos=best["max_open_pos"],
            consec_loss_limit=cl,
        )
        cl_str  = "unlimited" if cl == 999 else str(cl)
        vol_str = "off"       if vol is None else f"{vol:.1f}×"
        print_report(
            s,
            label=f"Best combo: {best['position_pct']*100:.0f}% size, "
                  f"{best['max_open_pos']} pos, loss limit {cl_str}, vol {vol_str}",
            max_open_pos=best["max_open_pos"],
        )
        if trades:
            pd.DataFrame(trades).to_csv("backtest_round1_best.csv", index=False)
            print(f"\nSaved to backtest_round1_best.csv ({len(trades)} trades)")

    print("\nRound 1 complete.")
    print("Next: Round 2 — SPY MA thresholds (50/100/200MA) + VIX filter (20/25/30/35)")

    from dotenv import load_dotenv
    load_dotenv()

    print("\nStock Signal Bot — Backtester (Phase 3e+ Improvement Analysis)")
    print(f"Period: {BACKTEST_YEARS} years  |  Capital: ${STARTING_CAPITAL:,.0f}\n")

    # ── 1. Load / download data ────────────────────────────────────────────────
    tickers = get_sp500_tickers()
    print(f"Found {len(tickers)} S&P 500 tickers.")
    data = load_or_download_data(tickers)
    if not data:
        print("ERROR: No data available.")
        sys.exit(1)

    # ── 2. Precompute indicators (v3) ──────────────────────────────────────────
    rows_2y, processed = precompute_signals(data)
    if not rows_2y:
        print("ERROR: Could not compute indicators.")
        sys.exit(1)

    # ── 3. Load SPY trend ──────────────────────────────────────────────────────
    spy_trend = load_spy_trend()

    # ── 4. Precompute exits ────────────────────────────────────────────────────
    atr_stops   = [1.5, 2.0, 2.5, 3.0, 3.5]
    atr_targets = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
    enriched_2y = precompute_exits(rows_2y, processed, atr_stops, atr_targets)

    cutoff_1y   = datetime.today() - timedelta(days=365)
    enriched_1y = [r for r in enriched_2y if r["date"] >= pd.Timestamp(cutoff_1y)]
    print(f"1-year rows: {len(enriched_1y)}  |  2-year rows: {len(enriched_2y)}\n")

    # ── Current best from Phase 3 (baseline for this analysis) ────────────────
    BASE_RSI_BUY    = 38
    BASE_RSI_SELL   = 55
    BASE_ATR_STOP   = 3.0
    BASE_ATR_TARGET = 5.0
    BASE_VOL        = 1.2

    # Helper: run one named scenario at the base ATR/RSI
    comparison = []
    def scenario(label, **kwargs):
        trades, s = simulate_fast(
            enriched_2y, BASE_RSI_BUY, BASE_RSI_SELL, BASE_ATR_STOP, BASE_ATR_TARGET,
            volume_min_ratio=BASE_VOL,   # always include volume filter (Phase 3 confirmed)
            spy_trend=spy_trend,         # always include SPY filter
            **kwargs
        )
        print_report(s, label)
        comparison.append({"label": label, "summary": s, "trades": trades})
        return trades, s

    # ════════════════════════════════════════════════════════════════════════════
    # PART A — Individual filter experiments
    # ════════════════════════════════════════════════════════════════════════════
    print("=" * 60)
    print("  PART A — Individual improvement experiments")
    print("=" * 60)

    # 0. Confirmed baseline (RSI 38/55, ATR ×3.0/×5.0, volume 1.2×, SPY filter)
    print("\n--- 0. Confirmed baseline (Phase 3 best) ---")
    scenario("0. Baseline (vol 1.2×, SPY filter)")

    # 1. RSI rising — RSI today > RSI yesterday (reversal starting)
    print("\n--- 1. RSI rising (reversal confirmed) ---")
    scenario("1. RSI rising", require_rsi_rising=True)

    # 2. Price above 200MA (long-term uptrend only)
    print("\n--- 2. Price above 200MA ---")
    scenario("2. Price above 200MA", require_price_above_200ma=True)

    # 3. Consecutive oversold — prev_rsi was also below threshold
    print("\n--- 3. Consecutive oversold (2 days below RSI threshold) ---")
    scenario("3. Consecutive RSI oversold", require_consecutive_rsi=True)

    # 4. Stricter volume (1.5× instead of 1.2×)
    print("\n--- 4. Volume raised to 1.5× ---")
    _, s4 = simulate_fast(
        enriched_2y, BASE_RSI_BUY, BASE_RSI_SELL, BASE_ATR_STOP, BASE_ATR_TARGET,
        volume_min_ratio=1.5, spy_trend=spy_trend
    )
    print_report(s4, "4. Volume > 1.5×")
    comparison.append({"label": "4. Volume > 1.5×", "summary": s4})

    # 5. Min price $10
    print("\n--- 5. Min price $10 ---")
    scenario("5. Min price $10", min_price=10.0)

    # 6. ATR% filter: skip boring (< 1%) and hyper-volatile (> 8%)
    print("\n--- 6. ATR% filter (1% – 8% of price) ---")
    scenario("6. ATR% 1–8%", atr_pct_min=1.0, atr_pct_max=8.0)

    # 7. RSI rising + Price above 200MA (combining two best candidates)
    print("\n--- 7. RSI rising + Price above 200MA ---")
    scenario("7. RSI rising + 200MA", require_rsi_rising=True, require_price_above_200ma=True)

    print_comparison(comparison, "PART A — Improvement experiment comparison (2-year, base ATR 3.0/5.0)")

    # ════════════════════════════════════════════════════════════════════════════
    # PART B — Optimizer with the best individual filters
    # Run optimizer on whichever filters showed improvement in Part A
    # ════════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 60)
    print("  PART B — Optimizer with best individual filters")
    print("=" * 60)

    print("\n--- Optimizer: RSI rising filter ---")
    best_rising = run_optimizer(
        enriched_2y, label="2Y + RSI rising",
        volume_min_ratio=BASE_VOL, spy_trend=spy_trend,
        require_rsi_rising=True,
    )

    print("\n--- Optimizer: Price above 200MA filter ---")
    best_200ma = run_optimizer(
        enriched_2y, label="2Y + 200MA",
        volume_min_ratio=BASE_VOL, spy_trend=spy_trend,
        require_price_above_200ma=True,
    )

    print("\n--- Optimizer: Consecutive RSI oversold ---")
    best_consec = run_optimizer(
        enriched_2y, label="2Y + Consecutive RSI",
        volume_min_ratio=BASE_VOL, spy_trend=spy_trend,
        require_consecutive_rsi=True,
    )

    print("\n--- Optimizer: ATR% filter (1–8%) ---")
    best_atrpct = run_optimizer(
        enriched_2y, label="2Y + ATR% filter",
        volume_min_ratio=BASE_VOL, spy_trend=spy_trend,
        atr_pct_min=1.0, atr_pct_max=8.0,
    )

    # ════════════════════════════════════════════════════════════════════════════
    # PART C — Best combination (all winning filters stacked)
    # ════════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 60)
    print("  PART C — Best combination of all improvements")
    print("=" * 60)

    # Identify best thresholds from Part B optimizers — use Part B results
    best_combo_rsi_buy    = best_rising.get("rsi_buy", BASE_RSI_BUY) if best_rising else BASE_RSI_BUY
    best_combo_rsi_sell   = best_rising.get("rsi_sell", BASE_RSI_SELL) if best_rising else BASE_RSI_SELL
    best_combo_atr_stop   = best_rising.get("atr_stop", BASE_ATR_STOP) if best_rising else BASE_ATR_STOP
    best_combo_atr_target = best_rising.get("atr_target", BASE_ATR_TARGET) if best_rising else BASE_ATR_TARGET

    print(f"\nUsing thresholds from RSI rising optimizer: "
          f"RSI {best_combo_rsi_buy}/{best_combo_rsi_sell}, "
          f"ATR ×{best_combo_atr_stop}/×{best_combo_atr_target}")

    combo_comparison = []

    def combo_scenario(label, rsi_buy=None, rsi_sell=None, atr_stop=None, atr_target=None, **kwargs):
        rb = rsi_buy   or best_combo_rsi_buy
        rs = rsi_sell  or best_combo_rsi_sell
        st = atr_stop  or best_combo_atr_stop
        ta = atr_target or best_combo_atr_target
        trades, s = simulate_fast(
            enriched_2y, rb, rs, st, ta,
            volume_min_ratio=BASE_VOL, spy_trend=spy_trend,
            **kwargs
        )
        print_report(s, label)
        combo_comparison.append({"label": label, "summary": s, "trades": trades})
        return trades, s

    print("\n--- C0. Baseline with re-optimized thresholds ---")
    combo_scenario("C0. Re-optimized thresholds")

    print("\n--- C1. + RSI rising ---")
    combo_scenario("C1. + RSI rising", require_rsi_rising=True)

    print("\n--- C2. + RSI rising + 200MA ---")
    combo_scenario("C2. + RSI rising + 200MA",
                   require_rsi_rising=True, require_price_above_200ma=True)

    print("\n--- C3. + RSI rising + 200MA + ATR% filter ---")
    combo_scenario("C3. + RSI rising + 200MA + ATR%",
                   require_rsi_rising=True, require_price_above_200ma=True,
                   atr_pct_min=1.0, atr_pct_max=8.0)

    print("\n--- C4. Full stack (all improvements) ---")
    combo_scenario("C4. Full stack",
                   require_rsi_rising=True, require_price_above_200ma=True,
                   require_consecutive_rsi=True, atr_pct_min=1.0, atr_pct_max=8.0)

    print_comparison(combo_comparison, "PART C — Best combination comparison")

    # ── Save best combined trades ──────────────────────────────────────────────
    best_combo_result = max(
        [r for r in combo_comparison if r["summary"]],
        key=lambda x: x["summary"]["total_net_pnl"],
        default=None
    )
    if best_combo_result and best_combo_result["trades"]:
        pd.DataFrame(best_combo_result["trades"]).to_csv(
            "backtest_best_improved.csv", index=False
        )
        print(f"\nSaved best improved trade list to backtest_best_improved.csv "
              f"({len(best_combo_result['trades'])} trades)")

    print("""
NOTE — Earnings filter (Phase 3d):
  Applied as LIVE-only filter (no historical backtest data available).
  Before placing any order: check yf.Ticker(ticker).calendar
  Skip if earnings within EARNINGS_BUFFER_DAYS (3 days).
""")
    print("Done.")

    # ═══════════════════════════════════════════════════════════════════════════
    # ROUND 4 — ATR-Target $40/Trade Sizing + Price Cap (Path to $40/Day)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 65)
    print("  ROUND 4: ATR-Target $40/Trade Sizing vs Fixed 12%")
    print("=" * 65)

    round4_results = []

    R4_RSI_BUY    = 38
    R4_RSI_SELL   = 55
    R4_ATR_STOP   = 3.5
    R4_ATR_TARGET = 6.0

    r4_filters = dict(
        volume_min_ratio=1.2,
        spy_trend=spy_trend,
        vix_data=vix_data,
        vix_max=25,
        spy_ma="above_50ma",
    )

    # ── 4A: Baseline (current best — 12% fixed, S&P 500 only) ────────────────
    _, s_4a = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.12, max_open_pos=5,
        sizing_mode="fixed_pct",
        **r4_filters,
    )
    round4_results.append({"label": "4A: Baseline (12% fixed, S&P 500)", "summary": s_4a})
    print_report(s_4a, "4A: Baseline — 12% fixed sizing, S&P 500 only")

    # ── 4B: ATR-target $40/trade sizing, S&P 500 only ────────────────────────
    _, s_4b = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.12, max_open_pos=5,
        sizing_mode="atr_target_40",
        **r4_filters,
    )
    round4_results.append({"label": "4B: ATR-target $40/trade (S&P 500 only)", "summary": s_4b})
    print_report(s_4b, "4B: ATR-target $40/trade sizing, S&P 500 only")

    # ── 4C: Hard price cap $5–$150, fixed sizing ──────────────────────────────
    _, s_4c = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.15, max_open_pos=5,
        sizing_mode="fixed_pct",
        min_price=5.0, max_price=150.0,
        **r4_filters,
    )
    round4_results.append({"label": "4C: Price $5–$150 hard cap, 15% fixed", "summary": s_4c})
    print_report(s_4c, "4C: Hard price cap $5–$150 (AZO fix), fixed 15% sizing")

    # ── 4D: Hard price cap + ATR-target + 15% cap ────────────────────────────
    _, s_4d = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.15, max_open_pos=5,
        sizing_mode="atr_target_40",
        min_price=5.0, max_price=150.0,
        **r4_filters,
    )
    round4_results.append({"label": "4D: Price $5–$150 + ATR-target $40 + 15% cap", "summary": s_4d})
    print_report(s_4d, "4D: Hard price cap + ATR-target $40 + 15% hard cap")

    # ── 4E: Hard price cap + ATR-target + 20% cap (vs 15%) ───────────────────
    _, s_4e = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.20, max_open_pos=5,
        sizing_mode="atr_target_40",
        min_price=5.0, max_price=150.0,
        **r4_filters,
    )
    round4_results.append({"label": "4E: Price $5–$150 + ATR-target $40 + 20% cap", "summary": s_4e})
    print_report(s_4e, "4E: Hard price cap + ATR-target $40 + 20% hard cap")

    # ── Summary comparison table ──────────────────────────────────────────────
    print_comparison(round4_results, title="ROUND 4 SUMMARY — Path to $40/Day")

    # ── Decision table ────────────────────────────────────────────────────────
    print(f"\n  {'Scenario':<48} {'$/Day':>8}  {'Win%':>6}  {'Decision'}")
    print(f"  {'-'*75}")
    baseline_daily = s_4a.get("avg_daily_pnl", 0) if s_4a else 0
    for r in round4_results:
        s = r.get("summary") or {}
        daily = s.get("avg_daily_pnl", 0)
        wr    = s.get("win_rate_pct", 0)
        delta = daily - baseline_daily
        arrow = f"▲ +${delta:.2f}/day" if delta > 0.01 else (f"▼ ${delta:.2f}/day" if delta < -0.01 else "  baseline")
        verdict = "✅ ADOPT" if daily >= 40 and wr >= 65 else ("⚠️  CONSIDER" if daily >= 30 else "❌ SKIP")
        print(f"  {r['label']:<48} ${daily:>6.2f}  {wr:>5.1f}%  {arrow}  {verdict}")

    print("\nRound 4 complete.")
    print("Adopt whichever scenario hits ≥$40/day with ≥65% win rate and ≤35% max drawdown.")

    # ═══════════════════════════════════════════════════════════════════════════
    # ROUND 5 — Extended Universe: S&P 500 + NASDAQ-100 (~600 tickers)
    # Does adding NASDAQ-100 stocks increase trade frequency and total P&L?
    # Uses separate cache files — does NOT invalidate Rounds 1-4 results.
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 68)
    print("  ROUND 5: Extended Universe — S&P 500 + NASDAQ-100 (~600 tickers)")
    print("=" * 68)
    print("  Downloading any NASDAQ-100 tickers not already in S&P 500 cache...")
    print("  (Incremental — only new tickers are downloaded)\n")

    ext_tickers = get_extended_tickers()
    sp_tickers  = get_sp500_tickers()
    new_tickers = [t for t in ext_tickers if t not in set(sp_tickers)]
    print(f"  S&P 500: {len(sp_tickers)} tickers  |  NASDAQ-100 additions: {len(new_tickers)}  |  Total: {len(ext_tickers)}")

    ext_data = load_or_download_data(ext_tickers, cache_file=EXTENDED_CACHE_FILE)
    if not ext_data:
        print("ERROR: Could not load extended universe data.")
    else:
        print(f"\n  Precomputing indicators for {len(ext_data)} tickers...")
        ext_rows, ext_processed = precompute_signals(ext_data, indicators_cache=EXTENDED_INDICATORS_CACHE)
        if not ext_rows:
            print("ERROR: Could not compute indicators for extended universe.")
        else:
            atr_stops   = [1.5, 2.0, 2.5, 3.0, 3.5]
            atr_targets = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
            ext_enriched = precompute_exits(ext_rows, ext_processed, atr_stops, atr_targets,
                                            exits_cache=EXTENDED_EXITS_CACHE)
            print(f"  Extended universe rows: {len(ext_enriched)}\n")

            R5_RSI_BUY    = 38
            R5_RSI_SELL   = 55
            R5_ATR_STOP   = 3.5
            R5_ATR_TARGET = 6.0
            r5_filters = dict(
                volume_min_ratio=1.2,
                spy_trend=spy_trend,
                vix_data=vix_data,
                vix_max=25,
                spy_ma="above_50ma",
                min_price=5.0,
                max_price=150.0,
            )

            # 5A: S&P 500 only (baseline from Round 4)
            _, s_5a = simulate_fast(
                enriched_2y, R5_RSI_BUY, R5_RSI_SELL, R5_ATR_STOP, R5_ATR_TARGET,
                max_position_pct=0.12, max_open_pos=5,
                sizing_mode="fixed_pct",
                **r5_filters,
            )
            print_report(s_5a, "5A: S&P 500 only (503 tickers, 12% fixed sizing)")

            # 5B: S&P 500 + NASDAQ-100 (extended universe)
            _, s_5b = simulate_fast(
                ext_enriched, R5_RSI_BUY, R5_RSI_SELL, R5_ATR_STOP, R5_ATR_TARGET,
                max_position_pct=0.12, max_open_pos=5,
                sizing_mode="fixed_pct",
                **r5_filters,
            )
            print_report(s_5b, f"5B: S&P 500 + NASDAQ-100 (~{len(ext_data)} tickers, 12% fixed)")

            # 5C: Full market (NYSE + NASDAQ, ~6,500 raw → filtered by price/vol in simulator)
            print("\n" + "=" * 68)
            print("  PART C — Full Market (NYSE + NASDAQ, ~6,500 raw tickers)")
            print("  Downloading missing tickers — this may take 15–30 min first run.")
            print("=" * 68)
            full_tickers = get_full_market_tickers()
            print(f"  Raw tickers fetched: {len(full_tickers)}")
            full_data = load_or_download_data(full_tickers, cache_file=FULL_MARKET_CACHE_FILE)
            print(f"  Tickers with 2yr+ data: {len(full_data)}")
            full_rows, full_processed = precompute_signals(full_data, indicators_cache=FULL_MARKET_INDICATORS_CACHE)
            print(f"  Signal candidates: {len(full_rows)}")
            full_enriched = precompute_exits(full_rows, full_processed, atr_stops, atr_targets,
                                             exits_cache=FULL_MARKET_EXITS_CACHE)
            print(f"  Enriched rows: {len(full_enriched)}\n")

            _, s_5c = simulate_fast(
                full_enriched, R5_RSI_BUY, R5_RSI_SELL, R5_ATR_STOP, R5_ATR_TARGET,
                max_position_pct=0.12, max_open_pos=5,
                sizing_mode="fixed_pct",
                **r5_filters,
            )
            print_report(s_5c, f"5C: Full market (~{len(full_data)} tickers, 12% fixed)")

            round5_results = [
                {"label": f"5A: S&P 500 only ({len(data)} tickers)", "summary": s_5a},
                {"label": f"5B: S&P 500 + NASDAQ-100 ({len(ext_data)} tickers)", "summary": s_5b},
                {"label": f"5C: Full NYSE + NASDAQ ({len(full_data)} tickers)", "summary": s_5c},
            ]
            print_comparison(round5_results, title="ROUND 5 SUMMARY — Universe Size Comparison")

            # Per-scenario verdict vs S&P 500 baseline
            if s_5a:
                base_pnl = s_5a["total_net_pnl"]
                print(f"\n  Impact vs S&P 500 baseline (5A):")
                for label, s in [("5B (NASDAQ-100 add)", s_5b), ("5C (Full market)", s_5c)]:
                    if s:
                        dp = s["total_net_pnl"] - base_pnl
                        dt = s["total_trades"] - s_5a["total_trades"]
                        dw = s["win_rate_pct"]  - s_5a["win_rate_pct"]
                        verdict = (
                            "✅ ADOPT"
                            if dp > 50 and dw >= -1.0
                            else "⚠️  NEUTRAL"
                            if dp > 0
                            else "❌ SKIP"
                        )
                        print(f"    {label}: trades {'+' if dt>=0 else ''}{dt}  "
                              f"P&L {'▲' if dp>=0 else '▼'} ${abs(dp):,.0f}  "
                              f"win rate {dw:+.1f}%  → {verdict}")

    print("\nRound 5 complete.")
