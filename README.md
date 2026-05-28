# Stock Signal Bot

A personal trading assistant that runs on my computer and sends stock alerts to a private Telegram channel — automatically, every day.

---

## What it does

Every trading day it scans all **500 of the largest US companies** (the S&P 500 — think Apple, Tesla, NVIDIA, Microsoft...) and finds the ones most likely to make a significant move.

It then watches those stocks and sends me a message on Telegram when a BUY or SELL signal fires.

---

## How it works, step by step

**Step 1 — Before market opens (4:20 PM Finnish time)**

The bot scores all 500 stocks and picks the top 10 most interesting ones for the day.

It scores each stock on:
- Is trading volume unusually high today? (people are paying attention)
- Is the RSI near an extreme? (stock may be oversold or overbought)
- Has the price moved significantly in the last 5 days? (momentum)

It posts a morning watchlist to the Telegram channel like this:

```
MORNING SCAN — Wed May 27

Top 10 stocks to watch today:

1. NVDA  RSI: 27 | Vol: 3.1x avg | Momentum: +5.2%
2. TSLA  RSI: 72 | Vol: 2.8x avg | Momentum: -3.8%
3. META  RSI: 31 | Vol: 2.2x avg | Momentum: +2.1%
...

Monitoring these for signals until 11:00 PM.
```

---

**Step 2 — After market closes (11:15 PM Finnish time)**

Once the trading day is over and all price data is finalised, it checks those 10 stocks for signals using two indicators:

**RSI (Relative Strength Index)**
A number from 0–100 that tells you if a stock has been bought or sold too aggressively.
- Below 30 → oversold → potential BUY opportunity
- Above 70 → overbought → potential SELL opportunity

**Moving Average Crossover**
Compares the 20-day average price vs the 50-day average price.
- 20-day crosses above 50-day → upward trend starting → BUY confirmation
- 20-day crosses below 50-day → downward trend starting → SELL confirmation

A signal only fires when **both agree** — this cuts out a lot of noise.

When a signal fires, the channel gets a message like this:

```
SIGNAL — BUY
Stock:  NVDA
Price:  $875.20
RSI:    27.4 (oversold)
MA:     20MA crossed above 50MA
Time:   11:15 PM Finnish time

Reason: Strong oversold signal with MA confirmation
```

---

**Step 3 — Daily summary**

At the end of each day it posts a recap of everything that happened:

```
DAILY SUMMARY — Wed May 27

Signals fired today: 2
  BUY  NVDA @ 875.20  (11:15 PM)
  SELL TSLA @ 218.40  (11:15 PM)

Next scan: Tomorrow at 4:00 PM
```

---

## You can also ask it things directly

The bot responds to commands:

| Command | What happens |
|---|---|
| `/watchlist` | Shows today's top 10 stocks |
| `/signal NVDA` | Shows live RSI and moving average status for any stock |
| `/chart NVDA` | Sends a price chart with all indicators as an image |
| `/status` | Shows whether the bot is running and when the next scan is |

---

## Important note

This bot is **signals only** — it tells me what looks interesting, I decide whether to act. No money is moved automatically. I place trades manually through my broker (IBKR) if I like what I see.

---

## Tech behind it

- Built in Python, runs on my Mac
- Stock data from Yahoo Finance (free, public)
- Signals delivered via Telegram
- Scans all 500 S&P 500 companies every day
- No subscriptions, no paid APIs
