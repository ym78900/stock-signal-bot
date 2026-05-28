import logging
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

logger = logging.getLogger(__name__)

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
    is_realtime = analysis.get("realtime", False)
    price_note  = "real-time price" if is_realtime else f"delayed price from {last_candle}"
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
    stocks = wl.get_watchlist_with_names()
    if not stocks:
        await update.message.reply_text("No watchlist available yet. Check back after 4:20 PM Finnish time.")
        return
    await update.message.reply_text(
        f"How many stocks do you want to see? (Total available: {len(stocks)})",
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
    with open(chart_path, "rb") as f:
        await query.get_bot().send_photo(
            chat_id=query.message.chat_id,
            photo=f,
            caption=f"*{payload}* — Daily chart (30 days)",
            parse_mode=ParseMode.MARKDOWN,
        )
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

    total_cost     = 0.0
    total_value    = 0.0
    total_pnl      = 0.0

    for p in positions:
        ticker    = p["ticker"]
        shares    = p["shares"]
        avg_cost  = p["avg_cost"]
        cost_basis = round(shares * avg_cost, 2)

        # Get real-time price from Alpaca
        live_price = sig.fetch_realtime_price(ticker)
        if live_price:
            current_val = round(shares * live_price, 2)
            pnl         = round(current_val - cost_basis, 2)
            pnl_pct     = round((pnl / cost_basis) * 100, 2) if cost_basis else 0.0
            pnl_sign    = "+" if pnl >= 0 else ""
            price_str   = f"${live_price}  _(live)_"
        else:
            current_val = cost_basis
            pnl         = 0.0
            pnl_pct     = 0.0
            pnl_sign    = ""
            price_str   = f"${avg_cost}  _(no live price)_"

        total_cost  += cost_basis
        total_value += current_val
        total_pnl   += pnl

        shares_str = int(shares) if shares == int(shares) else shares

        lines.append(
            f"*{ticker}*\n"
            f"  {shares_str} shares  |  avg cost ${avg_cost}\n"
            f"  Price: {price_str}\n"
            f"  P&L: {pnl_sign}${pnl}  ({pnl_sign}{pnl_pct}%)"
        )
        lines.append("")

    total_pnl_sign = "+" if total_pnl >= 0 else ""
    total_pnl_pct  = round((total_pnl / total_cost) * 100, 2) if total_cost else 0.0
    lines.append(f"*Total P&L: {total_pnl_sign}${round(total_pnl, 2)}  ({total_pnl_sign}{total_pnl_pct}%)*")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


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

    # /status
    app.add_handler(CommandHandler("status", cmd_status))

    # Text search input (for /signal and /chart search mode)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_search))

    return app


# ── Command menu registration ─────────────────────────────────────────────────

async def register_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands([
            BotCommand("watchlist",        "Show today's auto-generated top stocks"),
            BotCommand("signal",           "Check a stock — RSI & trend"),
            BotCommand("chart",            "Get a price chart"),
            BotCommand("mywatchlist",      "Manage your custom watchlist"),
            BotCommand("scanmywatchlist",  "Scan your custom watchlist for signals"),
            BotCommand("portfolio",        "Show your IBKR positions & P&L"),
            BotCommand("status",           "Bot status & schedule"),
        ])
        logger.info("Command menu registered with Telegram.")
    except Exception as e:
        logger.warning(
            f"Could not register command menu: {e}\n"
            "If you see 'no model endpoints available', go to @BotFather → "
            "your bot → Bot Settings → and disable any AI model configuration."
        )
