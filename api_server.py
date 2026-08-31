"""
Stock Signal Bot - HTTP API Server
Serves stock scan/signal data to the iOS app as JSON endpoints, on demand.
Auth: x-app-password header (validated against STOCK_API_PASSWORD env var).
"""
import math
import os
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import ai_signal as ai
import config
import signals as sig
import scanner as sc

logger = logging.getLogger(__name__)

app = FastAPI(title="Stock Signal Bot API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class PortfolioSignalsRequest(BaseModel):
    tickers: list[str]

_API_PASSWORD = os.environ.get("STOCK_API_PASSWORD", "")


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


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
