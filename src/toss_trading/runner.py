import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from toss_trading.adapters.toss import BrokerError, TossQueries
from toss_trading.config import Settings
from toss_trading.engine import TradingEngine
from toss_trading.marketdata import (
    NY,
    broker_session,
    fetch_minutes,
    minute_cutoff,
    parse_quote,
)
from toss_trading.notifications import dispatch
from toss_trading.persistence import Store
from toss_trading.trading_config import load_config


async def poll_once(engine: TradingEngine, broker: TossQueries) -> None:
    now = datetime.now(UTC)
    failures = []
    day = now.astimezone(NY).date()
    try:
        session = broker_session(await broker.market_calendar(day), day)
    except (BrokerError, ValueError, KeyError, TypeError):
        failures.append("Calendar unavailable; new entries suspended")
        engine.store.record_error(failures[-1])
        cached = engine.store.load()[1].session
        session = cached if cached and cached.day == day.isoformat() else None
    if session is None:
        engine.step(datetime.now(UTC), None, [], [])
        return
    now = datetime.now(UTC)
    if not session.contains(now):
        engine.step(now, session, [], [])
        return
    bars, quotes = [], []
    for symbol in engine.config.symbols:
        try:
            bars.extend(
                await fetch_minutes(broker, symbol, session, minute_cutoff(datetime.now(UTC)))
            )
        except (BrokerError, ValueError, KeyError, TypeError):
            failures.append(f"{symbol}: candles unavailable")
    # Fetch books after candles; stale books are rejected per symbol by the engine.
    for symbol in engine.config.symbols:
        try:
            quotes.append(parse_quote(symbol, await broker.orderbook(symbol)))
        except (BrokerError, ValueError, KeyError, TypeError):
            failures.append(f"{symbol}: orderbook unavailable")
    for failure in failures:
        engine.store.record_error(failure)
    records = engine.step(datetime.now(UTC), session, bars, quotes, allow_entries=not failures)
    for record in records:
        print(json.dumps(record, default=str, ensure_ascii=False))


async def run(args) -> None:
    settings = Settings()
    config = load_config(args.config or settings.strategy_config_path)
    if config.mode != "PAPER":
        raise ValueError("Runner permits PAPER only; LIVE has not been certified")
    Path("data").mkdir(exist_ok=True)
    store = Store(settings.paper_database_url.get_secret_value(), config)
    engine = TradingEngine(config, store)
    broker = TossQueries(settings)
    try:
        while True:
            try:
                await poll_once(engine, broker)
            except (BrokerError, ValueError, KeyError, TypeError):
                store.record_error("Market data or calendar unavailable; new entries suspended")
            if args.slack:
                webhook = settings.slack_webhook_url
                if webhook:
                    await dispatch(store, webhook.get_secret_value())
            if args.once:
                return
            await asyncio.sleep(config.paper_poll_seconds)
    finally:
        await broker.aclose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-data PAPER runner; no live orders")
    parser.add_argument("--config")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--slack", action="store_true", help="Explicitly enable Slack outbox delivery"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
