# Stock Signal Bot

A fully automated swing trading bot that scans the S&P 500 every day, generates BUY signals, and places bracket orders (entry + stop loss + take profit) automatically on Alpaca paper trading.

---

## What it does

Every trading day it:

1. **Scans all 503 S&P 500 stocks** before market open and picks the top 50 most interesting for the day
2. **Posts a morning watchlist** to a private Telegram channel
3. **After market close**, scans all 503 stocks for BUY signals using RSI + volume confirmation
4. **Queues valid signals** and executes bracket orders at 9:25 AM ET the next morning
5. **Monitors open positions** every 15 minutes — notifies when stop or target is hit
6. **Posts a weekly performance report** every Sunday with full stats vs backtest baseline

---

## Signal logic

```
BUY signal fires when:
  RSI(14) < 38          — stock is oversold on daily candles
  Volume > 1.2× avg     — confirmed interest, not a quiet drift
  SPY above 50MA        — only trade in bull market conditions
  VIX < 25              — skip if fear/volatility is spiking
  No earnings ±3 days   — avoid earnings volatility

Entry:       Market order at next-day open (9:30 AM ET)
Stop loss:   Entry − (ATR × 3.5)
Take profit: Entry + (ATR × 6.0)
Position:    12% of portfolio per trade
Max open:    5 positions simultaneously
```

These parameters were confirmed across a 3-round backtesting framework on 2 years of S&P 500 data:
- **+131.6% return** over 2 years on $5,000 starting capital
- **74.3% win rate**
- **3.23 profit factor**
- **-3.4% max drawdown**

---

## Daily schedule (Finnish time)

| Time | Job |
|---|---|
| 4:00 PM | Morning scan — score all 503 stocks |
| 4:20 PM | Post watchlist to Telegram |
| 4:25 PM | Execute queued trades — bracket orders placed 5 min before open |
| 4:30–11:00 PM | Monitor open positions every 15 min |
| 11:15 PM | Auto-scan all 503 tickers for BUY signals, queue for tomorrow |
| Sunday 8 PM | Weekly performance report posted to Telegram |

---

## Telegram commands

| Command | What it does |
|---|---|
| `/watchlist` | Today's top scored stocks |
| `/signal` | RSI + MA status for any ticker |
| `/chart` | Price chart with indicators |
| `/positions` | Open Alpaca positions with unrealised P&L |
| `/trades` | Trade history + win rate + P&L |
| `/report` | This week's performance report |
| `/report all` | Full inception-to-date report vs backtest |
| `/pause` | Pause auto-trading |
| `/resume` | Resume auto-trading |
| `/stopall confirm` | Emergency: cancel all orders + liquidate all positions |
| `/status` | Bot health, schedule, trading stats |
| `/mywatchlist` | Manage a custom watchlist |
| `/scanmywatchlist` | Scan your custom watchlist for signals |
| `/portfolio` | IBKR positions (when connected) |

---

## Project structure

```
stock-signal-bot/
├── main.py              — Entry point, all 5 scheduled jobs
├── scanner.py           — Morning scan + run_auto_scan() for all 503 tickers
├── signals.py           — RSI + MA analysis, price fetching
├── telegram_bot.py      — All Telegram commands and channel posting
├── charts.py            — Dark-mode price chart PNG
├── watchlist.py         — Daily auto-generated watchlist (JSON)
├── custom_watchlist.py  — Persistent custom watchlist per user
├── ibkr.py              — IB Gateway connection (future live trading)
├── config.py            — All constants and strategy parameters
├── trader.py            — Alpaca bracket order execution + circuit breakers
├── trade_logger.py      — CSV trade log + pending queue + pause flag
├── reporter.py          — Weekly and inception-to-date performance reports
├── backtester.py        — Swing strategy backtester (3-round framework complete)
├── intraday_backtester.py — Intraday backtester (15-min, tested and concluded)
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
Switch to live by setting `PAPER_TRADING = False` after validating paper performance.

---

## Tech stack

| Component | Library |
|---|---|
| Language | Python 3.9 |
| Market data | yfinance (daily), Alpaca IEX (intraday/real-time) |
| Indicators | ta library |
| Order execution | alpaca-py (paper → live) |
| Telegram | python-telegram-bot 20.7 |
| Scheduling | APScheduler |
| Charts | matplotlib |
