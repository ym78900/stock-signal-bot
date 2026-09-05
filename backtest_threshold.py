"""
backtest_threshold.py — Historical simulation of threshold_strategy.py.

Reuses the EXACT same decision functions the live strategy uses
(evaluate_entry, evaluate_exit, calculate_position_dollars) rather than a separate
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
    ai_check: bool = False,
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

    # Alpaca stamps daily bars at 04:00:00 UTC (not midnight) — normalize each
    # ticker's index to midnight so day-matching/slicing below is exact instead
    # of silently excluding every bar's own day (confirmed bug: this originally
    # caused zero signals to ever be evaluated, not zero signals to be found).
    data = {t: df.set_axis(df.index.normalize()) for t, df in data.items()}

    # Build the unified trading-day calendar from the union of all tickers'
    # dates within the test window (start..end).
    all_dates = set()
    for df in data.values():
        all_dates.update(d for d in df.index if start <= d <= end)
    trading_days = sorted(all_dates)
    if not trading_days:
        return {"error": "no trading days in window — check date range / data availability"}

    if verbose:
        print(f"Simulating {len(trading_days)} trading days from {trading_days[0].date()} to {trading_days[-1].date()}...")

    cash = initial_equity
    positions: Dict[str, dict] = {}   # ticker -> {qty, entry_price, entry_date, armed, peak}
    closed_trades: List[dict] = []
    equity_curve: List[tuple] = []
    ai_checks: List[dict] = []  # every candidate the quant rules approved, with AI's verdict on it
    max_open = config.THRESHOLD_MAX_OPEN_POSITIONS
    fee_pct = config.THRESHOLD_FEE_PCT_PER_SIDE / 100.0

    if ai_check and verbose:
        print("AI mode: calling the real sentiment layer (Alpaca News + gpt-4o-mini) "
              "for every signal the quant rules approve — this makes real, tiny OpenAI API calls.")

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
            if sub.empty or sub.index[-1] != day:
                continue  # no bar for this ticker today (holiday gap etc.)
            day_prices[ticker] = float(sub["Close"].iloc[-1])

        # ── Manage exits first ──────────────────────────────────────────────
        for ticker in list(positions.keys()):
            df = data.get(ticker)
            if df is None:
                continue
            sub = df.loc[:day]
            if sub.empty or sub.index[-1] != day:
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
                if sub.empty or sub.index[-1] != day:
                    continue
                signal = ts.evaluate_entry(ticker, sub, check_earnings=False)
                if not signal:
                    continue
                position_dollars = ts.calculate_position_dollars(equity_estimate)
                if position_dollars < 5.0:
                    continue
                qty = position_dollars / signal["price"]  # fractional shares, matches live notional buying
                cost = position_dollars * (1 + fee_pct)
                if cost > cash:
                    continue

                if ai_check:
                    ai_result = ts.get_ai_check(ticker, signal.get("rsi"), as_of_date=day.date())
                    ai_checks.append({
                        "ticker": ticker,
                        "entry_date": str(day.date()),
                        "entry_price": signal["price"],
                        **{k: ai_result[k] for k in (
                            "sentiment_score", "confidence_score", "catalyst_type",
                            "ai_verdict", "ai_reasoning", "should_block", "had_news",
                        )},
                    })

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

    # ── AI-blocking comparison (Phase 2) ──────────────────────────────────────
    # Cross-reference every candidate AI evaluated against its ACTUAL outcome
    # (matched by ticker+entry_date), so we can report what specifically AI
    # would have blocked and whether that would have helped or hurt.
    #
    # Simplification/limitation, stated plainly: this subtracts blocked trades'
    # P&L from the total rather than re-simulating a fully independent
    # alternate-history portfolio (which would need to account for compounding
    # effects — capital freed up by not taking a blocked trade could have been
    # redeployed into a different signal). For a directional "would this help"
    # read, this approximation is good enough; it is not a precise alternate P&L.
    if ai_check:
        outcome_by_key = {(t["ticker"], t["entry_date"]): t for t in closed_trades}
        open_by_key = {(p["ticker"], p["entry_date"]): p for p in open_positions_summary}

        blocked = [a for a in ai_checks if a["should_block"]]
        blocked_with_outcome = []
        blocked_pnl_total = 0.0
        for a in blocked:
            key = (a["ticker"], a["entry_date"])
            if key in outcome_by_key:
                t = outcome_by_key[key]
                blocked_with_outcome.append({**a, "outcome": "closed", "net_pnl": t["net_pnl"], "win": t["win"]})
                blocked_pnl_total += t["net_pnl"]
            elif key in open_by_key:
                p = open_by_key[key]
                blocked_with_outcome.append({**a, "outcome": "still open", "net_pnl": p["unrealized_pnl"], "win": p["unrealized_pnl"] > 0})
                blocked_pnl_total += p["unrealized_pnl"]
            else:
                blocked_with_outcome.append({**a, "outcome": "unknown", "net_pnl": 0.0, "win": None})

        had_news_count = sum(1 for a in ai_checks if a["had_news"])
        result["ai_summary"] = {
            "signals_checked": len(ai_checks),
            "signals_with_news": had_news_count,
            "signals_without_news_failopen": len(ai_checks) - had_news_count,
            "would_block_count": len(blocked),
            "blocked_trades": blocked_with_outcome,
            "blocked_pnl_total": round(blocked_pnl_total, 2),
            "hypothetical_pnl_with_blocking": round(total_realized_pnl - sum(
                b["net_pnl"] for b in blocked_with_outcome if b["outcome"] == "closed"
            ), 2),
            "blocked_wins": sum(1 for b in blocked_with_outcome if b["win"] is True),
            "blocked_losses": sum(1 for b in blocked_with_outcome if b["win"] is False),
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
    print("Note: earnings-date filter is DISABLED in this backtest (no historical")
    print("      earnings-calendar data source integrated — see evaluate_entry() docstring).")
    print(f"Initial equity:      ${result['initial_equity']:,.2f}")
    print(f"Final equity:        ${result['final_equity']:,.2f}")
    print(f"Total return:        {result['total_return_pct']:+.2f}%")
    print(f"Closed trades:       {result['closed_trades']}")
    print(f"Win rate:            {result['win_rate_pct']}% ({result['wins']}W / {result['losses']}L)")
    print(f"Realized P&L:        ${result['total_realized_pnl']:,.2f}")
    print(f"Max drawdown:        {result['max_drawdown_pct']}%")
    print(f"Open at end:         {len(result['open_positions_at_end'])} position(s)")
    for p in result["open_positions_at_end"]:
        print(f"  • {p['ticker']}: {p['qty']:.4f}sh @ ${p['entry_price']:.2f} → "
              f"${p['last_price']:.2f}  (unrealized ${p['unrealized_pnl']:+.2f}, "
              f"{'armed' if p['armed'] else 'not armed'})")
    print("-" * 60)
    print("Trade log:")
    for t in result["trade_log"]:
        outcome = "WIN " if t["win"] else "LOSS"
        print(f"  [{outcome}] {t['ticker']:6s} {t['entry_date']} @ ${t['entry_price']:.2f} → "
              f"{t['exit_date']} @ ${t['exit_price']:.2f}  "
              f"net=${t['net_pnl']:+.2f} ({t['pnl_pct']:+.2f}%)  [{t['exit_reason']}]")

    if "ai_summary" in result:
        ai = result["ai_summary"]
        print("-" * 60)
        print("AI SENTIMENT LAYER — PHASE 2 COMPARISON (blocking OFF in this run — informational)")
        print("-" * 60)
        print(f"Signals checked by AI:        {ai['signals_checked']}")
        print(f"  ...with real news found:    {ai['signals_with_news']}")
        print(f"  ...no news (fail-open):     {ai['signals_without_news_failopen']}")
        print(f"AI would have BLOCKED:        {ai['would_block_count']}")
        print(f"  of those: {ai['blocked_wins']} were actually WINS, {ai['blocked_losses']} were actually LOSSES")
        print(f"P&L of blocked trades:        ${ai['blocked_pnl_total']:+.2f}")
        print(f"Baseline realized P&L:        ${result['total_realized_pnl']:+.2f}")
        print(f"Hypothetical P&L w/ blocking: ${ai['hypothetical_pnl_with_blocking']:+.2f}  "
              f"(approximation — see run_backtest() docstring on this limitation)")
        print("-" * 60)
        for b in ai["blocked_trades"]:
            outcome_str = "WIN " if b["win"] else ("LOSS" if b["win"] is False else "????")
            print(f"  [BLOCKED, was {outcome_str}] {b['ticker']:6s} {b['entry_date']} — "
                  f"sentiment={b['sentiment_score']}, {b['catalyst_type']} — \"{b['ai_reasoning']}\" "
                  f"— actual net P&L ${b['net_pnl']:+.2f}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--universe", choices=["watchlist", "sp500"], default="watchlist")
    parser.add_argument("--equity", type=float, default=5000.0)
    parser.add_argument("--ai", action="store_true", help="Run the real AI sentiment layer on every candidate signal (makes real OpenAI + Alpaca News API calls)")
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

    result = run_backtest(tickers, months=args.months, initial_equity=args.equity, ai_check=args.ai)
    print_report(result)
