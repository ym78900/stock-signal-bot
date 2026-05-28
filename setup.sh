#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# Stock Signal Bot — Mac/Linux Setup Script
# ─────────────────────────────────────────────────────────────────────────────

set -e

PYTHON=""
REQUIRED_MAJOR=3
REQUIRED_MINOR=9

echo ""
echo "======================================"
echo "  Stock Signal Bot — Setup"
echo "======================================"
echo ""

# ── Find Python 3.9+ ──────────────────────────────────────────────────────────

CANDIDATES=(
    "/Library/Developer/CommandLineTools/usr/bin/python3.9"
    "/usr/bin/python3.9"
    "/usr/local/bin/python3.9"
    "python3.9"
    "python3"
    "python"
)

for candidate in "${CANDIDATES[@]}"; do
    if command -v "$candidate" &>/dev/null || [ -f "$candidate" ]; then
        version=$("$candidate" -c "import sys; print(sys.version_info.major, sys.version_info.minor)" 2>/dev/null)
        major=$(echo "$version" | awk '{print $1}')
        minor=$(echo "$version" | awk '{print $2}')
        if [ "$major" -eq "$REQUIRED_MAJOR" ] && [ "$minor" -ge "$REQUIRED_MINOR" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.9 or higher not found."
    echo ""
    echo "On Mac, install Xcode Command Line Tools:"
    echo "  xcode-select --install"
    echo ""
    echo "Or install Python 3.9 manually from https://www.python.org/downloads/"
    exit 1
fi

echo "Using Python: $PYTHON"
$PYTHON --version
echo ""

# ── Install dependencies ──────────────────────────────────────────────────────

echo "Installing dependencies from requirements.txt..."
echo ""
$PYTHON -m pip install --upgrade pip --quiet
$PYTHON -m pip install -r requirements.txt
echo ""
echo "Dependencies installed."
echo ""

# ── Create .env if missing ────────────────────────────────────────────────────

if [ -f ".env" ]; then
    echo ".env file already exists — skipping setup."
    echo "(Delete .env and re-run this script to reconfigure.)"
    echo ""
else
    echo "Let's set up your .env configuration."
    echo "Press Enter to skip any value and fill it in manually later."
    echo ""

    read -p "Telegram Bot Token (from @BotFather): " TELEGRAM_BOT_TOKEN
    read -p "Telegram Channel ID (e.g. -1001234567890): " TELEGRAM_CHANNEL_ID
    echo ""
    read -p "Alpaca API Key: " ALPACA_API_KEY
    read -p "Alpaca Secret Key: " ALPACA_SECRET_KEY
    read -p "Alpaca Base URL (press Enter for paper trading default): " ALPACA_BASE_URL
    ALPACA_BASE_URL=${ALPACA_BASE_URL:-https://paper-api.alpaca.markets/v2}
    echo ""
    echo "IB Gateway ports:"
    echo "  4001 = IB Gateway live"
    echo "  4002 = IB Gateway paper"
    echo "  7496 = TWS live"
    echo "  7497 = TWS paper"
    read -p "IBKR Port (press Enter for 4001): " IBKR_PORT
    IBKR_PORT=${IBKR_PORT:-4001}

    cat > .env <<EOF
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHANNEL_ID=${TELEGRAM_CHANNEL_ID}

# Alpaca Market Data API
ALPACA_API_KEY=${ALPACA_API_KEY}
ALPACA_SECRET_KEY=${ALPACA_SECRET_KEY}
ALPACA_BASE_URL=${ALPACA_BASE_URL}

# IBKR IB Gateway
# Port 4002 = IB Gateway paper trading
# Port 4001 = IB Gateway live trading
# Port 7497 = TWS paper trading
# Port 7496 = TWS live trading
IBKR_HOST=127.0.0.1
IBKR_PORT=${IBKR_PORT}
IBKR_CLIENT_ID=10
EOF

    echo ""
    echo ".env file created."
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "======================================"
echo "  Setup complete!"
echo "======================================"
echo ""
echo "To start the bot, run:"
echo "  $PYTHON main.py"
echo ""
