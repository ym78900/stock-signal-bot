@echo off
setlocal enabledelayedexpansion

:: ─────────────────────────────────────────────────────────────────────────────
:: Stock Signal Bot — Windows Setup Script
:: ─────────────────────────────────────────────────────────────────────────────

echo.
echo ======================================
echo   Stock Signal Bot — Setup
echo ======================================
echo.

:: ── Find Python 3.9+ ──────────────────────────────────────────────────────────

set PYTHON=

for %%c in (python3.9 python3 python) do (
    if "!PYTHON!"=="" (
        where %%c >nul 2>&1
        if !errorlevel! == 0 (
            for /f "tokens=1,2" %%a in ('%%c -c "import sys; print(sys.version_info.major, sys.version_info.minor)" 2^>nul') do (
                if %%a==3 if %%b GEQ 9 (
                    set PYTHON=%%c
                )
            )
        )
    )
)

if "!PYTHON!"=="" (
    echo ERROR: Python 3.9 or higher not found.
    echo.
    echo Please install Python 3.9+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Using Python: !PYTHON!
!PYTHON! --version
echo.

:: ── Install dependencies ──────────────────────────────────────────────────────

echo Installing dependencies from requirements.txt...
echo.
!PYTHON! -m pip install --upgrade pip --quiet
!PYTHON! -m pip install -r requirements.txt
echo.
echo Dependencies installed.
echo.

:: ── Create .env if missing ────────────────────────────────────────────────────

if exist ".env" (
    echo .env file already exists — skipping setup.
    echo ^(Delete .env and re-run this script to reconfigure.^)
    echo.
    goto done
)

echo Let's set up your .env configuration.
echo Press Enter to skip any value and fill it in manually later.
echo.

set /p TELEGRAM_BOT_TOKEN="Telegram Bot Token (from @BotFather): "
set /p TELEGRAM_CHANNEL_ID="Telegram Channel ID (e.g. -1001234567890): "
echo.
set /p ALPACA_API_KEY="Alpaca API Key: "
set /p ALPACA_SECRET_KEY="Alpaca Secret Key: "
set /p ALPACA_BASE_URL="Alpaca Base URL (press Enter for paper trading default): "
if "!ALPACA_BASE_URL!"=="" set ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2
echo.
echo IB Gateway ports:
echo   4001 = IB Gateway live
echo   4002 = IB Gateway paper
echo   7496 = TWS live
echo   7497 = TWS paper
set /p IBKR_PORT="IBKR Port (press Enter for 4001): "
if "!IBKR_PORT!"=="" set IBKR_PORT=4001

(
echo # Telegram Bot Configuration
echo TELEGRAM_BOT_TOKEN=!TELEGRAM_BOT_TOKEN!
echo TELEGRAM_CHANNEL_ID=!TELEGRAM_CHANNEL_ID!
echo.
echo # Alpaca Market Data API
echo ALPACA_API_KEY=!ALPACA_API_KEY!
echo ALPACA_SECRET_KEY=!ALPACA_SECRET_KEY!
echo ALPACA_BASE_URL=!ALPACA_BASE_URL!
echo.
echo # IBKR IB Gateway
echo # Port 4002 = IB Gateway paper trading
echo # Port 4001 = IB Gateway live trading
echo # Port 7497 = TWS paper trading
echo # Port 7496 = TWS live trading
echo IBKR_HOST=127.0.0.1
echo IBKR_PORT=!IBKR_PORT!
echo IBKR_CLIENT_ID=10
) > .env

echo.
echo .env file created.

:done
echo.
echo ======================================
echo   Setup complete!
echo ======================================
echo.
echo To start the bot, run:
echo   !PYTHON! main.py
echo.
pause
