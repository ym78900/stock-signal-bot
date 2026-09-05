"""
telegram_notify.py — Minimal Telegram Bot API sender.

No prior implementation existed despite the README describing Telegram
features — this is the first real, working sender. Kept intentionally
simple (plain HTTP POST via `requests`, no python-telegram-bot dependency)
since all we need right now is outbound alerts, not interactive commands.

Env vars required (.env):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(message: str, prefix: str = "") -> bool:
    """
    Send a message to the configured Telegram chat.
    Returns True on success, False otherwise (never raises — a failed
    notification should never crash the trading loop that triggered it).
    """
    if not _enabled():
        logger.debug("Telegram not configured (missing token/chat id) — skipping send.")
        return False

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    text = f"{prefix} {message}".strip() if prefix else message

    try:
        resp = requests.post(
            _API_BASE.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Telegram send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")
        return False


def send_trade_alert(strategy: str, action: str, ticker: str, price: float, detail: str = "") -> None:
    prefix = f"[{strategy}]"
    msg = f"{action} {ticker} @ ${price:.2f}"
    if detail:
        msg += f" ({detail})"
    send(msg, prefix=prefix)


def send_error_alert(strategy: str, context: str, error: str) -> None:
    prefix = f"[{strategy}] ⚠️ ERROR"
    send(f"{context}: {error}", prefix=prefix)


def send_run_summary(strategy: str, summary: str) -> None:
    prefix = f"[{strategy}]"
    send(summary, prefix=prefix)
