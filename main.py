import asyncio
import logging
import os

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import scanner
import signals as sig
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
    Job 1 — runs at 4:00 PM Finnish time.
    Scans all S&P 500 stocks, saves the top 10, then posts the watchlist at 4:20 PM.
    """
    logger.info("=== Morning scan started ===")
    top_stocks = scanner.run_morning_scan()

    if not top_stocks:
        logger.warning("Morning scan returned no results.")
        return

    wl.save_watchlist(top_stocks)

    # Wait 20 minutes before posting watchlist
    logger.info("Waiting 20 minutes before posting watchlist...")
    await asyncio.sleep(20 * 60)

    await tbot.post_watchlist(bot, top_stocks)
    logger.info("=== Morning scan complete ===")


async def job_signal_check(bot):
    """
    Job 2 — runs at 11:15 PM Finnish time.
    Checks today's watchlist for RSI + MA signals, posts results, posts daily summary.
    """
    logger.info("=== Signal check started ===")

    watchlist = wl.get_watchlist()
    if not watchlist:
        logger.warning("No watchlist found for today — skipping signal check.")
        return

    # Run signal analysis on all watchlist stocks
    fired_signals = sig.run_signal_check(watchlist)

    # Filter out signals that already fired today (dedup)
    new_signals = [s for s in fired_signals if not wl.has_signal_fired(s["ticker"])]

    # Post each new signal
    for analysis in new_signals:
        await tbot.post_signal(bot, analysis)
        wl.mark_signal_fired(analysis["ticker"])

    # Post daily summary
    all_fired_tickers = wl.get_fired_signals()
    all_fired = [s for s in fired_signals if s["ticker"] in all_fired_tickers]
    await tbot.post_summary(bot, all_fired)

    logger.info(f"=== Signal check complete — {len(new_signals)} new signal(s) ===")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    load_dotenv()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or token == "your_bot_token_here":
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Open the .env file and paste your bot token from @BotFather."
        )
        return

    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    if not channel_id or channel_id == "your_channel_id_here":
        logger.error(
            "TELEGRAM_CHANNEL_ID is not set.\n"
            "Open the .env file and paste your Telegram channel ID."
        )
        return

    logger.info("Starting Stock Signal Bot...")

    # Pre-load Alpaca asset cache for fast ticker search
    logger.info("Loading asset cache from Alpaca...")
    sig.load_asset_cache()

    # Build Telegram application
    app = tbot.build_application(token)
    bot = app.bot

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    scheduler.add_job(
        job_morning_scan,
        trigger="cron",
        hour=config.MORNING_SCAN_HOUR,
        minute=config.MORNING_SCAN_MINUTE,
        args=[bot],
        id="morning_scan",
        name="Morning scan + watchlist post",
        misfire_grace_time=300,  # Allow 5 min late start
    )

    scheduler.add_job(
        job_signal_check,
        trigger="cron",
        hour=config.SIGNAL_CHECK_HOUR,
        minute=config.SIGNAL_CHECK_MINUTE,
        args=[bot],
        id="signal_check",
        name="End-of-day signal check + summary",
        misfire_grace_time=300,
    )

    scheduler.start()
    logger.info(
        f"Scheduler running. Jobs:\n"
        f"  Morning scan:   {config.MORNING_SCAN_HOUR:02d}:{config.MORNING_SCAN_MINUTE:02d} Finnish time\n"
        f"  Signal check:   {config.SIGNAL_CHECK_HOUR:02d}:{config.SIGNAL_CHECK_MINUTE:02d} Finnish time"
    )

    # ── Start Telegram bot polling ─────────────────────────────────────────────
    logger.info("Bot is running. Press Ctrl+C to stop.")
    async with app:
        await app.start()

        # Register the command menu (the "/" button in Telegram chat)
        await tbot.register_commands(bot)

        await app.updater.start_polling(drop_pending_updates=True)

        # Keep running until interrupted
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
