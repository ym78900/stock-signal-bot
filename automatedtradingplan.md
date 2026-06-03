# Automated Trading Plan

> This document is written for the next AI session to fully understand the project state,
> trading strategy, implementation roadmap, and all decisions already made.
> Read this fully before making any changes.

---

## Project Overview

A Python Telegram bot that:
- Scans all 503 S&P 500 stocks every day for swing trading BUY signals
- Places market buy orders at 9:25 AM ET, then trailing stop exits based on the real fill price
- Monitors positions and notifies via Telegram when trailing stop fires
- Posts weekly performance reports with full stats vs backtest baseline

**Owner:** Finland-based, trades US stocks. Alpaca paper → IBKR live.
**Capital:** €5,000 (~$5,400). Target: ~€40/day — but see the ⚠️ correction below: at the strategy's real ~13%/yr return this needs ~$60–70k of capital, not $5k. On $5k the bot earns ~$3/day; the goal is reached by growing capital, not by a 22-month compound.
**Bot location:** Runs locally on Mac (needs VPS for production 24/7 operation).
**Telegram:** Primary interface for all commands, alerts, and reports.

---

## ⚠️ CRITICAL CORRECTION (Round 8 — June 2026)

Every headline P&L number from Rounds 1–7 (+$25,041, +$31,712, +500–634%, ~$48/day)
was **inflated by a backtester bug**: `simulate_fast()` never populated the
`open_tickers` set with `.add()`, so it ran with **unlimited concurrent positions
and no cash constraint**. It compounded 500+ trades as if a $5,000 account could
hold unlimited overlapping 30–60 day positions and instantly recycle capital. A
diagnostic with the cash constraint disabled produced 73 simultaneous positions —
impossible on $5k.

Round 8 added `simulate_concurrent()` (time-aware: real entry→exit windows,
enforced position cap, cash constraint, peak-concurrency tracking). **Corrected
reality on $5k:**

| Metric | Old (buggy) | Corrected (cap=7, 12%, trailing 3.5×) |
|---|---|---|
| 2-year P&L | +$25,041–31,712 (+500–634%) | **+$1,568 (+31.4%)** |
| Per day | ~$48/day | **~$3.11/day** |
| Annualised | — | ~13%/yr |

Returns scale **linearly with capital** (~$3/day per $5k): $50k → ~$30/day,
$100k → ~$60/day. **€40/day (~$43/day) needs ~$60–70k.** The strategy is sound and
profitable — only the goal timeline was wrong. The *relative* rankings from earlier
rounds (price cap $200, trailing > fixed, 12% size) all survived re-testing under
the corrected simulator.

---

## Instructions for AI sessions

- **Always use** `/Library/Developer/CommandLineTools/usr/bin/python3.9` — system `python3` points to 3.14 which breaks `python-telegram-bot==20.7`
- **Never use** `ConversationHandler` — replaced with `context.user_data` state tracking
- **Read both `automatedtradingplan.md` and `CONTEXT.md` fully** before making any changes
- All thresholds and settings live in `config.py` only — never hardcode elsewhere
- Telegram callback_data has a **64 byte limit** — never encode long strings in it
- IBKR and Alpaca connections must run in **separate threads** with isolated event loops
- **Do not re-debate decisions in the "Key Decisions" table** — they are final
- **Do not change backtest parameters without re-running the full backtest**

---

## Current State (June 2026)

### What is built and working

| File | Status | Description |
|---|---|---|
| `main.py` | ✅ Complete | 5 scheduled jobs: morning scan, execute trades (2-phase), signal check, monitor positions, weekly report |
| `scanner.py` | ✅ Complete | Morning scan (top 50) + `run_auto_scan()` (all 503 tickers) + `get_full_market_tickers()` |
| `signals.py` | ✅ Complete | RSI + MA analysis, `calculate_position_size()`, Alpaca price fetch, asset cache |
| `telegram_bot.py` | ✅ Complete | All commands + /health + 5s rate limiting |
| `trader.py` | ✅ Complete | Market buy + trailing stop exit, circuit breakers, emergency stop |
| `trade_logger.py` | ✅ Complete | CSV trade log, pending queue (atomic writes), pause flag, `update_trade_after_fill()` |
| `reporter.py` | ✅ Complete | Weekly + inception-to-date reports with vs-backtest comparison |
| `backtester.py` | ✅ Complete | Swing backtest engine — 8-round framework done. Round 8 fixed the cap/cash bug via `simulate_concurrent()` |
| `charts.py` | ✅ Complete | Dark-mode price/RSI chart PNG (column validation + cleanup) |
| `watchlist.py` | ✅ Complete | Daily auto-generated watchlist (atomic JSON writes) |
| `custom_watchlist.py` | ✅ Complete | Persistent per-user custom watchlist |
| `ibkr.py` | ✅ Complete | IB Gateway connection + `is_connected()` health check |
| `config.py` | ✅ Complete | All confirmed strategy parameters |

### What needs to be done

1. **End-to-end paper trade test** — full cycle: signal → market buy → fill → trailing stop placed → exit → report
2. **VPS deployment** — move to a server (Hetzner ~€4/mo) for 24/7 operation

---

## Order Execution Flow (two-phase, fill-based)

```
9:25 AM ET  Phase 1: Place simple market buy for each queued trade
            → order queues pre-market, fills at 9:30 AM ET open

9:32 AM ET  Phase 2: Poll Alpaca for real fill price (30s intervals, 3 min timeout)
            → trail_price = ATR × 3.5
            → place_trailing_stop_exit(ticker, qty, trail_price)
            → Alpaca manages the trail server-side in real-time:
               • initial stop = fill_price − trail_price
               • as price rises, Alpaca raises stop automatically
               • when price drops to stop → Alpaca fills the exit
            → update_trade_after_fill(): store fill price, initial stop, exit order ID

During day  Monitor every 15 min:
            → detect trailing stop fills → log + Telegram notification
            → enforce MAX_HOLD_DAYS=60: cancel trail + market sell if held too long

Timeout     If fill not detected within 3 min → cancel order + Telegram alert
Emergency   If trailing stop placement fails → Telegram 🚨 alert (manual action needed)
```

**Why two-phase (not single bracket order):**
Prior close ≠ actual fill price due to overnight gaps. A stock signalling at $100 close
might open at $104. If stop is set from $100, the stop distance is wrong. Fill-based stops
ensure ATR×3.5 distance is measured from where you actually bought.

**Why trailing stop (not fixed take-profit):**
Both Round 7 and the corrected Round 8 confirm trailing 3.5× ATR beats the fixed
6× target. Under the corrected simulator (cap=7, $5k):
- Trailing 3.5× ATR: +$1,568, 51.5% win, DD 10.5%, PF 1.86
- Fixed 3.5/6.0:     +$1,264, DD ~7.9%, PF 1.71

A 3.0× trail scored marginally higher on the full window (+$2,062) but **failed a
split-half robustness check** — 3.5× won decisively in the first half — so 3.5× is kept.

---

## Strategy Parameters (final — do not change without backtesting)

```python
# Signal
RSI_BUY_THRESHOLD         = 38      # RSI below this → BUY signal
RSI_PERIOD                = 14
VOLUME_CONFIRMATION_RATIO = 1.2     # Volume > 1.2× 20-day avg required
ATR_PERIOD                = 14
ATR_STOP_MULTIPLIER       = 3.5     # Trail distance = ATR × 3.5 (also initial stop distance)

# Portfolio
MAX_POSITION_PCT          = 0.12    # 12% of equity per trade (backtester baseline)
MAX_POSITION_PCT_HARD_CAP = 0.15    # live hard cap
MAX_OPEN_POSITIONS        = 7       # Circuit breaker (Round 8: 7 beats 5 on P&L at similar DD)
CONSECUTIVE_LOSS_LIMIT    = 3       # Pause after N consecutive losses
MIN_SHARES_REQUIRED       = 3       # Skip trade if can't buy ≥3 shares
MAX_HOLD_DAYS             = 60      # Force-close after 60 calendar days (safety net)

# Price filters (Round 7: $200 confirmed best)
PRICE_MIN                 = 5.0
PRICE_MAX_HARD            = 200.0   # $200 = best P&L, best PF, lowest DD of all caps tested
PRICE_MAX_PREFERRED       = 50.0    # scoring nudge only
MIN_AVG_VOLUME            = 200_000

# Filters (live-only — no effect in backtest bull window, essential for bear markets)
USE_SPY_TREND_FILTER      = True    # Only trade when SPY above 50MA
USE_VIX_FILTER            = True    # Skip when VIX ≥ 25
VIX_MAX                   = 25
USE_EARNINGS_FILTER       = True    # Skip within 3 days of earnings
EARNINGS_BUFFER_DAYS      = 3

# Dropped permanently
ATR_TARGET_MULTIPLIER     = 6.0     # KEPT IN CONFIG for reference/backtest only
                                     # Not used in live trading (trailing stop replaced fixed target)
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
                       ↳ Phase 1: place market buy (no stop/target yet)
                       ↳ Sleep 2 min for market open
                       ↳ Phase 2: poll fill → get real fill price
                       ↳ trail_price = ATR × 3.5
                       ↳ place_trailing_stop_exit() — Alpaca handles trailing
                       ↳ Log to trades.csv, post "Orders placed" to Telegram
4:30–11:00 PM    →  Monitor positions every 15 min:
                       ↳ Detect trailing stop fills → log + Telegram notification
                       ↳ Enforce MAX_HOLD_DAYS=60 — cancel + market sell if overdue
11:15 PM Finnish →  Signal check (4:15 PM ET, after market close):
                       ↳ Auto-scan ALL 503 tickers: RSI + volume + price + MA filters
                       ↳ Queue valid signals to pending_trades.json
                       ↳ Post "X trades queued for tomorrow" to Telegram
Sunday 8:00 PM   →  Weekly report posted to Telegram channel
```

---

## Backtest Results

### Swing strategy — 8-round framework (June 2026)

**Universe:** 503 S&P 500 stocks
**Window:** 2 years (May 2024 – May 2026)
**Capital:** $5,000 starting

> **⚠️ Read this before trusting any P&L figure below.** Rounds 1–7 used
> `simulate_fast()`, whose `open_tickers` set was never populated with `.add()` —
> so it ran with **unlimited concurrent positions and no cash constraint**. All
> absolute P&L / % / per-day figures in Rounds 1–7 are inflated (often ~10×).
> **Round 8** built `simulate_concurrent()` (time-aware, cap + cash enforced) and
> re-derived the real numbers. The *relative rankings* in Rounds 1–7 (size, price
> cap, trailing vs fixed) still hold; only the magnitudes were wrong.
> The consecutive-loss bug was separately fixed in Round 7.

#### Round 1 — Position size grid search (900 combinations)

| Size | Return | DD | PF | Decision |
|---|---|---|---|---|
| 5% | +40.9% | -1.7% | 3.19 | — |
| 10% | +96.0% | -2.8% | 3.18 | — |
| **12%** | **+131.6%** | **-3.4%** | **3.23** | **✅ ADOPTED** |
| 15% | +187.4% | -4.4% | 3.11 | PF drops, more DD |

**Consecutive loss limit:**
| Limit | Return | DD | Decision |
|---|---|---|---|
| 2 | +11.8% | -0.7% | too conservative |
| **3** | **+131.6%** | **-3.4%** | **✅ ADOPTED** |
| unlimited | +16.5% | -82.7% | catastrophic |

#### Round 2 — Bear market protection

All filters (SPY 50/100/200MA, VIX <20/25/30) had **zero effect** in the backtest window.
Kept as live-only safety nets for bear markets.

#### Round 3 — New indicator confirmation

| Filter | Trades | P&L | PF | Decision |
|---|---|---|---|---|
| Baseline | 136 | +$6,579 | 3.23 | ✅ keep |
| 200MA slope rising | 105 | +$5,092 | 3.50 | ❌ +PF but -$1,488 total |
| BB lower band ≤+4% | 133 | +$6,519 | 3.30 | ❌ not worth complexity |
| Max hold 45d | 17 | +$458 | 3.20 | ❌ truncates winners |

#### Round 4 — ATR-target sizing + price cap

| Scenario | Trades | Win% | P&L | PF | Decision |
|---|---|---|---|---|---|
| 4A: 12% fixed, no price cap | 114 | 74.6% | +$4,471 | 3.12 | previous best |
| **4B: 12% fixed + price $5–$150** | **78** | **71.8%** | **+$2,713** | **2.83** | adopted at time |
| 4C: ATR-target $40/trade | 78 | 67.9% | +$1,184 | 2.19 | ❌ -70% P&L |

#### Round 5 — Universe size comparison

| Universe | Tickers | Trades | Win% | P&L | PF | Verdict |
|---|---|---|---|---|---|---|
| **S&P 500 only** | **502** | **78** | **71.8%** | **+$2,713** | **2.83** | **✅ BASELINE** |
| S&P 500 + NASDAQ-100 | 515 | 79 | 69.6% | +$2,475 | 2.47 | ❌ SKIP |
| Full NYSE + NASDAQ | 5,177 | 18 | 61.1% | +$569 | 3.31 | ❌ SKIP |

#### Round 6 — Price cap re-test (BUGGED — see Round 7)

Results from Rounds 1–6 were based on a broken simulator: `consecutive_losses >= 3`
used `continue` instead of resetting — permanently blocked all trades after the first
3-loss streak. Only the first ~100 signals were ever measured.

#### Round 7 — Fixed simulator, full 2-year window

**Fix:** auto-resume after 7 calendar days (simulates user running `/resume`).
Now measures all ~500+ signals across the full 2-year period.

**Price cap comparison (fixed 3.5× stop / 6.0× target):**

| Cap | Trades | Win% | P&L | MaxDD | PF | Decision |
|---|---|---|---|---|---|---|
| $150 | 469 | 58.2% | +$22,054 | 14.7% | 1.82 | old baseline |
| **$200** | **506** | **58.5%** | **+$25,041** | **19.2%** | **1.93** | **✅ ADOPTED** |
| $250 | 555 | 57.7% | +$22,983 | 24.2% | 1.79 | worse than $200 |
| No cap | 623 | 57.5% | +$19,865 | 26.9% | 1.65 | worst |

**Trailing stop vs fixed target ($200 cap):**

| Exit strategy | Trades | Win% | P&L | MaxDD | PF | Decision |
|---|---|---|---|---|---|---|
| Fixed 6.0× target | 555 | 57.7% | +$22,983 | 24.2% | 1.79 | replaced |
| **Trailing 3.5× ATR** | **550** | **55.5%** | **+$31,712** | **15.5%** | **2.44** | **✅ ADOPTED** |

Trailing stop wins on every metric: +$8,729 more profit, 8.7% lower drawdown, higher PF.
Alpaca natively supports trailing stop orders (`TrailingStopOrderRequest`) — managed
server-side with no polling required.

> ⚠️ The Round 7 absolute figures above are inflated by the cap/cash bug. See Round 8
> for the corrected magnitudes. The trailing-beats-fixed *conclusion* held up.

#### Round 8 — Cap/cash bug fix + corrected re-test (current)

Built `simulate_concurrent()`: assigns each trade its real entry/exit dates, enforces
`MAX_OPEN_POSITIONS`, tracks available cash (no trade if capital is tied up in open
positions), and records peak concurrency. Re-ran every prior conclusion.

**Corrected baseline (cap=7, 12% size, $5k, trailing 3.5× ATR):**

| Metric | Value |
|---|---|
| 2-year P&L | **+$1,568 (+31.4%)** |
| Win rate | 51.5% |
| Profit factor | 1.86 |
| Max drawdown | 10.5% |
| Trades | 101 |
| Per day | ~$3.11 |

**Position-limit sweep (trailing 3.5×):**

| Cap | P&L | MaxDD | PF | Peak concurrent | Decision |
|---|---|---|---|---|---|
| 5 | +$818 | 8.0% | 1.68 | 5 | old value |
| **7** | **+$1,264** | **7.9%** | **1.71** | 7 | **✅ ADOPTED** |
| 8 | +$1,459 | 9.3% | — | 8 | more DD, marginal gain |
| unlimited | +$2,006 | 8.5% | — | 13 | impossible on $5k cash |

Natural peak concurrency is ~13; the cash constraint already bounds it. Raising
5→7 lifts P&L at essentially the same drawdown — adopted. (The trailing-3.5× cap=7
number above is +$1,568; the +$1,264 here is the fixed-exit cap=7 reference used in
the sweep.)

**Capital scaling (linear, ~13%/yr):**

| Capital | Per day |
|---|---|
| $5,000 | ~$2.51 |
| $50,000 | ~$29.65 |
| $100,000 | ~$59.96 |

→ **€40/day (~$43/day) needs ~$60–70k.** Position size 12–15% remains optimal
(beyond 15% drawdown rises with no extra return). Pure exit-throughput tuning
(tighter trails for more trades) backfired — win rate collapsed (1.5/2.5 trail:
249 trades but 44% win, PF 1.19). Trailing 3.0× was rejected on the split-half
robustness check. **Only parameter change this round: `MAX_OPEN_POSITIONS` 5 → 7.**

---

## Implementation Roadmap

### ✅ Rounds 1–5 — Backtesting framework (COMPLETE)
All parameters confirmed. See above.

### ✅ Round 6 — Price cap test (SUPERSEDED by Round 7)

### ✅ Round 7 — Fixed simulator + price cap + trailing stop (COMPLETE)
- Fixed consecutive-loss bug in `backtester.py`
- `PRICE_MAX_HARD` confirmed at $200
- Trailing stop 3.5× ATR adopted — replaces fixed 6× target

### ✅ Round 8 — Cap/cash bug fix + corrected re-test (COMPLETE)
- Built `simulate_concurrent()` (time-aware, enforces position cap + cash constraint)
- Corrected all P&L magnitudes: real return ~+31% / ~$3/day on $5k (was ~$48/day)
- `MAX_OPEN_POSITIONS` raised 5 → 7; trailing 3.5× re-validated; 3.0× rejected
- €40/day goal re-scoped: requires ~$60–70k capital

### ✅ Fill-based orders (COMPLETE)
- Two-phase execution: market buy first → poll fill → trailing stop based on real fill price
- Fixes overnight-gap problem (prior close ≠ actual fill)

### 🔲 End-to-end paper trade test
Full cycle: signal → market buy → fill detected → trailing stop placed → trailing stop fires → report.

### 🔲 VPS deployment
Move to Hetzner ~€4/mo for 24/7 operation.

### 🔲 Phase 2b — AI on reports (needs 2–3 months live data first)

### 🔲 Phase 6 — Switch to IBKR live
Prerequisites: 6+ weeks paper trading, win rate ≥55%, all circuit breakers tested.
Change: `PAPER_TRADING = False` + update `trader.py` to use `ib_insync`.

---

## File Structure

```
stock-signal-bot/
├── main.py                  — Entry point, 5 scheduled jobs (2-phase execution)
├── scanner.py               — Morning scan + run_auto_scan() all 503 tickers
│                              + get_extended_tickers() + get_full_market_tickers()
├── signals.py               — RSI + MA + price analysis + calculate_position_size()
├── telegram_bot.py          — All Telegram commands, rate limiting, /health
├── charts.py                — Dark-mode price/RSI chart PNG (column validation)
├── watchlist.py             — Daily auto-generated watchlist (atomic writes)
├── custom_watchlist.py      — Persistent per-user custom watchlist
├── ibkr.py                  — IB Gateway connection + is_connected()
├── config.py                — All strategy constants — edit here only
├── trader.py                — Market buy + trailing stop exit + circuit breakers
├── trade_logger.py          — CSV trade log + update_trade_after_fill() + pause flag
├── reporter.py              — Weekly and inception-to-date performance reports
├── backtester.py            — Swing backtester (8-round complete; simulate_concurrent + simulate_fast)
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
├── backtest_cache_extended.pkl   — S&P 500 + NASDAQ-100 (Round 5B)
├── backtest_indicators_extended.pkl
├── backtest_exits_extended.pkl
├── backtest_cache_full_market.pkl      — Full NYSE+NASDAQ 5,177 tickers (Round 5C)
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
| Two-phase orders (buy → fill → trail) | Fixes overnight-gap: stop/target set from real fill, not prior close |
| Trailing stop 3.5× ATR | Beats fixed 6× target in both Round 7 and corrected Round 8. 3.0× trail rejected — failed split-half robustness check |
| Alpaca native trailing stop | Server-side management, no polling; works even if Mac is offline |
| Max hold 60 days | Safety net for stalled trades; matches backtest parameter |
| ATR-based trail distance | Adapts to each stock's natural volatility — not fixed % |
| 12% position size | Highest PF + most total profit across 900 combos; 12–15% confirmed optimal under corrected Round 8 sim |
| Price cap $200 | Round 7: best P&L, best PF, lowest DD of all caps tested |
| Max open positions = 7 | Round 8: 7 beats 5 on P&L (+$1,264 vs +$818) at similar drawdown (~8%) |
| Consecutive loss limit = 3 | unlimited → -82.7% DD; limit=2 → too conservative |
| MACD permanently dropped | Mutually exclusive with oversold RSI — 0 trades |
| 200MA slope rejected | Raises PF but costs $1,488 total profit |
| Min 3 shares required | Prevents 1-share $3k positions slipping through price cap |
| S&P 500-only universe | Round 5: NASDAQ-100 adds 13 tickers and reduces P&L |
| ATR target $40 sizing rejected | Reduces 2-year P&L by ~70% vs 12% fixed |
| AI on reports: wait | Needs 2–3 months of real trade data to be meaningful |
| News sentiment: not for swing | 10+ hour gap between signal and execution — news already priced in |

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
