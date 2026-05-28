import logging
import os
from typing import Optional, List

logger = logging.getLogger(__name__)


def get_positions() -> Optional[List[dict]]:
    """
    Connect to IB Gateway, fetch all open positions, disconnect.

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
    try:
        from ib_insync import IB, util
        util.logToConsole(level=logging.WARNING)  # suppress ib_insync noise

        host = os.environ.get("IBKR_HOST", "127.0.0.1")
        port = int(os.environ.get("IBKR_PORT", "4002"))  # 4002 = IB Gateway paper, 7497 = TWS paper
        client_id = int(os.environ.get("IBKR_CLIENT_ID", "10"))

        ib = IB()
        ib.connect(host, port, clientId=client_id, timeout=5, readonly=True)

        raw_positions = ib.positions()
        ib.disconnect()

        positions = []
        for p in raw_positions:
            ticker = p.contract.symbol
            shares = float(p.position)
            avg_cost = round(float(p.avgCost), 2)
            market_val = round(shares * avg_cost, 2)

            if shares == 0:
                continue

            positions.append({
                "ticker":     ticker,
                "shares":     shares,
                "avg_cost":   avg_cost,
                "market_val": market_val,
            })

        return positions

    except ConnectionRefusedError:
        logger.warning("IBKR: Connection refused — is IB Gateway running?")
        return None
    except Exception as e:
        logger.warning(f"IBKR error: {e}")
        return None
