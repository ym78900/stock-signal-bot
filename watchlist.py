import json
import logging
from datetime import date
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"


def _empty_state() -> dict:
    return {
        "date":           str(date.today()),
        "watchlist":      [],   # list of stock dicts from scanner
        "fired_signals":  [],   # list of tickers that already fired a signal today
    }


def _load() -> dict:
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read watchlist file: {e}")
    return _empty_state()


def _save(state: dict) -> None:
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save watchlist file: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def save_watchlist(stocks: List[dict]) -> None:
    """Save today's top N stocks. Resets the fired signals list."""
    state = {
        "date":          str(date.today()),
        "watchlist":     stocks,
        "fired_signals": [],
    }
    _save(state)
    logger.info(f"Watchlist saved: {[s['ticker'] for s in stocks]}")


def get_watchlist() -> List[dict]:
    """
    Return today's watchlist.
    If the saved watchlist is from a previous day, returns an empty list.
    """
    state = _load()
    if state["date"] != str(date.today()):
        logger.info("Watchlist is from a previous day — returning empty.")
        return []
    return state["watchlist"]


def get_watchlist_with_names() -> List[dict]:
    """
    Return today's watchlist including company_name field.
    Falls back gracefully if company_name is missing (old watchlist.json format).
    """
    stocks = get_watchlist()
    for s in stocks:
        if "company_name" not in s:
            s["company_name"] = s["ticker"]
    return stocks
    """Record that a signal has already been sent for this ticker today."""
    state = _load()
    if ticker not in state["fired_signals"]:
        state["fired_signals"].append(ticker)
        _save(state)


def has_signal_fired(ticker: str) -> bool:
    """Return True if a signal has already been sent for this ticker today."""
    state = _load()
    return ticker in state.get("fired_signals", [])


def get_fired_signals() -> List[str]:
    """Return all tickers that have fired a signal today."""
    state = _load()
    if state["date"] != str(date.today()):
        return []
    return state.get("fired_signals", [])
