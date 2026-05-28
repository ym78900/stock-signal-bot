# Stock Signal Bot — Project Context

This file exists so that any new AI session can immediately understand the project
without re-explaining anything. Read this fully before making any changes.

---

## What this project is

A Python Telegram bot that runs on a personal computer, scans the S&P 500 every day,
and posts stock BUY/SELL signals to a Telegram channel. The user acts manually via
their IBKR account — the bot does NOT execute trades automatically.

---

## Owner context

- **Location:** Finland (Finnish time = UTC+3 summer / UTC+2 winter, always ET+7)
- **Broker:** IBKR (Interactive Brokers) — not connected yet, planned for future
- **Experience:** Node.js background, learning Python
- **Trading mode:** Signals only, user acts manually

---

## Key decisions already made (do not re-debate these)

| Topic | Decision | Reason |
|---|---|---|
| RSI timeframe | Daily candles only (`interval="1d"`) | Reliable, no noise, matches manual trading pace |
| MA crossover | 20-day MA vs 50-day MA | Classic swing trading indicator |
| Intraday scanning | None | Daily RSI doesn't change intraday — no value |
| Signal timing | Post-close only (11:15 PM Finnish) | New daily candles finalize at market close |
| S&P 500 source | Wikipedia scrape with User-Agent header | Free, no API key, auto-updates |
| yfinance fetch | `period="60d", interval="1d"` | 60 days covers 50-day MA comfortably |
| Data library | yfinance (unofficial Yahoo Finance) | Free, reliable for daily candles |
| Indicators | `ta` library (NOT pandas-ta) | pandas-ta dropped Python 3.9 support |
| State storage | Local JSON file (`watchlist.json`) | Simple, no database needed |
| Scheduling | 2 APScheduler jobs only | Morning scan + end-of-day signal check |

---

## US Market Hours in Finnish Time

```
Pre-Market:   11:00 AM – 4:30 PM  Finnish time
Market Open:   4:30 PM – 11:00 PM Finnish time  ← main session
After-Hours:  11:00 PM – 3:00 AM  Finnish time (next day)
```

Offset is always ET+7 (both summer and winter — Finnish DST and US DST cancel out).

---

## Daily Bot Schedule (Finnish time)

```
4:00 PM  → Fetch daily data for all 500 S&P stocks (prior-day close data)
           → Score + rank all 500 → pick top 10
4:20 PM  → Post "Today's Watchlist" to Telegram channel
           (bot sleeps during market hours — daily RSI doesn't change intraday)
11:00 PM → Market closes — new daily candles finalize
11:15 PM → Fetch updated daily data for top 10 only
           → Run RSI + MA crossover signal check
           → Post any BUY/SELL signals to channel
           → Post daily summary to channel
```

---

## Signal Logic

### Layer 1 — Morning Screener (4:00 PM)
Scores every S&P 500 stock on 3 dimensions:

```
Volume score    (35%) = today's volume / 20-day avg volume, capped at 5x → 1.0
RSI score       (40%) = |RSI - 50| / 20, capped at 1.0  (distance from neutral)
Momentum score  (25%) = |5-day % price change| / 10%, capped at 1.0
```

Top 10 by composite score → saved to `watchlist.json`.

### Layer 2 — Signal Engine (11:15 PM)
For each of the 10 watchlist stocks:

```
BUY  signal: RSI < 30  AND  (20MA > 50MA  OR  golden cross today)
SELL signal: RSI > 70  AND  (20MA < 50MA  OR  death cross today)
NONE:        RSI or MA not confirming — no signal fired
```

Both RSI and MA must agree before a signal fires (reduces false alerts).

### Layer 3 — Quality Filter
- Dedup: has this ticker already fired a signal today? → skip if yes
- (No open/close volatility filter — we're on daily candles, not intraday)

---

## Project Structure

```
stock-signal-bot/
├── main.py           — Entry point: loads .env, starts scheduler + Telegram bot
├── scanner.py        — Fetches S&P 500 list + bulk-downloads + scores all 500 stocks
├── signals.py        — RSI + MA crossover analysis for individual stocks
├── telegram_bot.py   — Bot commands + channel posting + message formatters
├── charts.py         — Generates dark-mode price/RSI chart PNG via matplotlib
├── watchlist.py      — Saves/loads daily top 10 + tracks fired signals (JSON)
├── config.py         — All thresholds, weights, schedule times, settings
├── .env              — Telegram token + channel ID (never commit this)
├── requirements.txt  — All Python dependencies
├── watchlist.json    — Auto-generated at runtime, ignored by git
├── CONTEXT.md        — This file (AI session context)
└── README.md         — Plain-language description (shareable with friends)
```

---

## File responsibilities (quick reference)

| File | Key functions |
|---|---|
| `config.py` | All constants — edit thresholds here, nowhere else |
| `scanner.py` | `get_sp500_tickers()`, `fetch_data()`, `run_morning_scan()` |
| `signals.py` | `analyse(ticker, df)`, `run_signal_check(watchlist)`, `fetch_ticker_data(ticker)` |
| `watchlist.py` | `save_watchlist()`, `get_watchlist()`, `mark_signal_fired()`, `has_signal_fired()` |
| `telegram_bot.py` | `build_application()`, `post_watchlist()`, `post_signal()`, `post_summary()` |
| `charts.py` | `generate_chart(ticker, df)` → returns `Path` to temp PNG |
| `main.py` | `job_morning_scan()`, `job_signal_check()`, `main()` |

---

## Telegram Bot Commands

| Command | What it does |
|---|---|
| `/watchlist` | Shows today's top 10 stocks |
| `/signal NVDA` | Shows current RSI + MA status for any ticker |
| `/chart NVDA` | Sends a price chart image with 20MA, 50MA, RSI |
| `/status` | Shows if bot is running and next scheduled job times |

---

## Tech Stack

| Component | Library | Version |
|---|---|---|
| Language | Python | 3.9 (machine constraint) |
| Market data | yfinance | 1.2.0+ |
| Data processing | pandas | 2.2.2 |
| Indicators | ta | 0.11.0 |
| Telegram | python-telegram-bot | 20.7 |
| Scheduling | APScheduler | 3.10.4 |
| Charts | matplotlib | 3.8.4 |
| Config | python-dotenv | 1.0.1 |

---

## Setup Instructions (first time)

```bash
# 1. Install dependencies
cd ~/Desktop/stock-signal-bot
pip3 install -r requirements.txt

# 2. .env is already filled in (token + channel ID set)

# 3. Run the bot
python3 main.py
```

The bot will log its schedule and wait for the next scheduled job time.

---

## Current Status

- [x] All files written and complete
- [x] Dependencies installed
- [x] `.env` filled in with real Telegram token and channel ID
- [x] Bot connects to Telegram successfully
- [x] Signal logic tested — AAPL returned correct RSI/MA values
- [x] Wikipedia 403 fix applied (User-Agent header in requests)
- [x] Python 3.9 type hint fixes applied (`Optional` instead of `X | None`)
- [x] yfinance MultiIndex column fix applied (newer yfinance returns MultiIndex)
- [ ] Full end-to-end test completed (watchlist posted to channel)
- [ ] Signals validated over 1 week of observation
- [ ] Schedule reset to production times (currently set to test times)
- [ ] (Future) IBKR TWS API connected for real-time data
- [ ] (Future) Auto-execution via IBKR with Telegram approval button

---

## Known Bugs Fixed

| Bug | Fix | File |
|---|---|---|
| `pandas-ta` not available on Python 3.9 | Switched to `ta` library | `scanner.py`, `signals.py`, `charts.py` |
| `X \| None` type hints fail on Python 3.9 | Replaced with `Optional[X]` from `typing` | All files |
| yfinance returns MultiIndex DataFrame | Added `.get_level_values(0)` flatten | `scanner.py`, `signals.py` |
| Wikipedia returns 403 Forbidden | Added `User-Agent` header via `requests` | `scanner.py` |

---

## What is NOT built yet (future phases)

- No automatic trade execution — user acts manually via IBKR
- No IBKR connection — data comes from yfinance only
- No earnings calendar awareness — bot may fire signals on earnings days (noisy)
- No news sentiment — signals are purely technical
- No paper trade tracker — P&L tracking not yet implemented
- No ML/AI predictions — keeping signals simple and explainable

---

## Known Gotchas

- **yfinance bulk download:** Use `yfinance.download(tickers=[...], group_by="ticker")` for 500 stocks — never download one by one (rate limit risk)
- **BRK.B ticker:** Wikipedia lists it as `BRK.B`, yfinance needs `BRK-B` — handled in `scanner.py` with `.replace(".", "-")`
- **APScheduler + asyncio:** Uses `AsyncIOScheduler`, not `BackgroundScheduler` — required because `python-telegram-bot` v20+ is fully async
- **Chart temp files:** `charts.py` saves PNGs to the system temp dir — caller is responsible for deleting after sending
- **watchlist.json date check:** `watchlist.py` always checks if saved date matches today — returns empty list if stale (previous day's data)
- **Test mode:** `config.py` schedule times may be set to test values — reset to production times before real use:
  - `MORNING_SCAN_HOUR = 16, MORNING_SCAN_MINUTE = 0`
  - `WATCHLIST_POST_HOUR = 16, WATCHLIST_POST_MINUTE = 20`
  - `SIGNAL_CHECK_HOUR = 23, SIGNAL_CHECK_MINUTE = 15`
  - `main.py` sleep: `await asyncio.sleep(20 * 60)` (not 1 min)
