# Stock Signal Bot

Two things live in this repo, running on a VPS via systemd (`stock-signal-bot.service`):

1. **Swing signal API** — on-demand RSI/MA/AI-sentiment analysis served over HTTP for a companion iOS app. Does not place trades automatically; the user acts on signals manually via the app.
2. **Threshold strategy** — a separate, fully automated dip-buy + RSI + AI-sentiment-gated bot that actually places paper trades on a schedule. This is the part that trades unattended.

---

## 1. Swing signal API (manual, on-demand)

`api_server.py` (FastAPI) serves scan/signal data to an iOS app:

- `GET /watchlist` — top-scored S&P 500 stocks (RSI + volume + momentum composite)
- `GET /watchlist/low` / `/watchlist/high` — oversold / overbought filtered lists
- `GET /signal?ticker=` — RSI/MA/AI-sentiment analysis for one ticker
- `POST /portfolio/signals` (+ `/base`, `/ai`) — batch signals for held tickers
- `GET /search?q=` — ticker search

Auth via `x-app-password` header (`STOCK_API_PASSWORD` env var). Nothing on this side executes orders — it's read-only analysis, consumed manually.

Market data: Alpaca Market Data API (`market_data.py`). Previously used `yfinance`, which failed constantly in production (Yahoo's anti-bot cookie auth) — replaced entirely for price/volume/news. `yfinance` remains only for the earnings-calendar lookup (Alpaca has no equivalent endpoint), which fails open and never blocks a decision.

AI sentiment (`ai_signal.py`): sends recent headlines (Alpaca News API, Benzinga-sourced) to `gpt-4o-mini`, classifies sentiment/catalyst type/permanent-damage, cached per ticker/day.

---

## 2. Threshold strategy (automated, paper trading)

`threshold_strategy.py` + APScheduler (in `api_server.py`), running every 5 minutes during US market hours.

**Entry** — all of:
- Price down ≥5% from its 20-day high, AND RSI(14) < 40 (oversold)
- Price $5–$500, average volume ≥ 200K/day (liquidity floor)
- Not already down >45% over the last ~6 months (falling-knife/distress guard)
- No earnings within 3 days (best-effort, fails open)
- **AI check**: if Alpaca News + gpt-4o-mini flags a real Lawsuit/Regulatory risk with high confidence, the trade is blocked — see `THRESHOLD_AI_BLOCKING` in `config.py` for why blocking is restricted to that one catalyst category (backtested: unrestricted blocking on any catalyst type made results worse, blocking real winners on ordinary analyst-downgrade/macro noise)

**Exit** — adaptive, not a fixed take-profit:
- Once price clears round-trip fees + a minimum margin, the exit "arms" and trails the peak (default 2% trail) — lets winners run instead of capping them
- Independent hard stop-loss at -9%, regardless of RSI/trailing state
- RSI-overbought while armed also triggers an exit (take profit rather than risk giving it back)

State: `threshold_watchlist.json`, `threshold_positions.json`, `threshold_trades.csv`, `threshold_paused.flag` (kill switch) — separate from anything the swing API side touches.

Alerts: Telegram (`telegram_notify.py`) — buy/sell/armed/error messages plus a daily 16:05 ET heartbeat (equity, open positions, all-time stats) so silence is never ambiguous. **Outbound alerts only** — there's no interactive command bot (no `/watchlist`, `/pause` etc. via Telegram); control is via the HTTP API (`/threshold/pause`, `/threshold/resume`, `/threshold/watchlist`) or SSH.

Backtesting: `backtest_threshold.py --months N --universe {watchlist,sp500} [--ai]` — reuses the exact live decision functions (not a separate reimplementation), so results reflect what the strategy actually does.

Broker: Alpaca only. Bitget Stock+ was evaluated and rejected (no paper trading mode, smaller symbol coverage than Alpaca, KYC-gated real-money-only API) — see `threshold_strategy.py` docstring.

---

## Logging & health

- `logs/app.log` (rotating, all levels), `logs/error.log` (warnings+) — survive restarts, unlike relying on `journalctl` alone
- `GET /logs?n=200&level=app|error` — remote log tail without SSH
- `GET /threshold/status` — paused state, watchlist, open positions, all-time stats

---

## Running

Deployed as a systemd service (`stock-signal-bot.service`) on the VPS, via git pull + `pip install -r requirements.txt` + `systemctl restart stock-signal-bot`. Not run manually / not a desktop app.

`config.py` — all constants and strategy parameters, edit there only. `PAPER_TRADING = True` (Alpaca paper) for both strategies currently; no live-money trading has been enabled.

---

## Tech stack

| Component | Library |
|---|---|
| Language | Python 3.13 |
| Market data (price/volume/news) | Alpaca Market Data API |
| Earnings calendar (fail-open only) | yfinance |
| Indicators | `ta` library |
| Order execution | alpaca-py (paper) |
| AI sentiment | OpenAI (`gpt-4o-mini`) |
| Alerts | Telegram Bot API (outbound only) |
| Scheduling | APScheduler |
| Web framework | FastAPI + uvicorn |
