"""
main.py — Watchlist-only bot.

Runs one daily job:
  16:00 Finnish (9:00 AM ET) — scan all S&P 500 stocks, rank by RSI
  buy-readiness, post the watchlist to Telegram.

No automated trading. Use /watchlist low or /watchlist high in Telegram
to pull the list on demand at any time.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import scanner
import watchlist as wl
import telegram_bot as tbot

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def job_morning_scan(bot):
    """
    16:00 Finnish / 9:00 AM ET.
    Scans all S&P 500 stocks, ranks by buy-readiness (RSI oversold first),
    saves the top 50, posts the watchlist to Telegram at 16:20.
    """
    logger.info("=== Morning scan started ===")
    top_stocks = scanner.run_morning_scan()
    if not top_stocks:
        logger.warning("Morning scan returned no results.")
        return
    wl.save_watchlist(top_stocks)
    logger.info("Waiting 20 minutes before posting watchlist...")
    await asyncio.sleep(20 * 60)
    await tbot.post_watchlist(bot, top_stocks)
    logger.info("=== Morning scan complete ===")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    load_dotenv()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or token == "your_bot_token_here":
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env")
        return

    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    if not channel_id or channel_id == "your_channel_id_here":
        logger.error("TELEGRAM_CHANNEL_ID is not set in .env")
        return

    logger.info("Starting Stock Watchlist Bot...")

    app = tbot.build_application(token)
    bot = app.bot

    # Register scan job so /testrun scan still works
    app.bot_data["test_jobs"] = {
        "scan": job_morning_scan,
    }

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        job_morning_scan, trigger="cron",
        hour=config.MORNING_SCAN_HOUR, minute=config.MORNING_SCAN_MINUTE,
        args=[bot], id="morning_scan", misfire_grace_time=300,
    )
    scheduler.start()
    logger.info(
        f"Scheduler running:\n"
        f"  Morning scan: {config.MORNING_SCAN_HOUR:02d}:{config.MORNING_SCAN_MINUTE:02d} Finnish"
        f"  (posts watchlist at {config.MORNING_SCAN_HOUR:02d}:20)"
    )

    # ── Start Telegram bot polling ─────────────────────────────────────────────
    logger.info("Bot is running. Press Ctrl+C to stop.")
    async with app:
        await app.start()
        await tbot.register_commands(bot)
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown requested.")
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
