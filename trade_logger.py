"""
trade_logger.py — Persistent trade log (CSV) + pending trade queue (JSON).

Trade lifecycle:
  queued  → order placed → open (filled) → closed (stop/target/manual)

CSV columns:
  id, ticker, signal_date, entry_date, entry_price, exit_date, exit_price,
  exit_reason, qty, stop_price, target_price, gross_pnl, fees, net_pnl,
  pnl_pct, win, alpaca_order_id, status
"""

import csv
import json
import logging
import os
import uuid
from datetime import date, datetime
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG     = os.path.join(BASE_DIR, "trades.csv")
PENDING_FILE  = os.path.join(BASE_DIR, "pending_trades.json")
PAUSE_FLAG    = os.path.join(BASE_DIR, "trading_paused.flag")

FEE_PER_SIDE  = 1.00   # IBKR fixed $1 per side

_COLUMNS = [
    "id", "ticker", "signal_date", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_reason",
    "qty", "stop_price", "target_price",
    "gross_pnl", "fees", "net_pnl", "pnl_pct",
    "win", "alpaca_order_id", "status",
]


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _ensure_csv() -> None:
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writeheader()


def _read_all() -> List[dict]:
    _ensure_csv()
    with open(TRADE_LOG, newline="") as f:
        return list(csv.DictReader(f))


def _write_all(rows: List[dict]) -> None:
    _ensure_csv()
    with open(TRADE_LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ── Pause flag ────────────────────────────────────────────────────────────────

def is_paused() -> bool:
    return os.path.exists(PAUSE_FLAG)


def pause_trading(reason: str = "") -> None:
    with open(PAUSE_FLAG, "w") as f:
        f.write(reason or "paused")
    logger.info(f"Auto-trading PAUSED. Reason: {reason or '(none)'}")


def resume_trading() -> None:
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    logger.info("Auto-trading RESUMED.")


# ── Pending trade queue ───────────────────────────────────────────────────────

def queue_pending_trade(
    ticker: str,
    signal_date: date,
    close_price: float,
    atr: float,
    rsi: float,
    volume_ratio: float,
) -> None:
    """Save a signal to the pending queue for next-morning execution."""
    stop_est   = round(close_price - atr * config.ATR_STOP_MULTIPLIER,   2)
    target_est = round(close_price + atr * config.ATR_TARGET_MULTIPLIER, 2)
    entry: dict = {
        "ticker":        ticker,
        "signal_date":   str(signal_date),
        "close_price":   close_price,
        "atr":           atr,
        "rsi":           rsi,
        "volume_ratio":  volume_ratio,
        "stop_est":      stop_est,
        "target_est":    target_est,
        "queued_at":     datetime.now(config.TIMEZONE).isoformat(),
    }
    trades = load_pending_trades()
    # Deduplicate by ticker — one pending trade per ticker at a time
    trades = [t for t in trades if t["ticker"] != ticker]
    trades.append(entry)
    with open(PENDING_FILE, "w") as f:
        json.dump(trades, f, indent=2)
    logger.info(f"Queued pending trade: {ticker} SL≈${stop_est} TP≈${target_est}")


def load_pending_trades() -> List[dict]:
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def clear_pending_trades() -> None:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "w") as f:
            json.dump([], f)


def remove_pending_trade(ticker: str) -> None:
    trades = [t for t in load_pending_trades() if t["ticker"] != ticker]
    with open(PENDING_FILE, "w") as f:
        json.dump(trades, f, indent=2)


# ── Trade lifecycle ───────────────────────────────────────────────────────────

def log_order_placed(
    ticker: str,
    signal_date: str,
    entry_price_est: float,
    stop_price: float,
    target_price: float,
    qty: int,
    alpaca_order_id: str,
) -> str:
    """Record an order as placed (status=open). Returns trade_id."""
    _ensure_csv()
    trade_id = str(uuid.uuid4())[:8]
    row = {
        "id":               trade_id,
        "ticker":           ticker,
        "signal_date":      signal_date,
        "entry_date":       str(date.today()),
        "entry_price":      entry_price_est,
        "exit_date":        "",
        "exit_price":       "",
        "exit_reason":      "",
        "qty":              qty,
        "stop_price":       stop_price,
        "target_price":     target_price,
        "gross_pnl":        "",
        "fees":             FEE_PER_SIDE * 2,
        "net_pnl":          "",
        "pnl_pct":          "",
        "win":              "",
        "alpaca_order_id":  alpaca_order_id,
        "status":           "open",
    }
    with open(TRADE_LOG, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore").writerow(row)
    logger.info(f"Logged order placed: {ticker} qty={qty} id={trade_id}")
    return trade_id


def update_entry_price(alpaca_order_id: str, entry_price: float, entry_date: str) -> bool:
    """Update trades.csv with the real fill price once the entry order executes."""
    rows = _read_all()
    updated = False
    for row in rows:
        if row["alpaca_order_id"] == alpaca_order_id and row["status"] == "open":
            if float(row.get("entry_price", 0)) != entry_price:
                row["entry_price"] = entry_price
                row["entry_date"]  = entry_date
                updated = True
                logger.info(f"Entry fill updated: {row['ticker']} @ ${entry_price}")
            break
    if updated:
        _write_all(rows)
    return updated


def mark_trade_closed(
    alpaca_order_id: str,
    exit_price: float,
    exit_date: str,
    exit_reason: str,
) -> Optional[dict]:
    """Update the trade row when a position closes. Returns the updated row."""
    rows = _read_all()
    updated = None
    for row in rows:
        if row["alpaca_order_id"] == alpaca_order_id and row["status"] == "open":
            entry_price = float(row["entry_price"])
            qty         = int(row["qty"])
            fees        = float(row["fees"])
            gross_pnl   = round((exit_price - entry_price) * qty, 2)
            net_pnl     = round(gross_pnl - fees, 2)
            cost        = entry_price * qty
            pnl_pct     = round((net_pnl / cost) * 100, 2) if cost else 0.0

            row["exit_date"]   = exit_date
            row["exit_price"]  = exit_price
            row["exit_reason"] = exit_reason
            row["gross_pnl"]   = gross_pnl
            row["net_pnl"]     = net_pnl
            row["pnl_pct"]     = pnl_pct
            row["win"]         = net_pnl > 0
            row["status"]      = "closed"
            updated = row
            break
    _write_all(rows)
    if updated:
        logger.info(
            f"Trade closed: {updated['ticker']} P&L=${updated['net_pnl']} ({updated['exit_reason']})"
        )
    return updated


# ── Queries ───────────────────────────────────────────────────────────────────

def get_open_trades() -> List[dict]:
    return [r for r in _read_all() if r["status"] == "open"]


def get_all_trades(n: int = 50) -> List[dict]:
    rows = _read_all()
    return rows[-n:] if len(rows) > n else rows


def get_stats() -> dict:
    rows   = _read_all()
    closed = [r for r in rows if r["status"] == "closed"]
    if not closed:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "total_net_pnl": 0.0,
            "total_return_pct": 0.0, "consecutive_losses": 0,
        }
    wins     = [r for r in closed if r.get("win") in (True, "True")]
    losses   = [r for r in closed if r.get("win") not in (True, "True")]
    net_pnls = [float(r["net_pnl"]) for r in closed if r["net_pnl"] not in ("", None)]
    total    = sum(net_pnls)
    return {
        "total_trades":       len(closed),
        "wins":               len(wins),
        "losses":             len(losses),
        "win_rate_pct":       round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_net_pnl":      round(total, 2),
        "total_return_pct":   round(total / 5000 * 100, 2),
        "consecutive_losses": get_consecutive_losses(),
    }


def get_consecutive_losses() -> int:
    """Count trailing consecutive losses from the most recent closed trades."""
    rows   = [r for r in _read_all() if r["status"] == "closed"]
    count  = 0
    for row in reversed(rows):
        if row.get("win") in (True, "True"):
            break
        count += 1
    return count
