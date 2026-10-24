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


async def poll_once(engines: list[TradingEngine], broker: TossQueries) -> None:
    """Fetch market data once and feed every engine; each keeps its own ledger."""
    now = datetime.now(UTC)
    failures = []
    day = now.astimezone(NY).date()
    try:
        session = broker_session(await broker.market_calendar(day), day)
    except (BrokerError, ValueError, KeyError, TypeError):
        failures.append("Calendar unavailable; new entries suspended")
        session = None
        for engine in engines:
            engine.store.record_error(failures[-1])
            cached = engine.store.load()[1].session
            if cached and cached.day == day.isoformat():
                session = cached
    if session is None:
        for engine in engines:
            engine.step(datetime.now(UTC), None, [], [])
        return
    now = datetime.now(UTC)
    if not session.contains(now):
        for engine in engines:
            engine.step(now, session, [], [])
        return
    symbols = list(dict.fromkeys(s for engine in engines for s in engine.config.symbols))
    bars, quotes = [], []
    for symbol in symbols:
        try:
            bars.extend(
                await fetch_minutes(broker, symbol, session, minute_cutoff(datetime.now(UTC)))
            )
        except (BrokerError, ValueError, KeyError, TypeError):
            failures.append(f"{symbol}: candles unavailable")
    # Fetch books after candles; stale books are rejected per symbol by the engine.
    for symbol in symbols:
        try:
            quotes.append(parse_quote(symbol, await broker.orderbook(symbol)))
        except (BrokerError, ValueError, KeyError, TypeError):
            failures.append(f"{symbol}: orderbook unavailable")
    for engine in engines:
        for failure in failures:
            engine.store.record_error(failure)
        allowed = engine.config.symbols
        records = engine.step(
            datetime.now(UTC),
            session,
            [b for b in bars if b.symbol in allowed],
            [q for q in quotes if q.symbol in allowed],
            allow_entries=not failures,
        )
        for record in records:
            record = {"strategy": engine.config.strategy_name, **record}
            print(json.dumps(record, default=str, ensure_ascii=False))


async def run(args) -> None:
    settings = Settings()
    ledgers = [
        (args.config or settings.strategy_config_path, settings.paper_database_url),
    ]
    if settings.compare_config_path:
        ledgers.append((settings.compare_config_path, settings.compare_database_url))
    stores, engines = [], []
    Path("data").mkdir(exist_ok=True)
    try:
        for path, url in ledgers:
            config = load_config(path)
            if config.mode != "PAPER":
                raise ValueError("Runner permits PAPER only; LIVE has not been certified")
            stores.append(Store(url.get_secret_value(), config))
            engines.append(TradingEngine(config, stores[-1]))
        # One client: the broker keeps a single valid token per client ID.
        broker = TossQueries(settings)
        try:
            while True:
                try:
                    await poll_once(engines, broker)
                except (BrokerError, ValueError, KeyError, TypeError):
                    for store in stores:
                        store.record_error(
                            "Market data or calendar unavailable; new entries suspended"
                        )
                if args.slack:
                    webhook = settings.slack_webhook_url
                    if webhook:
                        for store in stores:
                            await dispatch(store, webhook.get_secret_value())
                if args.once:
                    return
                await asyncio.sleep(min(e.config.paper_poll_seconds for e in engines))
        finally:
            await broker.aclose()
    finally:
        for store in stores:
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
