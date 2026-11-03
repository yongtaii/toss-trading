"""Dedicated live worker. Defaults to read-only preflight; execution is explicit."""

import argparse
import asyncio
import json
from contextlib import suppress
from pathlib import Path

from sqlalchemy.engine import make_url

from toss_trading.adapters.toss import BrokerError, ConfigurationError
from toss_trading.config import Settings
from toss_trading.live.bridge import LiveBridge
from toss_trading.live.broker import LiveBroker
from toss_trading.live.config import load_live_config
from toss_trading.live.control import AccountLock, read_control, write_control
from toss_trading.live.doctor import diagnose
from toss_trading.live.weekly import WeeklyLive
from toss_trading.monitoring import WorkerMonitor
from toss_trading.persistence import Store


async def run(args) -> None:
    settings, live = Settings(), load_live_config(args.config)
    config = live.strategy()
    if args.control:
        write_control(live.control_path, args.control)
        print(f"Live control: {args.control}")
        return
    if args.status:
        store = Store(live.database_url, config, initialize=False)
        try:
            _, state = store.load()
            print(
                json.dumps(
                    {
                        "mode": "LIVE",
                        "enabled": live.enabled,
                        "control": read_control(live.control_path),
                        "capital_usd": live.capital_usd,
                        "max_order_usd": live.max_order_usd,
                        "daily_loss_usd": live.daily_loss_usd,
                        "weekly_target_allocation": live.weekly_target_allocation,
                        "strategy": config.strategy_name,
                        "signal_sources": {s: config.signal_source(s)[0] for s in config.symbols},
                        "account_verified_at": state.live_verified_at,
                        "reconciled": state.reconciled,
                        "cash": state.cash,
                        "positions": {
                            s: p.model_dump(mode="json") for s, p in state.positions.items()
                        },
                        "pending_orders": [o.model_dump(mode="json") for o in state.pending()],
                    },
                    indent=2,
                    default=str,
                )
            )
        finally:
            store.close()
        return
    if not args.execute:
        print(json.dumps(await diagnose(settings, live), indent=2, ensure_ascii=False))
        return
    if not live.enabled:
        raise ValueError("Live execution requires enabled=true in the reviewed live configuration")
    if live.account_seq is None or settings.toss_account_seq != live.account_seq:
        raise ConfigurationError("Live account does not match TOSS_ACCOUNT_SEQ")
    if live.database_url == settings.paper_database_url.get_secret_value():
        raise ValueError("Live and PAPER must have separate ledgers")
    live_url = make_url(live.database_url)
    paper_url = make_url(settings.paper_database_url.get_secret_value())
    if live_url.get_backend_name() != "sqlite" or not live_url.database:
        raise ValueError("Weekly LIVE requires a persistent SQLite file")
    live_path = Path(live_url.database).resolve()
    if live_url.database == ":memory:" or (
        paper_url.get_backend_name() == "sqlite"
        and paper_url.database
        and live_path == Path(paper_url.database).resolve()
    ):
        raise ValueError("Live and PAPER must have separate persistent ledgers")
    Path("data").mkdir(exist_ok=True)
    with AccountLock(live.account_seq, directory=Path("data")):
        store = Store(live.database_url, config)
        broker = LiveBroker(settings, live, armed=True)
        bridge = LiveBridge(store, broker, live, config)
        monitor = WorkerMonitor([store])
        monitor.payload["weekly_target_allocation"] = (
            str(live.weekly_target_allocation)
            if live.weekly_target_allocation is not None
            else None
        )
        heartbeat = asyncio.create_task(monitor.heartbeat())
        try:
            # Mapping unknown orders is read-only at the broker and does not place orders.
            if args.attach:
                local_id, broker_id = args.attach.split("=", 1)
                await bridge.attach(local_id, broker_id)
                print("Broker order attached; run a fresh account verification before trading")
                return
            weekly = WeeklyLive(bridge)
            monitor.publish()
            while True:
                try:
                    regular = await weekly.poll()
                    monitor.finish_poll([], market_data="success")
                except (
                    BrokerError,
                    ConfigurationError,
                    ValueError,
                    KeyError,
                    TypeError,
                    ArithmeticError,
                ):
                    regular = True
                    bridge.block("live_poll_failed")
                    monitor.finish_poll(
                        ["Live poll failed; new entries suspended"], market_data="error"
                    )
                if args.once:
                    break
                await asyncio.sleep(config.paper_poll_seconds if regular else 30)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            try:
                monitor.stopped()
            finally:
                await broker.aclose()
                store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute", action="store_true", help="Allow broker mutations")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true", help="Read the existing live ledger")
    parser.add_argument("--control", choices=("run", "pause", "flatten"))
    parser.add_argument("--attach", metavar="LOCAL_ID=BROKER_ID")
    args = parser.parse_args()
    if args.attach and not args.execute:
        parser.error("--attach requires an explicit live worker invocation with --execute")
    if sum((bool(args.control), args.status, bool(args.attach))) > 1:
        parser.error("choose only one of --control, --status and --attach")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
