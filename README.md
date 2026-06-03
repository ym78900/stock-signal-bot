# Stock Signal Bot

A fully automated swing trading bot that scans all S&P 500 stocks every day, generates BUY signals, and places orders automatically on Alpaca paper trading with a native trailing stop exit.

---

## What it does

Every trading day it:

1. **Scans all 503 S&P 500 stocks** before market open and picks the top 50 most interesting for the day
2. **Posts a morning watchlist** to a private Telegram channel
3. **After market close**, scans all 503 stocks for BUY signals using RSI + volume confirmation
4. **Queues valid signals** and executes orders at 9:25 AM ET the next morning (two-phase: market buy → fill detected → trailing stop placed)
5. **Monitors open positions** every 15 minutes — notifies when trailing stop fires or max hold is reached
6. **Posts a weekly performance report** every Sunday with full stats vs backtest baseline

---

## Signal logic

```
BUY signal fires when:
  RSI(14) < 38          — stock is oversold on daily candles
  Volume > 1.2× avg     — confirmed interest, not a quiet drift
  20MA > 50MA           — uptrend confirmed (or golden cross)
  SPY above 50MA        — only trade in bull market conditions
  VIX < 25              — skip if fear/volatility is spiking
  No earnings ±3 days   — avoid earnings volatility
  Price $5–$200         — within tradeable range

Entry:        Market order at 9:30 AM ET open
Trailing stop: ATR × 3.5 below running peak (Alpaca manages server-side)
Position:     12% of portfolio per trade (min 3 shares)
Max open:     7 positions simultaneously
Max hold:     60 calendar days (force-close safety net)
```

Parameters confirmed across an 8-round backtesting framework on 2 years of S&P 500 data.
The corrected backtester (Round 8: enforces the position cap **and** a cash constraint —
earlier rounds did neither, which inflated the numbers ~10×):

| Metric | Value |
|---|---|
| 2-year return | +$1,568 on $5,000 (+31.4%) |
| Win rate | 51.5% |
| Profit factor | 1.86 |
| Max drawdown | 10.5% |
| Trades | 101 |
| Per day | ~$3.11 (~13%/yr) |

> Returns scale linearly with capital (~$3/day per $5k). The ~€40/day goal needs
> ~$60–70k of capital, not $5k — the strategy is profitable but earlier "$48/day"
> figures were a backtester artifact.

---

## Order execution (two-phase, fill-based)

```
9:25 AM ET  → Market buy placed (no stop/target yet)
9:30 AM ET  → Alpaca fills the order at open
9:32 AM ET  → Bot detects real fill price
             → trail_price = ATR × 3.5
             → Trailing stop sell placed with Alpaca
             → Alpaca raises stop automatically as price rises
             → Position has no fixed take-profit cap — trail handles the exit
60 days     → If still open, monitor cancels trail + market sells
```

**Why two-phase:** prior close ≠ actual fill price (overnight gaps). Using the real fill price ensures the ATR×3.5 trail distance is measured from where you actually bought.

---

## Daily schedule (Finnish time)

| Time | Job |
|---|---|
| 4:00 PM | Morning scan — score all 503 stocks |
| 4:20 PM | Post watchlist to Telegram |
| 4:25 PM | Execute queued trades — Phase 1: market buys placed |
| ~4:32 PM | Phase 2: poll fills → place trailing stops |
| 4:30–11:00 PM | Monitor positions every 15 min |
| 11:15 PM | Auto-scan all 503 tickers for BUY signals, queue for tomorrow |
| Sunday 8 PM | Weekly performance report posted to Telegram |

---

## Telegram commands

| Command | What it does |
|---|---|
| `/watchlist` | Today's top scored stocks |
| `/signal NVDA` | RSI + MA status + real-time price |
| `/chart NVDA` | Price chart with 20MA, 50MA, RSI |
| `/positions` | Open Alpaca positions with unrealised P&L |
| `/trades` | Trade history + win rate + P&L |
| `/report` | This week's performance report |
| `/report all` | Full inception-to-date report vs backtest |
| `/pause` | Pause auto-trading |
| `/resume` | Resume auto-trading |
| `/stopall confirm` | Emergency: cancel all orders + liquidate all positions |
| `/status` | Bot health, schedule, trading stats |
| `/health` | Live connectivity check: Alpaca / yfinance / IBKR |
| `/mywatchlist` | Manage a custom watchlist |
| `/scanmywatchlist` | Scan your custom watchlist for signals |
| `/portfolio` | IBKR positions (when connected) |

---

## Project structure

```
stock-signal-bot/
├── main.py              — Entry point, all 5 scheduled jobs (2-phase execution)
├── scanner.py           — Morning scan + run_auto_scan() for all 503 tickers
├── signals.py           — RSI + MA analysis, calculate_position_size(), price fetch
├── telegram_bot.py      — All Telegram commands, channel posting, rate limiting
├── charts.py            — Dark-mode price/RSI chart PNG
├── watchlist.py         — Daily auto-generated watchlist (atomic JSON writes)
├── custom_watchlist.py  — Persistent custom watchlist per user
├── ibkr.py              — IB Gateway connection + is_connected() health check
├── config.py            — All constants and strategy parameters — edit here only
├── trader.py            — Market buy + trailing stop exit + circuit breakers
├── trade_logger.py      — CSV trade log + pending queue + pause flag (atomic writes)
├── reporter.py          — Weekly and inception-to-date performance reports
├── backtester.py        — Swing strategy backtester (8-round framework complete)
├── intraday_backtester.py — Intraday backtester (tested and concluded — swing wins)
├── .env                 — API keys (never commit)
├── requirements.txt     — Python dependencies
├── trades.csv           — All trade records (auto-created)
├── pending_trades.json  — Trades queued for next morning (auto-created)
└── watchlist.json       — Daily watchlist (auto-created)
```

---

## Running the bot

```bash
cd ~/Desktop/stock-signal-bot
/Library/Developer/CommandLineTools/usr/bin/python3.9 main.py
```

> Always use Python 3.9. The system `python3` points to 3.14 which breaks python-telegram-bot 20.7.

Currently running in **paper trading mode** (`PAPER_TRADING = True` in `config.py`).
Switch to live by setting `PAPER_TRADING = False` after 6+ weeks of positive paper results.

---

## Tech stack

| Component | Library |
|---|---|
| Language | Python 3.9 |
| Market data | yfinance (daily), Alpaca Market Data (real-time) |
| Indicators | ta library |
| Order execution | alpaca-py (paper → IBKR live) |
| Telegram | python-telegram-bot 20.7 |
| Scheduling | APScheduler |
| Charts | matplotlib |
