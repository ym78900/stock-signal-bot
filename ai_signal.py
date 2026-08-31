"""LLM news sentiment/catalyst classification.

This does NOT predict price movement — it classifies whether current
news is temporary noise or a real structural problem, to make the
existing RSI-based signal (scanner.py, signals.py) more context-aware.
The user acts on the result manually; nothing here executes trades.
"""

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

from openai import OpenAI

import config
import finviz_source

_CACHE_FILE = Path(os.environ.get("SCAN_CACHE_DIR", Path(__file__).parent)) / "ai_sentiment_cache.json"

_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = """You are a senior financial analyst and quantitative risk officer.
Today's date is {today}.

Analyze the provided recent news headlines for the given stock ticker.

Your objective:
1. Determine if recent headlines represent temporary noise vs. fundamental business impairment.
2. Score sentiment from -1.0 (extremely bearish) to +1.0 (extremely bullish).
3. Identify primary catalyst category: Earnings, Lawsuit/Regulatory, Product/FDA, Analyst Rating, Macro, or General Noise.
4. Output STRICT JSON with exactly these keys: sentiment_score (float), confidence_score (float 0-1),
   catalyst_type (string), is_permanent_damage (bool), reasoning_summary (string, 1-2 sentences).

Do not compute or restate any price, RSI, or volume figures yourself — you are not given them and must not
invent them. Base your answer only on the headlines provided."""


def _load_cache() -> dict:
    if not _CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


def analyze_sentiment(ticker: str, company_name: Optional[str], headlines: list[dict]) -> Optional[dict]:
    """Returns the sentiment/catalyst dict, or None if there's nothing to analyze."""
    if not headlines:
        return None

    today = date.today().isoformat()
    cache_key = f"{ticker}:{today}"
    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key]

    headline_lines = "\n".join(f"- {h['title']} ({h['date']})" for h in headlines if h.get("title"))
    user_prompt = f"Ticker: {ticker}\nCompany: {company_name or ticker}\n\nHeadlines:\n{headline_lines}"

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT.format(today=today)},
            {"role": "user", "content": user_prompt},
        ],
    )
    result = json.loads(resp.choices[0].message.content)

    cache[cache_key] = result
    _save_cache(cache)
    return result


def build_verdict(rsi: Optional[float], sentiment: Optional[dict]) -> dict:
    """Combine the existing RSI signal with LLM sentiment, per the AI signal engine's
    decision matrix. This overlays signals.analyse()'s rule-based `signal` field —
    it doesn't replace it."""
    if rsi is None or sentiment is None:
        return {"ai_verdict": None, "ai_reasoning": None}

    score = sentiment.get("sentiment_score")
    reasoning = sentiment.get("reasoning_summary")

    if rsi < config.RSI_BUY_THRESHOLD:
        if sentiment.get("is_permanent_damage") or (score is not None and score < -0.5):
            verdict = "AVOID"
        elif score is not None and score > 0.4:
            verdict = "STRONG BUY"
        else:
            verdict = "BUY"
    elif rsi > config.RSI_SELL_THRESHOLD:
        if score is not None and score < -0.4:
            verdict = "STRONG SELL"
        elif score is not None and score > 0.5:
            verdict = "HOLD"
        else:
            verdict = "SELL"
    else:
        verdict = "HOLD"

    return {"ai_verdict": verdict, "ai_reasoning": reasoning}


def get_ai_enrichment(ticker: str, company_name: Optional[str], rsi: Optional[float]) -> dict:
    """Fetch headlines, run sentiment analysis, and build the composite verdict.
    Never raises — returns all-None fields on any failure so it can't break the
    base RSI/MA signal it's layered on top of."""
    defaults = {
        "sentiment_score": None,
        "confidence_score": None,
        "catalyst_type": None,
        "ai_verdict": None,
        "ai_reasoning": None,
    }
    try:
        headlines = finviz_source.fetch_finviz_news(ticker)
        sentiment = analyze_sentiment(ticker, company_name, headlines)
        if sentiment is None:
            return defaults
        return {
            "sentiment_score": sentiment.get("sentiment_score"),
            "confidence_score": sentiment.get("confidence_score"),
            "catalyst_type": sentiment.get("catalyst_type"),
            **build_verdict(rsi, sentiment),
        }
    except Exception:
        return defaults
