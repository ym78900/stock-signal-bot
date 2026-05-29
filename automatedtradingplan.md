# Automated Trading Plan
> This document is written for the next AI session to fully understand the project state,
> trading strategy, implementation roadmap, and decisions already made.
> Read this fully before making any changes.

---

## Project Overview

A Python Telegram bot running on a Mac (or server in future) that:
- Scans the S&P 500 every morning, picks top stocks by momentum/RSI/volume
- Posts a watchlist to a Telegram channel at 4:00 PM Finnish time
- Checks for BUY/SELL signals at 11:15 PM Finnish time after US market close
- Connects to IBKR IB Gateway for live portfolio data
- Will auto-execute trades on Alpaca (paper first, IBKR live later)

**Owner:** Finland-based, trades US stocks via IBKR live account.
**Bot location:** Runs locally on Mac (future: VPS server).
**Telegram:** Used as the primary interface for all commands and notifications.

---

## Current State

### What is built and working:
- Morning scan of all 500 S&P stocks, scores by RSI + volume + momentum
- Daily watchlist posted to Telegram channel
- `/signal` — RSI + MA crossover analysis for any ticker
- `/chart` — dark-mode price chart with 20MA, 50MA, RSI
- `/watchlist` — shows top stocks, auto-generates if missed and time is after 4 PM
- `/mywatchlist` — persistent custom watchlist per user
- `/scanmywatchlist` — scan custom watchlist for signals
- `/portfolio` — live IBKR positions with real P&L from IB Gateway
- `/status` — bot health and schedule info
- Price fetching: IBKR delayed data first, Alpaca REST as fallback
- Asset cache loaded at startup for fast ticker search (13,700 assets)
- Setup scripts for Mac (`setup.sh`) and Windows (`setup.bat`)

### Current signal logic:
```
BUY:  RSI < 30  AND  (20MA > 50MA  OR  golden cross today)
SELL: RSI > 70  AND  (20MA < 50MA  OR  death cross today)
```

### Current limitations:
- No auto-execution — user acts manually via IBKR
- No stop loss or take profit on signals
- No trade history or performance tracking
- No backtesting
- No additional confirmation filters (MACD, volume, earnings)
- Bot runs locally — stops when Mac sleeps

---

## Trading Strategy

### Instrument
- US stocks from S&P 500 only
- Swing trading style — hold positions 2–10 days
- No short selling for now — only long (buy) positions

### Entry logic (current)
- RSI < 30 (oversold) confirmed by MA crossover (20MA above 50MA)
- Signal fires after US market close (11:15 PM Finnish / 4:15 PM ET)

### Exit logic (to be built — Phase 1)
- ATR-based stop loss: entry price − (ATR × 1.5)
- ATR-based take profit: entry price + (ATR × 3.0)
- Risk/reward ratio target: minimum 1:2
- Also exit if SELL signal fires on the same position (RSI overbought + MA)
- Exit uses OR logic — any one condition triggers exit (stop loss, take profit, or SELL signal)

### Position sizing
- Maximum 10% of total portfolio per trade
- Example: €5,000 portfolio → max €500 per trade
- Never open more than 5 positions simultaneously

### Risk management rules
- Per trade max loss: stop loss always set before entry (ATR × 1.5)
- Never trade within 3 days of earnings (Phase 3)
- Stop trading after 3 consecutive losses — counter resets after a winning trade, not daily
- No hard daily % loss limit — per-trade stop losses provide sufficient protection

---

## Trading Mode Toggle

The bot supports two modes, controllable via Telegram (`/settings` command):

| Mode | Behavior |
|---|---|
| **Automatic** (default) | Bot places trades immediately when signal fires |
| **Manual** | Bot sends signal to Telegram, user acts themselves via IBKR |

Stored per user in `user_settings.json`. Default is Automatic.

---

## Implementation Roadmap

---

### Phase 1 — Auto Execution (BUILD NEXT)

**Goal:** Bot automatically places paper trades on Alpaca when a signal fires,
with ATR-based stop loss and take profit. Every trade is logged.

**Files to create:**
- `trader.py` — Alpaca order placement (buy/sell), position monitoring
- `trade_logger.py` — saves every trade to `trade_history.json`
- `user_settings.py` — per-user settings (auto/manual mode toggle)

**Files to modify:**
- `signals.py` — add ATR calculation, stop loss, take profit to analysis output
- `telegram_bot.py` — show stop loss + take profit in signal messages, add `/settings` command
- `main.py` — call `trader.py` after signal fires
- `config.py` — add trading config constants

**New config values needed in `config.py`:**
```python
# Trading
TRADING_MODE = "automatic"       # "automatic" or "manual"
PAPER_TRADING = True             # True = Alpaca paper, False = IBKR live
MAX_POSITION_PCT = 0.10          # 10% of portfolio per trade
MAX_OPEN_POSITIONS = 5
CONSECUTIVE_LOSS_LIMIT = 3       # Stop trading after 3 losses in a row (resets after a win)
ATR_PERIOD = 14                  # ATR lookback period
ATR_STOP_MULTIPLIER = 1.5        # Stop loss = entry - (ATR × 1.5)
ATR_TARGET_MULTIPLIER = 3.0      # Take profit = entry + (ATR × 3.0)
```

**`trader.py` responsibilities:**
- `place_buy_order(ticker, qty, stop_loss, take_profit)` — places bracket order on Alpaca
- `place_sell_order(ticker, qty)` — closes a position
- `get_open_positions()` — returns all open Alpaca paper positions
- `get_portfolio_value()` — total Alpaca paper portfolio value for position sizing
- Uses Alpaca paper trading API keys already in `.env`

**`trade_logger.py` responsibilities:**
- `log_trade_open(ticker, entry_price, qty, stop_loss, take_profit, indicators)` — saves to JSON
- `log_trade_close(ticker, exit_price, exit_reason, pnl)` — updates record
- `get_trade_history()` — returns all trades
- Saves to `trade_history.json` (gitignored)

**Trade record structure (stored in `trade_history.json`):**
```json
{
  "id": "AAPL_20260115_143022",
  "ticker": "AAPL",
  "direction": "BUY",
  "entry_price": 210.50,
  "exit_price": 221.00,
  "qty": 2,
  "stop_loss": 203.50,
  "take_profit": 221.00,
  "entry_time": "2026-01-15T14:30:22",
  "exit_time": "2026-01-17T20:15:00",
  "exit_reason": "take_profit",
  "pnl": 21.00,
  "pnl_pct": 4.99,
  "indicators_at_entry": {
    "rsi": 28.5,
    "ma_fast": 208.30,
    "ma_slow": 205.10,
    "atr": 4.67,
    "volume_ratio": 1.8,
    "signal": "BUY"
  },
  "fees": 2.00,
  "net_pnl": 19.00
}
```

**Signal message format after Phase 1:**
```
AAPL (Apple Inc.) — BUY

Price:       $210.50  (15-min delayed)
RSI:         28.5  (oversold — potential buy zone)
MA:          20-day above 50-day — uptrend in place

Signal:      BUY
Stop loss:   $203.50  (-3.1%)
Take profit: $221.00  (+4.9%)
Risk/Reward: 1:2.0
Qty:         2 shares  (~$421.00)

✅ Order placed on Alpaca paper account
```

---

### Phase 2 — Performance Reporting

**Goal:** Weekly automated report posted to Telegram showing strategy performance.

**Files to create:**
- `reporter.py` — reads `trade_history.json`, calculates stats, formats report

**Report posted to Telegram every Sunday at 8 PM Finnish time includes:**
- Total trades this week / month / all time
- Win rate (% of profitable trades)
- Average profit on winners
- Average loss on losers
- Profit factor (total gains / total losses)
- Best trade (ticker, P&L, indicators that triggered it)
- Worst trade (ticker, P&L, indicators that triggered it)
- Total P&L this week / cumulative
- Which tickers performed best and worst

**Example weekly report:**
```
WEEKLY REPORT — Mon Jan 13 to Sun Jan 19

Trades:        8  (5 wins, 3 losses)
Win rate:      62.5%
Avg winner:    +$24.30
Avg loser:     -$12.80
Profit factor: 1.90
Net P&L:       +$83.10
Cumulative:    +$143.60

Best:  NVDA +$41.20 (RSI 27, volume 2.1x)
Worst: META -$18.40 (RSI 29, volume 1.3x)
```

---

### Phase 3 — Signal Confirmations

**Goal:** Add filters to reduce false signals and increase win rate.
Implement one at a time and measure impact on win rate using trade history from Phase 2.

**Order of implementation (most impactful first):**

#### 3a. Volume confirmation
- Condition: today's volume must be > 1.2x the 20-day average volume
- Already partially available in scanner.py — extend to signal engine
- Config: `VOLUME_CONFIRMATION_RATIO = 1.2`
- Expected win rate improvement: +5–8%

#### 3b. S&P 500 trend filter
- Fetch SPY (S&P 500 ETF) daily data
- Only take BUY signals when SPY is above its 50-day MA
- Only take SELL signals when SPY is below its 50-day MA
- Prevents trading against the overall market direction
- Config: `USE_MARKET_TREND_FILTER = True`
- Expected win rate improvement: +8–12%

#### 3c. MACD confirmation
- MACD line must be crossing above signal line for BUY
- MACD line must be crossing below signal line for SELL
- Uses `ta` library: `ta.trend.MACD`
- Config: `USE_MACD_CONFIRMATION = True`
- Expected win rate improvement: +5–8%

#### 3d. Earnings calendar filter
- Skip signals within 3 days before or after earnings announcement
- Data source: Yahoo Finance earnings calendar via yfinance
- Config: `EARNINGS_BUFFER_DAYS = 3`, `USE_EARNINGS_FILTER = True`
- Prevents buying into earnings surprises

**Each filter is a config toggle so they can be turned on/off individually
to measure the isolated impact of each one using trade history.**

**Expected combined win rate improvement:**
- Current baseline: ~52%
- After all 4 filters: ~65–70%
- Fewer trades but significantly higher quality

---

### Phase 4 — Backtesting Engine

**IMPORTANT: This is actually Step 1 — build and run this BEFORE everything else.
The full plan may change based on backtest results. Do not build auto-execution
or intraday scanning until backtest confirms the strategy has positive expected value.**

**Goal:** Test the strategy against 2+ years of historical S&P 500 data
to validate performance before risking real capital and to find optimal thresholds.

**Files to create:**
- `backtester.py` — runs strategy simulation on historical data

**How it works:**
1. Download 2 years of daily data for all S&P 500 stocks (yfinance)
2. Simulate running the signal engine day by day historically
3. Record simulated trades: entry, exit, P&L
4. Generate backtest report

**Backtest report includes:**
- Total return over 2 years
- Win rate
- Maximum drawdown (largest peak-to-trough loss)
- Sharpe ratio (return per unit of risk)
- Best/worst months
- Comparison: current strategy vs strategy with each confirmation filter added

**Threshold optimizer:**
- Run backtest across RSI thresholds (20–40 for buy, 60–80 for sell)
- Run backtest across MA periods (10/30, 20/50, 20/100)
- Find combination with best risk-adjusted return
- Report best settings found to Telegram

---

### Phase 4b — Intraday Scanning (15-min candles)

**Goal:** Scan all 500 S&P stocks every 15 minutes during market hours using
Alpaca bulk API. Add VWAP as primary intraday confirmation.
Only build after Phase 3 confirmations are validated by backtest.

**Market hours (Finnish time):** 4:30 PM – 11:00 PM

**Data source:** Alpaca bulk snapshot API — fetches all 500 stocks in a single
API call. Much faster than yfinance for intraday. Already set up in .env.

**Intraday signal logic:**
```
BUY  signal: RSI < 30 on 15-min candle
             AND price above VWAP
             AND volume spike (> 1.5x average intraday volume)

SELL signal: RSI > 70 on 15-min candle
             AND price below VWAP
```

**Critical VWAP rule:**
VWAP resets every day at market open. Intraday signals using VWAP context
are only valid same-day. They NEVER carry over to next day open.
- Intraday BUY signal fires at 7:00 PM Finnish → order executes immediately (market is open)
- End-of-day BUY signal fires at 11:15 PM Finnish → order waits for next morning open with time_in_force="day"
- These are two separate execution paths — never mix them

**Consecutive loss counter:**
- Shared between intraday and end-of-day trades
- Resets after any winning trade (not daily)
- Stop all trading after 3 consecutive losses regardless of source

**Files to modify for intraday:**
- `scanner.py` — add `run_intraday_scan()` function using Alpaca bulk snapshot
- `signals.py` — add `analyse_intraday(ticker, bars_15min)` function with VWAP
- `main.py` — add intraday scheduler job every 15 min during market hours
- `config.py` — add intraday config constants

**New config values for intraday:**
```python
INTRADAY_INTERVAL_MINUTES = 15
INTRADAY_RSI_PERIOD = 14
INTRADAY_RSI_BUY = 30
INTRADAY_RSI_SELL = 70
INTRADAY_VOLUME_SPIKE = 1.5      # Volume must be 1.5x intraday average
MARKET_OPEN_HOUR_ET = 9          # 9:30 AM ET = 4:30 PM Finnish
MARKET_OPEN_MINUTE_ET = 30
MARKET_CLOSE_HOUR_ET = 16        # 4:00 PM ET = 11:00 PM Finnish
```

---

### Phase 5 — AI Integration

**Goal:** Add intelligence on top of technical signals using OpenAI API.
Only build this after Phase 3–4 are complete and strategy is validated.
AI needs trade data to be useful — do not add before Phase 2 is collecting data.

**Use cases:**

#### 5a. News sentiment filter
- Before placing a trade, fetch recent news headlines for the ticker
- Send to OpenAI: "Given these headlines, is sentiment positive, negative, or neutral?"
- Only place BUY if sentiment is neutral or positive
- Prevents buying into bad news even if technicals look good

#### 5b. Signal explanation
- After signal fires, GPT generates a plain English explanation
- Example: "AAPL is showing a buy signal because it has been oversold for 2 days
  while the overall market is trending up and volume is unusually high today."

#### 5c. Trade review
- After a losing trade, GPT analyzes the trade log entry
- Suggests what indicator combination might have avoided the loss
- Feeds insights into the weekly report

**Config:**
```python
USE_AI_SENTIMENT = False        # Toggle on/off
USE_AI_EXPLANATIONS = False
OPENAI_MODEL = "gpt-4o-mini"    # Cheap, fast, good enough
```

**Cost estimate:** ~$0.01–0.05 per signal check with gpt-4o-mini. Acceptable.

---

### Phase 6 — Live Execution on IBKR

**Goal:** Switch from Alpaca paper trading to IBKR live account for real execution.
Only do this after Phase 4 backtesting shows positive expected value AND
Phase 1–3 paper trading shows consistent real-world performance.

**Prerequisites before going live:**
- At least 6 weeks of paper trading with positive results
- Backtest shows positive expected value over 2 years
- Win rate consistently above 55% in paper trading
- Risk management rules tested and working

**Changes needed:**
- `trader.py` updated to use `ib_insync` for order placement instead of Alpaca
- `PAPER_TRADING = False` in `config.py`
- Bracket orders: entry + stop loss + take profit placed simultaneously on IBKR

**IBKR order types used:**
- `LMT` (limit order) for entry — avoids slippage
- `STP` (stop order) for stop loss
- `LMT` for take profit
- All three placed as a bracket order simultaneously

---

## File Structure (complete, including future files)

```
stock-signal-bot/
├── main.py                  — Entry point, scheduler, loads .env
├── scanner.py               — S&P 500 fetch, bulk scoring, morning scan
├── signals.py               — RSI + MA + ATR analysis, price fetching
├── telegram_bot.py          — All Telegram commands and handlers
├── charts.py                — Dark-mode price/RSI chart PNG
├── watchlist.py             — Daily auto-generated watchlist (JSON)
├── custom_watchlist.py      — Persistent custom watchlist per user
├── ibkr.py                  — IB Gateway connection, portfolio + price fetch
├── config.py                — All constants and feature toggles
├── trader.py                — [Phase 1] Alpaca/IBKR order execution
├── trade_logger.py          — [Phase 1] Trade history logging
├── user_settings.py         — [Phase 1] Per-user settings (auto/manual mode)
├── reporter.py              — [Phase 2] Weekly performance report
├── backtester.py            — [Phase 4] Historical strategy simulation
├── setup.sh                 — Mac/Linux setup script
├── setup.bat                — Windows setup script
├── .env                     — All API keys and secrets (never commit)
├── requirements.txt         — Python dependencies
├── trade_history.json       — [Phase 1] All trade records (gitignored)
├── user_settings.json       — [Phase 1] Per-user bot settings (gitignored)
├── watchlist.json           — Daily watchlist (gitignored)
├── custom_watchlist.json    — Custom watchlist (gitignored)
├── CONTEXT.md               — General project context for AI sessions
├── automatedtradingplan.md  — This file — trading strategy and roadmap
└── README.md                — Plain-language project description
```

---

## Key Decisions (do not re-debate)

| Decision | Reason |
|---|---|
| Swing trading only, no scalping | Fees eat small profits; daily RSI not suited for scalping |
| Alpaca paper first, IBKR live later | Validate strategy before risking real money |
| ATR-based stop loss, not fixed % | Adapts to each stock's natural volatility |
| Exit uses OR logic | Capital protection is priority — any trigger exits immediately |
| Confirmations added one at a time | Measure impact of each before adding next |
| AI added last (Phase 5) | Needs trade data to be useful; premature without history |
| Auto mode default, manual toggle | User wants automation; manual mode available as safety option |
| Max 10% portfolio per trade | Prevents overexposure to single position |
| Max 5 open positions | Keeps risk manageable, ensures capital always available |
| Daily loss limit 3% | Replaced with consecutive loss limit — smarter, doesn't block afternoon trades |
| Start with current 2 confirmations | Collect real data first, add more filters based on evidence |

---

## Environment Variables (.env)

```
TELEGRAM_BOT_TOKEN=        # From @BotFather
TELEGRAM_CHANNEL_ID=       # Channel where signals are posted
ALPACA_API_KEY=            # Alpaca paper trading key
ALPACA_SECRET_KEY=         # Alpaca paper trading secret
ALPACA_BASE_URL=           # https://paper-api.alpaca.markets/v2
IBKR_HOST=127.0.0.1
IBKR_PORT=4001             # 4001=live gateway, 4002=paper gateway
IBKR_CLIENT_ID=10
OPENAI_API_KEY=            # [Phase 5] OpenAI API key
```

---

## Python Environment

- Always use `/Library/Developer/CommandLineTools/usr/bin/python3.9` on Mac
- System `python3` points to 3.14 which breaks `python-telegram-bot==20.7`
- Windows: use `python3.9` or `python3` from PATH
- All async code uses `asyncio` + `python-telegram-bot` v20 (fully async)
- IBKR and Alpaca websocket calls run in separate threads with isolated event loops
  to avoid clashing with Telegram bot's running event loop
