from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_live import Broker

from toss_trading.domain import Position
from toss_trading.live import weekly
from toss_trading.live.bridge import LiveBridge
from toss_trading.live.config import LiveConfig
from toss_trading.live.control import write_control
from toss_trading.marketdata import offline_session
from toss_trading.persistence import Store

CONFIG = Path(__file__).resolve().parents[1] / "config/strategy-weekly-live.toml"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    day = date(2026, 9, 8)  # Tuesday after Labor Day.
    session = offline_session(day)
    now = session.open + timedelta(seconds=10)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr(weekly, "datetime", Clock)
    live = LiveConfig(
        account_seq=1,
        capital_usd=1490,
        max_order_usd=298,
        daily_loss_usd="14.9",
        strategy_path=str(CONFIG),
        control_path=str(tmp_path / "control.json"),
    )
    write_control(live.control_path, "run")
    config = live.strategy()
    store = Store(f"sqlite:///{tmp_path}/live.db", config)
    rev, state = store.load()
    state.live_buying_power = D(1490)
    store.commit(rev, state, [])

    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            return_value={
                "today": {
                    "date": day.isoformat(),
                    "regularMarket": {"startTime": session.open, "endTime": session.close},
                }
            }
        ),
        orderbook=AsyncMock(
            return_value={
                "currency": "USD",
                "timestamp": now,
                "bids": [{"price": "49.99", "volume": "100"}],
                "asks": [{"price": "50.00", "volume": "100"}],
            }
        ),
        commissions=AsyncMock(return_value=[{"marketCountry": "US", "commissionRate": ".001"}]),
    )
    bridge = SimpleNamespace(
        live=live,
        broker=broker,
        config=config,
        store=store,
        load=store.load,
        reconcile=AsyncMock(return_value=True),
        flush=AsyncMock(),
    )
    daily = AsyncMock(
        side_effect=lambda broker, symbol, days: [D(100 + i) for i in range(len(days))]
    )
    monkeypatch.setattr(weekly, "daily_closes", daily)
    yield bridge, daily, session
    store.close()


async def test_holiday_entry_is_sized_and_restart_cannot_duplicate(setup):
    bridge, daily, session = setup
    assert await weekly.WeeklyLive(bridge).poll()
    _, state = bridge.load()
    order = state.pending()[0]
    assert order.id == f"weekly:{session.day}:SOXL:BUY"
    assert order.quantity == 2
    assert order.reserved == D("100.10")
    assert all(len(call.args[2]) == 200 for call in daily.await_args_list)
    assert all(call.args[2][-1] == date(2026, 9, 4) for call in daily.await_args_list)
    assert await weekly.WeeklyLive(bridge).poll()
    assert len([o for o in bridge.load()[1].orders.values() if o.symbol == "SOXL"]) == 1


async def test_weekly_exit_precedes_new_buys(setup):
    bridge, daily, session = setup
    rev, state = bridge.load()
    state.positions["SOXL"] = Position(
        symbol="SOXL",
        quantity=2,
        entry=50,
        entry_fees="0.1",
        stop=47,
        target=59,
        initial_risk=3,
        opened=offline_session(date(2026, 9, 1)).open,
    )
    bridge.store.commit(rev, state, [])
    daily.side_effect = lambda broker, symbol, days: [D(50)] * (len(days) - 1) + [D(46)]
    await weekly.WeeklyLive(bridge).poll()
    _, state = bridge.load()
    assert [(o.side, o.reason, o.quantity) for o in state.pending()] == [
        ("SELL", "CLOSE_STOP_NEXT_OPEN", D(2))
    ]


async def test_account_reconciliation_failure_creates_no_orders(setup):
    bridge, _, _ = setup
    bridge.reconcile.return_value = False
    await weekly.WeeklyLive(bridge).poll()
    assert not bridge.load()[1].orders
    bridge.flush.assert_not_awaited()


async def test_weekly_intent_reaches_broker_through_durable_bridge(setup, monkeypatch):
    stub, _, session = setup
    monkeypatch.setattr("toss_trading.live.bridge.datetime", weekly.datetime)
    broker = Broker()
    broker.accounts = AsyncMock(return_value=[{"accountSeq": 1}])
    broker.buying_power = AsyncMock(return_value={"currency": "USD", "cashBuyingPower": "1490"})
    broker.market_calendar = stub.broker.market_calendar
    broker.orderbook = stub.broker.orderbook
    broker.commissions = stub.broker.commissions
    live = stub.live.model_copy(update={"enabled": True})
    bridge = LiveBridge(stub.store, broker, live, stub.config)

    def assert_durable(order):
        stored = bridge.load()[1].orders[order.id]
        assert stored.broker_phase == "SENDING"
        assert stored.broker_request["orderType"] == "LIMIT"

    broker.observe = assert_durable
    await weekly.WeeklyLive(bridge).poll()
    assert broker.create_calls == 1
    assert bridge.load()[1].orders[f"weekly:{session.day}:SOXL:BUY"].broker_id == "broker1"


def test_twenty_completed_sessions_count_the_first_fill_session():
    completed = weekly.completed_days(date(2026, 10, 1), 20)
    position = SimpleNamespace(opened=offline_session(completed[0]).open, entry=D(50))
    assert weekly.position_exit(position, completed, [D(50)] * 20) == "TIME"
    position.opened = offline_session(completed[1]).open
    assert weekly.position_exit(position, completed, [D(50)] * 20) is None


async def test_target30_splits_two_signals(setup):
    bridge, _, _ = setup
    bridge.live = bridge.live.model_copy(
        update={"weekly_target_allocation": D(".30"), "daily_loss_usd": D(50)}
    )
    await weekly.WeeklyLive(bridge).poll()
    _, state = bridge.load()
    assert [(o.symbol, o.side, o.quantity) for o in state.pending()] == [
        ("SOXL", "BUY", D(4)),
    ]


async def test_target30_trims_before_buying_and_pause_cancels_trim(setup):
    bridge, daily, session = setup
    bridge.live = bridge.live.model_copy(update={"weekly_target_allocation": D(".30")})
    rev, state = bridge.load()
    state.cash = D("1190")
    state.positions["SOXL"] = Position(
        symbol="SOXL",
        quantity=6,
        entry=50,
        entry_fees=0,
        stop=47,
        target=59,
        initial_risk=3,
        opened=offline_session(date(2026, 9, 1)).open,
    )
    bridge.store.commit(rev, state, [])
    daily.side_effect = lambda broker, symbol, days: (
        [D(50)] * len(days) if symbol == "SOXL" else [D(100 + i) for i in range(len(days))]
    )
    worker = weekly.WeeklyLive(bridge)
    await worker.poll()
    _, state = bridge.load()
    assert [(o.id, o.reason, o.quantity) for o in state.pending()] == [
        (f"weekly:{session.day}:SOXL:TRIM", "WEEKLY_TRIM", D(2)),
    ]
    write_control(bridge.live.control_path, "pause")
    await worker.poll()
    assert not bridge.load()[1].pending()
