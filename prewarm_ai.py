#!/usr/bin/env python3
"""
Pre-warm the AI sentiment cache for portfolio tickers.

Runs daily at 11:30 UTC (7:30 AM ET) Mon-Fri via cron so the AI badge
in the iOS app resolves instantly on first open of each trading day.

For each ticker in portfolio_tickers.json:
  1. Delete today's cache entry (forces a fresh OpenAI + FinViz call)
  2. Call get_ai_enrichment() to populate a fresh result
  3. Log the outcome

Cron entry (added automatically — do not edit manually):
  30 11 * * 1-5 /home/yousef/stock-signal-bot/venv/bin/python /home/yousef/stock-signal-bot/prewarm_ai.py >> /var/log/prewarm_ai.log 2>&1
"""
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

# Run from the project directory so relative imports in ai_signal work
PROJECT_DIR = Path(__file__).parent
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

# Load .env so OPENAI_API_KEY and friends are available
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
except ImportError:
    pass

import ai_signal as ai

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [prewarm] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

TICKERS_FILE = PROJECT_DIR / "portfolio_tickers.json"
CACHE_FILE   = PROJECT_DIR / "ai_sentiment_cache.json"


def load_tickers() -> list[str]:
    if not TICKERS_FILE.exists():
        logger.warning("portfolio_tickers.json not found — nothing to pre-warm.")
        return []
    try:
        tickers = json.loads(TICKERS_FILE.read_text())
        if not isinstance(tickers, list):
            logger.error("portfolio_tickers.json is not a JSON array.")
            return []
        return [t.upper().strip() for t in tickers if isinstance(t, str) and t.strip()]
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Could not read portfolio_tickers.json: {e}")
        return []


def clear_today_cache(tickers: list[str]) -> None:
    """Remove today's cache entry for each ticker so a fresh call is forced."""
    today = date.today().isoformat()
    if not CACHE_FILE.exists():
        return
    try:
        cache = json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return

    changed = False
    for ticker in tickers:
        key = f"{ticker}:{today}"
        if key in cache:
            del cache[key]
            changed = True
            logger.info(f"Cleared stale cache for {ticker} ({today})")

    if changed:
        try:
            CACHE_FILE.write_text(json.dumps(cache))
        except OSError as e:
            logger.error(f"Could not write cache file: {e}")


def prewarm(tickers: list[str]) -> None:
    today = date.today().isoformat()
    logger.info(f"Pre-warming AI cache for {len(tickers)} tickers: {', '.join(tickers)}")

    ok = 0
    failed = 0
    for ticker in tickers:
        try:
            result = ai.get_ai_enrichment(ticker, None, None)
            verdict = result.get("ai_verdict") or "no verdict"
            score   = result.get("sentiment_score")
            score_s = f"{score:+.2f}" if score is not None else "n/a"
            logger.info(f"  {ticker}: {verdict} (sentiment {score_s})")
            ok += 1
        except Exception as e:
            logger.error(f"  {ticker}: FAILED — {e}")
            failed += 1

    logger.info(f"Done. {ok} succeeded, {failed} failed.")


if __name__ == "__main__":
    tickers = load_tickers()
    if not tickers:
        sys.exit(0)

    clear_today_cache(tickers)
    prewarm(tickers)
