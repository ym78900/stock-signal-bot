# Stock Signal Bot — $40/Day Implementation Plan

> **Commit this file to your repo** so you can access it from any PC:
> ```bash
> git add STOCK_BOT_IMPLEMENTATION_PLAN.md
> git commit -m "Add $40/day implementation plan"
> git push
> ```

---

## What This Plan Does

Your bot already has a 74.3% win rate and 131.6% 2-year backtest return — the **strategy is solid**.
Two problems need fixing:

### Bug Fix (do first — costs real money now)
The current `max(1, int((portfolio * 0.12) / price))` formula forces at least 1 share even
when the math says 0. For AZO at ~$3,000: `int(5000 × 0.12 / 3000) = int(0.2) = 0` → `max(1, 0) = 1`
→ **1 share of AZO = $3,000 = 60% of your $5,000 budget**. That leaves room for only 1–2 more
positions and blows the 5-position diversification entirely.

### Enhancement (after the fix)
Daily profit averages ~$13/day on $5,000 capital, not $40.

Four targeted changes fix both problems:

| # | Change | Why |
|---|--------|-----|
| 0 | **Hard price cap** — skip stocks above $150 in scanner + trader | Prevents AZO-style blow-ups; fixes the live bug immediately |
| 1 | Expand stock universe (S&P 500 → +NASDAQ 100) | More cheap $5–$50 stocks = 2–3× more RSI signals/week |
| 2 | ATR-based position sizing targeting $40/trade | Each winning trade lands exactly $40 instead of random amounts |
| 3 | Round 4 backtester validation | Confirm changes work before risking live money |

**Do NOT change:** signal logic, RSI thresholds (38/55), ATR multipliers (3.5×/6.0×), SPY/VIX filters — all validated and working.

---

## Current Confirmed Config (for reference)

From `config.py` after 3-round optimization:

```
RSI buy threshold:     38
RSI sell threshold:    55
ATR stop multiplier:   3.5×
ATR target multiplier: 6.0×
Position size:         12% of portfolio
Max open positions:    5
Volume ratio filter:   1.2× (today's vol > 1.2× 20-day avg)
VIX ceiling:           25
SPY filter:            price above 50-day MA
Starting capital:      $5,000
```

---

## CHANGE 0 — Hard Price Cap (Fix the AZO Bug First)

This is the most urgent fix. Apply it before anything else.

### File: `config.py`

Add these constants at the bottom of the file:

```python
# ── Price Filters ─────────────────────────────────────────────────────────────
PRICE_MIN          = 5.0    # Hard skip: stocks below $5 (too illiquid)
PRICE_MAX_HARD     = 150.0  # Hard skip: stocks above $150 — with $5k budget,
                            # even 1 share at $3,000 wipes 60% of capital.
                            # $150 = max where 12% position ($600) ≥ 4 shares.
PRICE_MAX_PREFERRED = 50.0  # Soft scoring bonus for stocks in $5–$50 range
MIN_AVG_VOLUME     = 200_000  # Hard skip: under 200K avg daily volume = illiquid

# ── Position Sizing ───────────────────────────────────────────────────────────
DAILY_PROFIT_TARGET       = 40.0  # Target $ profit per winning trade
MAX_POSITION_PCT_HARD_CAP = 0.15  # Never exceed 15% of portfolio in one position
MIN_SHARES_REQUIRED       = 3     # Skip trade if can't buy at least 3 shares
                                  # within position cap (prevents 1-share AZO traps)

# ── Extended Universe ─────────────────────────────────────────────────────────
EXTENDED_UNIVERSE_ENABLED = True
NASDAQ100_WIKIPEDIA_URL   = "https://en.wikipedia.org/wiki/Nasdaq-100"
```

---

### File: `scanner.py` — Hard price filter in `_score_stock()`

Find where `price_now` is calculated:
```python
        price_now  = float(df["Close"].iloc[-1])
        price_then = float(df["Close"].iloc[-config.MOMENTUM_DAYS - 1])
```

**Immediately after** calculating `price_now`, add the hard price guard:

```python
        price_now  = float(df["Close"].iloc[-1])

        # ── Hard price cap — MUST come before any other calculation ───────────
        # Prevents the max(1, 0) = 1 share bug for stocks like AZO at $3,000.
        if price_now < config.PRICE_MIN or price_now > config.PRICE_MAX_HARD:
            return None
```

---

### File: `signals.py` (or wherever `analyse()` is) — Guard before returning signal

In `analyse()`, right before returning a BUY signal, add:

```python
        # Hard price guard — never signal stocks outside tradeable price range
        current_price = ...  # whatever variable holds current price in analyse()
        if current_price > config.PRICE_MAX_HARD or current_price < config.PRICE_MIN:
            return "NONE", "Price outside tradeable range"
```

---

### File: `trader.py` — Fix the minimum-shares calculation

Find the position sizing line:
```python
qty = max(1, int((portfolio * max_position_pct) / entry_price))
```

Replace it with this safe version:

```python
max_position_value = portfolio * config.MAX_POSITION_PCT_HARD_CAP
qty = int(max_position_value / entry_price)  # how many shares fit in budget

# Skip this trade if we can't buy enough shares to be meaningful
if qty < config.MIN_SHARES_REQUIRED:
    logger.info(f"Skipping {ticker}: only {qty} share(s) fit in position cap "
                f"(price=${entry_price:.2f}, cap=${max_position_value:.0f})")
    continue  # or return, depending on surrounding context
```

This completely eliminates the AZO-style bug. A $3,000 stock with 15% cap on $5,000 = $750 cap → `int(750 / 3000) = 0` → 0 < 3 → **trade is skipped**, not forced.

> **If `continue` doesn't apply** (e.g., you're in a function, not a loop), return `0` from
> a helper and check `if qty == 0: skip` at the call site.

---

## CHANGE 1 — Expand Stock Universe

### File: `config.py`

Already added in Change 0 above (`EXTENDED_UNIVERSE_ENABLED`, `NASDAQ100_WIKIPEDIA_URL`).

---

### File: `scanner.py`

#### Step 1 — Add the NASDAQ 100 fetcher

Add this function **after** `get_sp500_tickers()`:

```python
_nasdaq100_cache: Optional[List[str]] = None

def get_nasdaq100_tickers() -> List[str]:
    """
    Fetch the current NASDAQ-100 ticker list from Wikipedia.
    Cached in memory for the session.
    """
    global _nasdaq100_cache
    if _nasdaq100_cache is not None:
        return _nasdaq100_cache

    logger.info("Fetching NASDAQ-100 ticker list from Wikipedia...")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; stock-signal-bot/1.0)"}
        import requests, io
        response = requests.get(config.NASDAQ100_WIKIPEDIA_URL, headers=headers, timeout=15)
        response.raise_for_status()
        tables = pd.read_html(io.StringIO(response.text))
        # Wikipedia NASDAQ-100 table has "Ticker" column
        for table in tables:
            cols = [c.lower() for c in table.columns]
            if "ticker" in cols or "symbol" in cols:
                col = "Ticker" if "Ticker" in table.columns else "Symbol"
                tickers = table[col].dropna().tolist()
                tickers = [t.replace(".", "-") for t in tickers if isinstance(t, str)]
                _nasdaq100_cache = tickers
                logger.info(f"Loaded {len(tickers)} NASDAQ-100 tickers.")
                return tickers
        logger.warning("Could not find ticker column in NASDAQ-100 table.")
        return []
    except Exception as e:
        logger.error(f"Failed to fetch NASDAQ-100 list: {e}")
        return []
```

#### Step 2 — Add the combined universe function

Add this function **after** `get_nasdaq100_tickers()`:

```python
def get_extended_tickers() -> List[str]:
    """
    Return a deduplicated combined ticker list:
    S&P 500 + NASDAQ-100 (Russell 2000 via IWM ETF is optional — see note below).
    Falls back to S&P 500 only if EXTENDED_UNIVERSE_ENABLED is False.
    """
    sp500 = get_sp500_tickers()

    if not config.EXTENDED_UNIVERSE_ENABLED:
        return sp500

    nasdaq = get_nasdaq100_tickers()

    # Deduplicate while preserving S&P 500 order first
    seen = set(sp500)
    combined = list(sp500)
    for t in nasdaq:
        if t not in seen:
            combined.append(t)
            seen.add(t)

    logger.info(f"Extended universe: {len(combined)} tickers "
                f"(S&P500={len(sp500)}, NASDAQ-100 additions={len(combined)-len(sp500)})")
    return combined
```

> **Optional — Russell 2000:** If you want ~2,000 more tickers, download the IWM ETF
> holdings CSV from iShares (free, no login required) and add a `get_russell2000_tickers()`
> function that reads it. Add the CSV to the repo as `data/iwm_holdings.csv`. This is
> optional because NASDAQ-100 alone adds the key volatile cheaper stocks.

#### Step 3 — Add volume guard + price nudge in `_score_stock()`

The **hard** price filter (skip stocks above $150) was already added in Change 0.
Now add two more things:

**A) Volume liquidity guard** — add right after `try:`, before the RSI line:

```python
    try:
        # ── Liquidity guard ───────────────────────────────────────────────────
        avg_vol = df["Volume"].mean()
        if avg_vol < config.MIN_AVG_VOLUME:
            return None  # too illiquid — skip entirely
```

**B) Soft scoring nudge for cheap stocks** — find the composite score block:
```python
        # ── Weighted composite score ──────────────────────────────────────────
        composite = (
            config.WEIGHT_VOLUME   * volume_score +
            config.WEIGHT_RSI      * rsi_score    +
            config.WEIGHT_MOMENTUM * momentum_score
        )
```

Replace it with:
```python
        # ── Weighted composite score ──────────────────────────────────────────
        composite = (
            config.WEIGHT_VOLUME   * volume_score +
            config.WEIGHT_RSI      * rsi_score    +
            config.WEIGHT_MOMENTUM * momentum_score
        )

        # ── Soft scoring bonus for $5–$50 stocks ─────────────────────────────
        # Cheap stocks allow buying more shares → larger absolute $ per signal.
        # Already hard-filtered above $150; this nudges ranking, not eligibility.
        if config.PRICE_MIN <= price_now <= config.PRICE_MAX_PREFERRED:
            composite += 0.10
```

#### Step 4 — Update `run_morning_scan()` to use extended universe

Find:
```python
    tickers = get_sp500_tickers()
```

Replace with:
```python
    tickers = get_extended_tickers()
```

---

## CHANGE 2 — ATR-Based Position Sizing for $40/Trade

### File: `signals.py`

Add this import at the top if `math` isn't already imported:
```python
import math
```

Add this function anywhere in the file (good place: just before `run_signal_check()`):

```python
def calculate_position_size(
    entry_price: float,
    atr: float,
    atr_target_mult: float,
    profit_target: float = 40.0,
    portfolio_value: float = 5000.0,
    hard_cap_pct: float = 0.15,
    min_shares: int = 3,
) -> int:
    """
    Return the number of shares to buy so that a winning trade (price moves
    ATR × atr_target_mult) earns exactly profit_target dollars.

    Hard-capped at hard_cap_pct of portfolio_value.
    Returns 0 if the stock is too expensive to buy min_shares within the cap
    (caller should skip the trade — do NOT force min 1 share).

    Example — cheap stock:
        $8 stock, ATR=$0.50, mult=6.0, target=$40
        → atr_dollars = $3.00/share → shares_needed = ceil(40/3.00) = 14
        → cap = $5,000 × 15% = $750 → max_by_cap = 93 shares
        → result = min(14, 93) = 14 shares  ✅

    Example — AZO at $3,000 (should be hard-filtered in scanner, but belt+suspenders):
        $3,000 stock, ATR=$50, mult=6.0, target=$40
        → atr_dollars = $300/share → shares_needed = ceil(40/300) = 1
        → cap = $5,000 × 15% = $750 → max_by_cap = int(750/3000) = 0
        → 0 < min_shares (3) → return 0  ❌ caller skips this trade
    """
    if atr <= 0 or atr_target_mult <= 0 or entry_price <= 0:
        return 0

    atr_dollars_per_share = atr * atr_target_mult

    # Shares needed to hit profit_target at the ATR-based exit
    shares_needed = math.ceil(profit_target / atr_dollars_per_share)

    # Never exceed hard_cap_pct of portfolio in one position
    max_shares_by_cap = int((portfolio_value * hard_cap_pct) / entry_price)

    shares = min(shares_needed, max_shares_by_cap)

    # If we can't buy at least min_shares, the position is not worth taking.
    # Return 0 so the caller skips it — do NOT fall back to 1 share.
    if shares < min_shares:
        return 0

    return shares
```

---

### File: `config.py`

Add these two constants alongside the Change 1 block:

```python
# ── Position Sizing (Change 2) ────────────────────────────────────────────────
DAILY_PROFIT_TARGET      = 40.0   # Target $ profit per winning trade
MAX_POSITION_PCT_HARD_CAP = 0.20  # Never exceed 20% of portfolio in one position
```

---

### Wiring it into the trader

Your `trader.py` (Phase 1, already built) currently uses:
```python
qty = max(1, int((portfolio * max_position_pct) / entry_price))
```

Replace that block with this. Import at the top of `trader.py`:
```python
from signals import calculate_position_size
```

Then replace the qty calculation with:
```python
qty = calculate_position_size(
    entry_price=entry_price,
    atr=row["atr"],                                   # ATR value in the row dict
    atr_target_mult=config.ATR_TARGET,                # 6.0
    profit_target=config.DAILY_PROFIT_TARGET,         # 40.0
    portfolio_value=portfolio,
    hard_cap_pct=config.MAX_POSITION_PCT_HARD_CAP,   # 0.15
    min_shares=config.MIN_SHARES_REQUIRED,            # 3
)

if qty == 0:
    logger.info(f"Skipping {ticker} @ ${entry_price:.2f} — too expensive for position cap")
    continue
```

> **Note:** The exact variable names in `trader.py` may differ slightly — look for the line
> that computes `qty` or `quantity` or `shares` right before the order is submitted.
> The critical change is: **remove `max(1, ...)`** and **add `if qty == 0: continue`**.

---

## CHANGE 3 — Round 4 Backtester Validation

### File: `backtester.py`

#### Step 1 — Add `avg_daily_pnl` to `_build_summary()`

Find `_build_summary()` (the function that builds the stats dict from a list of trades). It currently returns keys like `total_trades`, `win_rate_pct`, `total_net_pnl`, etc.

Add this at the end of the summary dict:

```python
    # Trading days spanned by the backtest (approx 504 for 2 years)
    if trades:
        first_date = min(t["entry_date"] for t in trades)
        last_date  = max(t["entry_date"] for t in trades)
        trading_days = max(1, (last_date - first_date).days * 5 // 7)
    else:
        trading_days = 504  # fallback: 2 years

    summary["trading_days"]   = trading_days
    summary["avg_daily_pnl"]  = round(total_net_pnl / trading_days, 2)
```

> If `entry_date` is stored as a string instead of a `datetime`, use:
> `datetime.strptime(t["entry_date"], "%Y-%m-%d")` to parse it first.

#### Step 2 — Add `avg_daily_pnl` to `print_report()`

Find `print_report()`. After the `Total net P&L` line, add:

```python
    print(f"  {'Avg daily P&L:':<25} ${summary.get('avg_daily_pnl', 0):+,.2f}/day")
```

#### Step 3 — Update `simulate_fast()`: add `max_price` filter + fix sizing

**A) Add `max_price` and `sizing_mode` to the function signature:**

```python
def simulate_fast(
    enriched_rows: List[dict],
    rsi_buy: float,
    rsi_sell: float,
    atr_stop: float,
    atr_target: float,
    # ... existing params ...
    max_price: float = 0,           # ← ADD: 0 = no cap; 150.0 = hard cap at $150
    sizing_mode: str = "fixed_pct", # ← ADD: "fixed_pct" or "atr_target_40"
    # ... rest of params ...
```

**B) Add `max_price` filter** right after the existing `min_price` filter in the signal loop:

```python
        # Existing min_price filter (already there):
        if min_price > 0 and row.get("open_next", 0) < min_price:
            continue

        # ADD THIS — hard max price cap (fixes AZO-style single-share blowups):
        if max_price > 0 and row.get("open_next", 0) > max_price:
            continue
```

**C) Replace the position sizing block:**

Find:
```python
        # POSITION SIZING
        entry_price = row["open_next"]
        qty = max(1, int((portfolio * max_position_pct) / entry_price))
```

Replace with:
```python
        # POSITION SIZING
        entry_price = row["open_next"]
        import math

        if sizing_mode == "atr_target_40":
            atr_val = row.get("atr", 0)
            atr_dollars = atr_val * atr_target  # e.g. $0.50 ATR × 6.0 = $3.00/share
            if atr_dollars > 0:
                qty = math.ceil(40.0 / atr_dollars)
            else:
                qty = int((portfolio * max_position_pct) / entry_price)
        else:
            qty = int((portfolio * max_position_pct) / entry_price)

        # Hard cap at max_position_pct of portfolio
        max_by_cap = int((portfolio * max_position_pct) / entry_price)
        qty = min(qty, max_by_cap)

        # Skip if can't buy at least 3 shares — avoids 1-share $3,000 positions
        if qty < 3:
            continue
```

#### Step 4 — Add Round 4 to the `if __name__ == "__main__"` block

At the very end of the `__main__` block, after the existing Round 3 tests, add:

```python
    # ═══════════════════════════════════════════════════════════════════════════
    # ROUND 4 — Extended Universe + ATR-Target Sizing ($40/trade)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 65)
    print("  ROUND 4: ATR-Target $40/Trade Sizing vs Fixed 12%")
    print("=" * 65)

    round4_results = []

    # Fixed params from Round 3 best result
    R4_RSI_BUY    = 38
    R4_RSI_SELL   = 55
    R4_ATR_STOP   = 3.5
    R4_ATR_TARGET = 6.0

    # Common filter kwargs for all Round 4 tests
    r4_filters = dict(
        volume_min_ratio=1.2,
        spy_trend=spy_trend,
        vix_data=vix_data,
        vix_ceiling=25,
        spy_ma_period=50,
    )

    # ── 4A: Baseline (current best) ──────────────────────────────────────────
    _, s_4a = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.12, max_open_pos=5,
        sizing_mode="fixed_pct",
        **r4_filters,
    )
    round4_results.append({"label": "4A: Baseline (12% fixed, S&P 500)", "summary": s_4a})
    print_report(s_4a, "4A: Baseline — 12% fixed sizing, S&P 500 only")

    # ── 4B: ATR-target $40/trade sizing, S&P 500 only ────────────────────────
    _, s_4b = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.12, max_open_pos=5,
        sizing_mode="atr_target_40",
        **r4_filters,
    )
    round4_results.append({"label": "4B: ATR-target $40/trade (S&P 500 only)", "summary": s_4b})
    print_report(s_4b, "4B: ATR-target $40/trade sizing, S&P 500 only")

    # ── 4C: Hard price cap $5–$150 (fixes AZO bug), fixed sizing ────────────
    _, s_4c = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.15, max_open_pos=5,
        sizing_mode="fixed_pct",
        min_price=5.0,
        max_price=150.0,        # ← hard cap, eliminates AZO-style trades
        **r4_filters,
    )
    round4_results.append({"label": "4C: Price $5–$150 hard cap, 15% fixed", "summary": s_4c})
    print_report(s_4c, "4C: Hard price cap $5–$150 (AZO fix), fixed 15% sizing")

    # ── 4D: Hard price cap + ATR-target $40 sizing (full implementation) ─────
    _, s_4d = simulate_fast(
        enriched_2y, R4_RSI_BUY, R4_RSI_SELL, R4_ATR_STOP, R4_ATR_TARGET,
        max_position_pct=0.15, max_open_pos=5,
        sizing_mode="atr_target_40",
        min_price=5.0,
        max_price=150.0,
        **r4_filters,
    )
    round4_results.append({"label": "4D: Price $5–$150 + ATR-target $40 (BEST?)", "summary": s_4d})
    print_report(s_4d, "4D: Hard price cap + ATR-target $40 sizing COMBINED")

    # ── Summary table ─────────────────────────────────────────────────────────
    print_comparison(round4_results, title="ROUND 4 SUMMARY — Path to $40/Day")

    # Print avg daily P&L for quick decision
    print(f"\n  {'Scenario':<45} {'$/Day':>8}  {'Win%':>6}  {'Decision'}")
    print(f"  {'-'*70}")
    baseline_daily = s_4a.get("avg_daily_pnl", 0) if s_4a else 0
    for r in round4_results:
        s = r.get("summary") or {}
        daily = s.get("avg_daily_pnl", 0)
        wr    = s.get("win_rate_pct", 0)
        delta = daily - baseline_daily
        arrow = f"▲ +${delta:.2f}/day" if delta > 0.01 else (f"▼ ${delta:.2f}/day" if delta < -0.01 else "  same")
        verdict = "✅ ADOPT" if daily >= 40 and wr >= 65 else ("⚠️  CONSIDER" if daily >= 30 else "❌ SKIP")
        print(f"  {r['label']:<45} ${daily:>6.2f}  {wr:>5.1f}%  {arrow}  {verdict}")
```

> **Note about `min_price` parameter:** The backtester already has `min_price` in
> `simulate_fast()` — double check it exists (look for `if min_price > 0 and row.get("open_next", 0) < min_price: continue`).
> If it's already there, just pass `min_price=5.0` to the calls above.

---

## Running the Changes

### Step 1 — Apply all code changes (do in this order)

```
PRIORITY 0 (fix live bug immediately):
  config.py    → Add PRICE_MIN, PRICE_MAX_HARD, PRICE_MAX_PREFERRED,
                  MIN_AVG_VOLUME, MIN_SHARES_REQUIRED, MAX_POSITION_PCT_HARD_CAP
  scanner.py   → Add hard price cap in _score_stock() (skip if price > $150)
  trader.py    → Replace max(1, int(...)) with calculate_position_size()
                  Add if qty == 0: continue

THEN (enhancements):
  scanner.py   → Add get_nasdaq100_tickers(), get_extended_tickers(),
                  volume guard, price scoring nudge,
                  update run_morning_scan() to call get_extended_tickers()
  signals.py   → Add calculate_position_size() function, import math
  backtester.py → Add avg_daily_pnl to _build_summary() and print_report(),
                   add max_price + sizing_mode to simulate_fast(),
                   add Round 4 test block
```

### Step 2 — Run the backtester to validate (takes 5–15 min first run)

```bash
cd ~/Desktop/stock-signal-bot
/Library/Developer/CommandLineTools/usr/bin/python3.9 backtester.py
```

**What to look for in the output:**

```
ROUND 4 SUMMARY — Path to $40/Day

  Scenario                                       $/Day   Win%   Decision
  ───────────────────────────────────────────────────────────────────────
  4A: Baseline (12% fixed, S&P 500)             $13.xx  74.3%  ❌ baseline
  4B: ATR-target $40/trade (S&P 500 only)       $XX.xx  XX.X%  check
  4C: Price $5–$150 hard cap, 15% fixed         $XX.xx  XX.X%  check
  4D: Price $5–$150 + ATR-target $40 (BEST?)    $XX.xx  XX.X%  ✅ want ≥ $40
```

**Accept a result only if:**
- `$/Day` ≥ $40
- `Win%` ≥ 65%
- Max drawdown ≤ 35%

### Step 3 — Test on Alpaca paper mode (1–2 weeks)

```bash
/Library/Developer/CommandLineTools/usr/bin/python3.9 main.py
```

Watch Telegram for:
- Expensive stocks like AZO, NVR, BKNG are **no longer in the watchlist**
- Morning scan shows NASDAQ-100 + S&P 500 stocks ($5–$150 range)
- Trade execution: position sizes are now meaningful (e.g. 14 shares of $8 stock)
- Weekly report: `avg_daily_pnl` trending toward $40

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| AZO still appears / trade still executes | Make sure BOTH the scanner hard cap AND the trader `if qty == 0: continue` are applied — you need both as belt+suspenders |
| NASDAQ-100 fetch fails | Wikipedia table format changes occasionally — check column names in logger output and adjust the column name string in `get_nasdaq100_tickers()` |
| `simulate_fast()` doesn't accept `sizing_mode` or `max_price` | Add both to the function signature as described in Change 3 Step 3A |
| `_build_summary()` errors on date parsing | `entry_date` may be a string — wrap with `datetime.strptime(t["entry_date"], "%Y-%m-%d")` before `min()`/`max()` |
| `avg_daily_pnl` is 0 | `trades` list may be empty — add `print(f"Trade count: {len(trades)}")` before `_build_summary()` |
| Round 4 shows fewer trades than Round 3 | Expected — price cap removes some high-priced stocks. Win rate should be same or better. |
| Bot scans 600+ tickers, takes >5 min | Normal on first run — yfinance caches data. Copy the `load_or_download_data()` disk-cache pattern from `backtester.py` into `scanner.py` if needed |

---

## Expected Outcome

After implementing and validating all three changes:

| Metric | Before | After (estimated) |
|--------|--------|-------------------|
| Stock universe | 503 S&P 500 | ~600 S&P 500 + NASDAQ-100 |
| Signals/week | 3–5 | 5–9 |
| Avg position value | $600 (12% of $5k) | $112–$400 (ATR-sized) |
| Profit per winning trade | ~$30–$60 variable | ~$40 targeted |
| Avg daily P&L | ~$13 | ~$35–$50 |
| Win rate | 74.3% | Should stay ≥ 70% |

---

## Files Changed Summary

```
config.py      → +8 constants (price caps, position sizing, universe toggle)
scanner.py     → +2 functions (get_nasdaq100_tickers, get_extended_tickers)
                  modified _score_stock() (hard price cap + volume guard + nudge)
                  modified run_morning_scan() (call get_extended_tickers)
signals.py     → +1 function (calculate_position_size) + import math
trader.py      → modified qty calculation (replace max(1,int(...)) with
                  calculate_position_size() + if qty==0: continue)
backtester.py  → modified _build_summary() (add avg_daily_pnl)
                  modified print_report() (display avg_daily_pnl)
                  modified simulate_fast() (add max_price, sizing_mode params,
                  fix min-shares guard, add max_price filter)
                  added Round 4 block in __main__
```

**Total new lines of code: ~200**
**Files untouched:** `main.py`, `telegram_bot.py`, `charts.py`, `watchlist.py`, `ibkr.py`

---

## Commit this plan to your repo

```bash
cd ~/Desktop/stock-signal-bot
cp ~/Desktop/STOCK_BOT_IMPLEMENTATION_PLAN.md ./STOCK_BOT_IMPLEMENTATION_PLAN.md
git add STOCK_BOT_IMPLEMENTATION_PLAN.md
git commit -m "Add $40/day + AZO fix implementation plan"
git push
```

Then access it from your other PC via GitHub at:
`https://github.com/ym78900/stock-signal-bot/blob/main/STOCK_BOT_IMPLEMENTATION_PLAN.md`
