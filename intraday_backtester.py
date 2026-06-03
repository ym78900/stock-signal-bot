"""
intraday_backtester.py — Backtest a 15-min intraday RSI + VWAP mean-reversion
strategy against 2 years of S&P 500 intraday data via Alpaca IEX feed.

Run directly:
    /Library/Developer/CommandLineTools/usr/bin/python3.9 intraday_backtester.py

How it works:
1. Downloads 2 years of 15-min bars for all S&P 500 stocks via Alpaca IEX — cached
2. Computes per-session VWAP (resets at 9:30 AM ET daily — never carried over)
3. Computes RSI, ATR, volume ratio per bar — cached
4. Precomputes exit outcomes within the same session — cached
5. Runs 4-part test: RSI threshold, volume filter, VWAP filter, optimizer

Key rules:
- Positions NEVER held overnight — force-exit at 3:45 PM ET if not already closed
- VWAP resets every session at 9:30 AM ET — NEVER carried forward
- Entry: next bar open after signal bar closes
- No earnings filter needed (no overnight holds)
- SPY trend checked once at session open (not per bar)
"""

import logging
import os
import sys
import pickle
import time
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import ta as ta_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from scanner import get_sp500_tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

INTRADAY_YEARS      = 2
RSI_PERIOD          = 14
ATR_PERIOD          = 14
VOL_AVG_BARS        = 20         # rolling window for volume ratio (bars, not days)
STARTING_CAPITAL    = 5000.0
MAX_POSITION_PCT    = 0.12       # confirmed best from swing Round 1
MAX_OPEN_POSITIONS  = 5
CONSECUTIVE_LOSS_LIMIT = 3
IBKR_FEE_PER_TRADE  = 1.0       # per side

ET = ZoneInfo("America/New_York")
MARKET_OPEN_TIME  = "09:30"
MARKET_CLOSE_TIME = "16:00"
FORCE_EXIT_TIME   = "15:45"     # force-close any open positions at this bar

BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
INTRADAY_CACHE    = os.path.join(BASE_DIR, "intraday_cache.pkl")
INTRADAY_IND      = os.path.join(BASE_DIR, "intraday_indicators.pkl")
INTRADAY_EXITS    = os.path.join(BASE_DIR, "intraday_exits.pkl")
INTRADAY_SPY      = os.path.join(BASE_DIR, "intraday_spy.pkl")

INDICATORS_VERSION = 1


# ── Step 1: Download 15-min bars from Alpaca IEX ──────────────────────────────

def download_intraday_data(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    if os.path.exists(INTRADAY_CACHE):
        logger.info("Loading intraday data from cache...")
        with open(INTRADAY_CACHE, "rb") as f:
            return pickle.load(f)

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from dotenv import load_dotenv
    load_dotenv()

    api_key    = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    client     = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    end_date   = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=INTRADAY_YEARS * 365 + 10)

    logger.info(f"Downloading 15-min bars: {start_date.date()} → {end_date.date()}")
    logger.info(f"Tickers: {len(tickers)}  |  Feed: IEX (free tier)")

    BATCH_SIZE = 50
    batches    = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    all_data: Dict[str, pd.DataFrame] = {}

    for b_idx, batch in enumerate(batches):
        logger.info(f"  Batch {b_idx + 1}/{len(batches)} ({len(batch)} tickers)...")
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=start_date,
                end=end_date,
                feed="iex",
            )
            bars = client.get_stock_bars(req)
            df_all = bars.df

            if df_all.empty:
                continue

            # Multi-ticker response has (symbol, timestamp) MultiIndex
            if isinstance(df_all.index, pd.MultiIndex):
                for ticker in batch:
                    try:
                        df_t = df_all.xs(ticker, level="symbol").copy()
                        df_t = _filter_market_hours(df_t)
                        if len(df_t) >= 200:
                            all_data[ticker] = df_t
                    except KeyError:
                        continue
            else:
                # Single ticker response
                ticker = batch[0]
                df_t   = _filter_market_hours(df_all.copy())
                if len(df_t) >= 200:
                    all_data[ticker] = df_t

        except Exception as e:
            logger.warning(f"  Batch {b_idx + 1} failed: {e}")

        # Be gentle with the API
        time.sleep(0.3)

    logger.info(f"Downloaded {len(all_data)} tickers. Saving cache (~may be large)...")
    with open(INTRADAY_CACHE, "wb") as f:
        pickle.dump(all_data, f)
    return all_data


def _filter_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars during regular market hours (9:30–16:00 ET)."""
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.between_time(MARKET_OPEN_TIME, MARKET_CLOSE_TIME)
    return df


# ── Step 1b: SPY daily trend for session-open filter ─────────────────────────

def load_spy_daily() -> Dict[date, bool]:
    """Returns {date: above_50ma} using daily SPY data — one check per session."""
    if os.path.exists(INTRADAY_SPY):
        logger.info("Loading SPY daily trend from cache...")
        with open(INTRADAY_SPY, "rb") as f:
            return pickle.load(f)

    import yfinance as yf
    logger.info("Downloading SPY daily data for session trend filter...")
    end   = datetime.today()
    start = end - timedelta(days=INTRADAY_YEARS * 365 + 100)
    spy   = yf.download("SPY", start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), interval="1d",
                         auto_adjust=True, progress=False)
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = spy.columns.get_level_values(0)
    spy = spy.reset_index()
    spy["ma50"] = spy["Close"].rolling(50).mean()
    spy.dropna(subset=["ma50"], inplace=True)

    date_col = "Date" if "Date" in spy.columns else spy.columns[0]
    result   = {}
    for _, row in spy.iterrows():
        d = row[date_col]
        if hasattr(d, "date"):
            d = d.date()
        result[d] = bool(row["Close"] >= row["ma50"])

    with open(INTRADAY_SPY, "wb") as f:
        pickle.dump(result, f)
    logger.info(f"SPY daily trend cached ({len(result)} days).")
    return result


# ── Step 2: Precompute per-bar indicators ─────────────────────────────────────

def _compute_session_vwap(session_df: pd.DataFrame) -> pd.Series:
    """
    VWAP = cumulative(typical_price × volume) / cumulative(volume)
    Computed fresh per session — NEVER shared across days.
    typical_price = (High + Low + Close) / 3
    """
    tp      = (session_df["high"] + session_df["low"] + session_df["close"]) / 3
    cum_pv  = (tp * session_df["volume"]).cumsum()
    cum_vol = session_df["volume"].cumsum()
    vwap    = cum_pv / cum_vol.replace(0, float("nan"))
    return vwap


def precompute_intraday_signals(
    data: Dict[str, pd.DataFrame],
) -> Tuple[List[dict], Dict[str, pd.DataFrame]]:
    """
    For each ticker, compute per-bar:
      RSI(14), ATR(14), volume_ratio, VWAP (session-aware), prev_rsi
    Returns (rows, processed) where rows are signal candidates.
    Cached with version check.

    VWAP is computed via vectorised groupby cumsum — never a Python loop per session.
    """
    if os.path.exists(INTRADAY_IND):
        with open(INTRADAY_IND, "rb") as f:
            cached = pickle.load(f)
        ver = cached[0] if isinstance(cached[0], int) else 0
        if ver == INDICATORS_VERSION:
            logger.info("Loading intraday indicators from cache...")
            _, rows, processed = cached
            return rows, processed
        logger.info(f"Indicator cache v{ver} outdated → rebuilding...")

    logger.info(f"Precomputing intraday indicators for {len(data)} tickers...")
    rows: List[dict]                   = []
    processed: Dict[str, pd.DataFrame] = {}

    cutoff_start = (datetime.now(ET) - timedelta(days=INTRADAY_YEARS * 365)).date()
    done = 0

    for ticker, df in data.items():
        try:
            df = df.copy()
            df.columns = [c.lower() for c in df.columns]

            # ── Core indicators (vectorised via ta library) ───────────────────
            df["rsi"] = ta_lib.momentum.RSIIndicator(
                df["close"], window=RSI_PERIOD
            ).rsi()
            df["atr"] = ta_lib.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=ATR_PERIOD
            ).average_true_range()
            df["vol_avg"]      = df["volume"].rolling(window=VOL_AVG_BARS).mean()
            df["volume_ratio"] = df["volume"] / df["vol_avg"]

            df.dropna(subset=["rsi", "atr", "volume_ratio"], inplace=True)

            # ── Session date column ───────────────────────────────────────────
            df["session_date"] = df.index.date

            # ── Vectorised VWAP (no Python loop per session) ──────────────────
            # session_id increments each time the date changes
            df["_sid"]   = (df["session_date"] != pd.Series(
                df["session_date"].values, index=df.index
            ).shift().values).cumsum()
            df["_tp"]    = (df["high"] + df["low"] + df["close"]) / 3
            df["_tp_v"]  = df["_tp"] * df["volume"]
            df["_cpv"]   = df.groupby("_sid")["_tp_v"].cumsum()
            df["_cvol"]  = df.groupby("_sid")["volume"].cumsum()
            df["vwap"]   = df["_cpv"] / df["_cvol"].replace(0, float("nan"))
            df.drop(columns=["_sid", "_tp", "_tp_v", "_cpv", "_cvol"], inplace=True)

            df.dropna(subset=["vwap"], inplace=True)
            df = df.reset_index()   # move timestamp to column
            processed[ticker] = df

            # ── Build signal candidate rows ───────────────────────────────────
            ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]

            for i in range(1, len(df) - 1):
                row      = df.iloc[i]
                prev_row = df.iloc[i - 1]
                next_row = df.iloc[i + 1]

                ts        = row[ts_col]
                row_date  = ts.date() if hasattr(ts, "date") else ts
                if row_date < cutoff_start:
                    continue

                next_ts   = next_row[ts_col]
                next_date = next_ts.date() if hasattr(next_ts, "date") else None
                if next_date != row_date:
                    continue    # last bar of session — no next-bar entry

                bar_time = ts.strftime("%H:%M") if hasattr(ts, "strftime") else ""
                if bar_time >= FORCE_EXIT_TIME:
                    continue

                close_price = float(row["close"])
                atr_val     = float(row["atr"])
                vwap_val    = float(row["vwap"])

                rows.append({
                    "ticker":        ticker,
                    "timestamp":     ts,
                    "session_date":  row_date,
                    "bar_time":      bar_time,
                    "bar_idx":       i,
                    "rsi":           float(row["rsi"]),
                    "prev_rsi":      float(prev_row["rsi"]),
                    "close":         close_price,
                    "vwap":          vwap_val,
                    "above_vwap":    close_price >= vwap_val,
                    "atr":           atr_val,
                    "volume_ratio":  float(row["volume_ratio"]),
                    "entry_price":   float(next_row["open"]),
                    "entry_bar_idx": i + 1,
                    "entry_date":    next_date,
                })

        except Exception as e:
            logger.debug(f"Indicator precompute failed for {ticker}: {e}")
            continue

        done += 1
        if done % 50 == 0:
            logger.info(f"  Indicators: {done}/{len(data)} tickers, {len(rows):,} rows so far...")

    rows.sort(key=lambda x: x["timestamp"])
    logger.info(f"Precomputed {len(rows):,} intraday signal candidates. Saving...")
    with open(INTRADAY_IND, "wb") as f:
        pickle.dump((INDICATORS_VERSION, rows, processed), f)
    return rows, processed


# ── Step 3: Precompute intraday exit outcomes ─────────────────────────────────

def precompute_intraday_exits(
    rows: List[dict],
    processed: Dict[str, pd.DataFrame],
    atr_stop_values: List[float],
    atr_target_values: List[float],
) -> Tuple[List[dict], List[tuple], np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns:
        rows_compact  — compact row dicts (no exit_outcomes stored inline)
        atr_combos    — list of (stop_mult, target_mult) — column index for arrays
        exit_prices   — float32 (n_valid_rows, n_combos)
        exit_reasons  — uint8   (n_valid_rows, n_combos): 0=stop 1=target 2=eod
        bars_held     — uint16  (n_valid_rows, n_combos)
    Storing as numpy arrays instead of nested dicts keeps RAM ~1.5 GB vs ~40 GB.
    """
    CACHE_VERSION = 2
    if os.path.exists(INTRADAY_EXITS):
        logger.info("Loading intraday exits from cache...")
        with open(INTRADAY_EXITS, "rb") as f:
            cached = pickle.load(f)
        if isinstance(cached, dict) and cached.get("version") == CACHE_VERSION:
            return (cached["rows"], cached["atr_combos"],
                    cached["exit_prices"], cached["exit_reasons"], cached["bars_held"])
        logger.info("  Old exit cache format — rebuilding...")
        os.remove(INTRADAY_EXITS)

    atr_combos = [
        (s, t) for s in atr_stop_values
        for t in atr_target_values
        if t / s >= 1.3
    ]
    n_combos   = len(atr_combos)
    stop_mults = np.array([c[0] for c in atr_combos], dtype=np.float64)
    tgt_mults  = np.array([c[1] for c in atr_combos], dtype=np.float64)

    logger.info(f"Precomputing intraday exits: {len(rows):,} rows × {n_combos} ATR combos (numpy arrays)...")

    # ── Pre-build session index ────────────────────────────────────────────────
    logger.info("  Building session index...")
    session_index: Dict[str, Dict] = {}
    for ticker, df in processed.items():
        ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
        df["_date"] = df[ts_col].apply(lambda x: x.date() if hasattr(x, "date") else x)
        df["_time"] = df[ts_col].apply(
            lambda x: x.strftime("%H:%M") if hasattr(x, "strftime") else "23:59"
        )
        session_index[ticker] = {}
        for d, grp in df.groupby("_date"):
            grp_trimmed = grp[grp["_time"] <= FORCE_EXIT_TIME]
            session_index[ticker][d] = {
                "highs":   grp_trimmed["high"].values.astype(np.float32),
                "lows":    grp_trimmed["low"].values.astype(np.float32),
                "closes":  grp_trimmed["close"].values.astype(np.float32),
                "indices": grp_trimmed.index.values,
            }

    # ── Checkpoint support ─────────────────────────────────────────────────────
    CHECKPOINT   = os.path.join(BASE_DIR, "intraday_exits_ckpt.pkl")
    start_idx    = 0
    rows_compact: List[dict]      = []
    ep_chunks:    List[np.ndarray] = []
    er_chunks:    List[np.ndarray] = []
    bh_chunks:    List[np.ndarray] = []

    if os.path.exists(CHECKPOINT):
        try:
            with open(CHECKPOINT, "rb") as f:
                ckpt = pickle.load(f)
            start_idx    = ckpt["start_idx"]
            rows_compact = ckpt["rows_compact"]
            ep_chunks    = ckpt["ep_chunks"]
            er_chunks    = ckpt["er_chunks"]
            bh_chunks    = ckpt["bh_chunks"]
            logger.info(f"  Resuming from checkpoint at row {start_idx:,} / {len(rows):,}")
        except Exception as e:
            logger.warning(f"  Checkpoint load failed ({e}) — starting fresh")
            start_idx = 0
            rows_compact = []
            ep_chunks = er_chunks = bh_chunks = []

    CHUNK = 200_000
    buf_ep  = np.empty((CHUNK, n_combos), dtype=np.float32)
    buf_er  = np.empty((CHUNK, n_combos), dtype=np.uint8)
    buf_bh  = np.empty((CHUNK, n_combos), dtype=np.uint16)
    buf_rows: List[dict] = []
    buf_pos = 0
    total   = len(rows)

    def _flush():
        nonlocal buf_pos, buf_rows
        if buf_pos == 0:
            return
        ep_chunks.append(buf_ep[:buf_pos].copy())
        er_chunks.append(buf_er[:buf_pos].copy())
        bh_chunks.append(buf_bh[:buf_pos].copy())
        rows_compact.extend(buf_rows)
        buf_pos  = 0
        buf_rows = []

    REASON_EOD  = np.uint8(2)
    REASON_STOP = np.uint8(0)
    REASON_TGT  = np.uint8(1)

    for abs_i, row in enumerate(rows[start_idx:], start=start_idx):
        ticker      = row["ticker"]
        entry_idx   = row["entry_bar_idx"]
        entry_price = float(row["entry_price"])
        atr         = float(row["atr"])
        session_d   = row["session_date"]

        sess = session_index.get(ticker, {}).get(session_d)
        if sess is None:
            continue

        mask   = sess["indices"] >= entry_idx
        highs  = sess["highs"][mask].astype(np.float64)
        lows   = sess["lows"][mask].astype(np.float64)
        closes = sess["closes"][mask].astype(np.float64)
        n_bars = len(highs)
        if n_bars == 0:
            continue

        stop_prices   = entry_price - atr * stop_mults   # (n_combos,)
        target_prices = entry_price + atr * tgt_mults    # (n_combos,)

        # Vectorised across all combos simultaneously
        lows_2d    = lows[:, np.newaxis]                           # (n_bars, 1)
        highs_2d   = highs[:, np.newaxis]                          # (n_bars, 1)
        stop_mat   = lows_2d  <= stop_prices[np.newaxis, :]       # (n_bars, n_combos)
        target_mat = highs_2d >= target_prices[np.newaxis, :]      # (n_bars, n_combos)

        any_stop   = stop_mat.any(axis=0)                          # (n_combos,)
        any_target = target_mat.any(axis=0)

        stop_bars = np.where(any_stop,   np.argmax(stop_mat,   axis=0), n_bars).astype(np.int32)
        tgt_bars  = np.where(any_target, np.argmax(target_mat, axis=0), n_bars).astype(np.int32)

        # Classify each combo
        eod_mask  = (stop_bars == n_bars) & (tgt_bars == n_bars)
        stop_mask = (~eod_mask) & (stop_bars <= tgt_bars)
        tgt_mask  = (~eod_mask) & (~stop_mask)

        ep_row = np.empty(n_combos, dtype=np.float32)
        er_row = np.empty(n_combos, dtype=np.uint8)
        bh_row = np.empty(n_combos, dtype=np.uint16)

        last_close = float(closes[-1])
        ep_row[eod_mask]  = last_close
        er_row[eod_mask]  = REASON_EOD
        bh_row[eod_mask]  = np.uint16(n_bars)

        ep_row[stop_mask] = stop_prices[stop_mask].astype(np.float32)
        er_row[stop_mask] = REASON_STOP
        bh_row[stop_mask] = (stop_bars[stop_mask] + 1).astype(np.uint16)

        ep_row[tgt_mask]  = target_prices[tgt_mask].astype(np.float32)
        er_row[tgt_mask]  = REASON_TGT
        bh_row[tgt_mask]  = (tgt_bars[tgt_mask] + 1).astype(np.uint16)

        buf_ep[buf_pos]   = ep_row
        buf_er[buf_pos]   = er_row
        buf_bh[buf_pos]   = bh_row
        buf_rows.append(row)
        buf_pos += 1

        if buf_pos == CHUNK:
            _flush()

        processed_count = abs_i + 1
        if processed_count % 500_000 == 0:
            _flush()
            logger.info(f"  Exits: {processed_count:,}/{total:,} rows...")
            with open(CHECKPOINT, "wb") as f:
                pickle.dump({
                    "start_idx":   processed_count,
                    "rows_compact": rows_compact,
                    "ep_chunks":   ep_chunks,
                    "er_chunks":   er_chunks,
                    "bh_chunks":   bh_chunks,
                }, f)

    _flush()

    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)

    logger.info("  Stacking result arrays...")
    if ep_chunks:
        exit_prices  = np.vstack(ep_chunks).astype(np.float32)
        exit_reasons = np.vstack(er_chunks).astype(np.uint8)
        bars_held_np = np.vstack(bh_chunks).astype(np.uint16)
    else:
        exit_prices  = np.zeros((0, n_combos), dtype=np.float32)
        exit_reasons = np.zeros((0, n_combos), dtype=np.uint8)
        bars_held_np = np.zeros((0, n_combos), dtype=np.uint16)

    logger.info(f"Intraday exits precomputed for {len(rows_compact):,} rows. Saving...")
    with open(INTRADAY_EXITS, "wb") as f:
        pickle.dump({
            "version":      CACHE_VERSION,
            "rows":         rows_compact,
            "atr_combos":   atr_combos,
            "exit_prices":  exit_prices,
            "exit_reasons": exit_reasons,
            "bars_held":    bars_held_np,
        }, f)
    return rows_compact, atr_combos, exit_prices, exit_reasons, bars_held_np


# ── Step 4: Fast intraday simulation ──────────────────────────────────────────

def simulate_intraday(
    rows_compact: List[dict],
    atr_combos: List[tuple],
    exit_prices: np.ndarray,    # (n_rows, n_combos) float32
    exit_reasons: np.ndarray,   # (n_rows, n_combos) uint8: 0=stop 1=target 2=eod
    bars_held_arr: np.ndarray,  # (n_rows, n_combos) uint16
    rsi_buy: float,
    atr_stop: float,
    atr_target: float,
    # Filters
    volume_min_ratio: Optional[float]  = None,
    require_above_vwap: bool           = True,
    require_below_vwap: bool           = False,
    spy_daily: Optional[Dict]          = None,
    require_rsi_rising: bool           = False,
    # Portfolio params
    max_position_pct: float            = MAX_POSITION_PCT,
    max_open_pos: int                  = MAX_OPEN_POSITIONS,
    consec_loss_limit: int             = CONSECUTIVE_LOSS_LIMIT,
) -> Tuple[List[dict], dict]:
    try:
        combo_idx = atr_combos.index((atr_stop, atr_target))
    except ValueError:
        return [], {}

    _REASONS = ["stop_loss", "take_profit", "force_exit_eod"]

    trades:             List[dict] = []
    portfolio:          float      = STARTING_CAPITAL
    consecutive_losses: int        = 0
    open_positions: Dict[str, str] = {}

    for row_idx, row in enumerate(rows_compact):
        ticker    = row["ticker"]
        session_d = str(row["session_date"])

        open_positions = {t: d for t, d in open_positions.items() if d == session_d}

        if ticker in open_positions:
            continue
        if consecutive_losses >= consec_loss_limit:
            continue
        if len(open_positions) >= max_open_pos:
            continue

        if row["rsi"] >= rsi_buy:
            continue
        if require_above_vwap and not row["above_vwap"]:
            continue
        if require_below_vwap and row["above_vwap"]:
            continue
        if volume_min_ratio is not None and row.get("volume_ratio", 0) < volume_min_ratio:
            continue
        if spy_daily is not None:
            if not spy_daily.get(row["session_date"], True):
                continue
        if require_rsi_rising and row["rsi"] <= row["prev_rsi"]:
            continue

        entry_price = row["entry_price"]
        if entry_price <= 0:
            continue

        ep        = float(exit_prices[row_idx, combo_idx])
        er_code   = int(exit_reasons[row_idx, combo_idx])
        bh        = int(bars_held_arr[row_idx, combo_idx])
        reason    = _REASONS[er_code]

        qty      = max(1, int((portfolio * max_position_pct) / entry_price))
        gross    = (ep - entry_price) * qty
        fees     = IBKR_FEE_PER_TRADE * 2
        net_pnl  = round(gross - fees, 2)
        cost     = entry_price * qty
        pnl_pct  = round((net_pnl / cost) * 100, 2) if cost else 0

        portfolio += net_pnl
        open_positions[ticker] = session_d

        if net_pnl > 0:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        trades.append({
            "ticker":       ticker,
            "session_date": session_d,
            "bar_time":     row["bar_time"],
            "entry_price":  round(entry_price, 4),
            "exit_price":   round(ep, 4),
            "stop_loss":    round(entry_price - row["atr"] * atr_stop, 4),
            "take_profit":  round(entry_price + row["atr"] * atr_target, 4),
            "qty":          qty,
            "net_pnl":      net_pnl,
            "pnl_pct":      pnl_pct,
            "exit_reason":  reason,
            "bars_held":    bh,
            "rsi_at_entry": round(row["rsi"], 1),
            "win":          net_pnl > 0,
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
    profit_factor  = round(total_wins_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else 999.0

    equity = STARTING_CAPITAL
    peak   = STARTING_CAPITAL
    max_dd = 0.0
    for t in trades:
        equity += t["net_pnl"]
        peak    = max(peak, equity)
        dd      = (peak - equity) / peak * 100
        max_dd  = max(max_dd, dd)

    eod_exits = len([t for t in trades if t["exit_reason"] == "force_exit_eod"])
    sl_exits  = len([t for t in trades if t["exit_reason"] == "stop_loss"])
    tp_exits  = len([t for t in trades if t["exit_reason"] == "take_profit"])

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
        "stop_loss_exits":   sl_exits,
        "take_profit_exits": tp_exits,
        "eod_exits":         eod_exits,
        "best_trade":        max(trades, key=lambda x: x["net_pnl"]),
        "worst_trade":       min(trades, key=lambda x: x["net_pnl"]),
    }


def _print_row(label, s, baseline_pnl=None, width=32):
    if not s:
        print(f"  {label:<{width}} {'no trades':>7}")
        return
    delta = (s["total_net_pnl"] - baseline_pnl) if baseline_pnl is not None else 0
    arrow = (f"▲ +${delta:,.0f}" if delta > 1
             else (f"▼ -${abs(delta):,.0f}" if delta < -1 else "— baseline"))
    print(f"  {label:<{width}} {s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%"
          f"  ${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%"
          f"  -{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}"
          + (f"  {arrow}" if baseline_pnl is not None else ""))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    SWING_BASELINE = {
        "total_trades":     136,
        "win_rate_pct":     74.3,
        "total_net_pnl":    6579.25,
        "total_return_pct": 131.6,
        "max_drawdown_pct": 3.4,
        "profit_factor":    3.23,
    }

    print("\nStock Signal Bot — Intraday Backtester  |  15-min RSI + VWAP")
    print(f"Capital: ${STARTING_CAPITAL:,.0f}  |  Position: {MAX_POSITION_PCT*100:.0f}%  "
          f"|  Feed: Alpaca IEX  |  Window: {INTRADAY_YEARS} years\n")

    # ── 1. Load tickers ───────────────────────────────────────────────────────
    tickers = get_sp500_tickers()
    print(f"Found {len(tickers)} S&P 500 tickers.")

    # ── 2. Download / load 15-min data ────────────────────────────────────────
    data = download_intraday_data(tickers)
    if not data:
        print("ERROR: No intraday data available.")
        sys.exit(1)
    print(f"Loaded intraday data for {len(data)} tickers.")

    # ── 3. Load SPY daily trend ───────────────────────────────────────────────
    spy_daily = load_spy_daily()

    # ── 4. Precompute indicators ──────────────────────────────────────────────
    rows, processed = precompute_intraday_signals(data)
    if not rows:
        print("ERROR: No signal candidates computed.")
        sys.exit(1)
    print(f"Signal candidates: {len(rows):,}\n")

    # ── 5. Precompute exits ───────────────────────────────────────────────────
    atr_stops   = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    atr_targets = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
    rows_c, atr_combos, exit_prices, exit_reasons, bars_held_arr = \
        precompute_intraday_exits(rows, processed, atr_stops, atr_targets)
    print(f"Enriched rows: {len(rows_c):,}\n")

    # Convenience wrapper so call sites stay readable
    def _sim(rsi_buy, atr_stop, atr_target, **kw):
        return simulate_intraday(
            rows_c, atr_combos, exit_prices, exit_reasons, bars_held_arr,
            rsi_buy, atr_stop, atr_target, **kw
        )

    HDR = f"  {'Filter':<32} {'Trades':>7} {'Win%':>7} {'P&L':>12} {'Return':>9} {'MaxDD':>8} {'PF':>6}  vs baseline"
    SEP = "=" * 96

    # ══════════════════════════════════════════════════════════════════════════
    # PART A — RSI threshold sweep
    # Question: what RSI level gives the best quality intraday signals?
    # ══════════════════════════════════════════════════════════════════════════
    print(SEP)
    print("  PART A — RSI buy threshold  (above VWAP, vol 1.2×, SPY filter on)")
    print(SEP)
    print(HDR)
    print(f"  {'─'*94}")

    # Fixed params for Part A
    A_ATR_STOP   = 1.5
    A_ATR_TARGET = 3.0
    A_VOL        = 1.2

    rsi_results = []
    for rsi_thresh in [20, 25, 28, 30, 33, 35, 38, 40, 45]:
        _, s = _sim(
            rsi_thresh, A_ATR_STOP, A_ATR_TARGET,
            volume_min_ratio=A_VOL,
            require_above_vwap=True,
            spy_daily=spy_daily,
        )
        label = f"RSI < {rsi_thresh}"
        rsi_results.append((rsi_thresh, s))
        _print_row(label, s)

    # Best RSI by PF with >= 20 trades
    best_rsi = max(
        [(r, s) for r, s in rsi_results if s and s["total_trades"] >= 20],
        key=lambda x: x[1]["profit_factor"],
        default=(30, None),
    )[0]
    print(f"\n  → Best RSI threshold: < {best_rsi}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART B — Volume filter
    # Question: does intraday volume spike matter as much as daily?
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{SEP}")
    print(f"  PART B — Volume filter  (RSI < {best_rsi}, above VWAP, SPY filter on)")
    print(SEP)
    print(HDR)
    print(f"  {'─'*94}")

    vol_results = []
    for vol in [None, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5]:
        _, s = _sim(
            best_rsi, A_ATR_STOP, A_ATR_TARGET,
            volume_min_ratio=vol,
            require_above_vwap=True,
            spy_daily=spy_daily,
        )
        label = "No vol filter" if vol is None else f"Vol > {vol:.1f}×"
        baseline_pnl = vol_results[0][1]["total_net_pnl"] if vol_results else None
        vol_results.append((vol, s))
        _print_row(label, s, baseline_pnl)

    best_vol = max(
        [(v, s) for v, s in vol_results if s and s["total_trades"] >= 20],
        key=lambda x: x[1]["profit_factor"],
        default=(1.2, None),
    )[0]
    print(f"\n  → Best volume threshold: {best_vol if best_vol else 'none'}×")

    # ══════════════════════════════════════════════════════════════════════════
    # PART C — VWAP filter
    # Question: should we buy oversold stocks above OR below VWAP?
    # Above VWAP = dip in an uptrending session (with the trend)
    # Below VWAP = contrarian (session is already weak, buying oversold)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{SEP}")
    print(f"  PART C — VWAP filter  (RSI < {best_rsi}, vol {best_vol}×, SPY on)")
    print(SEP)
    print(HDR)
    print(f"  {'─'*94}")

    vwap_results = []
    for label, above, below in [
        ("No VWAP filter",         False, False),
        ("Price above VWAP only",  True,  False),
        ("Price below VWAP only",  False, True),
    ]:
        _, s = _sim(
            best_rsi, A_ATR_STOP, A_ATR_TARGET,
            volume_min_ratio=best_vol,
            require_above_vwap=above,
            require_below_vwap=below,
            spy_daily=spy_daily,
        )
        baseline_pnl = vwap_results[0][1]["total_net_pnl"] if vwap_results else None
        vwap_results.append((label, s))
        _print_row(label, s, baseline_pnl)

    best_vwap_label, best_vwap_s = max(
        vwap_results,
        key=lambda x: x[1]["profit_factor"] if x[1] else 0,
    )
    best_above_vwap = best_vwap_label == "Price above VWAP only"
    best_below_vwap = best_vwap_label == "Price below VWAP only"
    print(f"\n  → Best VWAP setting: {best_vwap_label}")

    # ══════════════════════════════════════════════════════════════════════════
    # PART D — Full ATR optimizer
    # Question: what stop/target multipliers work best intraday?
    # (Intraday moves are smaller — expect tighter ATR than swing's ×3.5/×6.0)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{SEP}")
    print(f"  PART D — ATR optimizer  (RSI < {best_rsi}, vol {best_vol}×, "
          f"{'above' if best_above_vwap else 'below' if best_below_vwap else 'no'} VWAP)")
    print(SEP)

    atr_combos_test = [
        (s, t) for s in atr_stops for t in atr_targets if t / s >= 1.3
    ]
    print(f"  Testing {len(atr_combos_test)} ATR combinations...\n")
    print(f"  {'Stop×':<8} {'Target×':<9} {'Trades':>7} {'Win%':>7} {'P&L':>12} "
          f"{'Return':>9} {'MaxDD':>8} {'PF':>6}")
    print(f"  {'─'*80}")

    optimizer_results = []
    for atr_s, atr_t in atr_combos_test:
        _, s = _sim(
            best_rsi, atr_s, atr_t,
            volume_min_ratio=best_vol,
            require_above_vwap=best_above_vwap,
            require_below_vwap=best_below_vwap,
            spy_daily=spy_daily,
        )
        if s and s["total_trades"] >= 20:
            optimizer_results.append((atr_s, atr_t, s))

    optimizer_results.sort(key=lambda x: x[2]["profit_factor"], reverse=True)

    for atr_s, atr_t, s in optimizer_results[:20]:
        print(f"  ×{atr_s:<6.1f} ×{atr_t:<7.1f} {s['total_trades']:>7} "
              f"{s['win_rate_pct']:>6.1f}%  ${s['total_net_pnl']:>+9,.2f} "
              f"{s['total_return_pct']:>+8.1f}%  -{s['max_drawdown_pct']:>4.1f}%  "
              f"{s['profit_factor']:>5.2f}")

    if optimizer_results:
        best_atr_s, best_atr_t, best_atr_s_obj = optimizer_results[0]
    else:
        best_atr_s, best_atr_t = A_ATR_STOP, A_ATR_TARGET
        best_atr_s_obj = None

    print(f"\n  → Best ATR: stop ×{best_atr_s}  target ×{best_atr_t}")

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL VERDICT — Best intraday combo vs swing strategy
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{SEP}")
    print("  FINAL VERDICT — Intraday best combo vs Swing strategy baseline")
    print(SEP)

    _, best_intraday = _sim(
        best_rsi, best_atr_s, best_atr_t,
        volume_min_ratio=best_vol,
        require_above_vwap=best_above_vwap,
        require_below_vwap=best_below_vwap,
        spy_daily=spy_daily,
    )

    print(f"\n  {'Strategy':<36} {'Trades':>7} {'Win%':>7} {'P&L':>12} "
          f"{'Return':>9} {'MaxDD':>8} {'PF':>6}  {'Trades/yr':>10}")
    print(f"  {'─'*96}")

    # Swing baseline (confirmed from all 3 rounds)
    sw = SWING_BASELINE
    print(f"  {'Swing (RSI38, ATR×3.5/×6.0, daily)':<36} "
          f"{sw['total_trades']:>7} {sw['win_rate_pct']:>6.1f}%  "
          f"${sw['total_net_pnl']:>+9,.2f} {sw['total_return_pct']:>+8.1f}%  "
          f"-{sw['max_drawdown_pct']:>4.1f}%  {sw['profit_factor']:>5.2f}  "
          f"{'~68/yr':>10}")

    if best_intraday:
        s    = best_intraday
        tpy  = round(s["total_trades"] / INTRADAY_YEARS)
        label = (f"Intraday (RSI<{best_rsi}, ATR×{best_atr_s}/×{best_atr_t}, "
                 f"{'above' if best_above_vwap else 'below' if best_below_vwap else 'no'} VWAP)")
        print(f"  {label:<36} "
              f"{s['total_trades']:>7} {s['win_rate_pct']:>6.1f}%  "
              f"${s['total_net_pnl']:>+9,.2f} {s['total_return_pct']:>+8.1f}%  "
              f"-{s['max_drawdown_pct']:>4.1f}%  {s['profit_factor']:>5.2f}  "
              f"{f'~{tpy}/yr':>10}")

        # Exit breakdown
        print(f"\n  Exit breakdown (intraday best):")
        print(f"    Take profit:    {s['take_profit_exits']}  "
              f"({s['take_profit_exits']/s['total_trades']*100:.0f}%)")
        print(f"    Stop loss:      {s['stop_loss_exits']}  "
              f"({s['stop_loss_exits']/s['total_trades']*100:.0f}%)")
        print(f"    Force-exit EOD: {s['eod_exits']}  "
              f"({s['eod_exits']/s['total_trades']*100:.0f}%)")

        # Verdict
        print(f"\n  {'─'*96}")
        intraday_better = (
            s["profit_factor"] > sw["profit_factor"] and
            s["total_net_pnl"] > sw["total_net_pnl"]
        )
        swing_better = (
            sw["profit_factor"] > s["profit_factor"] and
            sw["total_net_pnl"] > s["total_net_pnl"]
        )
        if intraday_better:
            print("  VERDICT: ✅ INTRADAY wins on both P&L and profit factor")
            print("           → Build Phase 1 around intraday strategy")
            print("           → Swing strategy remains as secondary/backup")
        elif swing_better:
            print("  VERDICT: ✅ SWING wins on both P&L and profit factor")
            print("           → Build Phase 1 around swing strategy (original plan)")
            print("           → Intraday can be added as a secondary layer later")
        else:
            print("  VERDICT: ⚖️  MIXED — each wins on different metrics")
            print("           → Both strategies have merit")
            print("           → Phase 1 can run both simultaneously")
        print(f"\n  Best intraday: RSI < {best_rsi}  ATR ×{best_atr_s}/×{best_atr_t}  "
              f"Vol {best_vol}×  {'Above' if best_above_vwap else 'Below' if best_below_vwap else 'No'} VWAP")

        # Save results
        import json
        result = {
            "rsi_buy":        best_rsi,
            "atr_stop":       best_atr_s,
            "atr_target":     best_atr_t,
            "volume_ratio":   best_vol,
            "above_vwap":     best_above_vwap,
            "below_vwap":     best_below_vwap,
            "trades":         s["total_trades"],
            "win_rate":       s["win_rate_pct"],
            "net_pnl":        s["total_net_pnl"],
            "return_pct":     s["total_return_pct"],
            "max_dd":         s["max_drawdown_pct"],
            "profit_factor":  s["profit_factor"],
        }
        with open(os.path.join(BASE_DIR, "intraday_best.json"), "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n  Saved to intraday_best.json")

    print(f"\n{SEP}")
    print("  Intraday backtest complete.")
    print(f"{SEP}\n")
