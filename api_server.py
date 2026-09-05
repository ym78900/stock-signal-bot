"""
Stock Signal Bot - HTTP API Server
Serves stock scan/signal data to the iOS app as JSON endpoints, on demand.
Auth: x-app-password header (validated against STOCK_API_PASSWORD env var).

Also runs the new Threshold+RSI automated strategy on a background scheduler
(see threshold_strategy.py) — this is the part that actually places
unattended paper trades on a clock, which nothing in this codebase did
before (see logging_config.py / market_data.py docstrings for the full
context on what was broken and why).
"""
import math
import os
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import ai_signal as ai
import config
import signals as sig
import scanner as sc
import logging_config
import threshold_strategy as ts
import telegram_notify

logger = logging.getLogger(__name__)

app = FastAPI(title="Stock Signal Bot API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_scheduler = None


def _is_market_hours_now() -> bool:
    now_et = datetime.now(config.TIMEZONE_ET)
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et <= close_t


def _threshold_job():
    """Scheduler entry point — wraps run_cycle with a job-level safety net so
    one bad exception can't silently kill future scheduled runs, and so a
    failure is never silent (Telegram alert + error log)."""
    try:
        if not _is_market_hours_now():
            logger.debug("Threshold job skipped — outside US market hours.")
            return
        summary = ts.run_cycle()
        if summary.get("errors"):
            logger.warning(f"Threshold cycle finished with errors: {summary}")
    except Exception as e:
        logger.error(f"Threshold scheduled job crashed: {e}", exc_info=True)
        telegram_notify.send_error_alert(ts.STRATEGY_NAME, "scheduled job crashed", str(e))


@app.on_event("startup")
def _on_startup():
    global _scheduler
    logging_config.setup_logging()
    logger.info("API server starting up.")

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.add_job(
            _threshold_job,
            "interval",
            minutes=config.THRESHOLD_CHECK_INTERVAL_MINUTES,
            id="threshold_cycle",
            next_run_time=datetime.utcnow(),  # run once immediately on boot too
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        logger.info(
            f"Scheduler started — threshold strategy cycle every "
            f"{config.THRESHOLD_CHECK_INTERVAL_MINUTES} min during market hours."
        )
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}", exc_info=True)
        telegram_notify.send_error_alert("System", "scheduler failed to start", str(e))


@app.on_event("shutdown")
def _on_shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)


class PortfolioSignalsRequest(BaseModel):
    tickers: list[str]

class PortfolioTickersRequest(BaseModel):
    tickers: list[str]

class ThresholdWatchlistRequest(BaseModel):
    tickers: list[str]

_API_PASSWORD = os.environ.get("STOCK_API_PASSWORD", "")

_PORTFOLIO_TICKERS_FILE = Path(__file__).parent / "portfolio_tickers.json"


def _auth(x_app_password: Optional[str]) -> None:
    if not _API_PASSWORD:
        return
    if x_app_password != _API_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid app password")


def _sanitize(obj):
    """Recursively replace NaN/Inf floats with None so JSON serialization never fails."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# Bounded, not unbounded — the VM is a 1GB e2-micro, and yfinance's session/crumb
# handling degrades under heavy concurrency, so a handful of workers is the sweet
# spot between "faster than one-at-a-time" and "doesn't take the box down again".
_ENRICH_MAX_WORKERS = 4


def _enrich_top(stocks: list, n: int = 10) -> None:
    """Adds AI fields to the top N stocks in place — bounds OpenAI cost on a 50-stock scan."""
    top = stocks[:n]
    with ThreadPoolExecutor(max_workers=_ENRICH_MAX_WORKERS) as pool:
        fields = pool.map(
            lambda s: ai.get_ai_enrichment(s["ticker"], s.get("company_name"), s.get("rsi")),
            top,
        )
        for stock, ai_fields in zip(top, fields):
            stock.update(ai_fields)


@app.get("/watchlist")
def get_watchlist(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    stocks = sc.run_morning_scan()
    _enrich_top(stocks)
    scan_date = datetime.now(config.TIMEZONE).strftime("%a %b %-d")
    scan_time = datetime.now(config.TIMEZONE).strftime("%-I:%M %p")
    return _sanitize({
        "date": scan_date,
        "time": scan_time,
        "stocks": stocks,
        "count": len(stocks),
    })


@app.get("/watchlist/low")
def get_watchlist_low(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    result = sc.run_watchlist_scan(mode="low")
    _enrich_top(result["results"])
    return _sanitize({
        "mode": "low",
        "label": "Oversold - Buy Candidates",
        "criteria": f"RSI < {config.WATCHLIST_LOW_RSI_MAX:.0f} - Vol >= {config.WATCHLIST_VOL_MIN}x",
        "stocks": result["results"],
        "total": result["total"],
        "filtered": result["filtered"],
        "date": datetime.now(config.TIMEZONE).strftime("%a %b %-d"),
        "time": datetime.now(config.TIMEZONE).strftime("%-I:%M %p"),
    })


@app.get("/watchlist/high")
def get_watchlist_high(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    result = sc.run_watchlist_scan(mode="high")
    _enrich_top(result["results"])
    return _sanitize({
        "mode": "high",
        "label": "Overbought - Extended",
        "criteria": f"RSI > {config.WATCHLIST_HIGH_RSI_MIN:.0f} - Vol >= {config.WATCHLIST_VOL_MIN}x",
        "stocks": result["results"],
        "total": result["total"],
        "filtered": result["filtered"],
        "date": datetime.now(config.TIMEZONE).strftime("%a %b %-d"),
        "time": datetime.now(config.TIMEZONE).strftime("%-I:%M %p"),
    })


def _build_signal(ticker: str) -> Optional[dict]:
    """Shared by /signal and /portfolio/signals — one ticker's full RSI/MA + AI analysis."""
    ticker = ticker.upper().strip()
    df = sig.fetch_ticker_data(ticker)
    if df is None:
        return None
    analysis = sig.analyse(ticker, df)
    earnings = sig.fetch_earnings_growth(ticker)
    ai_fields = ai.get_ai_enrichment(ticker, analysis.get("company_name"), analysis.get("rsi"))
    return {
        "ticker":             analysis["ticker"],
        "company_name":       analysis.get("company_name"),
        "price":              analysis.get("price"),
        "price_source":       analysis.get("price_source"),
        "rsi":                analysis.get("rsi"),
        "ma_fast":            analysis.get("ma_fast"),
        "ma_slow":            analysis.get("ma_slow"),
        "ma_crossover":       analysis.get("ma_crossover"),
        "crossover_dir":      analysis.get("crossover_dir"),
        "signal":             analysis.get("signal", "NONE"),
        "reason":             analysis.get("reason", ""),
        "last_candle":        analysis.get("last_candle"),
        "eps_growth_pct":     earnings.get("eps_growth_pct"),
        "revenue_growth_pct": earnings.get("revenue_growth_pct"),
        "next_earnings_date": earnings.get("next_earnings_date"),
        **ai_fields,
    }


@app.get("/signal")
def get_signal(
    ticker: str = Query(...),
    x_app_password: Optional[str] = Header(default=None),
):
    _auth(x_app_password)
    result = _build_signal(ticker)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Could not fetch data for {ticker.upper().strip()}")
    return _sanitize(result)


@app.post("/portfolio/signals")
def portfolio_signals(
    body: PortfolioSignalsRequest,
    x_app_password: Optional[str] = Header(default=None),
):
    """Signals for the tickers the user actually holds — not limited to the S&P 500
    screener universe, since each is looked up individually."""
    _auth(x_app_password)
    with ThreadPoolExecutor(max_workers=_ENRICH_MAX_WORKERS) as pool:
        results = [r for r in pool.map(_build_signal, body.tickers) if r is not None]
    return _sanitize({"signals": results})


def _build_signal_base(ticker: str) -> Optional[dict]:
    """RSI/MA/price/earnings only — no AI call. Fast path for the split-request flow."""
    ticker = ticker.upper().strip()
    df = sig.fetch_ticker_data(ticker)
    if df is None:
        return None
    analysis = sig.analyse(ticker, df)
    earnings = sig.fetch_earnings_growth(ticker)
    return {
        "ticker":             analysis["ticker"],
        "company_name":       analysis.get("company_name"),
        "price":              analysis.get("price"),
        "price_source":       analysis.get("price_source"),
        "rsi":                analysis.get("rsi"),
        "ma_fast":            analysis.get("ma_fast"),
        "ma_slow":            analysis.get("ma_slow"),
        "ma_crossover":       analysis.get("ma_crossover"),
        "crossover_dir":      analysis.get("crossover_dir"),
        "signal":             analysis.get("signal", "NONE"),
        "reason":             analysis.get("reason", ""),
        "last_candle":        analysis.get("last_candle"),
        "eps_growth_pct":     earnings.get("eps_growth_pct"),
        "revenue_growth_pct": earnings.get("revenue_growth_pct"),
        "next_earnings_date": earnings.get("next_earnings_date"),
        # AI fields absent — iOS merges them in from the /ai call
        "sentiment_score":    None,
        "confidence_score":   None,
        "catalyst_type":      None,
        "ai_verdict":         None,
        "ai_reasoning":       None,
    }


def _build_signal_ai(ticker: str) -> Optional[dict]:
    """AI enrichment for the split-request flow. Fetches the ticker's RSI so the
    composite verdict (ai.build_verdict) can be produced — without an RSI the
    verdict is always None, which is why the app's AI badge never appeared.
    Returns None on any failure."""
    ticker = ticker.upper().strip()
    try:
        # Best-effort RSI/company lookup so build_verdict has what it needs.
        rsi = None
        company_name = None
        try:
            df = sig.fetch_ticker_data(ticker)
            if df is not None:
                analysis = sig.analyse(ticker, df)
                rsi = analysis.get("rsi")
                company_name = analysis.get("company_name")
        except Exception:
            pass
        fields = ai.get_ai_enrichment(ticker, company_name, rsi)
        return {"ticker": ticker, **fields}
    except Exception:
        return None


@app.post("/portfolio/signals/base")
def portfolio_signals_base(
    body: PortfolioSignalsRequest,
    x_app_password: Optional[str] = Header(default=None),
):
    """Fast base signals (RSI/MA/price) with no AI call — returns in ~1-2s."""
    _auth(x_app_password)
    with ThreadPoolExecutor(max_workers=_ENRICH_MAX_WORKERS) as pool:
        results = [r for r in pool.map(_build_signal_base, body.tickers) if r is not None]
    return _sanitize({"signals": results})


@app.post("/portfolio/signals/ai")
def portfolio_signals_ai(
    body: PortfolioSignalsRequest,
    x_app_password: Optional[str] = Header(default=None),
):
    """AI-only enrichment fields for the given tickers — hits pre-warmed cache when available."""
    _auth(x_app_password)
    with ThreadPoolExecutor(max_workers=_ENRICH_MAX_WORKERS) as pool:
        results = [r for r in pool.map(_build_signal_ai, body.tickers) if r is not None]
    return _sanitize({"signals": results})


@app.post("/portfolio/tickers")
def update_portfolio_tickers(
    body: PortfolioTickersRequest,
    x_app_password: Optional[str] = Header(default=None),
):
    """Called by the iOS app when the user's held tickers change — persists the list
    so the pre-warm cron job knows which tickers to refresh each morning."""
    _auth(x_app_password)
    try:
        tickers = [t.upper().strip() for t in body.tickers if t.strip()]
        _PORTFOLIO_TICKERS_FILE.write_text(__import__("json").dumps(sorted(set(tickers))))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not save tickers: {e}")
    return {"status": "ok", "count": len(tickers)}


@app.get("/search")
def search(
    q: str = Query(...),
    x_app_password: Optional[str] = Header(default=None),
):
    _auth(x_app_password)
    results = sig.search_tickers(q.strip(), max_results=12)
    return _sanitize({"results": results, "query": q})


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Threshold+RSI strategy endpoints ─────────────────────────────────────────

@app.get("/threshold/status")
def threshold_status(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    return _sanitize({
        "paused": ts.is_paused(),
        "enabled": config.THRESHOLD_STRATEGY_ENABLED,
        "watchlist": ts.load_watchlist(),
        "positions": ts.load_positions(),
        "stats": ts.get_stats(),
        "check_interval_minutes": config.THRESHOLD_CHECK_INTERVAL_MINUTES,
    })


@app.get("/threshold/trades")
def threshold_trades(
    n: int = Query(default=100),
    x_app_password: Optional[str] = Header(default=None),
):
    _auth(x_app_password)
    return _sanitize({"trades": ts.get_all_trades(n)})


@app.get("/threshold/watchlist")
def threshold_get_watchlist(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    return _sanitize({"tickers": ts.load_watchlist()})


@app.post("/threshold/watchlist")
def threshold_set_watchlist(
    body: ThresholdWatchlistRequest,
    x_app_password: Optional[str] = Header(default=None),
):
    _auth(x_app_password)
    tickers = [t.upper().strip() for t in body.tickers if t.strip()]
    ts.save_watchlist(tickers)
    return {"status": "ok", "tickers": tickers}


@app.post("/threshold/pause")
def threshold_pause(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    ts.pause("manual pause via API")
    return {"status": "paused"}


@app.post("/threshold/resume")
def threshold_resume(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    ts.resume()
    return {"status": "resumed"}


@app.post("/threshold/run-now")
def threshold_run_now(x_app_password: Optional[str] = Header(default=None)):
    """Manually trigger one strategy cycle immediately — used for testing/verification,
    bypasses the market-hours gate the scheduler applies."""
    _auth(x_app_password)
    try:
        summary = ts.run_cycle()
        return _sanitize({"status": "ok", "summary": summary})
    except Exception as e:
        logger.error(f"Manual threshold run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Logs (basic health visibility without SSH) ───────────────────────────────

@app.get("/logs")
def get_logs(
    n: int = Query(default=200),
    level: str = Query(default="app"),
    x_app_password: Optional[str] = Header(default=None),
):
    _auth(x_app_password)
    filename = "error.log" if level == "error" else "app.log"
    return {"lines": logging_config.tail_log(filename, n)}


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    logging_config.setup_logging()
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
