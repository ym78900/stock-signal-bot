import logging
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd
import ta as ta_lib
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend (no display needed)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import config

logger = logging.getLogger(__name__)


def generate_chart(ticker: str, df: pd.DataFrame) -> Optional[Path]:
    """
    Generate a price chart with 20MA, 50MA, and RSI subplot.
    Saves to a temp PNG file and returns the Path.
    Returns None if generation fails.
    """
    try:
        # ── Flatten MultiIndex columns if present (newer yfinance) ────────────
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)

        # ── Column validation ─────────────────────────────────────────────────
        required = {"Close", "High", "Low", "Open", "Volume"}
        missing  = required - set(df.columns)
        if missing:
            logger.error(f"Chart generation for {ticker}: missing columns {missing}")
            return None

        if df["Close"].dropna().empty:
            logger.error(f"Chart generation for {ticker}: Close column is all NaN")
            return None

        close = df["Close"].squeeze()  # ensure it's a Series, not a DataFrame

        # ── Calculate indicators ──────────────────────────────────────────────
        ma_fast = close.rolling(window=config.MA_FAST).mean()
        ma_slow = close.rolling(window=config.MA_SLOW).mean()
        rsi     = ta_lib.momentum.RSIIndicator(close, window=config.RSI_PERIOD).rsi()

        # Use last 30 days for the chart (cleaner visual)
        plot_df = df.tail(30).copy()
        close_plot = close.tail(30)
        dates = plot_df.index

        # ── Layout: 2 subplots (price on top, RSI on bottom) ─────────────────
        fig, (ax1, ax2) = plt.subplots(
            2, 1,
            figsize=(10, 7),
            gridspec_kw={"height_ratios": [3, 1]},
            sharex=True
        )
        fig.patch.set_facecolor("#0f0f0f")
        for ax in (ax1, ax2):
            ax.set_facecolor("#1a1a1a")
            ax.tick_params(colors="#cccccc")
            ax.spines["bottom"].set_color("#444444")
            ax.spines["top"].set_color("#444444")
            ax.spines["left"].set_color("#444444")
            ax.spines["right"].set_color("#444444")

        # ── Price chart ───────────────────────────────────────────────────────
        ax1.plot(dates, close_plot, color="#4fc3f7", linewidth=1.5, label="Price")

        if ma_fast is not None:
            ax1.plot(dates, ma_fast.reindex(plot_df.index), color="#ffb74d",
                     linewidth=1.2, linestyle="--", label=f"{config.MA_FAST}MA")
        if ma_slow is not None:
            ax1.plot(dates, ma_slow.reindex(plot_df.index), color="#ef5350",
                     linewidth=1.2, linestyle="--", label=f"{config.MA_SLOW}MA")

        ax1.set_title(f"{ticker} — Daily Chart (last 30 days)", color="#ffffff", fontsize=13, pad=10)
        ax1.set_ylabel("Price (USD)", color="#cccccc")
        ax1.legend(loc="upper left", facecolor="#1a1a1a", labelcolor="#cccccc", fontsize=8)
        ax1.yaxis.label.set_color("#cccccc")

        # ── RSI chart ─────────────────────────────────────────────────────────
        if rsi is not None:
            rsi_plot = rsi.reindex(plot_df.index)
            ax2.plot(dates, rsi_plot, color="#ce93d8", linewidth=1.2, label="RSI")
            ax2.axhline(y=config.RSI_BUY_THRESHOLD,  color="#4caf50", linewidth=0.8,
                        linestyle=":", alpha=0.8)
            ax2.axhline(y=config.RSI_SELL_THRESHOLD, color="#f44336", linewidth=0.8,
                        linestyle=":", alpha=0.8)
            ax2.axhline(y=50, color="#666666", linewidth=0.5, linestyle=":")
            ax2.set_ylim(0, 100)
            ax2.set_ylabel("RSI", color="#cccccc")
            ax2.yaxis.label.set_color("#cccccc")
            ax2.text(dates[-1], config.RSI_BUY_THRESHOLD + 1,  "30", color="#4caf50",
                     fontsize=7, ha="right")
            ax2.text(dates[-1], config.RSI_SELL_THRESHOLD + 1, "70", color="#f44336",
                     fontsize=7, ha="right")

        # ── X-axis formatting ─────────────────────────────────────────────────
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %-d"))
        ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha="right", color="#cccccc")

        plt.tight_layout(pad=1.5)

        # ── Save to temp file ─────────────────────────────────────────────────
        tmp = tempfile.NamedTemporaryFile(
            suffix=".png", prefix=f"chart_{ticker}_", delete=False
        )
        plt.savefig(tmp.name, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)

        logger.info(f"Chart saved: {tmp.name}")
        return Path(tmp.name)

    except Exception as e:
        logger.error(f"Chart generation failed for {ticker}: {e}")
        plt.close("all")
        return None
