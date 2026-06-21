import logging
import time
from datetime import datetime
from typing import List

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

import config
import watchlist as wl
import custom_watchlist as cwl
import signals as sig
import charts
import ibkr
import trade_logger as tlog
import trader
import reporter

logger = logging.getLogger(__name__)

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Tracks last invocation timestamp per (user_id, command) pair.
# Prevents accidental spam — each command has a 5-second cooldown per user.
_RATE_LIMIT_SECONDS = 5
_last_command_time: dict = {}   # { (user_id, command): timestamp }

def _is_rate_limited(user_id: int, command: str) -> bool:
    key = (user_id, command)
    now = time.monotonic()
    last = _last_command_time.get(key, 0)
    if now - last < _RATE_LIMIT_SECONDS:
        return True
    _last_command_time[key] = now
    return False

# user_data keys for tracking search mode
SIGNAL_SEARCH_MODE = "waiting_signal_search"
CHART_SEARCH_MODE  = "waiting_chart_search"
CWL_ADD_MODE       = "waiting_cwl_add_search"


# ── Time helpers ──────────────────────────────────────────────────────────────

def dual_time() -> str:
    fi = datetime.now(config.TIMEZONE)
    et = datetime.now(config.TIMEZONE_ET)
    return f"{fi.strftime('%-I:%M %p')} Finnish / {et.strftime('%-I:%M %p')} ET"


def dual_date() -> str:
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
    is_realtime  = analysis.get("realtime", False)
    price_source = analysis.get("price_source")
    if price_source == "ibkr":
        price_note = "15-min delayed price (IBKR)"
    elif price_source == "alpaca":
        price_note = "15-min delayed price (Alpaca)"
    else:
        price_note = f"delayed price from {last_candle}"
    data_note   = f"RSI & MA based on closing data from {last_candle} (updates after US market close ~11 PM Finnish / 4 PM ET)"

    signal_label = analysis["signal"] if analysis["signal"] != "NONE" else None
    company_name = analysis.get("company_name")
    ticker_part  = f"*{analysis['ticker']}*" + (f" ({company_name})" if company_name else "")
    title        = f"{ticker_part} — {signal_label}" if signal_label else ticker_part

    return (
        f"{title}\n"
        f"\n"
        f"Price:  ${analysis['price']}  _({price_note})_\n"
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
    row = [InlineKeyboardButton(str(n), callback_data=f"watchlist_{n}") for n in [10, 20, 30, 40, 50]]
    return InlineKeyboardMarkup([row])


def _stock_picker_keyboard(stocks: List[dict], prefix: str) -> InlineKeyboardMarkup:
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
    try:
        await bot.send_message(chat_id=channel_id, text=format_watchlist_message(stocks), parse_mode=ParseMode.MARKDOWN)
        logger.info("Watchlist posted to channel.")
    except Exception as e:
        logger.error(f"Failed to post watchlist: {e}")


async def post_signal(bot: Bot, analysis: dict) -> None:
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    try:
        await bot.send_message(chat_id=channel_id, text=format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Signal posted: {analysis['signal']} {analysis['ticker']}")
    except Exception as e:
        logger.error(f"Failed to post signal: {e}")


async def post_summary(bot: Bot, fired: List[dict]) -> None:
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    try:
        await bot.send_message(chat_id=channel_id, text=format_summary_message(fired), parse_mode=ParseMode.MARKDOWN)
        logger.info("Daily summary posted to channel.")
    except Exception as e:
        logger.error(f"Failed to post summary: {e}")


# ── /watchlist command + callbacks ────────────────────────────────────────────

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_rate_limited(update.effective_user.id, "watchlist"):
        return

    import scanner as sc

    args = context.args or []
    mode = args[0].lower() if args else None

    # ── /watchlist low or /watchlist high — live RSI scan ────────────────────
    if mode in ("low", "high"):
        label = "oversold — buy candidates" if mode == "low" else "overbought — extended"
        await update.message.reply_text(
            f"Scanning all S&P 500 stocks for {label}... (~60s)"
        )
        scan = sc.run_watchlist_scan(mode=mode)
        results = scan["results"]

        if not results:
            criteria = (
                f"RSI < {config.WATCHLIST_LOW_RSI_MAX} + volume ≥ {config.WATCHLIST_VOL_MIN}×"
                if mode == "low" else
                f"RSI > {config.WATCHLIST_HIGH_RSI_MIN} + volume ≥ {config.WATCHLIST_VOL_MIN}×"
            )
            await update.message.reply_text(
                f"No stocks passed the filter ({criteria}) out of "
                f"{scan['total']} scanned. Market may be neutral today."
            )
            return

        top = results[:50]
        mode_header = "OVERSOLD — Buy Candidates" if mode == "low" \
                      else "OVERBOUGHT — Extended"
        criteria_note = (
            f"RSI < {config.WATCHLIST_LOW_RSI_MAX:.0f} + Vol ≥ {config.WATCHLIST_VOL_MIN}×"
            if mode == "low" else
            f"RSI > {config.WATCHLIST_HIGH_RSI_MIN:.0f} + Vol ≥ {config.WATCHLIST_VOL_MIN}×"
        )
        lines = [
            f"*{mode_header}*",
            f"_{scan['filtered']} of {scan['total']} stocks passed filter "
            f"({criteria_note})_",
            f"_{datetime.now(config.TIMEZONE).strftime('%a %b %-d, %H:%M')}_",
            "",
        ]

        for i, s in enumerate(top, 1):
            rsi = s["rsi"]
            if rsi <= config.RSI_BUY_THRESHOLD:
                rsi_tag = f"RSI {rsi} 🟢"
            elif rsi >= config.RSI_SELL_THRESHOLD:
                rsi_tag = f"RSI {rsi} 🔴"
            else:
                rsi_tag = f"RSI {rsi}"
            mom = s["momentum_pct"]
            mom_str = f"{'+' if mom >= 0 else ''}{mom}%"
            lines.append(
                f"{i}. *{s['ticker']}*  {rsi_tag}  |  "
                f"5d: {mom_str}  |  Vol {s['volume_ratio']}×"
            )

        lines += ["", "_🟢 RSI below buy threshold   🔴 RSI overbought_",
                  "_/watchlist low · /watchlist high_"]
        text = "\n".join(lines)
        if len(text) > 4000:
            text = "\n".join(lines[:55])

        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

        top = stocks[:50]
        mode_header = "OVERSOLD — Low RSI (buy candidates)" if mode == "low" \
                      else "OVERBOUGHT — High RSI (extended)"
        lines = [f"*{mode_header}*", f"_{datetime.now(config.TIMEZONE).strftime('%a %b %-d, %H:%M')}_", ""]

        for i, s in enumerate(top, 1):
            rsi = s["rsi"]
            if rsi <= config.RSI_BUY_THRESHOLD:
                rsi_tag = f"RSI {rsi} 🟢"
            elif rsi >= config.RSI_SELL_THRESHOLD:
                rsi_tag = f"RSI {rsi} 🔴"
            else:
                rsi_tag = f"RSI {rsi}"
            mom = s["momentum_pct"]
            mom_str = f"{'+' if mom >= 0 else ''}{mom}%"
            lines.append(f"{i}. *{s['ticker']}*  {rsi_tag}  |  5d: {mom_str}  |  Vol {s['volume_ratio']}×")

        lines += ["", "_/watchlist low — oversold   /watchlist high — overbought_"]
        text = "\n".join(lines)

        # Telegram message limit is 4096 chars — split if needed
        if len(text) > 4000:
            text = "\n".join(lines[:52])  # header + 50 rows + footer

        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    # ── /watchlist (no args) — show cached daily list with size picker ────────
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        now = datetime.now(config.TIMEZONE)
        if now.hour >= 16:
            await update.message.reply_text(
                "No watchlist yet today — running scan now (~60s)..."
            )
            top_stocks = sc.run_morning_scan()
            if top_stocks:
                wl.save_watchlist(top_stocks)
                stocks = wl.get_watchlist_with_names()
            else:
                await update.message.reply_text("Scan failed. Try again later.")
                return
        else:
            await update.message.reply_text(
                "No watchlist yet — check back after 4:00 PM Finnish.\n"
                "Or run a live scan now:\n"
                "  /watchlist low  — oversold stocks (buy candidates)\n"
                "  /watchlist high — overbought stocks"
            )
            return
    await update.message.reply_text(
        f"How many stocks do you want to see? (Total available: {len(stocks)})\n"
        f"Or use /watchlist low / /watchlist high for a live RSI scan.",
        reply_markup=_watchlist_size_keyboard(),
    )


async def callback_watchlist_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    n      = int(query.data.split("_")[1])
    stocks = wl.get_watchlist_with_names()

    if not stocks:
        await query.edit_message_text("No watchlist available yet. Check back after 4:20 PM.")
        return

    subset   = stocks[:n]
    date_str = datetime.now(config.TIMEZONE).strftime("%a %b %-d")

    buttons = []
    for s in subset:
        momentum_sign = "+" if s["momentum_pct"] >= 0 else ""
        rsi = s['rsi']
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
    query  = update.callback_query
    await query.answer()
    ticker = query.data[len("wl_stock_"):]
    await query.edit_message_text(f"Fetching signal for {ticker}...")

    df = sig.fetch_ticker_data(ticker)
    if df is None:
        await query.edit_message_text(f"Could not fetch data for {ticker}. Try again later.")
        return

    analysis = sig.analyse(ticker, df)
    await query.edit_message_text(format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)


# ── /signal command + callbacks ──────────────────────────────────────────────

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_rate_limited(update.effective_user.id, "signal"):
        return
    context.user_data[SIGNAL_SEARCH_MODE] = False
    context.user_data[CHART_SEARCH_MODE]  = False
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        context.user_data[SIGNAL_SEARCH_MODE] = True
        await update.message.reply_text("No watchlist loaded yet. Type a ticker or company name to search:")
        return
    await update.message.reply_text(
        "Pick a stock from today's watchlist, or search for any ticker:",
        reply_markup=_stock_picker_keyboard(stocks, "signal"),
    )


async def callback_signal_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    payload = query.data[len("signal_"):]

    if payload == "search":
        context.user_data[SIGNAL_SEARCH_MODE] = True
        context.user_data[CHART_SEARCH_MODE]  = False
        await query.edit_message_text("Type a ticker or company name to search (e.g. AAPL or Apple):")
        return

    await query.edit_message_text(f"Fetching data for {payload}...")
    df = sig.fetch_ticker_data(payload)
    if df is None:
        await query.edit_message_text(f"Could not fetch data for {payload}. Try again later.")
        return
    analysis = sig.analyse(payload, df)
    await query.edit_message_text(format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)


# ── /chart command + callbacks ────────────────────────────────────────────────

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_rate_limited(update.effective_user.id, "chart"):
        return
    context.user_data[SIGNAL_SEARCH_MODE] = False
    context.user_data[CHART_SEARCH_MODE]  = False
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        context.user_data[CHART_SEARCH_MODE] = True
        await update.message.reply_text("No watchlist loaded yet. Type a ticker or company name to search:")
        return
    await update.message.reply_text(
        "Pick a stock to see its chart:",
        reply_markup=_stock_picker_keyboard(stocks, "chart"),
    )


async def callback_chart_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    payload = query.data[len("chart_"):]

    if payload == "search":
        context.user_data[CHART_SEARCH_MODE]  = True
        context.user_data[SIGNAL_SEARCH_MODE] = False
        await query.edit_message_text("Type a ticker or company name to search (e.g. AAPL or Apple):")
        return

    await query.edit_message_text(f"Generating chart for {payload}...")
    df = sig.fetch_ticker_data(payload)
    if df is None:
        await query.edit_message_text(f"Could not fetch data for {payload}.")
        return
    chart_path = charts.generate_chart(payload, df)
    if chart_path is None:
        await query.edit_message_text("Chart generation failed.")
        return
    await query.delete_message()
    try:
        with open(chart_path, "rb") as f:
            await query.get_bot().send_photo(
                chat_id=query.message.chat_id,
                photo=f,
                caption=f"*{payload}* — Daily chart (30 days)",
                parse_mode=ParseMode.MARKDOWN,
            )
    finally:
        try:
            chart_path.unlink()
        except Exception:
            pass


# ── Text message handler (search input for /signal and /chart) ────────────────

async def handle_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_signal = context.user_data.get(SIGNAL_SEARCH_MODE, False)
    is_chart  = context.user_data.get(CHART_SEARCH_MODE, False)
    is_cwl    = context.user_data.get(CWL_ADD_MODE, False)

    if not is_signal and not is_chart and not is_cwl:
        return  # not waiting for input — ignore

    query_text = update.message.text.strip()
    await update.message.reply_text(f"Searching for '{query_text}'...")

    results = sig.search_tickers(query_text)

    if not results:
        await update.message.reply_text(
            f"No stocks found for *{query_text}*. Try a different name or ticker.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return  # keep mode on so user can try again

    # ── Custom watchlist add flow ─────────────────────────────────────────────
    if is_cwl:
        context.user_data[CWL_ADD_MODE] = False
        if len(results) == 1:
            r     = results[0]
            added = cwl.add_stock(r["ticker"], r["name"])
            stocks = cwl.get_custom_watchlist()
            msg   = f"✅ *{r['ticker']}* ({r['name']}) added." if added else f"*{r['ticker']}* is already in your watchlist."
            await update.message.reply_text(
                msg + f"\n\n*Your Watchlist* ({len(stocks)} stocks):",
                reply_markup=_build_mywatchlist_keyboard(stocks),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            buttons = [
                [InlineKeyboardButton(f"{r['ticker']}  —  {r['name']}", callback_data=f"cwl_pick_{r['ticker']}")]
                for r in results
            ]
            # Store name lookup in user_data so we can retrieve it when button is tapped
            context.user_data["cwl_search_names"] = {r["ticker"]: r["name"] for r in results}
            await update.message.reply_text(
                "Multiple results — pick one to add:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    # ── Signal / chart search flow ────────────────────────────────────────────
    prefix = "signal" if is_signal else "chart"
    context.user_data[SIGNAL_SEARCH_MODE] = False
    context.user_data[CHART_SEARCH_MODE]  = False

    if len(results) == 1:
        ticker = results[0]["ticker"]
        if is_signal:
            await update.message.reply_text(f"Fetching signal for {ticker}...")
            df = sig.fetch_ticker_data(ticker)
            if df is None:
                await update.message.reply_text(f"Could not fetch data for {ticker}. Try again later.")
                return
            analysis = sig.analyse(ticker, df)
            await update.message.reply_text(format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"Generating chart for {ticker}...")
            df = sig.fetch_ticker_data(ticker)
            if df is None:
                await update.message.reply_text(f"Could not fetch data for {ticker}. Try again later.")
                return
            chart_path = charts.generate_chart(ticker, df)
            if chart_path is None:
                await update.message.reply_text("Chart generation failed.")
                return
            try:
                with open(chart_path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        caption=f"*{ticker}* — Daily chart (30 days)",
                        parse_mode=ParseMode.MARKDOWN,
                    )
            finally:
                try:
                    chart_path.unlink()
                except Exception:
                    pass
        return

    # Multiple results — show buttons
    buttons = [
        [InlineKeyboardButton(f"{r['ticker']}  —  {r['name']}", callback_data=f"{prefix}_{r['ticker']}")]
        for r in results
    ]
    await update.message.reply_text(
        "Multiple results found — pick one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── /mywatchlist command + callbacks ─────────────────────────────────────────

def _build_mywatchlist_keyboard(stocks: list) -> InlineKeyboardMarkup:
    buttons = []
    for s in stocks:
        name = s.get("name", s["ticker"])
        buttons.append([
            InlineKeyboardButton(f"{s['ticker']}  —  {name}", callback_data=f"cwl_signal_{s['ticker']}"),
            InlineKeyboardButton("❌", callback_data=f"cwl_remove_{s['ticker']}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Add a stock", callback_data="cwl_add")])
    return InlineKeyboardMarkup(buttons)


async def cmd_mywatchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[CWL_ADD_MODE] = False
    stocks = cwl.get_custom_watchlist()
    if not stocks:
        await update.message.reply_text(
            "Your custom watchlist is empty.\nTap below to add your first stock:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add a stock", callback_data="cwl_add")]]),
        )
        return
    await update.message.reply_text(
        f"*Your Watchlist* ({len(stocks)} stocks)\nTap a stock to see its signal, or ❌ to remove:",
        reply_markup=_build_mywatchlist_keyboard(stocks),
        parse_mode=ParseMode.MARKDOWN,
    )


async def callback_cwl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "cwl_add":
        context.user_data[CWL_ADD_MODE]       = True
        context.user_data[SIGNAL_SEARCH_MODE] = False
        context.user_data[CHART_SEARCH_MODE]  = False
        await query.edit_message_text("Type a ticker or company name to add to your watchlist:")
        return

    if data.startswith("cwl_remove_"):
        ticker = data[len("cwl_remove_"):]
        cwl.remove_stock(ticker)
        stocks = cwl.get_custom_watchlist()
        if not stocks:
            await query.edit_message_text("Your custom watchlist is now empty.\nTap below to add stocks:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add a stock", callback_data="cwl_add")]]))
        else:
            await query.edit_message_text(
                f"*Your Watchlist* ({len(stocks)} stocks)\nTap a stock to see its signal, or ❌ to remove:",
                reply_markup=_build_mywatchlist_keyboard(stocks),
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if data.startswith("cwl_signal_"):
        ticker = data[len("cwl_signal_"):]
        await query.edit_message_text(f"Fetching signal for {ticker}...")
        df = sig.fetch_ticker_data(ticker)
        if df is None:
            await query.edit_message_text(f"Could not fetch data for {ticker}. Try again later.")
            return
        analysis = sig.analyse(ticker, df)
        await query.edit_message_text(format_signal_message(analysis), parse_mode=ParseMode.MARKDOWN)
        return

    if data.startswith("cwl_pick_"):
        ticker = data[len("cwl_pick_"):]
        name   = context.user_data.get("cwl_search_names", {}).get(ticker, ticker)
        added  = cwl.add_stock(ticker, name)
        stocks = cwl.get_custom_watchlist()
        msg    = f"✅ *{ticker}* ({name}) added to your watchlist." if added else f"*{ticker}* is already in your watchlist."
        await query.edit_message_text(
            msg + f"\n\n*Your Watchlist* ({len(stocks)} stocks)\nTap a stock to see its signal, or ❌ to remove:",
            reply_markup=_build_mywatchlist_keyboard(stocks),
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /scanmywatchlist command ──────────────────────────────────────────────────

async def cmd_scanmywatchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stocks = cwl.get_custom_watchlist()
    if not stocks:
        await update.message.reply_text(
            "Your custom watchlist is empty. Use /mywatchlist to add stocks first."
        )
        return

    await update.message.reply_text(f"Scanning {len(stocks)} stocks for signals...")

    buy_signals  = []
    sell_signals = []
    none_signals = []

    for s in stocks:
        ticker = s["ticker"]
        df = sig.fetch_ticker_data(ticker)
        if df is None:
            none_signals.append(f"{ticker} — could not fetch data")
            continue
        analysis = sig.analyse(ticker, df)
        name     = analysis.get("company_name") or s.get("name", ticker)
        price    = analysis["price"]
        rsi      = analysis["rsi"]
        signal   = analysis["signal"]

        line = f"*{ticker}* ({name})  |  ${price}  |  RSI {rsi}"
        if signal == "BUY":
            buy_signals.append(line)
        elif signal == "SELL":
            sell_signals.append(line)
        else:
            none_signals.append(line)

    lines = [f"*SCAN RESULTS — {dual_date()}*", f"🕓 {dual_time()}", ""]

    if buy_signals:
        lines.append("🟢 *BUY Signals*")
        lines += buy_signals
        lines.append("")

    if sell_signals:
        lines.append("🔴 *SELL Signals*")
        lines += sell_signals
        lines.append("")

    if none_signals:
        lines.append("⚪ *No Signal*")
        lines += none_signals

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /portfolio command ────────────────────────────────────────────────────────

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Fetching your IBKR positions...")

    positions = ibkr.get_positions()

    if positions is None:
        await update.message.reply_text(
            "Could not connect to IB Gateway.\n\n"
            "Make sure IB Gateway is running and logged in on your Mac.\n"
            "Default port: 4002 (paper) or 4001 (live)."
        )
        return

    if not positions:
        await update.message.reply_text("No open positions found in your IBKR account.")
        return

    lines = [f"*Portfolio — {dual_date()}*", f"🕓 {dual_time()}", ""]

    total_value    = 0.0
    total_pnl      = 0.0

    for p in positions:
        ticker         = p["ticker"]
        shares         = p["shares"]
        avg_cost       = p["avg_cost"]
        market_price   = p["market_price"]
        market_val     = p["market_val"]
        unrealized_pnl = p["unrealized_pnl"]

        cost_basis = round(shares * avg_cost, 2)
        pnl_pct    = round((unrealized_pnl / cost_basis) * 100, 2) if cost_basis else 0.0
        pnl_sign   = "+" if unrealized_pnl >= 0 else ""

        total_value += market_val
        total_pnl   += unrealized_pnl

        shares_str = int(shares) if shares == int(shares) else shares

        lines.append(
            f"*{ticker}*\n"
            f"  {shares_str} shares  |  avg cost ${avg_cost}\n"
            f"  Price: ${market_price}  _(from IBKR)_\n"
            f"  P&L: {pnl_sign}${unrealized_pnl}  ({pnl_sign}{pnl_pct}%)"
        )
        lines.append("")

    total_pnl       = round(total_pnl, 2)
    total_pnl_sign  = "+" if total_pnl >= 0 else ""
    total_cost_all  = round(total_value - total_pnl, 2)
    total_pnl_pct   = round((total_pnl / total_cost_all) * 100, 2) if total_cost_all else 0.0
    lines.append(f"*Total P&L: {total_pnl_sign}${total_pnl}  ({total_pnl_sign}{total_pnl_pct}%)*")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /status command ───────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stocks = wl.get_watchlist()
    watchlist_info = (
        f"Today's watchlist: {len(stocks)} stocks loaded."
        if stocks else "No watchlist loaded yet today."
    )
    stats   = tlog.get_stats()
    paused  = tlog.is_paused()
    pending = tlog.load_pending_trades()
    text = (
        f"*Bot Status — {dual_date()}*\n"
        f"Time: {dual_time()}\n"
        f"{watchlist_info}\n"
        f"\n*Auto-Trading*\n"
        f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}\n"
        f"Status: {'PAUSED' if paused else 'ACTIVE'}\n"
        f"Trades today: {stats['total_trades']}  Win rate: {stats['win_rate_pct']}%\n"
        f"Total P&L: ${stats['total_net_pnl']:+,.2f}\n"
        f"Consecutive losses: {stats['consecutive_losses']}/{config.CONSECUTIVE_LOSS_LIMIT}\n"
        f"Pending trades: {len(pending)}\n"
        f"\n*Schedule (Finnish time)*\n"
        f"  4:00 PM — Morning scan\n"
        f"  4:25 PM — Execute pending trades  (9:25 AM ET)\n"
        f"  11:15 PM — Signal check + auto-scan  (4:15 PM ET)\n"
        f"  Every 15 min — Position monitor  (market hours)\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick connectivity check: Alpaca, yfinance, IBKR, pending queue."""
    lines = [f"*Health Check — {dual_date()} {dual_time()}*\n"]

    # Alpaca
    try:
        from alpaca.trading.client import TradingClient
        import os
        tc = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
        acct = tc.get_account()
        lines.append(f"Alpaca: OK  (equity ${float(acct.equity):,.2f})")
    except Exception as e:
        lines.append(f"Alpaca: FAIL — {e}")

    # yfinance (quick single-ticker check)
    try:
        import yfinance as yf
        df = yf.download("SPY", period="2d", interval="1d", progress=False, auto_adjust=True)
        lines.append(f"yfinance: OK  (SPY rows={len(df)})")
    except Exception as e:
        lines.append(f"yfinance: FAIL — {e}")

    # IBKR (non-blocking)
    try:
        import ibkr as ibkr_module
        connected = ibkr_module.is_connected()
        lines.append(f"IBKR: {'Connected' if connected else 'Not connected (expected in paper mode)'}")
    except Exception as e:
        lines.append(f"IBKR: N/A — {e}")

    # Pending queue
    pending = tlog.load_pending_trades()
    paused  = tlog.is_paused()
    lines.append(f"\nPending trades: {len(pending)}")
    lines.append(f"Trading: {'PAUSED' if paused else 'ACTIVE'}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def post_alert(bot: Bot, text: str) -> None:
    """Post a plain alert message to the channel."""
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    try:
        await bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to post alert: {e}")


async def post_queued_trades(bot: Bot, signals: list) -> None:
    """Post the list of trades queued for tomorrow's open."""
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    lines = [
        f"*AUTO-SCAN — {dual_date()}*",
        f"🕓 {dual_time()}",
        f"{len(signals)} BUY signal(s) queued for tomorrow's open:\n",
    ]
    for s in signals:
        lines.append(
            f"*{s['ticker']}*  RSI {s['rsi']}  Vol {s['volume_ratio']}×  "
            f"SL≈${s['stop_est']}  TP≈${s['target_est']}"
        )
    lines += ["", "_Orders placed at 9:25 AM ET — circuit breakers re-checked at execution._"]
    try:
        await bot.send_message(chat_id=channel_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to post queued trades: {e}")


async def post_execution_summary(bot: Bot, placed: list, skipped: list) -> None:
    """Post confirmation of orders placed at 9:25 AM ET."""
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    lines = [
        f"*ORDER EXECUTION — {dual_date()}*",
        f"🕓 {dual_time()}",
        "",
    ]
    if placed:
        lines.append(f"*Placed {len(placed)} order(s):*")
        for p in placed:
            fill_str   = f"  filled @ ${p['fill']}" if p.get('fill') else ""
            target_str = f"trail" if p.get('target') == "trailing" else f"TP=${p.get('target','?')}"
            lines.append(
                f"  *{p['ticker']}*  qty={p['qty']}{fill_str}  "
                f"SL=${p['stop']}  {target_str}"
            )
    else:
        lines.append("No orders placed.")
    if skipped:
        lines.append(f"\n*Skipped {len(skipped)}:*")
        for s in skipped:
            lines.append(f"  {s}")
    try:
        await bot.send_message(chat_id=channel_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to post execution summary: {e}")


async def post_trade_closed(bot: Bot, trade: dict) -> None:
    """Post notification when a position closes (stop or target hit)."""
    import os
    channel_id = os.environ["TELEGRAM_CHANNEL_ID"]
    pnl     = float(trade.get("net_pnl", 0))
    pct     = float(trade.get("pnl_pct", 0))
    reason  = trade.get("exit_reason", "closed")
    ticker  = trade["ticker"]
    sign    = "+" if pnl >= 0 else ""
    emoji   = "✅" if pnl > 0 else "❌"
    reason_label = {
        "take_profit": "Target hit",
        "stop_loss":   "Stop hit",
    }.get(reason, "Closed")
    lines = [
        f"{emoji} *{ticker}* — {reason_label}",
        f"Entry: ${trade.get('entry_price')}  Exit: ${trade.get('exit_price')}",
        f"P&L: {sign}${pnl}  ({sign}{pct}%)",
        f"🕓 {dual_time()}",
    ]
    try:
        await bot.send_message(chat_id=channel_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to post trade closed: {e}")


# ── /positions command ────────────────────────────────────────────────────────

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Fetching open positions from Alpaca...")
    positions = trader.get_open_positions()
    if not positions:
        await update.message.reply_text("No open positions in Alpaca right now.")
        return
    equity = trader.get_account_equity()
    lines  = [
        f"*Open Positions — {dual_date()}*",
        f"Account equity: ${equity:,.2f}",
        "",
    ]
    for p in positions:
        sign = "+" if p["unrealized_pl"] >= 0 else ""
        lines.append(
            f"*{p['ticker']}*  {p['qty']} shares\n"
            f"  Entry: ${p['avg_entry']}  Now: ${p['current_price']}\n"
            f"  P&L: {sign}${p['unrealized_pl']}  ({sign}{p['unrealized_pct']}%)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /trades command ───────────────────────────────────────────────────────────

async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats  = tlog.get_stats()
    trades = tlog.get_all_trades(n=10)
    closed = [t for t in trades if t["status"] == "closed"]

    lines = [
        f"*Trade History — {dual_date()}*",
        f"Total: {stats['total_trades']}  Win rate: {stats['win_rate_pct']}%",
        f"Total P&L: ${stats['total_net_pnl']:+,.2f}  ({stats['total_return_pct']:+.1f}%)",
        f"Consecutive losses: {stats['consecutive_losses']}/{config.CONSECUTIVE_LOSS_LIMIT}",
        "",
    ]
    if not closed:
        lines.append("No closed trades yet.")
    else:
        lines.append("*Last 10 closed trades:*")
        for t in reversed(closed[-10:]):
            pnl  = float(t.get("net_pnl", 0))
            sign = "+" if pnl >= 0 else ""
            icon = "✅" if pnl > 0 else "❌"
            lines.append(
                f"{icon} {t['ticker']}  {sign}${pnl}  "
                f"({t.get('exit_reason','?')})  {t.get('exit_date','')}"
            )
    open_trades = tlog.get_open_trades()
    if open_trades:
        lines += ["", f"*{len(open_trades)} open:* " + ", ".join(t["ticker"] for t in open_trades)]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /pause and /resume commands ───────────────────────────────────────────────

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reason = " ".join(context.args) if context.args else "Manual pause via /pause"
    tlog.pause_trading(reason)
    await update.message.reply_text(
        f"Auto-trading PAUSED.\nReason: {reason}\n\nUse /resume to restart."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tlog.resume_trading()
    stats = tlog.get_stats()
    await update.message.reply_text(
        f"Auto-trading RESUMED.\n"
        f"Consecutive losses reset check: {stats['consecutive_losses']}/{config.CONSECUTIVE_LOSS_LIMIT}"
    )


# ── /stopall command (emergency) ──────────────────────────────────────────────

async def cmd_stopall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "EMERGENCY STOP requested.\n\nThis will:\n"
        "1. Cancel all open orders\n2. Liquidate all positions at market\n3. Pause auto-trading\n\n"
        "Confirm with /stopall confirm",
    )
    context.user_data["stopall_pending"] = True


async def cmd_stopall_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0] == "confirm":
        tlog.pause_trading("Emergency stop executed via /stopall confirm")
        n_cancelled = trader.cancel_all_orders()
        trader.liquidate_all_positions()
        await update.message.reply_text(
            f"Emergency stop complete.\n"
            f"Cancelled {n_cancelled} order(s). All positions liquidated.\n"
            f"Auto-trading PAUSED. Use /resume when ready."
        )
    else:
        await update.message.reply_text(
            "Type /stopall confirm to execute emergency stop."
        )


# ── /report command ───────────────────────────────────────────────────────────

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /report         — this week's report
    /report all     — full inception-to-date report
    """
    args = context.args or []
    if args and args[0].lower() == "all":
        text = reporter.build_inception_report()
    else:
        text = reporter.build_weekly_report()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /testrun command ──────────────────────────────────────────────────────────

async def cmd_testrun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /testrun               — show available jobs
    /testrun scan          — morning scan (fetches data + posts watchlist)
    /testrun execute       — execute pending trades
    /testrun signal        — signal check + auto-scan (queues trades)
    /testrun monitor       — check for closed positions
    /testrun report        — weekly report
    """
    jobs = context.bot_data.get("test_jobs", {})
    args = context.args or []

    valid = ["scan", "execute", "signal", "monitor", "report"]

    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            "*Available /testrun jobs:*\n"
            "  `/testrun scan`     — Morning scan (fetch + rank all S\\&P 500)\n"
            "  `/testrun signal`   — Signal check \\+ auto\\-scan \\(queues BUY signals\\)\n"
            "  `/testrun execute`  — Execute pending trades on Alpaca\n"
            "  `/testrun monitor`  — Check for closed positions\n"
            "  `/testrun report`   — Post weekly performance report",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    job_key = args[0].lower()
    fn = jobs.get(job_key)
    if fn is None:
        await update.message.reply_text(
            "Job runner not registered. Make sure the bot was started via main.py."
        )
        return

    await update.message.reply_text(f"Running job: *{job_key}* — this may take a minute...", parse_mode=ParseMode.MARKDOWN)
    try:
        await fn(context.bot)
        await update.message.reply_text(f"Job *{job_key}* complete.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"/testrun {job_key} failed: {e}", exc_info=True)
        await update.message.reply_text(f"Job *{job_key}* failed: `{e}`", parse_mode=ParseMode.MARKDOWN)


# ── Application builder ───────────────────────────────────────────────────────

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    # /watchlist
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CallbackQueryHandler(callback_watchlist_size,  pattern=r"^watchlist_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_watchlist_stock, pattern=r"^wl_stock_"))

    # /signal
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CallbackQueryHandler(callback_signal_pick, pattern=r"^signal_"))

    # /chart
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CallbackQueryHandler(callback_chart_pick, pattern=r"^chart_"))

    # /mywatchlist + /scanmywatchlist
    app.add_handler(CommandHandler("mywatchlist",     cmd_mywatchlist))
    app.add_handler(CommandHandler("scanmywatchlist", cmd_scanmywatchlist))
    app.add_handler(CallbackQueryHandler(callback_cwl, pattern=r"^cwl_"))

    # /portfolio
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))

    # /status + /health
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", cmd_health))

    # ── Trading commands (disabled — watchlist-only mode) ────────────────────
    # app.add_handler(CommandHandler("positions", cmd_positions))
    # app.add_handler(CommandHandler("trades",    cmd_trades))
    # app.add_handler(CommandHandler("pause",     cmd_pause))
    # app.add_handler(CommandHandler("resume",    cmd_resume))
    # app.add_handler(CommandHandler("stopall",   cmd_stopall_confirm))
    # app.add_handler(CommandHandler("report",    cmd_report))
    app.add_handler(CommandHandler("testrun",   cmd_testrun))

    # Text search input (for /signal and /chart search mode)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_search))

    return app


# ── Command menu registration ─────────────────────────────────────────────────

async def register_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands([
            BotCommand("watchlist",        "Top stocks — /watchlist low or /watchlist high"),
            BotCommand("signal",           "Check a stock — RSI & trend"),
            BotCommand("chart",            "Get a price chart"),
            BotCommand("mywatchlist",      "Manage your custom watchlist"),
            BotCommand("scanmywatchlist",  "Scan your custom watchlist for signals"),
            BotCommand("status",           "Bot status & schedule"),
            BotCommand("health",           "Check connectivity"),
            BotCommand("testrun",          "Trigger a job now (testing)"),
        ])
        logger.info("Command menu registered with Telegram.")
    except Exception as e:
        logger.warning(
            f"Could not register command menu: {e}\n"
            "If you see 'no model endpoints available', go to @BotFather → "
            "your bot → Bot Settings → and disable any AI model configuration."
        )
