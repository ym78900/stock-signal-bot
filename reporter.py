"""
reporter.py — Weekly and inception-to-date performance reports.

Reads trades.csv via trade_logger and produces formatted Telegram messages.
No AI, no external calls — pure arithmetic on the trade log.

Reports include:
  - This week's trades (wins/losses, P&L, exit breakdown)
  - Best and worst trade of the week
  - Running totals since inception
  - Comparison vs backtest baseline
  - Drawdown tracking
  - Projected daily/monthly rate
"""

import logging
from datetime import date, timedelta
from typing import List, Optional

import trade_logger as tlog
import config

logger = logging.getLogger(__name__)

# ── Backtest baseline (Round 8 corrected — concurrency-aware sim, June 2026) ──
# Earlier baseline (74.3% win / PF 3.23 / +131.6%) was a fictional artifact of the
# simulate_fast() cap/cash bug. These are the corrected locked numbers:
# cap=7, 12% size, $5k, trailing 3.5x ATR → +$1,568 (+31.4%), 51.5% win, PF 1.86,
# DD 10.5%, 101 trades over 2 years (~50/yr). See CONTEXT.md "Round 8".
BACKTEST = {
    "win_rate_pct":     51.5,
    "profit_factor":    1.86,
    "max_drawdown_pct": 10.5,
    "return_2yr_pct":   31.4,
    "trades_per_year":  50,
}

STARTING_CAPITAL = 5000.0


# ── Core report builder ───────────────────────────────────────────────────────

def _trades_in_range(rows: List[dict], start: date, end: date) -> List[dict]:
    result = []
    for r in rows:
        if r["status"] != "closed":
            continue
        try:
            d = date.fromisoformat(r["exit_date"][:10])
            if start <= d <= end:
                result.append(r)
        except Exception:
            continue
    return result


def _pnl(row: dict) -> float:
    try:
        return float(row["net_pnl"])
    except Exception:
        return 0.0


def _profit_factor(trades: List[dict]) -> float:
    wins_total   = sum(_pnl(t) for t in trades if _pnl(t) > 0)
    losses_total = abs(sum(_pnl(t) for t in trades if _pnl(t) < 0))
    if losses_total == 0:
        return 999.0 if wins_total > 0 else 0.0
    return round(wins_total / losses_total, 2)


def _max_drawdown(trades: List[dict]) -> float:
    """Max drawdown % across all closed trades (from STARTING_CAPITAL)."""
    equity = STARTING_CAPITAL
    peak   = STARTING_CAPITAL
    max_dd = 0.0
    for t in trades:
        equity += _pnl(t)
        peak    = max(peak, equity)
        dd      = (peak - equity) / peak * 100
        max_dd  = max(max_dd, dd)
    return round(max_dd, 2)


def _current_equity(all_closed: List[dict]) -> float:
    return round(STARTING_CAPITAL + sum(_pnl(t) for t in all_closed), 2)


def build_weekly_report(week_end: Optional[date] = None) -> str:
    """
    Build the weekly report message.
    week_end defaults to last Sunday (or today if Sunday).
    """
    if week_end is None:
        today    = date.today()
        # Roll back to last Sunday
        days_back = today.weekday() + 1   # Monday=0 … Sunday=6 → +1 gives days since Sunday
        week_end  = today - timedelta(days=days_back % 7)

    week_start = week_end - timedelta(days=6)

    all_rows    = tlog.get_all_trades(n=10000)
    all_closed  = [r for r in all_rows if r["status"] == "closed"]
    week_trades = _trades_in_range(all_rows, week_start, week_end)
    open_trades = tlog.get_open_trades()

    equity        = _current_equity(all_closed)
    total_return  = round((equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2)
    inception_dd  = _max_drawdown(all_closed)
    consec_losses = tlog.get_consecutive_losses()

    # ── Week stats ────────────────────────────────────────────────────────────
    week_wins   = [t for t in week_trades if _pnl(t) > 0]
    week_losses = [t for t in week_trades if _pnl(t) <= 0]
    week_pnl    = round(sum(_pnl(t) for t in week_trades), 2)
    week_pf     = _profit_factor(week_trades)
    week_wr     = (
        round(len(week_wins) / len(week_trades) * 100, 1)
        if week_trades else 0.0
    )

    # Exit breakdown for the week.
    # Live strategy is trailing-stop only (no fixed target). Exits are tagged
    # "trailing_stop" or "max_hold"; legacy rows may use take_profit/stop_loss.
    week_trail = len([t for t in week_trades
                      if t.get("exit_reason") in ("trailing_stop", "take_profit")])
    week_hold  = len([t for t in week_trades if t.get("exit_reason") == "max_hold"])
    week_sl    = len([t for t in week_trades if t.get("exit_reason") == "stop_loss"])
    week_oth   = len(week_trades) - week_trail - week_hold - week_sl

    # Best / worst trade this week
    best  = max(week_trades, key=_pnl) if week_trades else None
    worst = min(week_trades, key=_pnl) if week_trades else None

    # ── Inception stats ───────────────────────────────────────────────────────
    inc_trades = len(all_closed)
    inc_wr     = (
        round(len([t for t in all_closed if _pnl(t) > 0]) / inc_trades * 100, 1)
        if inc_trades else 0.0
    )
    inc_pf = _profit_factor(all_closed)

    # ── Projected daily rate ──────────────────────────────────────────────────
    # Based on: trades/year from backtest × average P&L per trade
    avg_pnl_per_trade = (
        round(sum(_pnl(t) for t in all_closed) / inc_trades, 2)
        if inc_trades else 0.0
    )
    projected_daily = round(avg_pnl_per_trade * BACKTEST["trades_per_year"] / 365, 2)

    # ── Vs backtest comparison ────────────────────────────────────────────────
    wr_diff = round(inc_wr - BACKTEST["win_rate_pct"], 1)
    pf_diff = round(inc_pf - BACKTEST["profit_factor"], 2)
    wr_vs   = f"{'+' if wr_diff >= 0 else ''}{wr_diff}% vs backtest"
    pf_vs   = f"{'+' if pf_diff >= 0 else ''}{pf_diff} vs backtest"

    # ── Format message ────────────────────────────────────────────────────────
    week_label = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d')}"
    sign       = "+" if week_pnl >= 0 else ""
    eq_sign    = "+" if total_return >= 0 else ""

    lines = [
        f"*WEEKLY REPORT — {week_label}*",
        f"",
    ]

    # ── This week ─────────────────────────────────────────────────────────────
    if not week_trades:
        lines.append("No trades closed this week.")
    else:
        lines += [
            f"*This week*",
            f"Trades:      {len(week_trades)}  ({len(week_wins)}W / {len(week_losses)}L)",
            f"Win rate:    {week_wr}%",
            f"Net P&L:     {sign}${week_pnl:,.2f}",
            f"Profit factor: {week_pf}",
            f"",
            f"Exit breakdown:",
            f"  Trailing stop: {week_trail}",
            f"  Max hold:      {week_hold}",
            (f"  Stop hit:      {week_sl}" if week_sl else ""),
            (f"  Other:         {week_oth}" if week_oth else ""),
        ]
        lines = [l for l in lines if l != ""]  # remove empty conditional lines

        if best:
            best_sign = "+" if _pnl(best) >= 0 else ""
            lines.append(
                f"Best:   {best['ticker']}  {best_sign}${_pnl(best):,.2f}"
                f"  ({best.get('exit_reason','?').replace('_',' ')}"
                f",  {best.get('bars_held','?')} days held)"
                if best.get("bars_held") else
                f"Best:   {best['ticker']}  {best_sign}${_pnl(best):,.2f}"
                f"  ({best.get('exit_reason','?').replace('_',' ')})"
            )
        if worst and worst != best:
            worst_sign = "+" if _pnl(worst) >= 0 else ""
            lines.append(
                f"Worst:  {worst['ticker']}  {worst_sign}${_pnl(worst):,.2f}"
                f"  ({worst.get('exit_reason','?').replace('_',' ')})"
            )

    # ── Portfolio ─────────────────────────────────────────────────────────────
    lines += [
        f"",
        f"*Portfolio*",
        f"Balance:     ${equity:,.2f}  ({eq_sign}{total_return}%)",
        f"Max DD:      -{inception_dd}%",
        f"Consec. losses: {consec_losses}/{config.CONSECUTIVE_LOSS_LIMIT}",
    ]
    if open_trades:
        open_str = ", ".join(t["ticker"] for t in open_trades)
        lines.append(f"Open trades: {open_str}")

    # ── Since inception ───────────────────────────────────────────────────────
    if inc_trades > 0:
        lines += [
            f"",
            f"*Since inception ({inc_trades} trades)*",
            f"Win rate:    {inc_wr}%  _({wr_vs})_",
            f"Profit factor: {inc_pf}  _({pf_vs})_",
            f"Avg P&L/trade: ${avg_pnl_per_trade:+,.2f}",
            f"Projected daily: ${projected_daily:+,.2f}/day  _(at {BACKTEST['trades_per_year']}/yr rate)_",
        ]

    # ── Status ────────────────────────────────────────────────────────────────
    mode  = "PAPER" if config.PAPER_TRADING else "LIVE"
    lines += [
        f"",
        f"_{mode} trading  |  Target: $40/day_",
    ]

    return "\n".join(lines)


def build_inception_report() -> str:
    """Full all-time performance report."""
    all_rows   = tlog.get_all_trades(n=10000)
    all_closed = [r for r in all_rows if r["status"] == "closed"]
    open_trades = tlog.get_open_trades()

    if not all_closed:
        return "No closed trades yet — nothing to report."

    equity       = _current_equity(all_closed)
    total_return = round((equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2)
    max_dd       = _max_drawdown(all_closed)
    wins         = [t for t in all_closed if _pnl(t) > 0]
    losses       = [t for t in all_closed if _pnl(t) <= 0]
    win_rate     = round(len(wins) / len(all_closed) * 100, 1)
    pf           = _profit_factor(all_closed)
    avg_win      = round(sum(_pnl(t) for t in wins) / len(wins), 2) if wins else 0
    avg_loss     = round(sum(_pnl(t) for t in losses) / len(losses), 2) if losses else 0

    trail_exits = len([t for t in all_closed
                       if t.get("exit_reason") in ("trailing_stop", "take_profit")])
    hold_exits  = len([t for t in all_closed if t.get("exit_reason") == "max_hold"])
    sl_exits    = len([t for t in all_closed if t.get("exit_reason") == "stop_loss"])

    best  = max(all_closed, key=_pnl)
    worst = min(all_closed, key=_pnl)

    first_date = all_closed[0].get("entry_date", "?")
    last_date  = all_closed[-1].get("exit_date", "?")

    wr_vs = round(win_rate - BACKTEST["win_rate_pct"], 1)
    pf_vs = round(pf - BACKTEST["profit_factor"], 2)
    dd_vs = round(max_dd - BACKTEST["max_drawdown_pct"], 2)
    sign  = "+" if total_return >= 0 else ""

    lines = [
        f"*FULL REPORT — Inception to Date*",
        f"_{first_date} → {last_date}_",
        f"",
        f"*Performance*",
        f"Trades:        {len(all_closed)}  ({len(wins)}W / {len(losses)}L)",
        f"Win rate:      {win_rate}%  _(backtest: {BACKTEST['win_rate_pct']}%,  {'+' if wr_vs>=0 else ''}{wr_vs}%)_",
        f"Profit factor: {pf}  _(backtest: {BACKTEST['profit_factor']},  {'+' if pf_vs>=0 else ''}{pf_vs})_",
        f"Max drawdown:  -{max_dd}%  _(backtest: -{BACKTEST['max_drawdown_pct']}%,  {'+' if dd_vs>=0 else ''}{dd_vs}%)_",
        f"",
        f"*P&L*",
        f"Net P&L:       ${equity - STARTING_CAPITAL:+,.2f}",
        f"Return:        {sign}{total_return}%",
        f"Avg win:       ${avg_win:+,.2f}",
        f"Avg loss:      ${avg_loss:+,.2f}",
        f"",
        f"*Exits*",
        f"Trailing stop: {trail_exits}  ({round(trail_exits/len(all_closed)*100)}%)",
        f"Max hold:      {hold_exits}  ({round(hold_exits/len(all_closed)*100)}%)",
        (f"Stop hit:      {sl_exits}  ({round(sl_exits/len(all_closed)*100)}%)"
         if sl_exits else ""),
        f"",
        f"*Best trade:*  {best['ticker']}  ${_pnl(best):+,.2f}",
        f"*Worst trade:* {worst['ticker']}  ${_pnl(worst):+,.2f}",
    ]

    if open_trades:
        lines += [
            f"",
            f"*Open ({len(open_trades)}):* " + ", ".join(t["ticker"] for t in open_trades),
        ]

    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    lines += [f"", f"_{mode} trading  |  Starting capital: ${STARTING_CAPITAL:,.0f}_"]

    return "\n".join(lines)
