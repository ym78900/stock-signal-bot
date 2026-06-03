import logging
import os
import asyncio
import threading
from typing import Optional, List

logger = logging.getLogger(__name__)


def get_positions() -> Optional[List[dict]]:
    """
    Connect to IB Gateway, fetch all open positions, disconnect.
    Runs ib_insync in a separate thread with its own event loop to avoid
    conflicting with the Telegram bot's running event loop.

    Returns a list of dicts:
      [
        {
          "ticker":     "AAPL",
          "shares":     100.0,
          "avg_cost":   180.00,
          "market_val": 19250.00,
        },
        ...
      ]
    Returns None if IB Gateway is not reachable.
    """
    result = []
    error = []

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from ib_insync import IB, util
            util.logToConsole(level=logging.WARNING)

            host = os.environ.get("IBKR_HOST", "127.0.0.1")
            port = int(os.environ.get("IBKR_PORT", "4001"))
            client_id = int(os.environ.get("IBKR_CLIENT_ID", "10"))

            ib = IB()
            ib.connect(host, port, clientId=client_id, timeout=5, readonly=True)

            raw_positions = ib.portfolio()
            ib.disconnect()

            for p in raw_positions:
                ticker = p.contract.symbol
                shares = float(p.position)
                avg_cost = round(float(p.averageCost), 2)
                market_price = round(float(p.marketPrice), 2)
                market_val = round(float(p.marketValue), 2)
                unrealized_pnl = round(float(p.unrealizedPNL), 2)
                realized_pnl = round(float(p.realizedPNL), 2)

                if shares == 0:
                    continue

                result.append({
                    "ticker":         ticker,
                    "shares":         shares,
                    "avg_cost":       avg_cost,
                    "market_price":   market_price,
                    "market_val":     market_val,
                    "unrealized_pnl": unrealized_pnl,
                    "realized_pnl":   realized_pnl,
                })
        except ConnectionRefusedError:
            error.append("connection_refused")
        except Exception as e:
            error.append(str(e))
        finally:
            loop.close()

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=15)

    if error:
        if error[0] == "connection_refused":
            logger.warning("IBKR: Connection refused — is IB Gateway running?")
        else:
            logger.warning(f"IBKR error: {error[0]}")
        return None

    return result


def is_connected() -> bool:
    """Quick check: can we reach IB Gateway at all? Returns True/False."""
    import socket
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4001"))
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False


def get_price(ticker: str) -> Optional[float]:
    """
    Fetch real-time price for a single ticker from IB Gateway.
    Returns None if IB Gateway is unavailable.
    """
    result = []
    error  = []

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from ib_insync import IB, Stock, util
            util.logToConsole(level=logging.WARNING)

            host      = os.environ.get("IBKR_HOST", "127.0.0.1")
            port      = int(os.environ.get("IBKR_PORT", "4001"))
            client_id = int(os.environ.get("IBKR_CLIENT_ID", "10"))

            ib = IB()
            ib.connect(host, port, clientId=client_id + 1, timeout=5, readonly=True)

            ib.reqMarketDataType(3)  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
            contract = Stock(ticker, "SMART", "USD")
            [ticker_data] = ib.reqTickers(contract)
            ib.disconnect()

            price = ticker_data.marketPrice()
            if price and price > 0:
                result.append(round(float(price), 2))
        except ConnectionRefusedError:
            error.append("connection_refused")
        except Exception as e:
            error.append(str(e))
        finally:
            loop.close()

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)

    if error or not result:
        if error and error[0] != "connection_refused":
            logger.warning(f"IBKR get_price({ticker}) error: {error[0]}")
        return None

    return result[0]
