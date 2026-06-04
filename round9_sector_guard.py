"""
round9_sector_guard.py — Test the sector-diversification guard (IMPROVEMENTS.md #1).

Question:
  Does capping the number of concurrent open positions per GICS sector improve the
  corrected Round 8 baseline (cap=7, 12% size, trailing 3.5x ATR, +$1,568 / 2yr),
  or does it just starve the engine of trades?

Method:
  - Build a {ticker -> GICS Sector} map from the same S&P 500 Wikipedia table that
    get_sp500_tickers() already uses.
  - Run simulate_concurrent() with the LIVE config (trailing 3.5x exit, cap=7) and
    sweep max_per_sector in {none (baseline), 4, 3, 2, 1}.
  - Report P&L, return, win%, drawdown, PF, peak concurrency, and how many candidate
    trades the sector guard skipped.

  The sector guard only ever REMOVES trades, so any improvement must come from
  avoiding correlated losers. We are looking for: equal-or-better P&L with lower DD.

Run:
  /Library/Developer/CommandLineTools/usr/bin/python3.9 round9_sector_guard.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from typing import Dict, List

import backtester as bt
import config
from scanner import get_sp500_tickers


def build_sector_map() -> Dict[str, str]:
    """Fetch {ticker -> GICS Sector} from the S&P 500 Wikipedia table."""
    import requests
    import io
    import pandas as pd

    headers = {"User-Agent": "Mozilla/5.0 (compatible; stock-signal-bot/1.0)"}
    resp = requests.get(config.SP500_WIKIPEDIA_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text))[0]

    # Column is "GICS Sector" on the current Wikipedia layout.
    sector_col = next((c for c in table.columns if "Sector" in str(c)), None)
    if sector_col is None:
        raise RuntimeError(f"No GICS Sector column found. Columns: {list(table.columns)}")

    smap: Dict[str, str] = {}
    for sym, sec in zip(table["Symbol"].tolist(), table[sector_col].tolist()):
        smap[str(sym).replace(".", "-")] = str(sec)
    return smap


def main():
    print("\n" + "=" * 88)
    print("  ROUND 9 — Sector diversification guard (trailing 3.5x, cap=7)")
    print("=" * 88)

    sector_map = build_sector_map()
    n_sectors = len(set(sector_map.values()))
    print(f"  Sector map: {len(sector_map)} tickers across {n_sectors} GICS sectors")

    tickers = get_sp500_tickers()
    data = bt.load_or_download_data(tickers)
    if not data:
        print("ERROR: no price data.")
        sys.exit(1)

    rows, processed = bt.precompute_signals(data)
    spy_trend = bt.load_spy_trend()
    vix_data  = bt.load_vix()

    atr_stops   = [3.5]
    atr_targets = [6.0]
    enriched = bt.precompute_exits(rows, processed, atr_stops, atr_targets)

    # Live exit = trailing 3.5x ATR
    TRAIL = config.ATR_STOP_MULTIPLIER  # 3.5
    bt.attach_trailing_exits(enriched, processed, [TRAIL])
    print(f"  Enriched rows: {len(enriched)} | trailing mult: {TRAIL}x | cap: {config.MAX_OPEN_POSITIONS}\n")

    RSI_BUY, RSI_SELL = config.RSI_BUY_THRESHOLD, 55
    live_filters = dict(
        volume_min_ratio = config.VOLUME_CONFIRMATION_RATIO,
        spy_trend        = spy_trend,
        spy_ma           = "above_50ma",
        vix_data         = vix_data,
        vix_max          = config.VIX_MAX,
        min_price        = config.PRICE_MIN,
        max_price        = config.PRICE_MAX_HARD,
    )

    def run(max_per_sector):
        trades, summary = bt.simulate_concurrent(
            enriched, processed, RSI_BUY, RSI_SELL, 3.5, 6.0,
            max_open_pos     = config.MAX_OPEN_POSITIONS,
            max_position_pct = config.MAX_POSITION_PCT,
            sizing_mode      = "fixed_pct",
            enforce_cash     = True,
            trail_mult       = TRAIL,
            sector_map       = sector_map if max_per_sector else None,
            max_per_sector   = max_per_sector,
            **live_filters,
        )
        return trades, summary

    variants = [
        ("none (baseline)", None),
        ("4 per sector", 4),
        ("3 per sector", 3),
        ("2 per sector", 2),
        ("1 per sector", 1),
    ]

    base_trades, base = run(None)
    base_n = base["total_trades"]

    print("=" * 88)
    print(f"  {'Sector cap':<18} {'Trades':>6} {'Win%':>7} {'P&L':>12} {'Return':>10} "
          f"{'MaxDD':>8} {'PF':>6} {'Peak':>5}")
    print("  " + "-" * 84)

    results = []
    for label, cap in variants:
        trades, s = run(cap)
        results.append((label, cap, s))
        if not s:
            print(f"  {label:<18} no trades")
            continue
        pk = s.get("peak_concurrent_positions", "—")
        print(f"  {label:<18} {s['total_trades']:>6} {s['win_rate_pct']:>6.1f}% "
              f"${s['total_net_pnl']:>+11,.0f} {s['total_return_pct']:>+9.1f}% "
              f"{s['max_drawdown_pct']:>7.1f}% {s['profit_factor']:>6.2f} {pk:>5}")
    print("=" * 88)

    # Verdict vs baseline (no sector cap)
    bp, bd, bpf = base["total_net_pnl"], base["max_drawdown_pct"], base["profit_factor"]
    print(f"\n  Baseline (no sector cap): ${bp:+,.0f} | DD {bd:.1f}% | PF {bpf:.2f} | {base_n} trades")
    print("  Impact of each sector cap:")
    for label, cap, s in results:
        if cap is None or not s:
            continue
        dp = s["total_net_pnl"] - bp
        dd = s["max_drawdown_pct"] - bd
        skipped = base_n - s["total_trades"]
        verdict = "BETTER" if (dp >= 0 and dd <= 0) else \
                  "mixed"  if (dp >= 0 or dd <= 0) else "WORSE"
        print(f"    {label:<14}: P&L {'+' if dp>=0 else '-'}${abs(dp):>6,.0f}  "
              f"DD {'+' if dd>=0 else '-'}{abs(dd):>4.1f}pp  "
              f"PF {s['profit_factor']:.2f}  (~{skipped} trades skipped)  -> {verdict}")

    print("\nRound 9 complete.")


if __name__ == "__main__":
    main()
