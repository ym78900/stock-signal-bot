"""
round8_position_limit.py — Round 8: MAX_OPEN_POSITIONS sweep.

Background:
  simulate_fast() never enforced MAX_OPEN_POSITIONS (the open_tickers set was
  only ever .discard()-ed, never .add()-ed). Every prior round effectively ran
  with UNLIMITED concurrent positions. This script uses the new time-aware
  simulate_concurrent(), which models real entry→exit windows, so the position
  limit is genuinely enforced and capital can't be deployed past 100% of equity.

What it tests:
  MAX_OPEN_POSITIONS ∈ {3, 5, 6, 7, 8, unlimited}
  using the live config filters (S&P 500, $5–$200 price cap, vol 1.2×, SPY 50MA,
  VIX < 25, RSI < 38, 12% position size, fixed 3.5×/6.0× ATR exits).

Caveat:
  The exit cache models a FIXED 3.5× stop / 6.0× target, not the live trailing
  stop. Round 7 already established trailing > fixed; Round 8 isolates the effect
  of the position limit, holding the exit model constant across all variants so
  the comparison is apples-to-apples.

Run:
  /Library/Developer/CommandLineTools/usr/bin/python3.9 round8_position_limit.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import backtester as bt
from scanner import get_sp500_tickers


def main():
    print("\n" + "=" * 78)
    print("  ROUND 8 — MAX_OPEN_POSITIONS sweep (time-aware concurrency)")
    print("=" * 78)

    # ── Load cached data / indicators / exits (fast — caches already exist) ────
    tickers = get_sp500_tickers()
    print(f"  S&P 500 tickers: {len(tickers)}")
    data = bt.load_or_download_data(tickers)
    if not data:
        print("ERROR: no price data.")
        sys.exit(1)

    rows, processed = bt.precompute_signals(data)
    spy_trend = bt.load_spy_trend()
    vix_data  = bt.load_vix()

    atr_stops   = [1.5, 2.0, 2.5, 3.0, 3.5]
    atr_targets = [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
    enriched = bt.precompute_exits(rows, processed, atr_stops, atr_targets)
    print(f"  Enriched rows: {len(enriched)}\n")

    # ── Live config parameters ─────────────────────────────────────────────────
    RSI_BUY, RSI_SELL = 38, 55
    ATR_STOP, ATR_TARGET = 3.5, 6.0

    live_filters = dict(
        volume_min_ratio = 1.2,
        spy_trend        = spy_trend,
        spy_ma           = "above_50ma",
        vix_data         = vix_data,
        vix_max          = 25,
        min_price        = 5.0,
        max_price        = 200.0,
    )

    # ── Reference: OLD simulate_fast (no concurrency enforcement) ──────────────
    _, s_old = bt.simulate_fast(
        enriched, RSI_BUY, RSI_SELL, ATR_STOP, ATR_TARGET,
        max_position_pct=0.12, max_open_pos=5, sizing_mode="fixed_pct",
        **live_filters,
    )

    # ── Sweep MAX_OPEN_POSITIONS with the corrected simulator ──────────────────
    limits = [3, 5, 6, 7, 8, 9999]
    results = []
    for lim in limits:
        _, s = bt.simulate_concurrent(
            enriched, processed, RSI_BUY, RSI_SELL, ATR_STOP, ATR_TARGET,
            max_open_pos     = lim,
            max_position_pct = 0.12,
            sizing_mode      = "fixed_pct",
            enforce_cash     = True,
            **live_filters,
        )
        label = "unlimited" if lim >= 9999 else str(lim)
        results.append((label, s))

    # ── Report ─────────────────────────────────────────────────────────────────
    def fmt(label, s, peak=True):
        if not s:
            print(f"  {label:<12} no trades")
            return
        pk = s.get("peak_concurrent_positions", "—")
        pk_str = f"{pk:>4}" if peak else "   —"
        print(f"  {label:<12} {s['total_trades']:>6} {s['win_rate_pct']:>6.1f}% "
              f"${s['total_net_pnl']:>+11,.0f} {s['total_return_pct']:>+9.1f}% "
              f"{s['max_drawdown_pct']:>7.1f}% {s['profit_factor']:>6.2f} {pk_str}")

    print("=" * 78)
    print(f"  {'Max pos':<12} {'Trades':>6} {'Win%':>7} {'P&L':>12} {'Return':>10} "
          f"{'MaxDD':>8} {'PF':>6} {'Peak':>4}")
    print("  " + "-" * 74)
    print("  -- old simulate_fast (no concurrency cap — all prior rounds) --")
    fmt("(old, =5)", s_old, peak=False)
    print("  -- simulate_concurrent (time-aware, cap enforced) --")
    for label, s in results:
        fmt(label, s)
    print("=" * 78)

    # ── Verdict vs current live setting (5) ────────────────────────────────────
    base = next((s for l, s in results if l == "5"), None)
    if base:
        base_pnl = base["total_net_pnl"]
        base_dd  = base["max_drawdown_pct"]
        print(f"\n  Current live setting = 5 positions.  Impact of changing:")
        for label, s in results:
            if not s or label == "5":
                continue
            dp = s["total_net_pnl"] - base_pnl
            dd = s["max_drawdown_pct"] - base_dd
            better_pnl = dp > 0
            worse_dd   = dd > 0
            print(f"    {label:>9}: P&L {'▲' if dp>=0 else '▼'} ${abs(dp):>7,.0f}  "
                  f"DD {'▲' if dd>=0 else '▼'} {abs(dd):>4.1f}pp  PF {s['profit_factor']:.2f}")

    print("\nRound 8 complete.")


if __name__ == "__main__":
    main()
