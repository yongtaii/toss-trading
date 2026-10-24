"""Download selected-symbol minute candles from the documented official REST API."""

import argparse
import asyncio
import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from toss_trading.adapters.toss import TossQueries
from toss_trading.config import Settings
from toss_trading.marketdata import broker_session, fetch_minutes
from toss_trading.trading_config import load_config


async def download(args) -> None:
    settings = Settings()
    config = load_config(args.config)
    if args.start > args.end:
        raise ValueError("start must precede end")
    broker = TossQueries(settings)
    rows = []
    try:
        day = args.start
        while day <= args.end:
            session = broker_session(await broker.market_calendar(day), day)
            if session:
                for symbol in config.symbols:
                    rows.extend(await fetch_minutes(broker, symbol, session, datetime.now(UTC)))
            day += timedelta(days=1)
    finally:
        await broker.aclose()
    if not rows:
        raise ValueError("No historical bars returned; check API retention/access")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: an existing reviewed dataset is not silently overwritten.
    with output.open("x", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "end", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(
            b.model_dump(mode="json") for b in sorted(rows, key=lambda b: (b.end, b.symbol))
        )
    print(f"Downloaded {len(rows)} minute candles to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--config", default="config/strategy.toml")
    parser.add_argument("--output", required=True)
    asyncio.run(download(parser.parse_args()))


if __name__ == "__main__":
    main()
