import pytz

# ── Timezones ─────────────────────────────────────────────────────────────────
TIMEZONE    = pytz.timezone("Europe/Helsinki")
TIMEZONE_ET = pytz.timezone("America/New_York")

# ── Scheduler times (Finnish time, 24h) ──────────────────────────────────────
MORNING_SCAN_HOUR   = 16   # 4:00 PM Finnish = 9:00 AM ET (pre-market)
MORNING_SCAN_MINUTE = 0

WATCHLIST_POST_HOUR   = 16  # 4:20 PM Finnish = 9:20 AM ET
WATCHLIST_POST_MINUTE = 20

SIGNAL_CHECK_HOUR   = 23   # 11:15 PM Finnish = 4:15 PM ET (post-close)
SIGNAL_CHECK_MINUTE = 15

# ── Stock universe ────────────────────────────────────────────────────────────
SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
TOP_N_STOCKS = 50          # How many stocks to put on the daily watchlist

# ── yfinance fetch settings ───────────────────────────────────────────────────
DATA_PERIOD   = "60d"      # 60 days of history (enough for 50-day MA)
DATA_INTERVAL = "1d"       # Daily candles

# ── Signal thresholds ─────────────────────────────────────────────────────────
RSI_BUY_THRESHOLD  = 30    # RSI below this → BUY signal
RSI_SELL_THRESHOLD = 70    # RSI above this → SELL signal
RSI_PERIOD         = 14    # Standard RSI period

MA_FAST = 20               # Fast moving average (days)
MA_SLOW = 50               # Slow moving average (days)

# ── Scoring weights (must add up to 1.0) ─────────────────────────────────────
WEIGHT_VOLUME   = 0.35     # Volume vs 20-day average
WEIGHT_RSI      = 0.40     # How close RSI is to buy/sell zone
WEIGHT_MOMENTUM = 0.25     # 5-day price momentum

# ── Momentum lookback ─────────────────────────────────────────────────────────
MOMENTUM_DAYS = 5          # % price change over last N days — change this to adjust momentum window

# ── Volume baseline ───────────────────────────────────────────────────────────
VOLUME_AVG_DAYS = 20       # Average volume over last N days
