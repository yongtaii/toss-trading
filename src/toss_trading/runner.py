import argparse
import asyncio
import json
from contextlib import suppress
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
from toss_trading.monitoring import WorkerMonitor
from toss_trading.notifications import dispatch
from toss_trading.persistence import Store
from toss_trading.risk import quote_checks
from toss_trading.trading_config import load_config


async def poll_once(
    engines: list[TradingEngine], broker: TossQueries, monitor: WorkerMonitor | None = None
) -> None:
    """Fetch market data once and feed every engine; each keeps its own ledger."""
    now = datetime.now(UTC)
    failures = []
    monitor = monitor or WorkerMonitor([])
    monitor.phase("calendar")
    day = now.astimezone(NY).date()
    try:
        session = broker_session(await broker.market_calendar(day), day)
        monitor.calendar(day, session)
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
        monitor.finish_poll(failures, market_data="error" if failures else "closed")
        return
    now = datetime.now(UTC)
    if not session.contains(now):
        for engine in engines:
            engine.step(now, session, [], [])
        monitor.finish_poll(failures, market_data="error" if failures else "closed")
        return
    symbols = list(dict.fromkeys(s for engine in engines for s in engine.config.symbols))
    # A fresh ledger has no prior session for indicator warmup: fetch it once per day.
    pending = [(engine, engine.needs_prior(session)) for engine in engines]
    pending = [(engine, prior) for engine, prior in pending if prior]
    if pending:
        monitor.phase("prior_session")
        prior, history = pending[0][1], []
        try:
            for symbol in symbols:
                history.extend(await fetch_minutes(broker, symbol, prior, prior.close))
        except (BrokerError, ValueError, KeyError, TypeError):
            history = []  # Strategy falls back to intraday-only and logs WARMUP_FALLBACK.
            failures.append("Prior session candles unavailable; intraday-only warmup")
        for engine, _ in pending:
            engine.seed_prior(prior, history)
    bars, quotes = [], []
    monitor.phase("candles")
    for symbol in symbols:
        try:
            bars.extend(
                await fetch_minutes(broker, symbol, session, minute_cutoff(datetime.now(UTC)))
            )
        except (BrokerError, ValueError, KeyError, TypeError):
            failures.append(f"{symbol}: candles unavailable")
    # Fetch books after candles; stale books are rejected per symbol by the engine.
    monitor.phase("quotes")
    for symbol in symbols:
        try:
            quotes.append(parse_quote(symbol, await broker.orderbook(symbol)))
        except (BrokerError, ValueError, KeyError, TypeError):
            failures.append(f"{symbol}: orderbook unavailable")
    monitor.phase("processing")
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
    # Collection success requires fresh candles and books for every configured symbol.
    checked_at = datetime.now(UTC)
    freshness_errors = []
    for engine in engines:
        for symbol in engine.config.symbols:
            latest = max((b.end for b in bars if b.symbol == symbol), default=None)
            quote = next((q for q in quotes if q.symbol == symbol), None)
            if (
                latest is None
                or not 0
                <= (checked_at - latest).total_seconds()
                <= engine.config.max_bar_age_seconds
            ):
                freshness_errors.append(f"{symbol}: candles missing or stale")
            if quote is None or not quote_checks(quote, checked_at, engine.config)["fresh_quote"]:
                freshness_errors.append(f"{symbol}: orderbook missing or stale")
    monitor.finish_poll(
        list(dict.fromkeys(failures + freshness_errors)),
        market_data="error" if failures else "stale" if freshness_errors else "success",
    )


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
        monitor = WorkerMonitor(stores)
        monitor.publish()
        heartbeat = asyncio.create_task(monitor.heartbeat())
        try:
            while True:
                try:
                    await poll_once(engines, broker, monitor)
                except (BrokerError, ValueError, KeyError, TypeError):
                    for store in stores:
                        store.record_error(
                            "Market data or calendar unavailable; new entries suspended"
                        )
                    monitor.finish_poll(
                        ["Market data or calendar unavailable; new entries suspended"],
                        market_data="error",
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
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            try:
                monitor.stopped()
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
