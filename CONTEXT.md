# Stock Signal Bot — Project Context

This file exists so that any new AI session can immediately understand the project
without re-explaining anything. Read this fully before making any changes.

---

## What this project is

A fully automated Python swing trading bot that:
- Scans S&P 500 + NASDAQ-100 (~600 tickers) every trading day
- Places bracket orders (entry + stop loss + take profit) automatically on Alpaca paper trading
- Monitors open positions and notifies via Telegram when a stop or target is hit
- Posts weekly performance reports to a private Telegram channel

**The bot executes trades automatically. The user does not act manually.**

---

## Owner context

- **Location:** Finland (Finnish time = UTC+3 summer / UTC+2 winter, always ET+7)
- **Broker:** Alpaca (paper) → IBKR (planned for live)
- **Experience:** Node.js background, learning Python
- **Trading mode:** Fully automated — bracket orders placed via Alpaca paper API

---

## Key decisions (do not re-debate these)

| Topic | Decision | Reason |
|---|---|---|
| RSI timeframe | Daily candles only (`interval="1d"`) | Reliable, matches swing hold periods |
| Signal timing | Post-close only (11:15 PM Finnish) | New daily candles finalize at market close |
| Universe | S&P 500 only (~503 tickers) | Round 5 backtest: NASDAQ-100 adds 13 tickers and reduces P&L; full NYSE+NASDAQ produces 18 trades vs 78 — filters reject most small-caps |
| Price filter | Hard skip below $5 or above $150 | Prevents 1-share position blowups (AZO bug) |
| Position sizing | ATR-based `calculate_position_size()` targeting $40/trade, 15% hard cap | Replaces broken `max(1, int(...))` formula |
| Min shares | 3 shares minimum | Skip trade if can't buy ≥3 within position cap |
| Data library | yfinance (unofficial Yahoo Finance) | Free, reliable for daily candles |
| Real-time price | Alpaca Market Data API (free tier) | For `/signal` command |
| Indicators | `ta` library (NOT pandas-ta) | pandas-ta dropped Python 3.9 support |
| State storage | Local JSON (atomic writes via `os.replace`) | Simple, race-condition safe |
| Intraday | Permanently shelved | Backtested — swing wins on every metric |
| ATR-based exits | Stop × 3.5, Target × 6.0 | Confirmed best across 3-round backtest framework |
| MACD | Permanently dropped | Mutually exclusive with oversold RSI — 0 trades |

---

## US Market Hours in Finnish Time

```
Pre-Market:   11:00 AM – 4:30 PM  Finnish time
Market Open:   4:30 PM – 11:00 PM Finnish time  ← main session
After-Hours:  11:00 PM – 3:00 AM  Finnish time (next day)
```

Offset is always ET+7 (Finnish DST and US DST cancel out).

---

## Daily Bot Schedule (Finnish time)

```
4:00 PM  → Morning scan: score all ~600 S&P500+NASDAQ100 stocks, pick top 50
4:20 PM  → Post watchlist to Telegram channel
4:25 PM  → Execute pending trades (9:25 AM ET, 5 min before open):
             Check VIX + SPY circuit breakers
             Per-ticker: earnings check, position limits, consecutive loss limit
             qty = calculate_position_size(entry_price, atr, target_mult=6.0, profit_target=$40, cap=15%)
             Place bracket order: market buy + SL@(entry−ATR×3.5) + TP@(entry+ATR×6.0)
             Log to trades.csv, post confirmation to Telegram
4:30–11:00 PM → Monitor positions every 15 min (market hours)
11:15 PM → Auto-scan ALL ~600 tickers: RSI + volume filter
             Queue valid BUY signals to pending_trades.json
             Post "X trades queued for tomorrow" to Telegram
Sunday 8 PM → Weekly performance report
```

---

## Signal Logic

### Morning screener (4:00 PM)
Scores each stock on 3 dimensions:
```
Volume score    (35%) = today's volume / 20-day avg volume, capped at 5× → 1.0
RSI score       (40%) = |RSI - 50| / 20, capped at 1.0
Momentum score  (25%) = |5-day % price change| / 10%, capped at 1.0
+ 0.10 bonus if price in $5–$50 range (preferred cheap stocks)
```
Stocks below $5 or above $150 are hard-filtered out before scoring.
Stocks with average volume < 200K/day are hard-filtered out.

### Auto-scan signal engine (11:15 PM)
```
BUY signal fires when ALL of:
  RSI(14) < 38                    — oversold on daily candles
  Volume > 1.2× 20-day avg        — confirmed interest
  Price in $5–$150                — within tradeable range
  SPY above 50MA                  — bull market condition
  VIX < 25                        — not a fear/volatility spike
  No earnings within 3 days       — avoid event risk
  20MA > 50MA (or golden cross)   — uptrend confirmed
```

---

## Strategy Parameters (locked — confirmed by 4-round backtesting)

```python
RSI_BUY_THRESHOLD         = 38
RSI_PERIOD                = 14
VOLUME_CONFIRMATION_RATIO = 1.2
ATR_PERIOD                = 14
ATR_STOP_MULTIPLIER       = 3.5
ATR_TARGET_MULTIPLIER     = 6.0

MAX_POSITION_PCT          = 0.12   # used in backtester baseline only
MAX_POSITION_PCT_HARD_CAP = 0.15   # live hard cap (15% of equity max per trade)
MAX_OPEN_POSITIONS        = 5
CONSECUTIVE_LOSS_LIMIT    = 3
DAILY_PROFIT_TARGET       = 40.0   # target $ per winning trade for sizing

PRICE_MIN                 = 5.0
PRICE_MAX_HARD            = 150.0
PRICE_MAX_PREFERRED       = 50.0   # scoring nudge only
MIN_AVG_VOLUME            = 200_000
MIN_SHARES_REQUIRED       = 3

USE_SPY_TREND_FILTER      = True
USE_VIX_FILTER            = True
VIX_MAX                   = 25
USE_EARNINGS_FILTER       = True
EARNINGS_BUFFER_DAYS      = 3
USE_MACD_CONFIRMATION     = False   # permanently off
EXTENDED_UNIVERSE_ENABLED = True    # S&P 500 + NASDAQ-100
```

---

## Backtest Results (do not change parameters without re-running)

**Universe:** S&P 500 (503 tickers) | **Window:** 2 years (May 2024–May 2026) | **Capital:** $5,000

| Round | What was tested | Result |
|---|---|---|
| Round 1 | Position size grid (900 combos) | 12% best PF (3.23) + most profit |
| Round 2 | SPY MA + VIX filters | No effect in bull window — kept as live safety nets |
| Round 3 | BB bands, 200MA slope, max hold caps | None beat baseline — baseline locked |
| Round 4 | ATR-target sizing, price cap $5–$150 | Baseline still best total profit; price cap fixes AZO bug |
| Round 5 | Universe size: S&P 500 vs +NASDAQ-100 vs full NYSE+NASDAQ (5,177 tickers) | S&P 500 wins decisively. Full market: only 18 trades, P&L ▼$2,144 vs baseline. NASDAQ-100 adds nothing. S&P 500-only confirmed. |

**Confirmed best (baseline):**
- Return: +131.6% | Win rate: 74.3% | Profit factor: 3.23 | Max drawdown: -3.4% | 136 trades

**Round 4 key finding:** ATR-target $40/trade sizing reduces per-trade variance (avg win $37, avg loss $27, DD -1.3%) but also reduces 2-year total P&L by ~70%. Price cap alone ($5–$150) is the correct fix — adopted for live. ATR-target sizing not adopted.

**Round 5 key finding (three-way universe comparison):**

| Universe | Tickers | Trades | Win% | P&L | PF | Verdict |
|---|---|---|---|---|---|---|
| S&P 500 only | 502 | 78 | 71.8% | +$2,713 | 2.83 | ✅ BASELINE |
| S&P 500 + NASDAQ-100 | 515 | 79 | 69.6% | +$2,475 | 2.47 | ❌ SKIP |
| Full NYSE + NASDAQ | 5,177 | 18 | 61.1% | +$569 | 3.31 | ❌ SKIP |

The 5× wider universe produces only 18 trades (vs 78) — the RSI+MA+volume+price filters aggressively reject most small-cap stocks, starving the engine of signals. S&P 500-only universe confirmed as best. `EXTENDED_UNIVERSE_ENABLED` set to False in the key decisions table — the NASDAQ-100 expansion adds 13 new tickers and reduces P&L.

---

## Project Structure

```
stock-signal-bot/
├── main.py                  — Entry point, 5 scheduled jobs
├── scanner.py               — Morning scan + run_auto_scan() ~600 tickers
│                              + get_nasdaq100_tickers() + get_extended_tickers()
├── signals.py               — RSI + MA analysis + calculate_position_size()
├── telegram_bot.py          — All commands + channel posting + rate limiting
├── charts.py                — Dark-mode price/RSI chart PNG (with column validation)
├── watchlist.py             — Daily watchlist (atomic JSON writes)
├── custom_watchlist.py      — Persistent custom watchlist per user
├── ibkr.py                  — IB Gateway connection + is_connected() health check
├── config.py                — All strategy constants — edit here only
├── trader.py                — Alpaca bracket orders + circuit breakers
├── trade_logger.py          — CSV trade log + atomic pending queue + pause flag
├── reporter.py              — Weekly and inception-to-date reports
├── backtester.py            — Swing backtester (4-round framework complete)
├── intraday_backtester.py   — Intraday backtester (concluded — swing wins)
├── .env                     — All API keys (never commit)
├── requirements.txt         — Python dependencies
│
├── trades.csv               — All trade records — DO NOT DELETE
├── pending_trades.json      — Trades queued for next morning (atomic writes)
├── trading_paused.flag      — Exists when auto-trading is paused
├── watchlist.json           — Daily top-50 watchlist (atomic writes)
├── custom_watchlist.json    — Custom watchlist
│
├── backtest_cache.pkl       — Daily price data (2yr+300d) — DO NOT DELETE
├── backtest_indicators.pkl  — Precomputed swing indicators
├── backtest_exits.pkl       — Precomputed exit outcomes
├── backtest_spy.pkl         — SPY MA data
├── backtest_vix.pkl         — VIX data
│
├── CONTEXT.md               — This file
├── automatedtradingplan.md  — Full strategy + backtest results + roadmap
├── STOCK_BOT_IMPLEMENTATION_PLAN.md — Round 4 implementation plan (executed)
└── README.md                — Plain-language project description
```

---

## File responsibilities (quick reference)

| File | Key functions |
|---|---|
| `config.py` | All constants — edit thresholds here, nowhere else |
| `scanner.py` | `get_extended_tickers()`, `get_sp500_tickers()`, `get_nasdaq100_tickers()`, `fetch_data()`, `run_morning_scan()`, `run_auto_scan()` |
| `signals.py` | `analyse()`, `calculate_position_size()`, `fetch_ticker_data()`, `fetch_realtime_price()` |
| `watchlist.py` | `save_watchlist()`, `get_watchlist()`, `mark_signal_fired()` — atomic writes |
| `trade_logger.py` | `queue_pending_trade()`, `load_pending_trades()`, `log_order_placed()`, `_atomic_json_write()` |
| `trader.py` | `place_bracket_order()`, `run_circuit_breakers()`, `check_earnings()`, `check_vix()` |
| `telegram_bot.py` | All command handlers + `build_application()` + `_is_rate_limited()` |
| `charts.py` | `generate_chart(ticker, df)` — validates columns before rendering |
| `main.py` | `job_morning_scan()`, `job_execute_trades()`, `job_signal_check()`, `job_monitor_positions()` |
| `ibkr.py` | `is_connected()`, `get_price()`, `get_portfolio()` |

---

## Telegram Commands

| Command | What it does |
|---|---|
| `/watchlist` | Today's top scored stocks (rate-limited) |
| `/signal NVDA` | RSI + MA status + real-time price (rate-limited) |
| `/chart NVDA` | Price chart with 20MA, 50MA, RSI (rate-limited) |
| `/positions` | Open Alpaca positions with unrealised P&L |
| `/trades` | Full trade history + win rate + P&L |
| `/report` | This week's performance report |
| `/report all` | Full inception-to-date report vs backtest |
| `/pause` | Pause auto-trading |
| `/resume` | Resume auto-trading |
| `/stopall confirm` | Emergency: cancel all orders + liquidate all positions |
| `/status` | Bot health, schedule, trading stats |
| `/health` | Check Alpaca / yfinance / IBKR connectivity live |
| `/mywatchlist` | Manage a custom watchlist |
| `/scanmywatchlist` | Scan your custom watchlist for signals |
| `/portfolio` | IBKR positions (when connected) |
| `/testrun` | Manually trigger a scheduled job (testing) |

---

## Tech Stack

| Component | Library | Version |
|---|---|---|
| Language | Python | 3.9 (machine constraint — do NOT use 3.14+) |
| Market data (daily) | yfinance | 1.2.0+ |
| Real-time price | alpaca-py | 0.43.0+ |
| Data processing | pandas | 2.2.2 |
| Indicators | ta | 0.11.0 |
| Telegram | python-telegram-bot | 20.7 |
| Scheduling | APScheduler | 3.10.4 |
| Charts | matplotlib | 3.8.4 |
| Config | python-dotenv | 1.0.1 |

---

## Setup Instructions (first time)

```bash
cd ~/Desktop/stock-signal-bot
/Library/Developer/CommandLineTools/usr/bin/python3.9 -m pip install -r requirements.txt
/Library/Developer/CommandLineTools/usr/bin/python3.9 main.py
```

> IMPORTANT: Always use Python 3.9 explicitly. The system `python3` points to 3.14
> which breaks python-telegram-bot 20.7.

---

## Current Status

- [x] Fully automated trading pipeline: scan → queue → bracket order → monitor
- [x] 5 circuit breakers: VIX, SPY trend, consecutive loss pause, position limits, earnings filter
- [x] ATR-based stop/target sizing (Stop: Entry−ATR×3.5, Target: Entry+ATR×6.0)
- [x] Position sizing via `calculate_position_size()` — AZO bug fixed, min 3 shares enforced
- [x] Price cap $5–$150 in scanner + auto-scan (hard filter)
- [x] NASDAQ-100 expansion — ~600 tickers in universe
- [x] 4-round backtesting framework complete — all parameters locked
- [x] CSV trade logging, pending queue with atomic JSON writes
- [x] Telegram bot with full command set + 5s rate limiting on /signal /chart /watchlist
- [x] /health command — live connectivity check for Alpaca, yfinance, IBKR
- [x] charts.py — column validation before rendering, temp files always cleaned up
- [x] Pushed to GitHub: https://github.com/ym78900/stock-signal-bot
- [ ] End-to-end paper trade test (full cycle: signal → order → fill → exit → report)
- [ ] 1–2 weeks paper trading to validate order fills and Telegram notifications
- [ ] VPS deployment for 24/7 operation (Hetzner ~€4/mo recommended)
- [ ] IBKR live trading (after 6+ weeks positive paper results)

---

## Known Bugs Fixed

| Bug | Fix | File |
|---|---|---|
| `max(1, int(...))` forces 1 share (AZO at $3k = 60% of portfolio) | `calculate_position_size()` with min 3 shares + `if qty == 0: skip` | `main.py`, `signals.py` |
| Hard price cap missing | Skip stocks below $5 or above $150 in scanner + auto-scan | `scanner.py`, `config.py` |
| charts.py silently fails on malformed yfinance response | Column validation before rendering | `charts.py` |
| Temp chart PNGs accumulate in /tmp | `try/finally` ensures deletion even on send failure | `telegram_bot.py` |
| JSON race condition (concurrent scheduler + Telegram commands) | Atomic writes via temp file + `os.replace()` | `watchlist.py`, `trade_logger.py` |
| No Telegram rate limiting — vulnerable to command spam | 5-second per-user cooldown on /signal /chart /watchlist | `telegram_bot.py` |
| `pandas-ta` not available on Python 3.9 | Switched to `ta` library | `scanner.py`, `signals.py`, `charts.py` |
| yfinance returns MultiIndex DataFrame | Added `.get_level_values(0)` flatten | `scanner.py`, `signals.py` |
| Wikipedia returns 403 Forbidden | Added `User-Agent` header via `requests` | `scanner.py` |

---

## Known Gotchas

- **Python version:** Always `/Library/Developer/CommandLineTools/usr/bin/python3.9` — system `python3` = 3.14 which breaks PTB 20.7
- **yfinance bulk download:** Use `yfinance.download(tickers=[...], group_by="ticker")` — never one by one
- **BRK.B ticker:** Wikipedia uses dot notation, yfinance needs dash — `.replace(".", "-")` in `scanner.py`
- **APScheduler + asyncio:** Uses `AsyncIOScheduler` — required because PTB v20+ is fully async
- **IBKR thread isolation:** `ib_insync` runs in a separate thread with its own event loop — do not share with asyncio loop
- **Telegram callback_data:** 64-byte limit — never encode long strings in it
- **Look-ahead bias in backtester:** Exit cache scans full future hold period — acknowledged trade-off for speed. Live performance may be 5–15% below backtest numbers.
- **Cache files (.pkl):** Delete to force re-download. `backtest_cache.pkl` and `backtest_indicators.pkl` are the slow ones (2yr data for 500+ tickers). Do not delete unless necessary.
