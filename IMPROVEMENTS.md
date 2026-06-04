# Stock Signal Bot — Profit Improvement Opportunities

> Identified June 2026. No code has been changed — this document lists proposed
> improvements ranked by expected impact and implementation risk.
> Parameters that were backtested and locked (RSI 38, ATR 3.5×/6.0×, S&P 500 universe)
> are NOT listed here — they are already optimal per the 8-round framework.

---

## Priority 1 — Safe to implement now (no backtesting required)

### 1. RSI-turning confirmation
**File:** `scanner.py` → `run_auto_scan()`  
**Change:** Only fire a BUY signal when `rsi_today > rsi_yesterday` — i.e. RSI has
stopped falling and is starting to turn up. Currently the signal fires the moment RSI
drops below 38, even if it is still declining (catching a falling knife).  
**Expected effect:** Fewer signals but higher win rate (+5–10 pp). Reduces false entries
on stocks in free-fall.  
**Risk:** Low. Additive filter — only removes trades, never adds new ones.

```python
# In run_auto_scan(), after computing rsi:
rsi_prev = float(rsi_series.iloc[-2])
if rsi <= rsi_prev:   # RSI still falling — skip
    continue
```

---

### 2. Sector diversification guard
**Files:** `scanner.py` → `get_sp500_tickers()`, `main.py` → `job_execute_trades()`  
**Change:** Pull the GICS sector column from the S&P 500 Wikipedia table (already
fetched in `get_sp500_tickers()`). Before placing a new trade, count how many open
positions are already in the same sector. Skip the trade if the count is ≥ 2.  
**Expected effect:** Prevents correlated drawdowns (e.g. 7 tech stocks all dropping
together in a sector rotation). Reduces max drawdown in sector-specific selloffs.  
**Risk:** Low. No change to signal logic. Adds a pre-trade guard in execution.

```python
# Example structure to add to scanner.py:
_sp500_sectors: Dict[str, str] = {}  # { "NVDA": "Information Technology", ... }
# Populate alongside _sp500_names in get_sp500_tickers()

# In job_execute_trades(), before place_market_buy():
sector = scanner._sp500_sectors.get(ticker, "Unknown")
open_in_sector = sum(1 for t in open_trades if scanner._sp500_sectors.get(t["ticker"]) == sector)
if open_in_sector >= 2:
    skipped.append(f"{ticker} (sector cap reached: {sector})")
    continue
```

---

### 3. Post-close extended-hours price monitor
**File:** `main.py` — new scheduled job  
**Change:** Add a job at ~8:00 PM Finnish (1:00 PM ET, mid after-hours) that checks
unrealized gain on each open position via `fetch_realtime_price()`. If the unrealized
gain exceeds 2× the original ATR target distance, place a GTC limit sell for
next-day pre-market to lock in profit before the regular session opens.  
**Why:** Currently the trailing stop is inactive during extended hours. A stock can
spike +15% after-hours and give it all back at next-day open — the bot never acts.  
**Expected effect:** Captures after-hours spikes that would otherwise be lost.  
**Risk:** Medium. New order type. Needs careful handling to avoid placing a sell when
the trailing stop already filled (check `get_closed_bracket_legs()` first).

---

## Priority 2 — Needs backtesting before changing

### 4. Volume confirmation threshold raise (1.2× → 1.5×)
**File:** `config.py` → `VOLUME_CONFIRMATION_RATIO`  
**Change:** One line. Raise from 1.2 to 1.5.  
**Why:** 1.2× is very permissive — only 20% above average. Higher volume spikes are
stronger indicators of institutional accumulation.  
**Risk:** Medium. Reduces signal count. Run `simulate_concurrent()` first to confirm
it does not reduce total P&L below current baseline (+$1,568 / 2yr).

---

### 5. Partial profit taking (two-leg exit)
**Files:** `trader.py`, `main.py` → `job_execute_trades()`  
**Change:** After entry fill, place two exit orders instead of one:
- Sell 50% of shares at a fixed limit price = `fill + ATR × 3.0` (locks in profit)
- Trailing stop at 3.5× ATR on the remaining 50% (lets winners run)  
**Why:** A pure trailing stop can give back a large open gain on a sudden reversal
before the trail triggers. Partial profit taking ensures some gain is always captured.  
**Expected effect:** Lower average winner, but smoother P&L curve and lower per-trade
drawdown. Particularly useful for volatile stocks.  
**Risk:** High. Significant rewrite of the two-phase exit flow. Requires backtesting
with `simulate_concurrent()` using split-exit logic.

---

### 6. Limit orders instead of market buys at open
**File:** `trader.py` → replace `place_market_buy()` with `place_limit_buy()`  
**Change:** Use `LimitOrderRequest` at yesterday's close price (or close + 0.1–0.2%)
instead of `MarketOrderRequest`. If the stock gaps up significantly, the order simply
won't fill — which is a quality filter (a gap-up open means the oversold condition
may have resolved overnight).  
**Why:** Market orders at 9:30 AM ET open face the widest bid/ask spreads and most
erratic price action of the day. Limit orders reduce slippage.  
**Expected effect:** Lower fill rate, but better average entry price on fills.  
**Risk:** Medium. Missed fills need handling (cancel and log). Alpaca supports this
with `LimitOrderRequest` and `TimeInForce.DAY`.

---

## Priority 3 — Capital scaling (not a code change)

### 7. Scale capital to reach the €40/day goal
The strategy earns ~13%/year (corrected Round 8 figures). Daily P&L scales linearly
with capital:

| Capital | Expected daily P&L |
|---------|-------------------|
| $5,000  | ~$3/day           |
| $20,000 | ~$12/day          |
| $50,000 | ~$29/day          |
| $70,000 | ~$43/day (~€40)   |

No code changes needed. The position sizing, circuit breakers, and risk management
already scale correctly with `get_account_equity()` which reads live equity from Alpaca.

---

## Extended hours — current limitation

The bot does **not** watch or act on pre-market or after-hours price movements:
- Position monitor runs only 4:30 PM – 11:00 PM Finnish (regular market hours)
- Trailing stop orders (`TimeInForce.GTC`) are inactive outside regular session
- Signal scan at 11:15 PM uses the regular-session closing candle only

Improvement #3 above addresses this partially. Full extended-hours order support
is limited by Alpaca — trailing stops are not supported in extended hours.

---

## What NOT to change

These parameters are backtested and locked across 8 rounds. Do not modify without
a full re-run of `simulate_concurrent()`:

| Parameter | Value | Reason |
|-----------|-------|--------|
| `RSI_BUY_THRESHOLD` | 38 | Confirmed best in Rounds 1–8 |
| `ATR_STOP_MULTIPLIER` | 3.5× | Trailing; 3.0× failed split-half robustness |
| `ATR_TARGET_MULTIPLIER` | 6.0× | Fixed target (informational only in live) |
| `MAX_OPEN_POSITIONS` | 7 | Round 8: 7 > 5; natural peak ~13 but cash-constrained |
| `EXTENDED_UNIVERSE_ENABLED` | False | Round 5: NASDAQ-100 adds 13 tickers, reduces P&L |
| `USE_MACD_CONFIRMATION` | False | Permanently off — incompatible with oversold RSI |
| Universe | S&P 500 only | Full NYSE+NASDAQ produces 18 trades vs 78 |
