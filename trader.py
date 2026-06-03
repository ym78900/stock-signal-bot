"""
trader.py — Alpaca paper/live order execution for the swing strategy.

Responsibilities:
  - Place market buy orders (entry)
  - Place OCO exit orders (stop loss + take profit) based on real fill price
  - Check circuit breakers before every order
  - Detect closed positions and return them for logging
  - Emergency stop (cancel all + liquidate)

Order flow (two-step, fill-based):
  1. Place market buy at 9:25 AM ET (queues for market open at 9:30 AM ET)
  2. After market opens, poll for real fill price
  3. Calculate stop = fill − ATR×3.5, target = fill + ATR×6.0 from ACTUAL fill
  4. Place OCO exit: limit sell at target + stop sell at stop_price

This avoids the overnight-gap problem where yesterday's close ≠ actual fill price,
which would cause the stop/target distances to be unbalanced.

Circuit breakers (checked at execution time, 9:25 AM ET):
  1. VIX ≥ 25            → skip all trades today
  2. SPY below 50-day MA → skip all trades today
  3. Consecutive losses ≥ 3 → pause (already paused via trade_logger flag)
  4. Open positions ≥ 5  → skip new trades until one closes
  5. Earnings within 3 days → skip that specific ticker
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)


# ── Alpaca client factory ─────────────────────────────────────────────────────

def _trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key    = os.environ["ALPACA_API_KEY"],
        secret_key = os.environ["ALPACA_SECRET_KEY"],
        paper      = config.PAPER_TRADING,
    )


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        api_key    = os.environ["ALPACA_API_KEY"],
        secret_key = os.environ["ALPACA_SECRET_KEY"],
    )


# ── Account info ──────────────────────────────────────────────────────────────

def get_account_equity() -> float:
    """Return current portfolio equity in USD."""
    try:
        acct = _trading_client().get_account()
        return float(acct.equity)
    except Exception as e:
        logger.error(f"Could not fetch account equity: {e}")
        return 0.0


def get_open_position_count() -> int:
    """Return number of currently open positions in Alpaca."""
    try:
        positions = _trading_client().get_all_positions()
        return len(positions)
    except Exception as e:
        logger.error(f"Could not fetch positions: {e}")
        return 0


def get_open_positions() -> List[dict]:
    """Return list of open positions as plain dicts."""
    try:
        raw = _trading_client().get_all_positions()
        result = []
        for p in raw:
            cost = float(p.avg_entry_price) * float(p.qty)
            result.append({
                "ticker":        p.symbol,
                "qty":           int(float(p.qty)),
                "avg_entry":     round(float(p.avg_entry_price), 2),
                "current_price": round(float(p.current_price), 2),
                "market_value":  round(float(p.market_value), 2),
                "unrealized_pl": round(float(p.unrealized_pl), 2),
                "unrealized_pct": round(float(p.unrealized_plpc) * 100, 2),
            })
        return result
    except Exception as e:
        logger.error(f"Could not fetch open positions: {e}")
        return []


# ── Circuit breakers ──────────────────────────────────────────────────────────

def check_vix() -> Tuple[bool, float]:
    """
    Returns (safe, vix_level).
    safe=True means VIX is below threshold — OK to trade.
    """
    try:
        import yfinance as yf
        vix = yf.download("^VIX", period="5d", interval="1d",
                          auto_adjust=True, progress=False)
        if vix.empty:
            logger.warning("VIX data unavailable — assuming safe.")
            return True, 0.0
        import pandas as pd
        if isinstance(vix.columns, pd.MultiIndex):
            vix.columns = vix.columns.get_level_values(0)
        level = float(vix["Close"].dropna().iloc[-1])
        safe  = level < config.VIX_MAX
        logger.info(f"VIX = {level:.1f}  (threshold: {config.VIX_MAX})  safe={safe}")
        return safe, round(level, 1)
    except Exception as e:
        logger.error(f"VIX check failed: {e}")
        return True, 0.0   # fail-open: don't block trades on data error


def check_spy_trend() -> Tuple[bool, float]:
    """
    Returns (safe, spy_close).
    safe=True means SPY is above 50-day MA — OK to trade.
    """
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="60d", interval="1d",
                          auto_adjust=True, progress=False)
        if spy.empty:
            logger.warning("SPY data unavailable — assuming safe.")
            return True, 0.0
        import pandas as pd
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        close = spy["Close"].dropna()
        ma50  = close.rolling(50).mean().dropna()
        if ma50.empty:
            return True, 0.0
        spy_now = float(close.iloc[-1])
        ma_now  = float(ma50.iloc[-1])
        safe    = spy_now >= ma_now
        logger.info(f"SPY={spy_now:.2f}  50MA={ma_now:.2f}  safe={safe}")
        return safe, round(spy_now, 2)
    except Exception as e:
        logger.error(f"SPY trend check failed: {e}")
        return True, 0.0


def check_earnings(ticker: str) -> Tuple[bool, Optional[str]]:
    """
    Returns (safe, earnings_date_str).
    safe=True means no earnings within EARNINGS_BUFFER_DAYS — OK to trade.
    """
    if not config.USE_EARNINGS_FILTER:
        return True, None
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        if cal is None or cal.empty:
            return True, None
        # calendar has columns; earnings date is typically index 0 "Earnings Date"
        if "Earnings Date" in cal.index:
            earn_dt = cal.loc["Earnings Date"].iloc[0]
        elif len(cal) > 0:
            earn_dt = cal.iloc[0, 0]
        else:
            return True, None

        if hasattr(earn_dt, "date"):
            earn_date = earn_dt.date()
        else:
            earn_date = date.fromisoformat(str(earn_dt)[:10])

        days_away = (earn_date - date.today()).days
        safe = days_away < 0 or days_away > config.EARNINGS_BUFFER_DAYS
        earn_str = str(earn_date)
        if not safe:
            logger.info(f"{ticker}: earnings in {days_away} days ({earn_str}) — SKIPPING")
        return safe, earn_str
    except Exception as e:
        logger.debug(f"Earnings check failed for {ticker}: {e}")
        return True, None   # fail-open


def run_circuit_breakers() -> Tuple[bool, str]:
    """
    Run market-wide circuit breakers (VIX + SPY).
    Returns (ok, reason_if_blocked).
    """
    if config.USE_VIX_FILTER:
        vix_safe, vix_level = check_vix()
        if not vix_safe:
            return False, f"VIX={vix_level} ≥ {config.VIX_MAX} — skipping all trades today"

    if config.USE_SPY_TREND_FILTER:
        spy_safe, _ = check_spy_trend()
        if not spy_safe:
            return False, "SPY below 50-day MA — skipping all trades today"

    return True, ""


# ── Order placement ───────────────────────────────────────────────────────────

def place_market_buy(ticker: str, qty: int) -> Optional[str]:
    """
    Step 1 of 2: Place a simple market buy with no attached stop/target.
    Returns the Alpaca order ID string, or None on failure.

    The order queues pre-market and fills at/near the 9:30 AM ET open.
    After fill is confirmed, call place_oco_exit() with the real fill price.
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        req = MarketOrderRequest(
            symbol        = ticker,
            qty           = qty,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.DAY,
            order_class   = OrderClass.SIMPLE,
        )
        order = _trading_client().submit_order(req)
        logger.info(f"Market buy placed: {ticker} qty={qty}  id={order.id}")
        return str(order.id)
    except Exception as e:
        logger.error(f"Failed to place market buy for {ticker}: {e}")
        return None


def get_order_fill_price(order_id: str) -> Optional[float]:
    """
    Return the average fill price of an order if it has been filled, else None.
    Used to poll for fill after a market buy.
    """
    try:
        order  = _trading_client().get_order_by_id(order_id)
        status = str(order.status).lower()
        if status in ("filled", "partially_filled") and order.filled_avg_price:
            return round(float(order.filled_avg_price), 4)
        return None
    except Exception as e:
        logger.error(f"get_order_fill_price({order_id}): {e}")
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel a specific open order. Returns True on success."""
    try:
        _trading_client().cancel_order_by_id(order_id)
        logger.info(f"Cancelled order {order_id}")
        return True
    except Exception as e:
        logger.error(f"cancel_order({order_id}): {e}")
        return False


def place_oco_exit(
    ticker: str,
    qty: int,
    stop_price: float,
    target_price: float,
) -> Optional[str]:
    """
    Step 2 of 2: Place an OCO (One-Cancels-Other) sell order after the entry fill.

    Creates two linked sell orders:
      - Limit sell at target_price  (take profit)
      - Stop  sell at stop_price    (stop loss)
    When either fills, Alpaca automatically cancels the other.

    stop_price and target_price must be calculated from the REAL fill price,
    not the prior close estimate.

    Returns the OCO order ID (used to track exits), or None on failure.
    """
    try:
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        req = LimitOrderRequest(
            symbol        = ticker,
            qty           = qty,
            side          = OrderSide.SELL,
            time_in_force = TimeInForce.GTC,
            order_class   = OrderClass.OCO,
            limit_price   = round(target_price, 2),
            stop_loss     = StopLossRequest(stop_price=round(stop_price, 2)),
        )
        order = _trading_client().submit_order(req)
        logger.info(
            f"OCO exit placed: {ticker} qty={qty} "
            f"SL=${stop_price:.2f} TP=${target_price:.2f}  id={order.id}"
        )
        return str(order.id)
    except Exception as e:
        logger.error(f"Failed to place OCO exit for {ticker}: {e}")
        return None


# ── Position monitoring ───────────────────────────────────────────────────────

def get_recently_closed_orders(tracked_order_ids: List[str]) -> List[dict]:
    """
    Check Alpaca for any orders in tracked_order_ids that are now filled/closed.
    Returns a list of closed order dicts with exit info.

    We track the parent bracket order ID. When a bracket closes (stop or target hit),
    the parent order shows status=filled and we can read the filled price.

    Bracket child orders (stop/take_profit legs) share the same parent order ID.
    We query all orders for tracked IDs and look for closed legs.
    """
    if not tracked_order_ids:
        return []

    closed = []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        client = _trading_client()

        # Query all recent orders (last 500) and filter to our tracked IDs
        req    = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200)
        orders = client.get_orders(req)

        tracked_set = set(tracked_order_ids)
        for order in orders:
            order_id = str(order.id)
            # Also check legs by client_order_id or parent matching
            if order_id in tracked_set or str(getattr(order, "legs", None) or "") in tracked_set:
                exit_price  = float(order.filled_avg_price) if order.filled_avg_price else None
                exit_reason = _map_exit_reason(order)
                if exit_price:
                    closed.append({
                        "alpaca_order_id": order_id,
                        "ticker":          order.symbol,
                        "exit_price":      round(exit_price, 4),
                        "exit_reason":     exit_reason,
                        "exit_date":       str(date.today()),
                    })

    except Exception as e:
        logger.error(f"Error checking closed orders: {e}")

    return closed


def get_closed_bracket_legs(tracked_order_ids: List[str]) -> List[dict]:
    """
    More reliable approach: query each tracked order directly and check
    if any of its child legs (stop/take_profit) have been filled.
    Returns list of {alpaca_order_id, ticker, exit_price, exit_reason, exit_date}.
    """
    if not tracked_order_ids:
        return []

    closed = []
    try:
        client = _trading_client()
        for order_id in tracked_order_ids:
            try:
                order = client.get_order_by_id(order_id)
                legs  = getattr(order, "legs", None) or []
                for leg in legs:
                    leg_status = str(leg.status).lower()
                    if leg_status in ("filled",) and leg.filled_avg_price:
                        exit_price  = round(float(leg.filled_avg_price), 4)
                        exit_reason = _map_exit_reason(leg)
                        closed.append({
                            "alpaca_order_id": order_id,
                            "ticker":          order.symbol,
                            "exit_price":      exit_price,
                            "exit_reason":     exit_reason,
                            "exit_date":       str(date.today()),
                        })
                        break  # only one leg fills
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error checking bracket legs: {e}")

    return closed


def _map_exit_reason(order) -> str:
    order_type = str(getattr(order, "order_type", "") or "").lower()
    if "stop" in order_type:
        return "stop_loss"
    if "limit" in order_type or "take_profit" in order_type:
        return "take_profit"
    return "closed"


def get_filled_entries(tracked_order_ids: List[str]) -> List[dict]:
    """
    Check if any tracked bracket order's entry (market buy) leg has been filled.
    Returns list of {alpaca_order_id, ticker, entry_price, entry_date}.
    Used by the monitor to update trades.csv with the real fill price.
    """
    if not tracked_order_ids:
        return []

    filled = []
    try:
        client = _trading_client()
        for order_id in tracked_order_ids:
            try:
                order = client.get_order_by_id(order_id)
                status = str(order.status).lower()
                # Parent order filled = entry executed
                if status in ("filled", "partially_filled") and order.filled_avg_price:
                    filled.append({
                        "alpaca_order_id": order_id,
                        "ticker":          order.symbol,
                        "entry_price":     round(float(order.filled_avg_price), 4),
                        "entry_date":      str(date.today()),
                    })
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error checking filled entries: {e}")

    return filled


# ── Emergency controls ────────────────────────────────────────────────────────

def cancel_all_orders() -> int:
    """Cancel all open orders. Returns number cancelled."""
    try:
        client  = _trading_client()
        results = client.cancel_orders()
        n = len(results) if results else 0
        logger.warning(f"Cancelled {n} open orders.")
        return n
    except Exception as e:
        logger.error(f"cancel_all_orders failed: {e}")
        return 0


def liquidate_all_positions() -> int:
    """Close all open positions at market. Returns number liquidated."""
    try:
        client = _trading_client()
        client.close_all_positions(cancel_orders=True)
        logger.warning("All positions liquidated.")
        return 1
    except Exception as e:
        logger.error(f"liquidate_all_positions failed: {e}")
        return 0
