import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

CUSTOM_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "custom_watchlist.json")


def _load() -> List[dict]:
    """Load custom watchlist from disk."""
    try:
        if os.path.exists(CUSTOM_WATCHLIST_FILE):
            with open(CUSTOM_WATCHLIST_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load custom watchlist: {e}")
    return []


def _save(stocks: List[dict]) -> None:
    """Save custom watchlist to disk."""
    try:
        with open(CUSTOM_WATCHLIST_FILE, "w") as f:
            json.dump(stocks, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save custom watchlist: {e}")


def get_custom_watchlist() -> List[dict]:
    """Return the full custom watchlist."""
    return _load()


def add_stock(ticker: str, name: Optional[str] = None) -> bool:
    """
    Add a stock to the custom watchlist.
    Returns True if added, False if already exists.
    """
    stocks = _load()
    if any(s["ticker"] == ticker for s in stocks):
        return False
    stocks.append({"ticker": ticker, "name": name or ticker})
    _save(stocks)
    logger.info(f"Added {ticker} to custom watchlist.")
    return True


def remove_stock(ticker: str) -> bool:
    """
    Remove a stock from the custom watchlist.
    Returns True if removed, False if not found.
    """
    stocks = _load()
    new_stocks = [s for s in stocks if s["ticker"] != ticker]
    if len(new_stocks) == len(stocks):
        return False
    _save(new_stocks)
    logger.info(f"Removed {ticker} from custom watchlist.")
    return True
