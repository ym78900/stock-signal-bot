"""
backtest_threshold.py — Historical simulation of threshold_strategy.py.

Reuses the EXACT same decision functions the live strategy uses
(evaluate_entry, evaluate_exit, calculate_shares) rather than a separate
reimplementation — so this tells us what the live strategy would actually
have done, not an idealized approximation that could drift from reality.

Limitation: uses daily closing bars only (Alpaca daily bars), evaluated once
per trading day. The live strategy re-checks every 5 minutes intraday via
real-time price, so it can react faster to intraday moves (e.g. arm/trail
exit mid-day) than this backtest can. Real performance may differ from this
simulation in both directions — this is a sanity check, not a guarantee.

Usage:
  ./venv/bin/python3 backtest_threshold.py --months 3
  ./venv/bin/python3 backtest_threshold.py --months 3 --universe sp500
"""

import argparse
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import ta as ta_lib

import config
import market_data
import threshold_strategy as ts

logging.basicConfig(level=logging.WARNING)  # quiet — we want clean stdout for the report
logger = logging.getLogger(__name__)


def _rsi(df: pd.DataFrame) -> Optional[float]:
    if len(df) < config.RSI_PERIOD + 1:
        return None
    series = ta_lib.momentum.RSIIndicator(df["Close"], window=config.RSI_PERIOD).rsi().dropna()
    if series.empty:
        return None
    return float(series.iloc[-1])


def run_backtest(
    tickers: List[str],
    months: int = 3,
    initial_equity: float = 5000.0,
    verbose: bool = True,
) -> dict:
    end = pd.Timestamp.now(tz="UTC")
    start = end - timedelta(days=months * 31)
    # Extra buffer before start_date for indicator warmup (distress lookback
    # is the longest at ~126 trading days ≈ 180 calendar days).
    fetch_period = f"{months * 31 + 200}d"

    if verbose:
        print(f"Fetching {len(tickers)} tickers, {fetch_period} of history...")
    data = market_data.get_daily_bars(tickers, period=fetch_period)
    if verbose:
        print(f"Got data for {len(data)}/{len(tickers)} tickers.")

    if not data:
        return {"error": "no data returned"}

    # Build the unified trading-day calendar from the union of all tickers'
    # dates within the test window (start..end).
    all_dates = set()
    for df in data.values():
        all_dates.update(pd.Timestamp(d).normalize() for d in df.index if start <= d <= end)
    trading_days = sorted(all_dates)
    if not trading_days:
        return {"error": "no trading days in window — check date range / data availability"}

    if verbose:
        print(f"Simulating {len(trading_days)} trading days from {trading_days[0].date()} to {trading_days[-1].date()}...")

    cash = initial_equity
    positions: Dict[str, dict] = {}   # ticker -> {qty, entry_price, entry_date, armed, peak}
    closed_trades: List[dict] = []
    equity_curve: List[tuple] = []
    max_open = config.THRESHOLD_MAX_OPEN_POSITIONS
    fee_pct = config.THRESHOLD_FEE_PCT_PER_SIDE / 100.0

    def mark_to_market(as_of_prices: Dict[str, float]) -> float:
        val = cash
        for t, p in positions.items():
            price = as_of_prices.get(t, p["entry_price"])
            val += p["qty"] * price
        return val

    for day in trading_days:
        day_prices = {}
        for ticker, df in data.items():
            sub = df.loc[:day]
            if sub.empty or pd.Timestamp(sub.index[-1]).normalize() != day:
                continue  # no bar for this ticker today (holiday gap etc.)
            day_prices[ticker] = float(sub["Close"].iloc[-1])

        # ── Manage exits first ──────────────────────────────────────────────
        for ticker in list(positions.keys()):
            df = data.get(ticker)
            if df is None:
                continue
            sub = df.loc[:day]
            if sub.empty or pd.Timestamp(sub.index[-1]).normalize() != day:
                continue
            price = float(sub["Close"].iloc[-1])
            rsi = _rsi(sub)
            position = positions[ticker]
            decision = ts.evaluate_exit(ticker, position, price, rsi)
            if decision is None:
                continue
            if decision["action"] == "arm":
                position["armed"] = True
                position["peak"] = decision["peak"]
            elif decision["action"] == "update_peak":
                position["peak"] = decision["peak"]
            elif decision["action"] == "sell":
                qty = position["qty"]
                entry_price = position["entry_price"]
                fees = round((entry_price * qty * fee_pct) + (price * qty * fee_pct), 2)
                gross_pnl = round((price - entry_price) * qty, 2)
                net_pnl = round(gross_pnl - fees, 2)
                cash += (price * qty) - (price * qty * fee_pct)
                closed_trades.append({
                    "ticker": ticker,
                    "entry_date": position["entry_date"],
                    "entry_price": entry_price,
                    "exit_date": str(day.date()),
                    "exit_price": price,
                    "qty": qty,
                    "fees": fees,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "pnl_pct": round((net_pnl / (entry_price * qty)) * 100, 2),
                    "exit_reason": decision["reason"],
                    "win": net_pnl > 0,
                })
                del positions[ticker]

        # ── Check entries ────────────────────────────────────────────────────
        if len(positions) < max_open:
            equity_estimate = mark_to_market(day_prices)
            for ticker, df in data.items():
                if ticker in positions or len(positions) >= max_open:
                    continue
                sub = df.loc[:day]
                if sub.empty or pd.Timestamp(sub.index[-1]).normalize() != day:
                    continue
                signal = ts.evaluate_entry(ticker, sub)
                if not signal:
                    continue
                qty = ts.calculate_shares(signal["price"], equity_estimate)
                if qty < config.THRESHOLD_MIN_SHARES:
                    continue
                cost = signal["price"] * qty * (1 + fee_pct)
                if cost > cash:
                    continue
                cash -= cost
                positions[ticker] = {
                    "qty": qty,
                    "entry_price": signal["price"],
                    "entry_date": str(day.date()),
                    "armed": False,
                    "peak": signal["price"],
                }

        equity_curve.append((str(day.date()), mark_to_market(day_prices)))

    # ── Final mark-to-market for still-open positions ────────────────────────
    last_prices = {t: float(df["Close"].iloc[-1]) for t, df in data.items()}
    final_equity = mark_to_market(last_prices)
    open_positions_summary = [
        {
            "ticker": t,
            "qty": p["qty"],
            "entry_price": p["entry_price"],
            "entry_date": p["entry_date"],
            "last_price": last_prices.get(t),
            "unrealized_pnl": round((last_prices.get(t, p["entry_price"]) - p["entry_price"]) * p["qty"], 2),
            "armed": p["armed"],
        }
        for t, p in positions.items()
    ]

    wins = [t for t in closed_trades if t["win"]]
    total_realized_pnl = round(sum(t["net_pnl"] for t in closed_trades), 2)

    # Max drawdown from the equity curve
    peak = initial_equity
    max_dd = 0.0
    for _, val in equity_curve:
        peak = max(peak, val)
        dd = (peak - val) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    result = {
        "period": f"{trading_days[0].date()} to {trading_days[-1].date()}",
        "tickers_tested": len(data),
        "initial_equity": initial_equity,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity - initial_equity) / initial_equity * 100, 2),
        "closed_trades": len(closed_trades),
        "wins": len(wins),
        "losses": len(closed_trades) - len(wins),
        "win_rate_pct": round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0.0,
        "total_realized_pnl": total_realized_pnl,
        "max_drawdown_pct": round(max_dd, 2),
        "open_positions_at_end": open_positions_summary,
        "trade_log": closed_trades,
    }
    return result


def print_report(result: dict) -> None:
    if "error" in result:
        print(f"ERROR: {result['error']}")
        return
    print("\n" + "=" * 60)
    print("THRESHOLD STRATEGY BACKTEST")
    print("=" * 60)
    print(f"Period:              {result['period']}")
    print(f"Tickers tested:      {result['tickers_tested']}")
    print(f"Initial equity:      ${result['initial_equity']:,.2f}")
    print(f"Final equity:        ${result['final_equity']:,.2f}")
    print(f"Total return:        {result['total_return_pct']:+.2f}%")
    print(f"Closed trades:       {result['closed_trades']}")
    print(f"Win rate:            {result['win_rate_pct']}% ({result['wins']}W / {result['losses']}L)")
    print(f"Realized P&L:        ${result['total_realized_pnl']:,.2f}")
    print(f"Max drawdown:        {result['max_drawdown_pct']}%")
    print(f"Open at end:         {len(result['open_positions_at_end'])} position(s)")
    for p in result["open_positions_at_end"]:
        print(f"  • {p['ticker']}: {p['qty']}sh @ ${p['entry_price']:.2f} → "
              f"${p['last_price']:.2f}  (unrealized ${p['unrealized_pnl']:+.2f}, "
              f"{'armed' if p['armed'] else 'not armed'})")
    print("-" * 60)
    print("Trade log:")
    for t in result["trade_log"]:
        outcome = "WIN " if t["win"] else "LOSS"
        print(f"  [{outcome}] {t['ticker']:6s} {t['entry_date']} @ ${t['entry_price']:.2f} → "
              f"{t['exit_date']} @ ${t['exit_price']:.2f}  "
              f"net=${t['net_pnl']:+.2f} ({t['pnl_pct']:+.2f}%)  [{t['exit_reason']}]")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--universe", choices=["watchlist", "sp500"], default="watchlist")
    parser.add_argument("--equity", type=float, default=5000.0)
    args = parser.parse_args()

    if args.universe == "sp500":
        import scanner as sc
        tickers = sc.get_sp500_tickers()
    else:
        tickers = ts.load_watchlist()

    if not tickers:
        print("No tickers to test (empty watchlist and universe=watchlist). "
              "Try --universe sp500 or set a watchlist first.")
        raise SystemExit(1)

    result = run_backtest(tickers, months=args.months, initial_equity=args.equity)
    print_report(result)
