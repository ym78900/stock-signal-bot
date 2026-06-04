"""
preflight_check.py — Read-only connectivity & readiness validator.

Runs a battery of READ-ONLY checks against the live environment so you can
confirm the bot is wired up correctly BEFORE placing any paper orders.

It NEVER places, cancels, or modifies any order or position. Safe to run
anytime — including while the US market is closed.

Checks performed:
  1. .env keys present (Alpaca + Telegram)
  2. config sanity (PAPER_TRADING flag, key thresholds)
  3. Alpaca account reachable — equity, buying power, account status
  4. Market clock — open/closed + next open/close (so you know if a paper
     order would actually fill right now)
  5. Open positions + open orders snapshot
  6. Circuit breakers — VIX + SPY 50-day MA (the same gates live trading uses)

Usage:
    /Library/Developer/CommandLineTools/usr/bin/python3.9 preflight_check.py
"""

import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import config
import trader

# ── tiny output helpers ───────────────────────────────────────────────────────

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_results = []


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ── 1. environment keys ───────────────────────────────────────────────────────

def check_env() -> None:
    section("1. Environment (.env) keys")
    required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]
    optional = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]
    for key in required:
        if os.environ.get(key):
            record(key, PASS, "set")
        else:
            record(key, FAIL, "MISSING — required for Alpaca")
    for key in optional:
        if os.environ.get(key):
            record(key, PASS, "set")
        else:
            record(key, WARN, "not set (Telegram reporting will be disabled)")


# ── 2. config sanity ──────────────────────────────────────────────────────────

def check_config() -> None:
    section("2. Config sanity")
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    status = PASS if config.PAPER_TRADING else WARN
    record("PAPER_TRADING", status, f"{mode} trading")
    record("MAX_OPEN_POSITIONS", PASS, str(config.MAX_OPEN_POSITIONS))
    record("ATR_STOP_MULTIPLIER", PASS, f"{config.ATR_STOP_MULTIPLIER}x (trailing)")
    record("PRICE band", PASS, f"${config.PRICE_MIN:.0f}–${config.PRICE_MAX_HARD:.0f}")
    record("RSI_BUY_THRESHOLD", PASS, str(config.RSI_BUY_THRESHOLD))


# ── 3. Alpaca account ─────────────────────────────────────────────────────────

def check_account() -> None:
    section("3. Alpaca account")
    try:
        acct = trader._trading_client().get_account()
    except Exception as e:
        record("Account fetch", FAIL, f"could not reach Alpaca: {e}")
        return

    acct_status = str(getattr(acct, "status", "unknown"))
    record("Account status", PASS if "ACTIVE" in acct_status.upper() else WARN, acct_status)

    equity = float(getattr(acct, "equity", 0) or 0)
    buying_power = float(getattr(acct, "buying_power", 0) or 0)
    cash = float(getattr(acct, "cash", 0) or 0)
    record("Equity", PASS, f"${equity:,.2f}")
    record("Cash", PASS, f"${cash:,.2f}")
    record("Buying power", PASS, f"${buying_power:,.2f}")

    if getattr(acct, "trading_blocked", False):
        record("Trading blocked", FAIL, "account flag trading_blocked=True")
    if getattr(acct, "account_blocked", False):
        record("Account blocked", FAIL, "account flag account_blocked=True")


# ── 4. market clock ───────────────────────────────────────────────────────────

def check_market_clock() -> None:
    section("4. Market clock")
    try:
        clock = trader._trading_client().get_clock()
    except Exception as e:
        record("Clock fetch", FAIL, f"could not reach Alpaca clock: {e}")
        return

    is_open = bool(getattr(clock, "is_open", False))
    if is_open:
        record("Market", PASS, f"OPEN — next close {getattr(clock, 'next_close', '?')}")
        record("Order fill", PASS, "a paper market buy would fill now")
    else:
        record("Market", WARN, f"CLOSED — next open {getattr(clock, 'next_open', '?')}")
        record("Order fill", WARN,
               "a paper market buy will QUEUE and fill at next open, not immediately")


# ── 5. positions & open orders ────────────────────────────────────────────────

def check_positions_and_orders() -> None:
    section("5. Positions & open orders")
    try:
        client = trader._trading_client()
        positions = client.get_all_positions()
        record("Open positions", PASS, f"{len(positions)} open")
        for p in positions:
            print(f"        - {p.symbol}: {p.qty} @ ${float(p.avg_entry_price):.2f} "
                  f"(uPL ${float(p.unrealized_pl):.2f})")
    except Exception as e:
        record("Positions fetch", FAIL, str(e))

    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
        orders = trader._trading_client().get_orders(req)
        record("Open orders", PASS, f"{len(orders)} open")
        for o in orders:
            print(f"        - {o.symbol}: {o.side} {o.qty} {o.order_type} ({o.status})")
    except Exception as e:
        record("Orders fetch", FAIL, str(e))


# ── 6. circuit breakers ───────────────────────────────────────────────────────

def check_circuit_breakers() -> None:
    section("6. Circuit breakers (VIX + SPY trend)")
    try:
        ok, reason = trader.run_circuit_breakers()
        if ok:
            record("Circuit breakers", PASS, "clear — trading would be allowed today")
        else:
            record("Circuit breakers", WARN, f"BLOCKED: {reason}")
    except Exception as e:
        record("Circuit breakers", FAIL, str(e))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print(" PREFLIGHT CHECK — read-only, no orders placed")
    print(f" {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 60)

    check_env()
    check_config()
    check_account()
    check_market_clock()
    check_positions_and_orders()
    check_circuit_breakers()

    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    n_warn = sum(1 for _, s, _ in _results if s == WARN)
    n_pass = sum(1 for _, s, _ in _results if s == PASS)

    print("\n" + "=" * 60)
    print(f" SUMMARY: {n_pass} pass · {n_warn} warn · {n_fail} fail")
    print("=" * 60)

    if n_fail:
        print(" Result: NOT READY — resolve FAIL items above before testing.")
        return 1
    if n_warn:
        print(" Result: READY (with warnings — review WARN items, e.g. market closed).")
        return 0
    print(" Result: ALL CLEAR.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
