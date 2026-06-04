"""
e2e_test_order.py — Controlled, single-order end-to-end paper test.

Mirrors the live two-phase execution path (main.py job_execute_trades) but for
ONE ticker, synchronously, with guardrails — so you can watch the full cycle:

    market buy  →  poll for fill  →  attach trailing stop  →  verify it's live
                →  log to trades.csv (so monitor + reports pick it up)

It is deliberately small and explicit. Nothing is scheduled; you run it by hand.

SAFETY:
  - Refuses to run unless config.PAPER_TRADING is True.
  - Defaults to a tiny order (1 share of a cheap, liquid ticker).
  - --dry-run validates everything and places NO order.
  - --liquidate cancels all open orders + closes all positions (cleanup).

Usage:
  # validate only, no order:
  python3.9 e2e_test_order.py --dry-run

  # place 1 share of F with an auto trail, watch the cycle:
  python3.9 e2e_test_order.py --ticker F --qty 1

  # custom trail distance in dollars:
  python3.9 e2e_test_order.py --ticker F --qty 1 --trail 0.40

  # clean up afterwards (cancel orders + flatten):
  python3.9 e2e_test_order.py --liquidate

(Use the Python 3.9 interpreter:
 /Library/Developer/CommandLineTools/usr/bin/python3.9 e2e_test_order.py ...)
"""

import argparse
import os
import sys
import tempfile
import time
import uuid
from datetime import date

from dotenv import load_dotenv

load_dotenv()

import config
import trader
import trade_logger as tlog


def _clock():
    return trader._trading_client().get_clock()


def _open_orders():
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
    return trader._trading_client().get_orders(req)


def liquidate() -> None:
    print("Cleanup: cancelling all open orders + liquidating all positions...")
    n_cancelled = trader.cancel_all_orders()
    trader.liquidate_all_positions()
    print(f"  cancelled {n_cancelled} order(s); liquidation requested.")
    print("  (verify with: python3.9 preflight_check.py)")


def run_test(ticker: str, qty: int, trail: float, log_it: bool) -> int:
    print("=" * 60)
    print(f" E2E TEST ORDER — {ticker} x{qty}")
    print("=" * 60)

    # ── guards ────────────────────────────────────────────────────────────────
    if not config.PAPER_TRADING:
        print("ABORT: config.PAPER_TRADING is False. Refusing to trade live.")
        return 1

    clock = _clock()
    is_open = bool(getattr(clock, "is_open", False))
    print(f"Market open: {is_open}"
          + ("" if is_open else f"  (next open {getattr(clock, 'next_open', '?')})"))
    if not is_open:
        print("NOTE: market is CLOSED — the buy will QUEUE and fill at the next open.")
        print("      This script polls for ~5 min then stops. Re-run at/after the")
        print("      open (16:30 Finnish) to see the fill + trailing stop attach.")

    equity = trader.get_account_equity()
    print(f"Account equity: ${equity:,.2f}")
    if equity <= 0:
        print("ABORT: could not read account equity.")
        return 1

    # ── Phase 1: market buy ───────────────────────────────────────────────────
    print(f"\nPhase 1 — placing market buy: {ticker} x{qty}")
    order_id = trader.place_market_buy(ticker, qty)
    if not order_id:
        print("ABORT: market buy failed (see log above).")
        return 1
    print(f"  order id: {order_id}")

    if log_it:
        tlog.log_order_placed(
            ticker          = ticker,
            signal_date     = str(date.today()),
            entry_price_est = 0.0,
            stop_price      = None,
            target_price    = None,
            qty             = qty,
            alpaca_order_id = order_id,
        )
        print("  logged to trades.csv (status=open)")

    # ── Phase 2: poll for fill ────────────────────────────────────────────────
    print("\nPhase 2 — polling for fill (10 attempts x 30s = up to 5 min)...")
    fill_price = None
    for attempt in range(10):
        fill_price = trader.get_order_fill_price(order_id)
        if fill_price:
            print(f"  FILLED @ ${fill_price}")
            break
        print(f"  not yet filled (attempt {attempt + 1}/10)...")
        if attempt < 9:
            time.sleep(30)

    if not fill_price:
        print("\nNo fill within 5 min.")
        if not is_open:
            print("Expected — market is closed. The order is still working; cancel it")
            print("with --liquidate, or re-run this script after the open to continue.")
        else:
            print("Unexpected while market is open — cancelling the order to be safe.")
            trader.cancel_order(order_id)
        return 1

    # ── attach trailing stop ──────────────────────────────────────────────────
    if trail <= 0:
        trail = round(max(0.25, fill_price * 0.05), 2)  # plumbing-test default
        print(f"\n(no --trail given; using auto trail ${trail} = ~5% of fill)")
    print(f"\nPhase 2b — attaching trailing stop: trail=${trail}")
    exit_id = trader.place_trailing_stop_exit(ticker, qty, trail)
    if not exit_id:
        print("FAIL: trailing stop did NOT attach — position is UNPROTECTED.")
        print("      Run with --liquidate to flatten, then investigate.")
        return 1
    print(f"  trailing stop order id: {exit_id}")

    if log_it:
        tlog.update_trade_after_fill(
            entry_order_id = order_id,
            fill_price     = fill_price,
            stop_price     = round(fill_price - trail, 2),
            target_price   = None,
            oco_order_id   = exit_id,
        )
        print("  trades.csv updated with fill + exit-order id (monitor will track it)")

    # ── verify the trailing stop is actually live ─────────────────────────────
    print("\nVerify — open orders now on the account:")
    found = False
    for o in _open_orders():
        marker = "  <-- our trailing stop" if str(o.id) == exit_id else ""
        print(f"  {o.symbol}: {o.side} {o.qty} {o.order_type} ({o.status}){marker}")
        if str(o.id) == exit_id:
            found = True

    print("\n" + "=" * 60)
    if found:
        print(" RESULT: PASS — buy filled, trailing stop is live and protecting.")
        print(" Next: watch /positions in Telegram, and the monitor will report the")
        print(" exit when the trailing stop fills (or on max-hold).")
    else:
        print(" RESULT: WARN — exit order id not found in open orders. Check Alpaca.")
    print("=" * 60)
    return 0


def simulate(ticker: str, qty: int, fill: float, atr: float,
             trail: float, exit_price: float) -> int:
    """
    Fully offline simulation of the live trade lifecycle.

    Touches NO market and places NO order. It mocks the Alpaca fill and the
    trailing-stop order, then drives the exact same logging chain the live
    bot uses (log_order_placed -> update_trade_after_fill -> mark_trade_closed)
    and renders the resulting report — so you can watch the whole process now.

    To avoid polluting the real trades.csv, the trade log is redirected to a
    throwaway temp file for the duration of this run.
    """
    print("=" * 60)
    print(f" SIMULATION (offline, no market, no orders) — {ticker} x{qty}")
    print("=" * 60)

    # ── redirect the trade log to a throwaway file ────────────────────────────
    sim_log = os.path.join(tempfile.gettempdir(),
                           f"sim_trades_{date.today().isoformat()}.csv")
    if os.path.exists(sim_log):
        os.remove(sim_log)
    real_log = tlog.TRADE_LOG
    tlog.TRADE_LOG = sim_log
    print(f"Throwaway trade log: {sim_log}")
    print(f"(real trades.csv at {real_log} is NOT touched)\n")

    try:
        # ── derive the trailing distance the same way the live path does ──────
        if atr > 0:
            trail = round(atr * config.ATR_STOP_MULTIPLIER, 2)
            print(f"Trail = ATR({atr}) x {config.ATR_STOP_MULTIPLIER} = ${trail}")
        elif trail > 0:
            print(f"Trail = ${trail} (provided)")
        else:
            trail = round(max(0.25, fill * 0.05), 2)
            print(f"Trail = ${trail} (auto ~5% of fill)")

        entry_order_id = f"SIM-{uuid.uuid4().hex[:8]}"
        exit_order_id  = f"SIM-{uuid.uuid4().hex[:8]}"

        # ── Phase 1: market buy placed (mocked) ───────────────────────────────
        print(f"\n[1] Market buy placed (mock id {entry_order_id})")
        tlog.log_order_placed(
            ticker          = ticker,
            signal_date     = str(date.today()),
            entry_price_est = fill,
            stop_price      = None,
            target_price    = None,
            qty             = qty,
            alpaca_order_id = entry_order_id,
        )

        # ── Phase 2: fill confirmed + trailing stop attached (mocked) ─────────
        init_stop = round(fill - trail, 2)
        print(f"[2] FILLED @ ${fill}  ->  trailing stop attached "
              f"(mock id {exit_order_id}, initial stop ${init_stop})")
        tlog.update_trade_after_fill(
            entry_order_id = entry_order_id,
            fill_price     = fill,
            stop_price     = init_stop,
            target_price   = None,
            oco_order_id   = exit_order_id,
        )

        # show OPEN state
        import importlib
        import reporter
        importlib.reload(reporter)  # pick up redirected TRADE_LOG via tlog
        open_trades = tlog.get_open_trades()
        print(f"\n--- State after entry: {len(open_trades)} open position(s) ---")
        for t in open_trades:
            print(f"    {t['ticker']}  qty={t['qty']}  entry=${t['entry_price']}  "
                  f"stop=${t['stop_price']}  exit-order={t['alpaca_order_id']}")

        # ── Phase 3: exit (mocked trailing-stop fill) ─────────────────────────
        if exit_price <= 0:
            exit_price = round(fill + trail, 2)  # default: a modest winning exit
        gross = round((exit_price - fill) * qty, 2)
        print(f"\n[3] Trailing stop fills @ ${exit_price}  "
              f"(simulated exit, gross ${gross:+,.2f} before fees)")
        closed = tlog.mark_trade_closed(
            alpaca_order_id = exit_order_id,
            exit_price      = exit_price,
            exit_date       = str(date.today()),
            exit_reason     = "trailing_stop",
        )
        if closed:
            print(f"    closed: net ${closed['net_pnl']}  ({closed['pnl_pct']}%)  "
                  f"win={closed['win']}")

        # ── Phase 4: the report the bot would post ────────────────────────────
        print("\n[4] Inception report the bot would post to Telegram:")
        print("-" * 60)
        print(reporter.build_inception_report())
        print("-" * 60)

        print("\n" + "=" * 60)
        print(" SIMULATION COMPLETE — full lifecycle exercised offline.")
        print(" buy -> fill -> trailing stop -> exit -> log -> report all ran.")
        print(f" Inspect the throwaway log at: {sim_log}")
        print("=" * 60)
        return 0
    finally:
        tlog.TRADE_LOG = real_log  # always restore the real path


def main() -> int:
    p = argparse.ArgumentParser(description="Controlled single-order paper test.")
    p.add_argument("--ticker", default="F", help="ticker to buy (default: F)")
    p.add_argument("--qty", type=int, default=1, help="shares to buy (default: 1)")
    p.add_argument("--trail", type=float, default=0.0,
                   help="trailing stop distance in $ (default: auto ~5%% of fill)")
    p.add_argument("--dry-run", action="store_true",
                   help="validate only, place NO order")
    p.add_argument("--simulate", action="store_true",
                   help="offline: mock the fill + exit and run the full logging/"
                        "report chain now (no market, no order)")
    p.add_argument("--fill", type=float, default=10.0,
                   help="[--simulate] mock entry fill price (default: 10.00)")
    p.add_argument("--atr", type=float, default=0.0,
                   help="[--simulate] ATR to derive trail = ATR x stop-mult "
                        "(overrides --trail)")
    p.add_argument("--exit", type=float, default=0.0, dest="exit_price",
                   help="[--simulate] mock exit price (default: fill + trail)")
    p.add_argument("--no-log", action="store_true",
                   help="do not write to trades.csv")
    p.add_argument("--liquidate", action="store_true",
                   help="cancel all orders + close all positions, then exit")
    args = p.parse_args()

    if args.liquidate:
        liquidate()
        return 0

    if args.simulate:
        return simulate(args.ticker, args.qty, args.fill, args.atr,
                        args.trail, args.exit_price)

    if args.dry_run:
        print("DRY RUN — no order will be placed. Running preflight instead:")
        import preflight_check
        return preflight_check.main()

    return run_test(args.ticker, args.qty, args.trail, log_it=not args.no_log)


if __name__ == "__main__":
    sys.exit(main())
