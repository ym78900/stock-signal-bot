import logging
from datetime import datetime
from typing import List

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

import config
import watchlist as wl
import signals as sig
import charts

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
WAITING_SIGNAL_TICKER = 1
WAITING_CHART_TICKER  = 2


# ── Time helpers ──────────────────────────────────────────────────────────────

def dual_time() -> str:
    """Returns current time as 'H:MM PM (Finnish) / H:MM AM (ET)'"""
    fi = datetime.now(config.TIMEZONE)
    et = datetime.now(config.TIMEZONE_ET)
    return f"{fi.strftime('%-I:%M %p')} Finnish / {et.strftime('%-I:%M %p')} ET"


def dual_date() -> str:
    """Returns current date in Finnish time."""
    return datetime.now(config.TIMEZONE).strftime("%a %b %-d")


# ── Message formatters ────────────────────────────────────────────────────────

def format_watchlist_message(stocks: List[dict]) -> str:
    n = len(stocks)
    lines = [
        f"*MORNING SCAN — {dual_date()}*",
        f"🕓 {dual_time()}",
        f"Top {n} stocks to watch today:",
        "",
    ]
    for i, s in enumerate(stocks, 1):
        momentum_sign = "+" if s["momentum_pct"] >= 0 else ""
        name = s.get("company_name", s["ticker"])
        lines.append(
            f"{i}. *{s['ticker']}* ({name})  |  "
            f"RSI: {s['rsi']}  |  "
            f"Vol: {s['volume_ratio']}x  |  "
            f"{config.MOMENTUM_DAYS}d: {momentum_sign}{s['momentum_pct']}%"
        )

    lines += ["", "Monitoring these for signals until 11:00 PM Finnish / 4:00 PM ET."]
    return "\n".join(lines)


def format_signal_message(analysis: dict) -> str:
    fast = analysis["ma_fast"]
    slow = analysis["ma_slow"]

    if analysis["ma_crossover"]:
        if analysis["crossover_dir"] == "UP":
            ma_line = f"20-day avg (${fast}) just crossed ABOVE 50-day avg (${slow}) — uptrend starting"
        else:
            ma_line = f"20-day avg (${fast}) just crossed BELOW 50-day avg (${slow}) — downtrend starting"
    else:
        if fast and slow:
            position = "above" if fast > slow else "below"
            trend    = "uptrend in place" if fast > slow else "downtrend in place"
            ma_line  = f"20-day avg (${fast}) is {position} 50-day avg (${slow}) — {trend}"
        else:
            ma_line = "MA data unavailable"

    rsi = analysis["rsi"]
    if rsi and rsi < 30:
        rsi_label = "oversold — potential buy zone"
    elif rsi and rsi > 70:
        rsi_label = "overbought — potential sell zone"
    else:
        rsi_label = "neutral"

    last_candle = analysis.get("last_candle", "unknown")
    data_note   = f"Based on closing price from {last_candle} (daily data, updates after US market close ~11 PM Finnish / 4 PM ET)"

    signal_label = analysis["signal"] if analysis["signal"] != "NONE" else "STATUS"

    return (
        f"*{analysis['ticker']} — {signal_label}*\n"
        f"\n"
        f"Price:  ${analysis['price']}\n"
        f"RSI:    {rsi}  ({rsi_label})\n"
        f"\n"
        f"MA:     {ma_line}\n"
        f"\n"
        f"Signal: {analysis['signal']}\n"
        f"Reason: {analysis['reason']}\n"
        f"\n"
        f"🕓 {dual_time()}\n"
        f"_{data_note}_"
    )


def format_summary_message(fired: List[dict]) -> str:
    lines = [
        f"*DAILY SUMMARY — {dual_date()}*",
        f"🕓 {dual_time()}",
        "",
    ]

    if not fired:
        lines.append("No signals fired today.")
    else:
        lines.append(f"Signals fired today: {len(fired)}")
        for s in fired:
            lines.append(f"  {s['signal']}  {s['ticker']} @ ${s['price']}")

    lines += ["", "Next scan: Tomorrow at 4:00 PM Finnish / 9:00 AM ET"]
    return "\n".join(lines)


# ── Inline keyboard builders ──────────────────────────────────────────────────

def _watchlist_size_keyboard() -> InlineKeyboardMarkup:
    """Buttons: [ 10 ] [ 20 ] [ 30 ] [ 40 ] [ 50 ]"""
    row = [
        InlineKeyboardButton(str(n), callback_data=f"watchlist_{n}")
        for n in [10, 20, 30, 40, 50]
    ]
    return InlineKeyboardMarkup([row])


def _stock_picker_keyboard(stocks: List[dict], prefix: str) -> InlineKeyboardMarkup:
    """
    One button per stock: "NVDA — NVIDIA Corporation"
    Plus a search button at the bottom.
    prefix is "signal" or "chart".
    """
    buttons = []
    for s in stocks:
        name  = s.get("company_name", s["ticker"])
        label = f"{s['ticker']}  —  {name}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{prefix}_{s['ticker']}")])

    buttons.append([InlineKeyboardButton("🔍 Search any stock", callback_data=f"{prefix}_search")])
    return InlineKeyboardMarkup(buttons)


# ── Channel posting ───────────────────────────────────────────────────────────

async def post_watchlist(bot: Bot, stocks: List[dict]) -> None:
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    text = format_watchlist_message(stocks)
    try:
        await bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.MARKDOWN)
        logger.info("Watchlist posted to channel.")
    except Exception as e:
        logger.error(f"Failed to post watchlist: {e}")


async def post_signal(bot: Bot, analysis: dict) -> None:
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    text = format_signal_message(analysis)
    try:
        await bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Signal posted: {analysis['signal']} {analysis['ticker']}")
    except Exception as e:
        logger.error(f"Failed to post signal: {e}")


async def post_summary(bot: Bot, fired: List[dict]) -> None:
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    text = format_summary_message(fired)
    try:
        await bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.MARKDOWN)
        logger.info("Daily summary posted to channel.")
    except Exception as e:
        logger.error(f"Failed to post summary: {e}")


# ── /watchlist command + callback ─────────────────────────────────────────────

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        await update.message.reply_text(
            "No watchlist available yet. Check back after 4:20 PM Finnish time."
        )
        return
    await update.message.reply_text(
        f"How many stocks do you want to see? (Total available: {len(stocks)})",
        reply_markup=_watchlist_size_keyboard(),
    )


async def callback_watchlist_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    n = int(query.data.split("_")[1])
    stocks = wl.get_watchlist_with_names()

    if not stocks:
        await query.edit_message_text("No watchlist available yet. Check back after 4:20 PM.")
        return

    subset = stocks[:n]
    now = datetime.now(config.TIMEZONE)
    date_str = now.strftime("%a %b %-d")

    # Build one button per stock — compact single-line label, tap to get signal
    buttons = []
    for s in subset:
        momentum_sign = "+" if s["momentum_pct"] >= 0 else ""
        rsi = s['rsi']
        # RSI zone indicator
        if rsi <= config.RSI_BUY_THRESHOLD:
            rsi_tag = f"RSI {rsi} 🟢"
        elif rsi >= config.RSI_SELL_THRESHOLD:
            rsi_tag = f"RSI {rsi} 🔴"
        else:
            rsi_tag = f"RSI {rsi}"
        label = f"{s['ticker']}  |  {rsi_tag}  |  {config.MOMENTUM_DAYS}d: {momentum_sign}{s['momentum_pct']}%"
        buttons.append([InlineKeyboardButton(label, callback_data=f"wl_stock_{s['ticker']}")])

    await query.edit_message_text(
        f"*MORNING SCAN — {date_str}*\n"
        f"Top {n} stocks — tap any to see its signal:\n"
        f"🟢 oversold (RSI < 30)  🔴 overbought (RSI > 70)  |  {config.MOMENTUM_DAYS}d = {config.MOMENTUM_DAYS}-day price change",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def callback_watchlist_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when a stock button is tapped from the watchlist view."""
    query = update.callback_query
    await query.answer()

    ticker = query.data[len("wl_stock_"):]
    await query.edit_message_text(f"Fetching signal for {ticker}...")

    df = sig.fetch_ticker_data(ticker)
    if df is None:
        await query.edit_message_text(f"Could not fetch data for {ticker}. Try again later.")
        return

    analysis = sig.analyse(ticker, df)
    await query.edit_message_text(
        format_signal_message(analysis),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /signal command + callbacks + search conversation ────────────────────────

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        await update.message.reply_text(
            "No watchlist loaded yet. Use /signal once the watchlist is available,\n"
            "or tap 🔍 Search to look up any stock directly.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Search any stock", callback_data="signal_search")
            ]]),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Pick a stock from today's watchlist, or search for any ticker:",
        reply_markup=_stock_picker_keyboard(stocks, "signal"),
    )
    return ConversationHandler.END


async def callback_signal_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    payload = query.data[len("signal_"):]   # strip "signal_" prefix

    if payload == "search":
        await query.edit_message_text("Type the ticker symbol you want to check (e.g. AAPL):")
        context.user_data["signal_search_message_id"] = query.message.message_id
        return WAITING_SIGNAL_TICKER

    # Stock button tapped — fetch and show signal
    ticker = payload
    await query.edit_message_text(f"Fetching data for {ticker}...")
    df = sig.fetch_ticker_data(ticker)
    if df is None:
        await query.edit_message_text(f"Could not fetch data for {ticker}. Try again later.")
        return ConversationHandler.END

    analysis = sig.analyse(ticker, df)
    await query.edit_message_text(format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def handle_signal_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ticker = update.message.text.strip().upper()
    await update.message.reply_text(f"Fetching data for {ticker}...")

    df = sig.fetch_ticker_data(ticker)
    if df is None:
        await update.message.reply_text(
            f"Could not find data for *{ticker}*. Check the ticker symbol and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    analysis = sig.analyse(ticker, df)
    await update.message.reply_text(format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ── /chart command + callbacks + search conversation ─────────────────────────

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        await update.message.reply_text(
            "No watchlist loaded yet.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Search any stock", callback_data="chart_search")
            ]]),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Pick a stock to see its chart:",
        reply_markup=_stock_picker_keyboard(stocks, "chart"),
    )
    return ConversationHandler.END


async def callback_chart_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    payload = query.data[len("chart_"):]

    if payload == "search":
        await query.edit_message_text("Type the ticker symbol you want a chart for (e.g. AAPL):")
        return WAITING_CHART_TICKER

    ticker = payload
    await query.edit_message_text(f"Generating chart for {ticker}...")

    df = sig.fetch_ticker_data(ticker)
    if df is None:
        await query.edit_message_text(f"Could not fetch data for {ticker}.")
        return ConversationHandler.END

    chart_path = charts.generate_chart(ticker, df)
    if chart_path is None:
        await query.edit_message_text("Chart generation failed.")
        return ConversationHandler.END

    await query.delete_message()
    with open(chart_path, "rb") as f:
        await query.get_bot().send_photo(
            chat_id=query.message.chat_id,
            photo=f,
            caption=f"*{ticker}* — Daily chart (30 days)",
            parse_mode=ParseMode.MARKDOWN,
        )
    try:
        chart_path.unlink()
    except Exception:
        pass
    return ConversationHandler.END


async def handle_chart_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ticker = update.message.text.strip().upper()
    await update.message.reply_text(f"Generating chart for {ticker}...")

    df = sig.fetch_ticker_data(ticker)
    if df is None:
        await update.message.reply_text(
            f"Could not find data for *{ticker}*. Check the ticker symbol and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    chart_path = charts.generate_chart(ticker, df)
    if chart_path is None:
        await update.message.reply_text("Chart generation failed.")
        return ConversationHandler.END

    with open(chart_path, "rb") as f:
        await update.message.reply_photo(
            photo=f,
            caption=f"*{ticker}* — Daily chart (30 days)",
            parse_mode=ParseMode.MARKDOWN,
        )
    try:
        chart_path.unlink()
    except Exception:
        pass
    return ConversationHandler.END


# ── /status command ───────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stocks = wl.get_watchlist()
    watchlist_info = (
        f"Today's watchlist: {len(stocks)} stocks loaded."
        if stocks
        else "No watchlist loaded yet today."
    )
    text = (
        f"*Bot Status*\n"
        f"Running: Yes\n"
        f"Time now: {dual_time()}\n"
        f"{watchlist_info}\n"
        f"\nDaily schedule:\n"
        f"  4:00 PM Finnish / 9:00 AM ET — Morning scan\n"
        f"  4:20 PM Finnish / 9:20 AM ET — Post watchlist\n"
        f"  11:15 PM Finnish / 4:15 PM ET — Signal check + summary\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Application builder ───────────────────────────────────────────────────────

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    # /watchlist — size picker buttons → stock buttons → signal on tap
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CallbackQueryHandler(callback_watchlist_size,  pattern=r"^watchlist_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_watchlist_stock, pattern=r"^wl_stock_"))

    # /signal — stock picker + search conversation
    signal_conv = ConversationHandler(
        entry_points=[
            CommandHandler("signal", cmd_signal),
            CallbackQueryHandler(callback_signal_pick, pattern=r"^signal_"),
        ],
        states={
            WAITING_SIGNAL_TICKER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_signal_search)
            ],
        },
        fallbacks=[CommandHandler("signal", cmd_signal)],
        per_message=False,
    )
    app.add_handler(signal_conv)

    # /chart — stock picker + search conversation
    chart_conv = ConversationHandler(
        entry_points=[
            CommandHandler("chart", cmd_chart),
            CallbackQueryHandler(callback_chart_pick, pattern=r"^chart_"),
        ],
        states={
            WAITING_CHART_TICKER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chart_search)
            ],
        },
        fallbacks=[CommandHandler("chart", cmd_chart)],
        per_message=False,
    )
    app.add_handler(chart_conv)

    # /status — no buttons
    app.add_handler(CommandHandler("status", cmd_status))

    return app


# ── Command menu registration ─────────────────────────────────────────────────

async def register_commands(bot: Bot) -> None:
    """Register the command menu that appears in the / button in Telegram."""
    try:
        await bot.set_my_commands([
            BotCommand("watchlist", "Show today's top stocks"),
            BotCommand("signal",    "Check a stock — RSI & trend"),
            BotCommand("chart",     "Get a price chart"),
            BotCommand("status",    "Bot status & schedule"),
        ])
        logger.info("Command menu registered with Telegram.")
    except Exception as e:
        logger.warning(
            f"Could not register command menu: {e}\n"
            "If you see 'no model endpoints available', go to @BotFather → "
            "your bot → Bot Settings → and disable any AI model configuration."
        )
