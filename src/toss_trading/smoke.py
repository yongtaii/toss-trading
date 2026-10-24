"""Explicit read-only live connectivity check; never run from pytest or CI."""

import argparse
import asyncio
import sys

from toss_trading.adapters.toss import BrokerError, ConfigurationError, TossQueries
from toss_trading.config import Settings


async def run(include_account: bool) -> None:
    broker = TossQueries(Settings())
    try:
        await broker.prices(["AAPL"])
        print("PASS: AAPL price query")
        if include_account:
            await broker.accounts()
            await broker.holdings()
            await broker.buying_power()
            await broker.orders(status="OPEN")
            print("PASS: accounts, holdings, USD buying power, open orders")
        print("No account data or tokens printed. No order mutations sent.")
    finally:
        await broker.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-account", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.include_account))
    except (BrokerError, ConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
