"""
round10_sector_robustness.py — Split-half robustness check for the sector guard.

Round 9 found max_per_sector=4 held P&L (+$1,574 vs +$1,568) while nearly halving
max drawdown (10.5% -> 5.7%) on the FULL 2-year window. But the backtester's
drawdown is a realized, exit-ordered curve (it ignores simultaneous unrealized
losses), and we already learned this project that full-window path-shape wins can be
regime-dependent (the trailing 3.0x episode). So before trusting it, split the window
in half by entry date and require the improvement to hold in BOTH halves.

Pass criterion for adopting max_per_sector=4:
  In each half independently, vs the no-cap baseline:
    - P&L not materially worse (>= baseline - small tolerance), AND
    - max drawdown <= baseline drawdown (the whole point of the guard)

Run:
  /Library/Developer/CommandLineTools/usr/bin/python3.9 round10_sector_robustness.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from typing import Dict, List, Optional

import backtester as bt
import config
from scanner import get_sp500_tickers
from round9_sector_guard import build_sector_map


def _entry_date(row):
    return bt._to_date(row.get("date_next"))


def run_window(enriched, processed, sector_map, live_filters, trail,
               max_per_sector: Optional[int]):
    return bt.simulate_concurrent(
        enriched, processed,
        config.RSI_BUY_THRESHOLD, 55, 3.5, 6.0,
        max_open_pos     = config.MAX_OPEN_POSITIONS,
        max_position_pct = config.MAX_POSITION_PCT,
        sizing_mode      = "fixed_pct",
        enforce_cash     = True,
        trail_mult       = trail,
        sector_map       = sector_map if max_per_sector else None,
        max_per_sector   = max_per_sector,
        **live_filters,
    )


def main():
    print("\n" + "=" * 90)
    print("  ROUND 10 — Sector guard split-half robustness (max_per_sector = 4 vs none)")
    print("=" * 90)

    sector_map = build_sector_map()
    tickers = get_sp500_tickers()
    data = bt.load_or_download_data(tickers)
    if not data:
        print("ERROR: no price data.")
        sys.exit(1)

    rows, processed = bt.precompute_signals(data)
    spy_trend = bt.load_spy_trend()
    vix_data  = bt.load_vix()
    enriched  = bt.precompute_exits(rows, processed, [3.5], [6.0])

    TRAIL = config.ATR_STOP_MULTIPLIER
    bt.attach_trailing_exits(enriched, processed, [TRAIL])

    live_filters = dict(
        volume_min_ratio = config.VOLUME_CONFIRMATION_RATIO,
        spy_trend        = spy_trend,
        spy_ma           = "above_50ma",
        vix_data         = vix_data,
        vix_max          = config.VIX_MAX,
        min_price        = config.PRICE_MIN,
        max_price        = config.PRICE_MAX_HARD,
    )

    # ── Determine split point: median entry date across all dated rows ──────────
    dated = sorted(d for d in (_entry_date(r) for r in enriched) if d is not None)
    if not dated:
        print("ERROR: no dated rows.")
        sys.exit(1)
    mid = dated[len(dated) // 2]
    print(f"  Entry-date span: {dated[0]} -> {dated[-1]}  |  split at {mid}\n")

    first_half  = [r for r in enriched if (_entry_date(r) is not None and _entry_date(r) <= mid)]
    second_half = [r for r in enriched if (_entry_date(r) is not None and _entry_date(r) >  mid)]

    halves = [
        ("FULL window",  enriched),
        ("First half",   first_half),
        ("Second half",  second_half),
    ]

    def fmt(s):
        if not s:
            return "      no trades"
        return (f"{s['total_trades']:>5} {s['win_rate_pct']:>6.1f}% "
                f"${s['total_net_pnl']:>+9,.0f} {s['max_drawdown_pct']:>7.1f}% {s['profit_factor']:>6.2f}")

    print(f"  {'Window':<13} {'Cap':<6} {'Trades':>5} {'Win%':>7} {'P&L':>11} {'MaxDD':>8} {'PF':>7}")
    print("  " + "-" * 72)

    verdicts = []
    for name, rows_subset in halves:
        _, base = run_window(rows_subset, processed, sector_map, live_filters, TRAIL, None)
        _, cap4 = run_window(rows_subset, processed, sector_map, live_filters, TRAIL, 4)
        print(f"  {name:<13} {'none':<6} {fmt(base)}")
        print(f"  {name:<13} {'4/sec':<6} {fmt(cap4)}")
        print("  " + "-" * 72)

        if name == "FULL window" or not base or not cap4:
            continue
        dp = cap4["total_net_pnl"] - base["total_net_pnl"]
        dd = cap4["max_drawdown_pct"] - base["max_drawdown_pct"]
        # tolerance: P&L allowed to dip up to 5% of baseline; DD must not increase
        pnl_ok = dp >= -abs(base["total_net_pnl"]) * 0.05
        dd_ok  = dd <= 0.0
        verdicts.append((name, pnl_ok and dd_ok, dp, dd))

    print("\n  ── Robustness verdict (each half vs its own no-cap baseline) ──")
    for name, ok, dp, dd in verdicts:
        print(f"    {name:<12}: P&L {'+' if dp>=0 else '-'}${abs(dp):>6,.0f}  "
              f"DD {'+' if dd>=0 else '-'}{abs(dd):>4.1f}pp  -> {'PASS' if ok else 'FAIL'}")

    all_pass = verdicts and all(ok for _, ok, _, _ in verdicts)
    print("\n  " + ("ROBUST: max_per_sector=4 holds in BOTH halves — safe to consider for config."
                    if all_pass else
                    "NOT ROBUST: improvement does not hold in both halves — keep no sector cap for now."))
    print("\nRound 10 complete.")


if __name__ == "__main__":
    main()
