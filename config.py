import pytz

# ── Timezones ─────────────────────────────────────────────────────────────────
TIMEZONE    = pytz.timezone("Europe/Helsinki")
TIMEZONE_ET = pytz.timezone("America/New_York")

# ── Scheduler times (Finnish time, 24h) ──────────────────────────────────────
MORNING_SCAN_HOUR   = 16   # 4:00 PM Finnish = 9:00 AM ET (pre-market)
MORNING_SCAN_MINUTE = 0

WATCHLIST_POST_HOUR   = 16  # 4:20 PM Finnish = 9:20 AM ET
WATCHLIST_POST_MINUTE = 20

# Execute queued trades — 9:25 AM ET, 5 min before market open
EXECUTE_TRADES_HOUR   = 16  # 4:25 PM Finnish = 9:25 AM ET
EXECUTE_TRADES_MINUTE = 25

# Monitor open positions every N minutes (during market hours)
MONITOR_INTERVAL_MINUTES = 15

SIGNAL_CHECK_HOUR   = 23   # 11:15 PM Finnish = 4:15 PM ET (post-close)
SIGNAL_CHECK_MINUTE = 15

# ── Stock universe ────────────────────────────────────────────────────────────
SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
TOP_N_STOCKS = 50          # How many stocks to put on the daily watchlist

# ── yfinance fetch settings ───────────────────────────────────────────────────
DATA_PERIOD   = "60d"      # 60 days of history (enough for 50-day MA + ATR)
DATA_INTERVAL = "1d"       # Daily candles

# ── Signal thresholds ─────────────────────────────────────────────────────────
# Final confirmed values from 5-round backtesting framework (June 2026).
# Tested on 503 S&P 500 stocks, 2-year window (May 2024–May 2026), $5,000 capital.
#
# Round 1 (portfolio params): 900 combos → 12% pos size best PF (3.23)
# Round 2 (bear protection):  SPY 50/100/200MA + VIX filters → no effect in bull window
#                              Kept as live-only safety nets
# Round 3 (new indicators):   BB lower band, 200MA slope, max hold days →
#                              none beat baseline; baseline kept
# Round 4 (price cap + sizing): $5–$150 hard cap adopted; ATR-target $40 sizing rejected
# Round 5 (universe size):    S&P 500-only confirmed best; NASDAQ-100 and full market rejected
# Round 6 (price cap raise):  BUGGED (consecutive-loss sim only measured first 100 trades)
# Round 7 (full-window fix):  Fixed consecutive-loss bug — auto-resume after 7 calendar days.
#                              $200 cap confirmed best: 506 trades, 58.5% win, PF 1.93, +$25,041
#                              Trailing stop 3.5x ATR also beats fixed target: +$31,712, DD 15.2%
#
# Backtester note: open_tickers set is never populated (known limitation — no concurrent tracking).
#                  Both simulations affected equally so comparisons remain valid.
#
# Final performance (Round 6): +86.0% over 2 years, 74.0% win rate, -4.0% max DD, PF 3.22
RSI_BUY_THRESHOLD  = 38    # RSI below this → BUY signal
RSI_SELL_THRESHOLD = 55    # RSI above this → informational only (exits handled by ATR bracket)
RSI_PERIOD         = 14    # Standard RSI period

# ── ATR-based exit multipliers ────────────────────────────────────────────────
# ×3.5/×6.0 confirmed best across all rounds (wider stop lets trades breathe,
# wider target captures full swing move)
ATR_PERIOD            = 14    # ATR lookback period
ATR_STOP_MULTIPLIER   = 3.5   # Stop loss   = entry − (ATR × 3.5)
ATR_TARGET_MULTIPLIER = 6.0   # Take profit = entry + (ATR × 6.0)

# ── Confirmation filters ──────────────────────────────────────────────────────
# Volume: only trade if today's volume > this × 20-day avg.
# 1.2× confirmed as best threshold — tighter cuts winners, looser loses quality.
VOLUME_CONFIRMATION_RATIO = 1.2

# SPY trend filter: only take BUY signals when SPY is above its 50-day MA.
# Had zero effect in 2-year backtest (pure bull market) — kept as live safety net.
USE_SPY_TREND_FILTER = True

# VIX filter: skip all trading when VIX ≥ threshold (high fear / volatility spike).
# Threshold: 25 (blocks March 2020 / April 2025 style selloffs)
USE_VIX_FILTER  = True
VIX_MAX         = 25

# MACD filter: permanently dropped — incompatible with oversold RSI mean-reversion.
USE_MACD_CONFIRMATION = False

# Earnings filter: skip if earnings within N days of signal.
EARNINGS_BUFFER_DAYS = 3
USE_EARNINGS_FILTER  = True

# ── Moving averages (for signal display / watchlist ranking) ──────────────────
MA_FAST = 20               # Fast moving average (days)
MA_SLOW = 50               # Slow moving average (days)

# ── Portfolio / position sizing ───────────────────────────────────────────────
# Confirmed best in Round 1 grid search (900 combinations, 2-year backtest):
#  12% → +131.6% return, PF 3.23, DD -3.4%  ← BEST
MAX_POSITION_PCT        = 0.12   # 12% of account equity per trade
MAX_OPEN_POSITIONS      = 5      # Circuit breaker — skip new signals if N already open
CONSECUTIVE_LOSS_LIMIT  = 3      # Pause auto-trading after N consecutive losses
                                  # (unlimited → -82.7% DD; limit=2 too conservative)

# ── Trading mode ─────────────────────────────────────────────────────────────
TRADING_MODE  = "automatic"   # "automatic" or "manual"
PAPER_TRADING = True          # True = Alpaca paper, False = live

# ── Scoring weights (morning scan ranking — must add up to 1.0) ──────────────
WEIGHT_VOLUME   = 0.35
WEIGHT_RSI      = 0.40
WEIGHT_MOMENTUM = 0.25

# ── Momentum lookback ─────────────────────────────────────────────────────────
MOMENTUM_DAYS = 5

# ── Volume baseline ───────────────────────────────────────────────────────────
VOLUME_AVG_DAYS = 20

# ── Price Filters ─────────────────────────────────────────────────────────────
PRICE_MIN           = 5.0    # Hard skip: stocks below $5 (too illiquid)
PRICE_MAX_HARD      = 200.0  # Hard skip: stocks above $200 — confirmed best in Round 7
                             # Full 2-year backtest (fixed consecutive-loss bug):
                             # $200 = 506 trades, 58.5% win, PF 1.93, +$25,041, DD 19.2%  ← BEST
                             # $250 = 555 trades, 57.7% win, PF 1.79, +$22,983, DD 24.2%  (worse)
                             # Natural floor: 12% of $5k = $600 ÷ 3 shares min = $200 anyway.
PRICE_MAX_PREFERRED = 50.0   # Soft scoring bonus for stocks in the $5–$50 range
MIN_AVG_VOLUME      = 200_000  # Hard skip: under 200K avg daily volume = illiquid

# ── Position Sizing ───────────────────────────────────────────────────────────
DAILY_PROFIT_TARGET       = 40.0  # Target $ profit per winning trade
MAX_POSITION_PCT_HARD_CAP = 0.15  # Never exceed 15% of portfolio in one position
MIN_SHARES_REQUIRED       = 3     # Skip trade if can't buy at least 3 shares
                                   # within position cap (prevents 1-share AZO traps)
MAX_HOLD_DAYS             = 60    # Force-close position after 60 calendar days
                                   # (backtest used 60-day max hold with trailing stop)

# ── Extended Universe ─────────────────────────────────────────────────────────
# Round 5 backtest confirmed S&P 500-only is best:
#   NASDAQ-100 adds only 13 new tickers and reduces P&L by $238, win rate -2.2%
#   Full NYSE+NASDAQ (5,177 tickers) produces only 18 trades vs 78 — filters reject small-caps
EXTENDED_UNIVERSE_ENABLED = False
NASDAQ100_WIKIPEDIA_URL   = "https://en.wikipedia.org/wiki/Nasdaq-100"
