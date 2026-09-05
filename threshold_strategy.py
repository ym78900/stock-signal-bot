"""
threshold_strategy.py — Simple dip-buy + RSI + fee-aware trailing-exit strategy.

This is intentionally separate from the existing swing/ATR-trailing bot
(scanner.py / signals.py analyse() / trader.py bracket logic) — different
state files, different trade log, different watchlist — so it can run
alongside the proven strategy without any risk of interfering with it.

Strategy (see conversation for full reasoning):

  ENTRY — all of:
    - price down THRESHOLD_DIP_PCT% from its N-day high (THRESHOLD_DIP_LOOKBACK_DAYS)
    - RSI(14) < THRESHOLD_RSI_BUY_MAX (oversold)
    - price within [THRESHOLD_PRICE_MIN, THRESHOLD_PRICE_MAX]
    - average volume >= THRESHOLD_MIN_AVG_VOLUME (liquidity floor)
    - NOT already down more than THRESHOLD_DISTRESS_MAX_DRAWDOWN_PCT over the
      last THRESHOLD_DISTRESS_LOOKBACK_DAYS — falling-knife / distress guard
      (the "maybe the company has legal issues" filter — pure price/volume
      behavior, not real news, but blocks the most obvious structurally
      broken names from being bought just because they're "cheap")
    - no earnings within THRESHOLD_EARNINGS_BUFFER_DAYS (best-effort; degrades
      gracefully to "unknown" rather than blocking, since the data source
      for this is not fully reliable)

  EXIT — adaptive, not a fixed sell-at-+X%:
    - Once price rises enough to clear round-trip fees + a minimum profit
      margin (computed from THRESHOLD_FEE_PCT_PER_SIDE, not guessed), the
      exit is "armed" and a trailing peak starts being tracked.
    - While armed, sell when price drops THRESHOLD_TRAIL_PCT% below the
      running peak since arming — this lets winners keep running past the
      original profit target instead of capping upside artificially.
    - Independent hard stop-loss at THRESHOLD_HARD_STOP_PCT% below entry,
      regardless of RSI/arming state — dip-buying without a stop is how a
      "dip" becomes a total loss.

State files (all separate from the swing bot's trades.csv/pending_trades.json):
  threshold_watchlist.json — tickers this strategy is allowed to trade
  threshold_positions.json — open positions with entry/peak/armed state
  threshold_trades.csv     — closed-trade log with real fees + net P&L
  threshold_paused.flag    — kill switch

Broker: Alpaca only (decided after evaluating Bitget Stock+ as an
alternative — see below). Do not re-litigate this without new information.

  Bitget Stock+ (tokenized US stocks) was considered so tickers "unavailable"
  on Alpaca could still be traded. Checked their live API docs
  (bitget.com/api-doc/uta/stockplus/) directly:
    - No paper/demo trading mode exists for Stock+ at all — every order is
      real money. Conflicts with "paper trade first."
    - Requires KYC + a separate in-app "US stock module" activation before
      the trading API even works — a real brokered product, not a sandbox.
    - Symbol coverage is a small curated list (~40-45 large caps in their
      published option-eligible lists) — a SUBSET of, not an addition to,
      Alpaca's coverage.
  Alpaca has 13,404 tradable US equities (confirmed live from this account)
  with real, working paper trading and deep real-exchange liquidity, vs.
  Bitget's tokenized/market-maker-quoted wrapper. There is no realistic
  scenario where a ticker exists on Bitget Stock+ but not Alpaca, so the
  "combine both" idea added risk (no paper mode, thinner liquidity) with no
  actual coverage benefit. Alpaca-only.
"""

import csv
import json
import logging
import math
import os
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import ta as ta_lib

import config
import market_data
import trader
import telegram_notify
import ai_signal
import signals as sig

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "threshold_watchlist.json")
POSITIONS_FILE = os.path.join(BASE_DIR, "threshold_positions.json")
TRADE_LOG = os.path.join(BASE_DIR, "threshold_trades.csv")
PAUSE_FLAG = os.path.join(BASE_DIR, "threshold_paused.flag")

STRATEGY_NAME = "Threshold"

_COLUMNS = [
    "id", "ticker", "entry_date", "entry_price", "exit_date", "exit_price",
    "exit_reason", "qty", "fees", "gross_pnl", "net_pnl", "pnl_pct", "win",
    "alpaca_order_id", "status",
]


# ── Atomic JSON helpers (same pattern as trade_logger.py) ─────────────────────

def _atomic_json_write(path: str, data) -> None:
    dir_ = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


# ── Watchlist ──────────────────────────────────────────────────────────────────

def load_watchlist() -> List[str]:
    return _read_json(WATCHLIST_FILE, [])


def save_watchlist(tickers: List[str]) -> None:
    _atomic_json_write(WATCHLIST_FILE, sorted(set(t.upper() for t in tickers)))


def add_ticker(ticker: str) -> None:
    wl = set(load_watchlist())
    wl.add(ticker.upper())
    save_watchlist(list(wl))


def remove_ticker(ticker: str) -> None:
    wl = [t for t in load_watchlist() if t.upper() != ticker.upper()]
    save_watchlist(wl)


# ── Positions ──────────────────────────────────────────────────────────────────

def load_positions() -> Dict[str, dict]:
    return _read_json(POSITIONS_FILE, {})


def save_positions(positions: Dict[str, dict]) -> None:
    _atomic_json_write(POSITIONS_FILE, positions)


# ── Pause flag (kill switch) ──────────────────────────────────────────────────

def is_paused() -> bool:
    return os.path.exists(PAUSE_FLAG)


def pause(reason: str = "") -> None:
    with open(PAUSE_FLAG, "w") as f:
        f.write(reason or "paused")
    logger.info(f"Threshold strategy PAUSED. Reason: {reason or '(none)'}")


def resume() -> None:
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    logger.info("Threshold strategy RESUMED.")


# ── Trade log (CSV) ────────────────────────────────────────────────────────────

def _ensure_csv() -> None:
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writeheader()


def _read_all_trades() -> List[dict]:
    _ensure_csv()
    with open(TRADE_LOG, newline="") as f:
        return list(csv.DictReader(f))


def log_closed_trade(
    ticker: str, entry_date: str, entry_price: float, exit_date: str,
    exit_price: float, exit_reason: str, qty: int, alpaca_order_id: str,
) -> dict:
    _ensure_csv()
    fee_pct = config.THRESHOLD_FEE_PCT_PER_SIDE / 100.0
    fees = round((entry_price * qty * fee_pct) + (exit_price * qty * fee_pct), 2)
    gross_pnl = round((exit_price - entry_price) * qty, 2)
    net_pnl = round(gross_pnl - fees, 2)
    cost = entry_price * qty
    pnl_pct = round((net_pnl / cost) * 100, 2) if cost else 0.0
    row = {
        "id": str(uuid.uuid4())[:8],
        "ticker": ticker,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "qty": qty,
        "fees": fees,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "pnl_pct": pnl_pct,
        "win": net_pnl > 0,
        "alpaca_order_id": alpaca_order_id,
        "status": "closed",
    }
    with open(TRADE_LOG, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore").writerow(row)
    logger.info(f"[{STRATEGY_NAME}] Trade closed: {ticker} net_pnl=${net_pnl} ({exit_reason})")
    return row


def get_stats() -> dict:
    closed = [r for r in _read_all_trades() if r["status"] == "closed"]
    if not closed:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "total_net_pnl": 0.0}
    wins = [r for r in closed if r.get("win") in (True, "True")]
    net_pnls = [float(r["net_pnl"]) for r in closed if r["net_pnl"] not in ("", None)]
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
        "total_net_pnl": round(sum(net_pnls), 2),
    }


def get_all_trades(n: int = 100) -> List[dict]:
    rows = _read_all_trades()
    return rows[-n:] if len(rows) > n else rows


# ── Filters ────────────────────────────────────────────────────────────────────

def _distress_check(df) -> tuple:
    """
    Falling-knife / distress guard: skip if the stock is already down more
    than THRESHOLD_DISTRESS_MAX_DRAWDOWN_PCT over the last ~6 months. A sharp
    daily dip on top of a stock that's already been collapsing for months is
    a much higher-risk "buy" than a dip in an otherwise healthy stock — this
    is a pure price-behavior proxy for "something structurally wrong",
    not a substitute for real news/legal-filing checks.
    """
    lookback = config.THRESHOLD_DISTRESS_LOOKBACK_DAYS
    if len(df) < lookback:
        return True, None
    window = df["Close"].iloc[-lookback:]
    high = float(window.max())
    now = float(window.iloc[-1])
    if high <= 0:
        return True, None
    drawdown_pct = (high - now) / high * 100
    if drawdown_pct > config.THRESHOLD_DISTRESS_MAX_DRAWDOWN_PCT:
        return False, f"down {drawdown_pct:.1f}% over {lookback}d — distress guard"
    return True, None


def evaluate_entry(ticker: str, df, check_earnings: bool = True) -> Optional[dict]:
    """
    Return a dict describing the BUY signal if all entry conditions are met, else None.

    check_earnings: set False in backtests. trader.check_earnings() calls a
    live "next earnings date relative to today" lookup — correct for live
    trading, but meaningless against a simulated historical date (there's no
    historical earnings-calendar data source integrated yet). Rather than
    silently produce a filter that's checking the wrong date, backtests skip
    this filter entirely and say so.
    """
    try:
        if len(df) < max(config.THRESHOLD_DIP_LOOKBACK_DAYS, config.RSI_PERIOD) + 5:
            return None


        price = float(df["Close"].iloc[-1])
        if price < config.THRESHOLD_PRICE_MIN or price > config.THRESHOLD_PRICE_MAX:
            return None

        avg_vol = float(df["Volume"].iloc[-config.THRESHOLD_DIP_LOOKBACK_DAYS:].mean())
        if avg_vol < config.THRESHOLD_MIN_AVG_VOLUME:
            return None

        # Dip check
        recent_high = float(df["Close"].iloc[-config.THRESHOLD_DIP_LOOKBACK_DAYS:].max())
        if recent_high <= 0:
            return None
        dip_pct = (recent_high - price) / recent_high * 100
        if dip_pct < config.THRESHOLD_DIP_PCT:
            return None

        # RSI check
        rsi_series = ta_lib.momentum.RSIIndicator(df["Close"], window=config.RSI_PERIOD).rsi().dropna()
        if rsi_series.empty:
            return None
        rsi = float(rsi_series.iloc[-1])
        if rsi >= config.THRESHOLD_RSI_BUY_MAX:
            return None

        # Distress / falling-knife guard
        safe, reason = _distress_check(df)
        if not safe:
            logger.info(f"[{STRATEGY_NAME}] {ticker}: skipped — {reason}")
            return None

        # Best-effort earnings check (never blocks on failure). Skipped
        # entirely in backtests — see check_earnings param docstring above.
        if check_earnings:
            try:
                earnings_safe, _ = trader.check_earnings(ticker)
                if not earnings_safe:
                    logger.info(f"[{STRATEGY_NAME}] {ticker}: skipped — earnings within buffer window")
                    return None
            except Exception:
                pass

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "dip_pct": round(dip_pct, 2),
            "rsi": round(rsi, 1),
            "reason": f"dip -{dip_pct:.1f}% from {config.THRESHOLD_DIP_LOOKBACK_DAYS}d high, RSI {rsi:.1f}",
        }
    except Exception as e:
        logger.error(f"[{STRATEGY_NAME}] evaluate_entry failed for {ticker}: {e}")
        return None


def _breakeven_price(entry_price: float) -> float:
    """Price needed to clear round-trip fees + minimum profit margin."""
    fee_pct = config.THRESHOLD_FEE_PCT_PER_SIDE / 100.0
    margin_pct = config.THRESHOLD_MIN_PROFIT_MARGIN_PCT / 100.0
    # Solve for exit price X such that (X - entry - fees) / entry >= margin_pct
    # fees ≈ entry*fee_pct + X*fee_pct
    # X*(1-fee_pct) >= entry*(1+fee_pct+margin_pct)
    return entry_price * (1 + fee_pct + margin_pct) / (1 - fee_pct)


def evaluate_exit(ticker: str, position: dict, current_price: float, rsi: Optional[float]) -> Optional[dict]:
    """
    Returns a dict describing the SELL decision if an exit condition is met, else None.
    Mutates nothing — caller updates position state (peak/armed) based on the
    returned action (or lack thereof).
    """
    entry_price = position["entry_price"]

    # Hard stop-loss — always checked first, independent of everything else
    stop_price = entry_price * (1 - config.THRESHOLD_HARD_STOP_PCT / 100.0)
    if current_price <= stop_price:
        return {"action": "sell", "reason": f"hard stop -{config.THRESHOLD_HARD_STOP_PCT}%"}

    breakeven = _breakeven_price(entry_price)
    armed = position.get("armed", False)

    if not armed:
        if current_price >= breakeven:
            return {"action": "arm", "peak": current_price}
        # RSI overbought without having cleared fees yet — still not worth selling at a loss
        return None

    # Armed: track/trail
    peak = max(position.get("peak", current_price), current_price)
    trail_stop = peak * (1 - config.THRESHOLD_TRAIL_PCT / 100.0)
    if current_price <= trail_stop:
        return {"action": "sell", "reason": f"trailing stop -{config.THRESHOLD_TRAIL_PCT}% from peak ${peak:.2f}"}

    if rsi is not None and rsi >= config.THRESHOLD_RSI_SELL_MIN:
        # Overbought while armed — take some profit rather than risk giving it back,
        # but only if we've already cleared the breakeven+margin threshold (we're armed).
        return {"action": "sell", "reason": f"RSI overbought ({rsi:.1f}) while in profit"}

    return {"action": "update_peak", "peak": peak}


# ── AI sentiment layer (optional, on top of the quant entry signal) ──────────

def get_ai_check(ticker: str, rsi: Optional[float], as_of_date=None) -> dict:
    """
    Fetch AI news sentiment for a candidate BUY signal. Never raises and
    never blocks on its own missing/failed data (fail-open, same philosophy
    as the VIX/SPY/earnings circuit breakers) — the caller decides whether
    to actually use `should_block` based on config.THRESHOLD_AI_BLOCKING.

    as_of_date: pass the simulated date for backtests.
    """
    result = {
        "sentiment_score": None,
        "confidence_score": None,
        "catalyst_type": None,
        "ai_verdict": None,
        "ai_reasoning": None,
        "should_block": False,
        "had_news": False,
    }
    if not config.THRESHOLD_AI_ENABLED:
        return result
    try:
        company_name = sig.get_company_name(ticker)
        enrichment = ai_signal.get_ai_enrichment(ticker, company_name, rsi, as_of_date=as_of_date)
        result.update(enrichment)
        result["had_news"] = enrichment.get("sentiment_score") is not None

        score = enrichment.get("sentiment_score")
        confidence = enrichment.get("confidence_score") or 0.0
        permanent_damage = False
        # ai_signal doesn't expose is_permanent_damage directly in get_ai_enrichment's
        # return (it's consumed internally by build_verdict) — re-derive from verdict:
        # "AVOID" is exactly the case build_verdict assigns for permanent-damage-or-very-
        # bearish RSI-oversold signals, which is exactly our entry condition (RSI oversold).
        if enrichment.get("ai_verdict") == "AVOID":
            permanent_damage = True

        catalyst = enrichment.get("catalyst_type")
        catalyst_allowed = catalyst in config.THRESHOLD_AI_BLOCK_CATALYSTS

        # Restricting to specific catalyst types (see config.py comment): the
        # 3-month backtest found AI over-eager to call ordinary analyst
        # downgrades/macro jitters "fundamental impairment" — every incorrect
        # block in the unrestricted run was outside THRESHOLD_AI_BLOCK_CATALYSTS.
        if catalyst_allowed and (
            permanent_damage or (
                score is not None
                and score <= config.THRESHOLD_AI_AVOID_SENTIMENT
                and confidence >= config.THRESHOLD_AI_MIN_CONFIDENCE_TO_BLOCK
            )
        ):
            result["should_block"] = True
    except Exception as e:
        logger.warning(f"[{STRATEGY_NAME}] AI check failed for {ticker} (failing open): {e}")
    return result


# ── Position sizing ────────────────────────────────────────────────────────────

def calculate_shares(price: float, account_equity: float) -> int:
    cap_dollars = account_equity * config.THRESHOLD_MAX_POSITION_PCT
    shares = int(cap_dollars // price)
    return max(shares, 0)


# ── Main cycle (called by scheduler) ──────────────────────────────────────────

def run_cycle() -> dict:
    """
    One full pass: check exits for open positions, then check entries for
    watchlist tickers not already held. Returns a summary dict for logging.
    """
    summary = {"checked": 0, "buys": 0, "sells": 0, "armed": 0, "errors": 0}

    if is_paused():
        logger.info(f"[{STRATEGY_NAME}] Skipping cycle — trading paused.")
        return summary

    if not config.THRESHOLD_STRATEGY_ENABLED:
        return summary

    positions = load_positions()
    watchlist = load_watchlist()

    all_tickers = sorted(set(list(positions.keys()) + watchlist))
    if not all_tickers:
        logger.info(f"[{STRATEGY_NAME}] Watchlist is empty — nothing to do.")
        return summary

    # Live account equity (not a hardcoded assumption) — used for position sizing.
    account_equity = trader.get_account_equity() or config.THRESHOLD_ACCOUNT_EQUITY

    data = market_data.get_daily_bars(all_tickers, period="180d")

    # ── Reconcile any positions still awaiting fill confirmation ─────────────
    # (order placed but didn't fill within the initial poll window — e.g.
    # placed right before a halt, or the order is still working). Check again
    # each cycle until confirmed; skip exit evaluation for these until then,
    # since there's no real entry price yet to measure an exit against.
    for ticker, position in list(positions.items()):
        if position.get("status") != "pending_fill":
            continue
        order_id = position.get("alpaca_order_id")
        real_fill = trader.get_order_fill_price(order_id) if order_id else None
        if real_fill:
            position["entry_price"] = real_fill
            position["peak"] = real_fill
            position["status"] = "open"
            positions[ticker] = position
            logger.info(f"[{STRATEGY_NAME}] {ticker}: fill confirmed at ${real_fill:.2f}")
            telegram_notify.send(
                f"✅ {ticker} fill confirmed @ ${real_fill:.2f}", prefix=f"[{STRATEGY_NAME}]"
            )

    # ── Manage open positions first (exits take priority over new entries) ──
    for ticker, position in list(positions.items()):
        if position.get("status") == "pending_fill":
            continue  # nothing to manage yet — no confirmed entry price
        summary["checked"] += 1
        try:
            df = data.get(ticker)
            current_price = market_data.get_latest_price(ticker)
            if current_price is None and df is not None and not df.empty:
                current_price = float(df["Close"].iloc[-1])
            if current_price is None:
                logger.warning(f"[{STRATEGY_NAME}] {ticker}: no price available, skipping this cycle.")
                continue

            rsi = None
            if df is not None and len(df) >= config.RSI_PERIOD + 1:
                rsi_series = ta_lib.momentum.RSIIndicator(df["Close"], window=config.RSI_PERIOD).rsi().dropna()
                if not rsi_series.empty:
                    rsi = float(rsi_series.iloc[-1])

            decision = evaluate_exit(ticker, position, current_price, rsi)
            if decision is None:
                continue

            if decision["action"] == "arm":
                position["armed"] = True
                position["peak"] = decision["peak"]
                positions[ticker] = position
                summary["armed"] += 1
                logger.info(f"[{STRATEGY_NAME}] {ticker}: exit ARMED at ${current_price:.2f} (breakeven+margin cleared)")
                telegram_notify.send(
                    f"📈 {ticker} exit ARMED at ${current_price:.2f} — in profit past fees, "
                    f"now trailing {config.THRESHOLD_TRAIL_PCT}% below peak.",
                    prefix=f"[{STRATEGY_NAME}]",
                )

            elif decision["action"] == "update_peak":
                position["peak"] = decision["peak"]
                positions[ticker] = position

            elif decision["action"] == "sell":
                qty = position["qty"]
                order_id = trader.place_market_sell(ticker, qty)
                if order_id:
                    log_closed_trade(
                        ticker=ticker,
                        entry_date=position["entry_date"],
                        entry_price=position["entry_price"],
                        exit_date=str(date.today()),
                        exit_price=current_price,
                        exit_reason=decision["reason"],
                        qty=qty,
                        alpaca_order_id=order_id,
                    )
                    del positions[ticker]
                    summary["sells"] += 1
                    telegram_notify.send_trade_alert(
                        STRATEGY_NAME, "SELL", ticker, current_price, decision["reason"]
                    )
                else:
                    summary["errors"] += 1
                    telegram_notify.send_error_alert(STRATEGY_NAME, f"sell order failed for {ticker}", "order returned no id")

        except Exception as e:
            summary["errors"] += 1
            logger.error(f"[{STRATEGY_NAME}] Error managing position {ticker}: {e}")
            telegram_notify.send_error_alert(STRATEGY_NAME, f"position management for {ticker}", str(e))

    save_positions(positions)

    # ── Check for new entries ────────────────────────────────────────────────
    if len(positions) >= config.THRESHOLD_MAX_OPEN_POSITIONS:
        logger.info(f"[{STRATEGY_NAME}] Max open positions ({config.THRESHOLD_MAX_OPEN_POSITIONS}) reached — skipping new entries.")
    else:
        for ticker in watchlist:
            if ticker in positions:
                continue
            if len(positions) >= config.THRESHOLD_MAX_OPEN_POSITIONS:
                break
            summary["checked"] += 1
            try:
                df = data.get(ticker)
                if df is None or df.empty:
                    continue
                signal = evaluate_entry(ticker, df)
                if not signal:
                    continue

                qty = calculate_shares(signal["price"], account_equity)
                if qty < config.THRESHOLD_MIN_SHARES:
                    logger.info(f"[{STRATEGY_NAME}] {ticker}: signal fired but position size too small, skipping.")
                    continue

                ai_check = get_ai_check(ticker, signal.get("rsi"))
                if config.THRESHOLD_AI_BLOCKING and ai_check["should_block"]:
                    logger.info(
                        f"[{STRATEGY_NAME}] {ticker}: BUY signal blocked by AI "
                        f"(sentiment={ai_check['sentiment_score']}, verdict={ai_check['ai_verdict']}) "
                        f"— {ai_check['ai_reasoning']}"
                    )
                    telegram_notify.send(
                        f"🚫 {ticker} BUY signal blocked by AI — {ai_check['ai_reasoning'] or 'flagged as high risk'}",
                        prefix=f"[{STRATEGY_NAME}]",
                    )
                    continue

                order_id = trader.place_market_buy(ticker, qty)
                if order_id:
                    # Poll briefly for the real fill price rather than trusting the
                    # signal-time estimate — market orders during live trading hours
                    # usually fill within a couple seconds, but this also protects
                    # against the case where the order doesn't fill immediately
                    # (e.g. placed right at a halt, or outside hours) — those stay
                    # "pending_fill" and get reconciled on a later cycle instead of
                    # locking in a stale/estimated entry price that could be very
                    # wrong (confirmed bug: an order placed Saturday and left
                    # unreconciled would carry Friday's estimated price into
                    # Monday's actual, possibly very different, fill).
                    real_fill = None
                    for _ in range(5):
                        time.sleep(1)
                        real_fill = trader.get_order_fill_price(order_id)
                        if real_fill:
                            break

                    entry_price = real_fill or signal["price"]
                    positions[ticker] = {
                        "qty": qty,
                        "entry_price": entry_price,
                        "entry_date": str(date.today()),
                        "armed": False,
                        "peak": entry_price,
                        "alpaca_order_id": order_id,
                        "status": "open" if real_fill else "pending_fill",
                    }
                    summary["buys"] += 1
                    ai_note = ""
                    if ai_check["had_news"]:
                        ai_note = (
                            f" | AI: {ai_check['ai_verdict']} "
                            f"(sentiment {ai_check['sentiment_score']:+.2f}, {ai_check['catalyst_type']})"
                        )
                    if not real_fill:
                        ai_note += " | ⏳ order not yet filled, entry price will be confirmed on fill"
                    telegram_notify.send_trade_alert(
                        STRATEGY_NAME, "BUY", ticker, signal["price"], signal["reason"] + ai_note
                    )
                else:
                    summary["errors"] += 1
                    telegram_notify.send_error_alert(STRATEGY_NAME, f"buy order failed for {ticker}", "order returned no id")

            except Exception as e:
                summary["errors"] += 1
                logger.error(f"[{STRATEGY_NAME}] Error evaluating entry for {ticker}: {e}")

        save_positions(positions)

    logger.info(f"[{STRATEGY_NAME}] Cycle complete: {summary}")
    return summary


def send_heartbeat() -> None:
    """
    Daily "still alive" summary — sent regardless of whether anything traded,
    so silence in Telegram never means "did it crash?" the way the old
    completely-silent pipeline did (5 days, zero trades, zero visibility).
    """
    try:
        equity = trader.get_account_equity()
        positions = load_positions()
        stats = get_stats()
        paused = "⏸ PAUSED" if is_paused() else "▶️ running"

        lines = [
            f"Status: {paused}",
            f"Paper equity: ${equity:,.2f}",
            f"Open positions: {len(positions)}",
        ]
        if positions:
            for ticker, p in positions.items():
                armed = "armed" if p.get("armed") else "not armed"
                lines.append(f"  • {ticker}: {p['qty']}sh @ ${p['entry_price']:.2f} ({armed})")
        lines.append(
            f"All-time: {stats['total_trades']} trades, "
            f"{stats['win_rate_pct']}% win rate, "
            f"net P&L ${stats['total_net_pnl']:,.2f}"
        )
        telegram_notify.send("\n".join(lines), prefix=f"[{STRATEGY_NAME}] Daily summary —")
    except Exception as e:
        logger.error(f"[{STRATEGY_NAME}] Heartbeat failed: {e}")
