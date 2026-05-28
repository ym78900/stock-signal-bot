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

            raw_positions = ib.positions()
            ib.disconnect()

            for p in raw_positions:
                ticker = p.contract.symbol
                shares = float(p.position)
                avg_cost = round(float(p.avgCost), 2)
                market_val = round(shares * avg_cost, 2)

                if shares == 0:
                    continue

                result.append({
                    "ticker":     ticker,
                    "shares":     shares,
                    "avg_cost":   avg_cost,
                    "market_val": market_val,
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
