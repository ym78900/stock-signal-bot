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
import trade_logger as tlog
import trader
import reporter

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
    Job 1 — 4:00 PM Finnish / 9:00 AM ET.
    Scans all S&P 500 stocks, saves the top 50, posts watchlist at 4:20 PM.
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


async def job_execute_trades(bot):
    """
    Job 2 — 4:25 PM Finnish / 9:25 AM ET (5 min before market open).
    Execute any trades queued by last night's signal check.
    """
    logger.info("=== Execute pending trades started ===")

    if tlog.is_paused():
        msg = "Auto-trading is PAUSED — skipping execution. Use /resume to restart."
        logger.info(msg)
        await tbot.post_alert(bot, msg)
        return

    pending = tlog.load_pending_trades()
    if not pending:
        logger.info("No pending trades to execute.")
        return

    # Market-wide circuit breakers (run once for all trades)
    ok, reason = trader.run_circuit_breakers()
    if not ok:
        msg = f"Circuit breaker: {reason}\nQueued trades cancelled: {[t['ticker'] for t in pending]}"
        logger.warning(msg)
        await tbot.post_alert(bot, msg)
        tlog.clear_pending_trades()
        return

    open_count = trader.get_open_position_count()
    equity     = trader.get_account_equity()
    if equity <= 0:
        logger.error("Could not fetch account equity — aborting execution.")
        return

    placed    = []
    skipped   = []

    for trade in pending:
        ticker      = trade["ticker"]
        close_price = float(trade["close_price"])
        atr         = float(trade["atr"])
        signal_date = trade["signal_date"]

        if open_count >= config.MAX_OPEN_POSITIONS:
            skipped.append(f"{ticker} (max positions reached)")
            continue

        consec = tlog.get_consecutive_losses()
        if consec >= config.CONSECUTIVE_LOSS_LIMIT:
            msg = (f"Consecutive loss limit reached ({consec}/{config.CONSECUTIVE_LOSS_LIMIT}). "
                   f"Auto-trading PAUSED. Use /resume to restart.")
            tlog.pause_trading(msg)
            await tbot.post_alert(bot, msg)
            break

        # Per-ticker earnings check
        earn_safe, earn_date = trader.check_earnings(ticker)
        if not earn_safe:
            skipped.append(f"{ticker} (earnings {earn_date})")
            continue

        # Position sizing — ATR-based to target $40/trade, hard-capped at 15%
        from signals import calculate_position_size
        qty = calculate_position_size(
            entry_price     = close_price,
            atr             = atr,
            atr_target_mult = config.ATR_TARGET_MULTIPLIER,
            profit_target   = config.DAILY_PROFIT_TARGET,
            portfolio_value = equity,
            hard_cap_pct    = config.MAX_POSITION_PCT_HARD_CAP,
            min_shares      = config.MIN_SHARES_REQUIRED,
        )

        if qty == 0:
            logger.info(f"Skipping {ticker} @ ${close_price:.2f} — too expensive for position cap")
            skipped.append(f"{ticker} (price too high for position cap)")
            continue

        stop_price   = round(close_price - atr * config.ATR_STOP_MULTIPLIER,   2)
        target_price = round(close_price + atr * config.ATR_TARGET_MULTIPLIER, 2)

        order_id = trader.place_bracket_order(ticker, qty, stop_price, target_price)
        if order_id:
            tlog.log_order_placed(
                ticker          = ticker,
                signal_date     = signal_date,
                entry_price_est = close_price,
                stop_price      = stop_price,
                target_price    = target_price,
                qty             = qty,
                alpaca_order_id = order_id,
            )
            tlog.remove_pending_trade(ticker)
            placed.append({
                "ticker":  ticker,
                "qty":     qty,
                "stop":    stop_price,
                "target":  target_price,
            })
            open_count += 1
        else:
            skipped.append(f"{ticker} (order failed)")

    await tbot.post_execution_summary(bot, placed, skipped)
    logger.info(f"=== Execute trades complete — {len(placed)} placed, {len(skipped)} skipped ===")


async def job_signal_check(bot):
    """
    Job 3 — 11:15 PM Finnish / 4:15 PM ET (after market close).
    Runs auto-scan on all 503 S&P 500 tickers and queues any BUY signals
    for execution at next morning's open.
    """
    logger.info("=== Signal check started ===")

    # ── Auto-trading scan (all 503 tickers) ──────────────────────────────────
    if tlog.is_paused():
        logger.info("Auto-trading paused — skipping auto-scan.")
        logger.info("=== Signal check complete ===")
        return

    logger.info("Running auto-scan on all S&P 500 tickers...")
    buy_signals = scanner.run_auto_scan()

    if not buy_signals:
        logger.info("Auto-scan: no BUY signals found tonight.")
        await tbot.post_alert(bot, "Auto-scan complete — no BUY signals tonight.")
    else:
        for sig_data in buy_signals:
            tlog.queue_pending_trade(
                ticker       = sig_data["ticker"],
                signal_date  = sig_data["signal_date"],
                close_price  = sig_data["close_price"],
                atr          = sig_data["atr"],
                rsi          = sig_data["rsi"],
                volume_ratio = sig_data["volume_ratio"],
            )
        tickers = [s["ticker"] for s in buy_signals]
        await tbot.post_queued_trades(bot, buy_signals)
        logger.info(f"Queued {len(buy_signals)} trade(s): {tickers}")

    logger.info("=== Signal check complete ===")


async def job_weekly_report(bot):
    """
    Job 5 — Sunday 8:00 PM Finnish.
    Posts the weekly performance report to the Telegram channel.
    """
    logger.info("=== Weekly report started ===")
    try:
        text = reporter.build_weekly_report()
        await tbot.post_alert(bot, text)
        logger.info("=== Weekly report posted ===")
    except Exception as e:
        logger.error(f"Weekly report failed: {e}")


async def job_monitor_positions(bot):
    """
    Job 4 — every 15 min during market hours (4:30 PM – 11:00 PM Finnish).
    1. Updates real entry fill price for any newly filled entries.
    2. Checks for closed bracket legs (stop/target hit) and notifies.
    """
    open_trades = tlog.get_open_trades()
    if not open_trades:
        return

    tracked_ids = [t["alpaca_order_id"] for t in open_trades if t.get("alpaca_order_id")]

    # ── Update real entry fill prices ────────────────────────────────────────
    filled_entries = trader.get_filled_entries(tracked_ids)
    for fe in filled_entries:
        tlog.update_entry_price(
            alpaca_order_id = fe["alpaca_order_id"],
            entry_price     = fe["entry_price"],
            entry_date      = fe["entry_date"],
        )

    # ── Check for closed positions (stop or target hit) ───────────────────────
    closed = trader.get_closed_bracket_legs(tracked_ids)

    for c in closed:
        updated = tlog.mark_trade_closed(
            alpaca_order_id = c["alpaca_order_id"],
            exit_price      = c["exit_price"],
            exit_date       = c["exit_date"],
            exit_reason     = c["exit_reason"],
        )
        if updated:
            await tbot.post_trade_closed(bot, updated)


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

    logger.info("Starting Stock Signal Bot...")
    logger.info(f"Trading mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'} | {config.TRADING_MODE}")

    # Pre-load Alpaca asset cache
    logger.info("Loading asset cache from Alpaca...")
    sig.load_asset_cache()

    app = tbot.build_application(token)
    bot = app.bot

    # Register job functions so /testrun can invoke them on demand
    app.bot_data["test_jobs"] = {
        "scan":    job_morning_scan,
        "execute": job_execute_trades,
        "signal":  job_signal_check,
        "monitor": job_monitor_positions,
        "report":  job_weekly_report,
    }

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    scheduler.add_job(
        job_morning_scan, trigger="cron",
        hour=config.MORNING_SCAN_HOUR, minute=config.MORNING_SCAN_MINUTE,
        args=[bot], id="morning_scan", misfire_grace_time=300,
    )
    scheduler.add_job(
        job_execute_trades, trigger="cron",
        hour=config.EXECUTE_TRADES_HOUR, minute=config.EXECUTE_TRADES_MINUTE,
        args=[bot], id="execute_trades", misfire_grace_time=120,
    )
    scheduler.add_job(
        job_signal_check, trigger="cron",
        hour=config.SIGNAL_CHECK_HOUR, minute=config.SIGNAL_CHECK_MINUTE,
        args=[bot], id="signal_check", misfire_grace_time=300,
    )
    # Monitor positions every 15 min from 4:30 PM to 11:00 PM Finnish
    scheduler.add_job(
        job_monitor_positions, trigger="cron",
        hour="16-22", minute=f"*/{config.MONITOR_INTERVAL_MINUTES}",
        args=[bot], id="monitor_positions", misfire_grace_time=60,
    )
    # Weekly report — Sunday 8:00 PM Finnish
    scheduler.add_job(
        job_weekly_report, trigger="cron",
        day_of_week="sun", hour=20, minute=0,
        args=[bot], id="weekly_report", misfire_grace_time=600,
    )

    scheduler.start()
    logger.info(
        f"Scheduler running:\n"
        f"  Morning scan:      {config.MORNING_SCAN_HOUR:02d}:{config.MORNING_SCAN_MINUTE:02d} Finnish\n"
        f"  Execute trades:    {config.EXECUTE_TRADES_HOUR:02d}:{config.EXECUTE_TRADES_MINUTE:02d} Finnish  (9:25 AM ET)\n"
        f"  Signal check:      {config.SIGNAL_CHECK_HOUR:02d}:{config.SIGNAL_CHECK_MINUTE:02d} Finnish  (4:15 PM ET)\n"
        f"  Position monitor:  every {config.MONITOR_INTERVAL_MINUTES} min  16:30–23:00 Finnish\n"
        f"  Weekly report:     Sunday 20:00 Finnish"
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
