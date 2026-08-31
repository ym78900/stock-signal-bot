"""Finviz-based fundamentals and news headlines, per ticker.

yfinance covers OHLCV/RSI/MA (already backtested — see config.py); this
module adds what yfinance doesn't: fundamentals for extra structural
filters, and news headlines, which is the input the LLM sentiment layer
needs.
"""

from typing import Optional

from finvizfinance.quote import finvizfinance


def fetch_finviz_fundamentals(ticker: str) -> Optional[dict]:
    """P/E, market cap, short float, optionable — None if the ticker isn't on Finviz."""
    try:
        stock = finvizfinance(ticker)
        fundament = stock.ticker_fundament()
    except Exception:
        return None

    return {
        "pe_ratio": fundament.get("P/E"),
        "market_cap": fundament.get("Market Cap"),
        "short_float": fundament.get("Short Float"),
        "optionable": fundament.get("Optionable"),
    }


def fetch_finviz_news(ticker: str, limit: int = 5) -> list[dict]:
    """Recent headlines for a ticker — the LLM sentiment layer reads these."""
    try:
        stock = finvizfinance(ticker)
        news_df = stock.ticker_news()
    except Exception:
        return []

    if news_df is None or news_df.empty:
        return []

    records = news_df.head(limit).to_dict(orient="records")
    return [
        {
            "title": r.get("Title"),
            "date": str(r.get("Date")) if r.get("Date") is not None else None,
            "link": r.get("Link"),
        }
        for r in records
    ]
