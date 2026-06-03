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
| `scanner.py` | ✅ Complete | Morning scan (top 50) + `run_auto_scan()` (all 503 tickers at 11:15 PM) + `get_full_market_tickers()` |
| `signals.py` | ✅ Complete | RSI + MA analysis, `calculate_position_size()`, Alpaca price fetch, asset cache |
| `telegram_bot.py` | ✅ Complete | All commands including /positions /trades /pause /resume /stopall /report /health — 5s rate limiting |
| `trader.py` | ✅ Complete | Alpaca bracket orders, circuit breakers (VIX/SPY/earnings), emergency stop |
| `trade_logger.py` | ✅ Complete | CSV trade log, pending queue (atomic writes), pause flag, consecutive loss tracking |
| `reporter.py` | ✅ Complete | Weekly + inception-to-date reports with vs-backtest comparison |
| `backtester.py` | ✅ Complete | Swing backtest engine — 5-round framework done |
| `intraday_backtester.py` | ✅ Complete | Intraday backtest — tested and concluded (swing wins) |
| `charts.py` | ✅ Complete | Dark-mode price/RSI chart PNG (column validation + cleanup) |
| `watchlist.py` | ✅ Complete | Daily auto-generated watchlist (atomic JSON writes) |
| `custom_watchlist.py` | ✅ Complete | Persistent per-user custom watchlist |
| `ibkr.py` | ✅ Complete | IB Gateway connection + `is_connected()` health check |
| `config.py` | ✅ Complete | All confirmed strategy parameters, no duplicates |

### What needs to be done before the bot can run 24/7

1. **End-to-end paper trade test** — full cycle: signal → order → fill → exit → report
2. **VPS deployment** — move to a server (Hetzner ~€4/mo recommended) for 24/7 operation

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
MAX_POSITION_PCT          = 0.12    # 12% of equity per trade (backtester baseline)
MAX_POSITION_PCT_HARD_CAP = 0.15    # live hard cap
MAX_OPEN_POSITIONS        = 5       # Circuit breaker
CONSECUTIVE_LOSS_LIMIT    = 3       # Pause after N consecutive losses
MIN_SHARES_REQUIRED       = 3       # Skip trade if can't buy ≥3 shares

# Price filters (AZO bug fix — Round 4)
PRICE_MIN                 = 5.0
PRICE_MAX_HARD            = 150.0
PRICE_MAX_PREFERRED       = 50.0    # scoring nudge only
MIN_AVG_VOLUME            = 200_000

# Filters (live-only — no effect in backtest bull window, essential for bear markets)
USE_SPY_TREND_FILTER      = True    # Only trade when SPY above 50MA
USE_VIX_FILTER            = True    # Skip when VIX ≥ 25
VIX_MAX                   = 25
USE_EARNINGS_FILTER       = True    # Skip within 3 days of earnings
EARNINGS_BUFFER_DAYS      = 3

# Dropped permanently
USE_MACD_CONFIRMATION     = False   # Incompatible with oversold RSI — 0 trades
EXTENDED_UNIVERSE_ENABLED = False   # Round 5: NASDAQ-100 adds 13 tickers, reduces P&L
```

---

## Daily Flow

```
4:00 PM Finnish  →  Morning scan: score all 503 stocks, save top 50 watchlist
4:20 PM Finnish  →  Post watchlist to Telegram channel
4:25 PM Finnish  →  Execute pending trades (9:25 AM ET, 5 min before open):
                       ↳ Check VIX + SPY circuit breakers (once for all)
                       ↳ Per-ticker: earnings check, position count, consec losses
                       ↳ qty = calculate_position_size(price, atr, target=$40, cap=15%)
                       ↳ if qty < 3: skip trade
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

### Swing strategy — 5-round framework (June 2026)

**Universe:** 503 S&P 500 stocks  
**Window:** 2 years (May 2024 – May 2026)  
**Capital:** $5,000 starting

#### Round 1 — Position size grid search (900 combinations)

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

#### Round 2 — Bear market protection

All filters (SPY 50/100/200MA, VIX <20/25/30) had **zero effect** in the backtest window — market was bullish throughout.

**Conclusion:** Live-only safety nets. Essential for bear markets. All kept enabled.

#### Round 3 — New indicator confirmation

| Filter | Trades | P&L | PF | Decision |
|---|---|---|---|---|
| Baseline | 136 | +$6,579 | 3.23 | ✅ keep |
| 200MA slope rising | 105 | +$5,092 | **3.50** | ❌ +PF but -$1,488 total profit |
| BB lower band ≤+4% | 133 | +$6,519 | 3.30 | ❌ -$60, not worth the complexity |
| Max hold 45d | 17 | +$458 | 3.20 | ❌ truncates winners |

**Key findings:**
- MACD: permanently dropped — requires bullish momentum when RSI requires oversold (0 trades)
- 200MA slope: raises PF 3.23→3.50 but costs $1,488 in total profit — not adopted
- Max hold caps: all hurt — ATR ×6.0 target naturally exits winners, let them run

#### Round 4 — ATR-target sizing + price cap

| Scenario | Trades | Win% | P&L | PF | Decision |
|---|---|---|---|---|---|
| 4A: 12% fixed, no price cap (old baseline) | 114 | 74.6% | +$4,471 | 3.12 | previous best |
| **4B: 12% fixed + price $5–$150** | **78** | **71.8%** | **+$2,713** | **2.83** | **✅ ADOPTED** |
| 4C: ATR-target $40/trade | 78 | 67.9% | +$1,184 | 2.19 | ❌ -70% P&L |

**Key findings:**
- Hard price cap $5–$150 fixes the AZO bug (1 share = 60% of portfolio) — adopted
- ATR-target $40/trade sizing reduces 2-year P&L by ~70% — not adopted
- Baseline (12% fixed) still produces best total profit after applying price cap

#### Round 5 — Universe size comparison

| Universe | Tickers | Trades | Win% | P&L | PF | Verdict |
|---|---|---|---|---|---|---|
| **S&P 500 only** | **502** | **78** | **71.8%** | **+$2,713** | **2.83** | **✅ BASELINE** |
| S&P 500 + NASDAQ-100 | 515 | 79 | 69.6% | +$2,475 | 2.47 | ❌ SKIP |
| Full NYSE + NASDAQ | 5,177 | 18 | 61.1% | +$569 | 3.31 | ❌ SKIP |

**Key finding:** RSI/MA/price filters reject most small-caps. Expanding to full market produces only 18 trades vs 78 — the strategy is inherently S&P 500-selective. S&P 500-only universe confirmed as definitively best.

### Phase 4b — Intraday backtest (June 2026)

**Tested:** 15-min RSI + VWAP mean-reversion on all 453 available S&P 500 tickers, 2 years of Alpaca IEX data

| Strategy | Trades | Win% | P&L | Return | MaxDD | PF |
|---|---|---|---|---|---|---|
| **Swing** (RSI38, ATR×3.5/×6.0, daily) | 136 | 74.3% | +$6,579 | +131.6% | -3.4% | **3.23** |
| Intraday (RSI<28, ATR×2.0/×4.0, VWAP) | 55 | 54.5% | -$16 | -0.3% | -0.5% | 0.87 |

**Verdict: Swing wins on every metric. Intraday permanently shelved.**

---

## Implementation Roadmap

### ✅ Phase 3e+ — Swing backtesting (COMPLETE)
5-round framework. All parameters confirmed and locked. See Backtest Results above.

### ✅ Phase 4b — Intraday backtesting (COMPLETE)
Tested and concluded. Swing wins. Intraday not pursued.

### ✅ Phase 1 — Auto execution (COMPLETE)
- `trader.py` — Alpaca bracket orders, circuit breakers, emergency stop
- `trade_logger.py` — CSV log, pending queue (atomic writes), pause flag
- `scanner.py` — `run_auto_scan()` for all 503 tickers
- `signals.py` — `calculate_position_size()`, min 3 shares guard
- `main.py` — 5 jobs: morning scan, execute trades, signal check, monitor, weekly report
- `telegram_bot.py` — all commands + 5s rate limiting + `/health`

### ✅ Phase 2a — Weekly reports (COMPLETE)
- `reporter.py` — weekly + inception-to-date reports, vs-backtest comparison
- Posts automatically every Sunday 8 PM Finnish
- `/report` for on-demand, `/report all` for full history

### ✅ Round 4 — AZO bug fix + price cap (COMPLETE)
- `calculate_position_size()` in `signals.py` — replaces broken `max(1, int(...))` formula
- Hard price cap $5–$150 in `scanner.py` and `main.py`
- Min 3 shares guard — skip trade if can't buy ≥3 within position cap
- Backtested and confirmed (see Round 4 above)

### ✅ Round 5 — Universe expansion (COMPLETE, NOT ADOPTED)
- `get_full_market_tickers()` in `scanner.py` — fetches ~6,500 NYSE+NASDAQ tickers
- Three-way backtest: S&P 500 vs +NASDAQ-100 vs full market
- Result: S&P 500-only wins — expansion hurts signal quality
- `EXTENDED_UNIVERSE_ENABLED = False` confirmed

### 🔲 End-to-end paper trade test
Full cycle: signal → order → fill → exit → report. Not yet done.

### 🔲 Phase 2b — AI on reports (NOT YET — needs 2-3 months of live data first)
After running live for 2-3 months, add AI pattern analysis to weekly reports.

### 🔲 Phase 5 — AI news sentiment (NOT applicable for swing)
News is already priced in over a 10+ hour gap. Not useful for swing.

### 🔲 Phase 6 — Switch to IBKR live
**Prerequisites:**
- At least 6 weeks paper trading with positive results
- Win rate consistently ≥ 55% in paper trading
- All circuit breakers tested and confirmed working

**Change required:** `PAPER_TRADING = False` in `config.py` + update `trader.py` to use `ib_insync` instead of Alpaca.

---

## File Structure

```
stock-signal-bot/
├── main.py                  — Entry point, 5 scheduled jobs
├── scanner.py               — Morning scan + run_auto_scan() all 503 tickers
│                              + get_extended_tickers() + get_full_market_tickers()
├── signals.py               — RSI + MA + price analysis + calculate_position_size()
├── telegram_bot.py          — All Telegram commands, rate limiting, /health
├── charts.py                — Dark-mode price/RSI chart PNG (column validation)
├── watchlist.py             — Daily auto-generated watchlist (atomic writes)
├── custom_watchlist.py      — Persistent per-user custom watchlist
├── ibkr.py                  — IB Gateway connection + is_connected()
├── config.py                — All strategy constants — edit here only
├── trader.py                — Alpaca bracket orders + circuit breakers
├── trade_logger.py          — CSV trade log + pending queue (atomic writes) + pause flag
├── reporter.py              — Weekly and inception-to-date performance reports
├── backtester.py            — Swing backtester (5-round complete)
│                              load_or_download_data() — batched + incremental
│                              precompute_signals/exits() — accept alternate cache paths
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
├── backtest_cache.pkl            — S&P 500 daily price data (502 tickers, 2yr+300d) — DO NOT DELETE
├── backtest_indicators.pkl       — Precomputed swing indicators (S&P 500)
├── backtest_exits.pkl            — Precomputed swing exits (S&P 500)
├── backtest_cache_extended.pkl   — S&P 500 + NASDAQ-100 data (Round 5B)
├── backtest_indicators_extended.pkl
├── backtest_exits_extended.pkl
├── backtest_cache_full_market.pkl      — Full NYSE+NASDAQ data, 5,177 tickers (Round 5C)
├── backtest_indicators_full_market.pkl
├── backtest_exits_full_market.pkl
├── backtest_spy.pkl         — SPY MA data
├── backtest_vix.pkl         — VIX data
│
├── CONTEXT.md               — General project context (primary reference)
├── automatedtradingplan.md  — This file
├── STOCK_BOT_IMPLEMENTATION_PLAN.md — Round 4 implementation plan (executed ✅)
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
| Price cap $5–$150 | Fixes AZO bug (1 share = 60% of portfolio); confirmed in Round 4 |
| ATR-target $40 sizing rejected | Reduces 2-year P&L by ~70% vs 12% fixed — not adopted |
| Min 3 shares required | Prevents 1-share $3k positions slipping through price cap |
| S&P 500-only universe | Round 5: NASDAQ-100 adds 13 tickers and reduces P&L; full market produces only 18 trades |
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
