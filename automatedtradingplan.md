# Automated Trading Plan

> This document is written for the next AI session to fully understand the project state,
> trading strategy, implementation roadmap, and all decisions already made.
> Read this fully before making any changes.

---

## Project Overview

A Python Telegram bot that:
- Scans all 503 S&P 500 stocks every day for swing trading BUY signals
- Places bracket orders automatically on Alpaca (paper first, IBKR live later)
- Monitors positions and notifies via Telegram when stop/target is hit
- Posts weekly performance reports with full stats vs backtest baseline

**Owner:** Finland-based, trades US stocks. IBKR account (not subject to PDT rules).  
**Capital:** €5,000 (~$5,400). Target: ~€40/day via compounding over ~22 months.  
**Bot location:** Runs locally on Mac (needs VPS for production 24/7 operation).  
**Telegram:** Primary interface for all commands, alerts, and reports.

---

## Instructions for AI sessions

- **Always use** `/Library/Developer/CommandLineTools/usr/bin/python3.9` — system `python3` points to 3.14 which breaks `python-telegram-bot==20.7`
- **Never use** `ConversationHandler` — replaced with `context.user_data` state tracking
- **Read both `automatedtradingplan.md` and `CONTEXT.md` fully** before making any changes
- All thresholds and settings live in `config.py` only — never hardcode elsewhere
- Telegram callback_data has a **64 byte limit** — never encode long strings in it
- IBKR and Alpaca connections must run in **separate threads** with isolated event loops
- **Do not re-debate decisions in the "Key Decisions" table** — they are final

---

## Current State (June 2026)

### What is built and working

| File | Status | Description |
|---|---|---|
| `main.py` | ✅ Complete | 5 scheduled jobs: morning scan, execute trades, signal check, monitor positions, weekly report |
| `scanner.py` | ✅ Complete | Morning scan (top 50) + `run_auto_scan()` (all 503 tickers at 11:15 PM) |
| `signals.py` | ✅ Complete | RSI + MA analysis, Alpaca price fetch, asset cache |
| `telegram_bot.py` | ✅ Complete | All commands including /positions /trades /pause /resume /stopall /report |
| `trader.py` | ✅ Complete | Alpaca bracket orders, circuit breakers (VIX/SPY/earnings), emergency stop |
| `trade_logger.py` | ✅ Complete | CSV trade log, pending queue, pause flag, consecutive loss tracking |
| `reporter.py` | ✅ Complete | Weekly + inception-to-date reports with vs-backtest comparison |
| `backtester.py` | ✅ Complete | Swing backtest engine — 3-round framework done |
| `intraday_backtester.py` | ✅ Complete | Intraday backtest — tested and concluded (swing wins) |
| `charts.py` | ✅ Complete | Dark-mode price/RSI chart PNG |
| `watchlist.py` | ✅ Complete | Daily auto-generated watchlist |
| `custom_watchlist.py` | ✅ Complete | Persistent per-user custom watchlist |
| `ibkr.py` | ✅ Complete | IB Gateway connection for future live trading |
| `config.py` | ✅ Complete | All confirmed strategy parameters, no duplicates |

### What needs to be done before the bot can run

1. **Local test** — run `main.py`, confirm all jobs start, all Telegram commands respond
2. **Manual job trigger test** — override schedule times to fire immediately, verify:
   - Auto-scan finds signals → `pending_trades.json` written
   - Execute job places bracket order on Alpaca paper
   - Alpaca paper shows the order correctly
   - Monitor job detects close → logs to `trades.csv` → notifies Telegram
   - `/report` shows correct numbers
3. **VPS deployment** — move to a server (Hetzner ~€4/mo recommended) for 24/7 operation

---

## Strategy Parameters (final — do not change without backtesting)

```python
# Signal
RSI_BUY_THRESHOLD         = 38      # RSI below this → BUY signal
RSI_PERIOD                = 14
VOLUME_CONFIRMATION_RATIO = 1.2     # Volume > 1.2× 20-day avg required
ATR_PERIOD                = 14
ATR_STOP_MULTIPLIER       = 3.5     # Stop loss   = entry − (ATR × 3.5)
ATR_TARGET_MULTIPLIER     = 6.0     # Take profit = entry + (ATR × 6.0)

# Portfolio
MAX_POSITION_PCT          = 0.12    # 12% of equity per trade
MAX_OPEN_POSITIONS        = 5       # Circuit breaker
CONSECUTIVE_LOSS_LIMIT    = 3       # Pause after N consecutive losses

# Filters (live-only — no effect in backtest bull window, essential for bear markets)
USE_SPY_TREND_FILTER      = True    # Only trade when SPY above 50MA
USE_VIX_FILTER            = True    # Skip when VIX ≥ 25
VIX_MAX                   = 25
USE_EARNINGS_FILTER       = True    # Skip within 3 days of earnings
EARNINGS_BUFFER_DAYS      = 3

# Dropped permanently
USE_MACD_CONFIRMATION     = False   # Incompatible with oversold RSI — 0 trades
```

---

## Daily Flow

```
4:00 PM Finnish  →  Morning scan: score all 503 stocks, save top 50 watchlist
4:20 PM Finnish  →  Post watchlist to Telegram channel
4:25 PM Finnish  →  Execute pending trades (9:25 AM ET, 5 min before open):
                       ↳ Check VIX + SPY circuit breakers (once for all)
                       ↳ Per-ticker: earnings check, position count, consec losses
                       ↳ Calculate qty = int(equity × 12% / close_price)
                       ↳ Place bracket order: market + SL@(close−ATR×3.5) + TP@(close+ATR×6.0)
                       ↳ Log to trades.csv (status=open)
                       ↳ Post "Orders placed" to Telegram
4:30–11:00 PM    →  Monitor positions every 15 min:
                       ↳ Check bracket legs for fills
                       ↳ On close: update trades.csv, post "Target/Stop hit" to Telegram
11:15 PM Finnish →  Signal check (4:15 PM ET, after market close):
                       ↳ Existing display signals on top-50 watchlist (Telegram)
                       ↳ Auto-scan ALL 503 tickers: RSI + volume filter
                       ↳ Queue valid signals to pending_trades.json
                       ↳ Post "X trades queued for tomorrow" to Telegram
Sunday 8:00 PM   →  Weekly report posted to Telegram channel
```

---

## Backtest Results

### Swing strategy — 3-round framework (May 2026)

**Universe:** 503 S&P 500 stocks  
**Window:** 2 years (May 2024 – May 2026)  
**Capital:** $5,000 starting

| Metric | Value |
|---|---|
| Return | **+131.6%** (+$6,579 on $5,000) |
| Win rate | **74.3%** |
| Profit factor | **3.23** |
| Max drawdown | **-3.4%** |
| Trades / 2 years | **136** (~68/year, ~1.3/week) |
| Median hold time | ~35 days |
| Stop loss exits | ~26% |
| Take profit exits | ~54% |

### Round 1 — Position size grid search (900 combinations)

| Size | Return | DD | PF | Decision |
|---|---|---|---|---|
| 5% | +40.9% | -1.7% | 3.19 | — |
| 10% | +96.0% | -2.8% | 3.18 | previous default |
| **12%** | **+131.6%** | **-3.4%** | **3.23** | **✅ ADOPTED** |
| 15% | +187.4% | -4.4% | 3.11 | PF drops, more DD |

**Consecutive loss limit:**
| Limit | Return | DD | Decision |
|---|---|---|---|
| 2 | +11.8% | -0.7% | too conservative |
| **3** | **+131.6%** | **-3.4%** | **✅ ADOPTED** |
| unlimited | +16.5% | **-82.7%** | catastrophic |

### Round 2 — Bear market protection

All filters (SPY 50/100/200MA, VIX <20/25/30) had **zero effect** in the backtest window — market was bullish throughout and no signal days coincided with breached thresholds.

**Conclusion:** Live-only safety nets. Essential for bear markets. All kept enabled.

### Round 3 — New indicator confirmation

| Filter | Trades | P&L | PF | Decision |
|---|---|---|---|---|
| Baseline | 136 | +$6,579 | 3.23 | ✅ keep |
| 200MA slope rising | 105 | +$5,092 | **3.50** | ❌ +PF but -$1,488 total profit |
| BB lower band ≤+4% | 133 | +$6,519 | 3.30 | ❌ -$60, not worth the complexity |
| Max hold 45d | 17 | +$458 | 3.20 | ❌ truncates winners |
| Max hold 30d | 43 | +$967 | 2.04 | ❌ much worse |

**Key findings:**
- MACD: permanently dropped — requires bullish momentum when RSI requires oversold — mutually exclusive (0 trades)
- 200MA slope: raises PF 3.23→3.50 but costs $1,488 in total profit — not adopted
- Max hold caps: all hurt — ATR ×6.0 target naturally exits winners, let them run

### Phase 4b — Intraday backtest (June 2026)

**Tested:** 15-min RSI + VWAP mean-reversion on all 453 available S&P 500 tickers, 2 years of Alpaca IEX data  
**Best intraday combo:** RSI<28, ATR×2.0/×4.0, Vol 1.5×, above VWAP

| Strategy | Trades | Win% | P&L | Return | MaxDD | PF |
|---|---|---|---|---|---|---|
| **Swing** (RSI38, ATR×3.5/×6.0, daily) | 136 | 74.3% | +$6,579 | +131.6% | -3.4% | **3.23** |
| Intraday (RSI<28, ATR×2.0/×4.0, VWAP) | 55 | 54.5% | -$16 | -0.3% | -0.5% | 0.87 |

**Verdict: Swing strategy wins on every metric. Intraday strategy is loss-making.**  
Root cause: S&P 500 large caps rarely hit RSI<30 intraday. When they do, 55% force-exit at EOD without resolution. Intraday mean-reversion does not hold for this universe.

**Decision: Build Phase 1 around swing strategy only. Intraday permanently shelved.**

---

## Implementation Roadmap

### ✅ Phase 3e+ — Swing backtesting (COMPLETE)
3-round framework. All parameters confirmed and locked. See Backtest Results above.

### ✅ Phase 4b — Intraday backtesting (COMPLETE)
Tested and concluded. Swing wins. Intraday not pursued.

### ✅ Phase 1 — Auto execution (COMPLETE)
- `trader.py` — Alpaca bracket orders, circuit breakers, emergency stop
- `trade_logger.py` — CSV log, pending queue, pause flag
- `scanner.py` — added `run_auto_scan()` for all 503 tickers
- `main.py` — 5 jobs: morning scan, execute trades, signal check, monitor, weekly report
- `telegram_bot.py` — /positions, /trades, /pause, /resume, /stopall, /report

### ✅ Phase 2a — Weekly reports (COMPLETE)
- `reporter.py` — weekly + inception-to-date reports, vs-backtest comparison
- Posts automatically every Sunday 8 PM Finnish
- `/report` for on-demand, `/report all` for full history

### Phase 2b — AI on reports (NOT YET — needs 2-3 months of live data first)
After running live for 2-3 months, add AI pattern analysis layer to the weekly report:
- Pattern recognition across trade entries (what RSI/volume combos win most)
- Post-trade analysis on losers (what could have been avoided)
- Not useful until there is real data to analyse

### Phase 5 — AI news sentiment (NOT YET — intraday only, not applicable for swing)
News sentiment is only useful for intraday strategies where execution happens within minutes of the news. For swing trading there is a 10+ hour gap between signal and execution — news is already priced in. **Do not build for swing.**

If intraday is ever revisited: news sentiment filter before entry.

### Phase 6 — Switch to IBKR live
**Prerequisites:**
- At least 6 weeks paper trading with positive results
- Win rate consistently ≥ 55% in paper trading
- All circuit breakers tested and confirmed working

**Change required:** `PAPER_TRADING = False` in `config.py` + update `trader.py` to use `ib_insync` instead of Alpaca for order placement.

---

## File Structure

```
stock-signal-bot/
├── main.py                  — Entry point, 5 scheduled jobs
├── scanner.py               — Morning scan + run_auto_scan() all 503 tickers
├── signals.py               — RSI + MA + price analysis
├── telegram_bot.py          — All Telegram commands and channel posting
├── charts.py                — Dark-mode price/RSI chart PNG
├── watchlist.py             — Daily auto-generated watchlist (JSON)
├── custom_watchlist.py      — Persistent custom watchlist per user
├── ibkr.py                  — IB Gateway connection (future live trading)
├── config.py                — All strategy constants — edit here only
├── trader.py                — Alpaca bracket orders + circuit breakers
├── trade_logger.py          — CSV trade log + pending queue + pause flag
├── reporter.py              — Weekly and inception-to-date performance reports
├── backtester.py            — Swing backtester (3-round complete)
├── intraday_backtester.py   — Intraday backtester (concluded — swing wins)
├── .env                     — All API keys (never commit)
├── requirements.txt         — Python dependencies
│
├── trades.csv               — All trade records — DO NOT DELETE
├── pending_trades.json      — Trades queued for next morning execution
├── trading_paused.flag      — Exists when auto-trading is paused
├── watchlist.json           — Daily top-50 watchlist (auto-created)
├── custom_watchlist.json    — Custom watchlist (auto-created)
│
├── backtest_cache.pkl       — Daily price data (502 tickers, 2yr+300d) — DO NOT DELETE
├── backtest_indicators.pkl  — Precomputed swing indicators
├── backtest_exits.pkl       — Precomputed swing exits
├── backtest_spy.pkl         — SPY MA data
├── backtest_vix.pkl         — VIX data
├── intraday_cache.pkl       — 15-min bars, 453 tickers, 2yr IEX — DO NOT DELETE
├── intraday_indicators.pkl  — Precomputed intraday indicators
├── intraday_exits.pkl       — Precomputed intraday exits
├── intraday_best.json       — Best intraday params (for reference)
│
├── CONTEXT.md               — General project context
├── automatedtradingplan.md  — This file
└── README.md                — Plain-language project description
```

---

## Key Decisions (do not re-debate)

| Decision | Reason |
|---|---|
| Swing only, intraday shelved | Intraday backtested and lost to swing on every metric |
| Alpaca paper → IBKR live | Validate execution before risking real money |
| Bracket orders at entry | Cleanest — exchange monitors stop/target, no polling needed |
| Market order at open | Matches backtest entry assumptions, always fills |
| ATR-based stop/target | Adapts to each stock's natural volatility — not fixed % |
| 12% position size | Highest PF (3.23) + most total profit across 900 combos |
| Consecutive loss limit = 3 | unlimited → -82.7% DD; limit=2 → too conservative (19 trades) |
| MACD permanently dropped | Mutually exclusive with oversold RSI — 0 trades |
| Max hold caps dropped | ATR ×6.0 target does the job naturally — don't truncate winners |
| 200MA slope rejected | Raises PF but costs $1,488 total profit — more money without it |
| AI on reports: wait | Needs 2-3 months of real trade data to be meaningful |
| News sentiment: not for swing | 10+ hour gap between signal and execution — news already priced in |
| Reports via Telegram | Simple, mobile-readable, permanently archived in channel |
| No real-time data subscription | Not needed for swing — daily close is sufficient, broker handles exits |

---

## Environment Variables (.env)

```
TELEGRAM_BOT_TOKEN=        # From @BotFather
TELEGRAM_CHANNEL_ID=       # Channel where signals are posted
ALPACA_API_KEY=            # Alpaca paper trading key
ALPACA_SECRET_KEY=         # Alpaca paper trading secret
IBKR_HOST=127.0.0.1
IBKR_PORT=4001             # 4001=live, 4002=paper
IBKR_CLIENT_ID=10
```

---

## Python Environment

- Always use `/Library/Developer/CommandLineTools/usr/bin/python3.9` on Mac
- System `python3` points to 3.14 which breaks `python-telegram-bot==20.7`
- All async code uses `asyncio` + `python-telegram-bot` v20 (fully async)
- IBKR and Alpaca connections run in separate threads with isolated event loops
