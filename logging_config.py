"""
logging_config.py — Centralized structured logging for the whole app.

Fixes the original problem: errors (like yfinance's "Invalid Crumb" auth
failures) were only visible in journald, which gets pruned, with no file
history, no rotation, and no alerting — so a data source could fail silently
for days without anyone noticing (confirmed: 5 days deployed, zero trades,
thousands of buried errors).

What this gives you:
  - logs/app.log — rotating file log (10MB x 5 backups), survives restarts
  - logs/error.log — errors only, easy to grep for "what broke"
  - Console output unchanged (still visible via journalctl)
  - Noisy third-party loggers (yfinance, urllib3) turned down so real
    signal isn't buried in library-internal retries
"""

import logging
import logging.handlers
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console (systemd journal captures this)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    # Rotating file — all levels
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # Rotating file — errors only, for fast "what broke" grepping
    error_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "error.log", maxBytes=5 * 1024 * 1024, backupCount=5
    )
    error_handler.setFormatter(fmt)
    error_handler.setLevel(logging.WARNING)
    root.addHandler(error_handler)

    # Quiet down noisy third-party libraries.
    # yfinance is kept only for supplementary/low-frequency lookups (earnings
    # calendar) — its internal "Invalid Crumb" retries would otherwise flood
    # logs exactly like the original bug. CRITICAL = essentially silent;
    # our own code still logs a single clean warning when it degrades.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)

    logging.getLogger(__name__).info(
        f"Logging initialized. Writing to {LOG_DIR}/app.log and {LOG_DIR}/error.log"
    )


def tail_log(name: str = "app.log", n: int = 200) -> list:
    """Return the last n lines of a log file — used by the /logs API endpoint."""
    path = LOG_DIR / name
    if not path.exists():
        return []
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except Exception:
        return []
