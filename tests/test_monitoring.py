import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from test_dashboard import PASSWORD, dashboard_settings
from test_trading import account, minutes, position, quote, save

from toss_trading.adapters.toss import BrokerError
from toss_trading.api import create_app
from toss_trading.dashboard import symbol_view
from toss_trading.engine import TradingEngine
from toss_trading.monitoring import WorkerMonitor, market_view, worker_view
from toss_trading.persistence import Store, events, symbol_signals, worker_status
from toss_trading.runner import poll_once


def test_worker_liveness_is_independent_of_feed_success(store):
    monitor = WorkerMonitor([store])
    monitor.finish_poll(["SOXL: candles unavailable"], market_data="error")
    with store.engine.connect() as conn:
        payload = json.loads(conn.execute(select(worker_status.c.payload)).scalar_one())
        assert conn.execute(select(events.c.id)).all() == []
    now = datetime.now(UTC)
    assert worker_view(payload, now, 5)["status"] == "running"
    assert worker_view(payload, now, 5)["market_data_status"] == "error"
    assert worker_view(payload, now + timedelta(minutes=1), 5)["status"] == "delayed"
    assert worker_view(None, now, 5)["status"] == "unknown"
    monitor.finish_poll([], market_data="success")
    assert monitor.payload["last_error"]["reasons"] == ["SOXL: candles unavailable"]
    assert monitor.payload["errors"] == []
    assert monitor.payload["last_market_data_success"]
    monitor.stopped()
    assert worker_view(monitor.payload, now, 5)["status"] == "stopped"


async def test_heartbeat_continues_during_waiting_work(store):
    monitor = WorkerMonitor([store])
    observed = asyncio.Event()
    original = store.write_worker_status

    def record(payload):
        original(payload)
        observed.set()

    store.write_worker_status = record
    monitor.phase("candles")
    task = asyncio.create_task(monitor.heartbeat(interval_seconds=0.01))
    try:
        await asyncio.wait_for(observed.wait(), timeout=1)
        with store.engine.connect() as conn:
            payload = json.loads(conn.execute(select(worker_status.c.payload)).scalar_one())
        assert payload["phase"] == "candles"
        assert store.load()[1].last_event is None
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_telemetry_storage_failure_does_not_disable_processing(store, monkeypatch):
    monitor = WorkerMonitor([store])
    monkeypatch.setattr(
        store, "write_worker_status", lambda _: (_ for _ in ()).throw(SQLAlchemyError())
    )
    monitor.finish_poll([], market_data="success")
    assert monitor.payload["market_data_status"] == "success"


def test_next_open_handles_weekend_dst_and_early_close():
    weekend = market_view(datetime(2026, 10, 24, 12, tzinfo=UTC), {})
    assert weekend["is_open"] is False
    assert weekend["next_open"] == datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
    dst = market_view(datetime(2026, 10, 30, 21, tzinfo=UTC), {})
    assert dst["next_open"] == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    early = market_view(datetime(2026, 11, 27, 16, tzinfo=UTC), {})
    assert early["is_open"] is True
    assert early["close"] == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert early["source"] == "local_estimate"


def test_broker_closed_day_overrides_local_estimate():
    view = market_view(
        datetime(2026, 10, 19, 15, tzinfo=UTC),
        {
            "calendar_day": "2026-10-19",
            "calendar_session": None,
        },
    )
    assert view["is_open"] is False
    assert view["source"] == "broker"
    assert view["next_open_source"] == "local_estimate"
    assert view["next_open"] == datetime(2026, 10, 20, 13, 30, tzinfo=UTC)


async def test_poll_reports_calendar_error_without_hiding_heartbeat(
    config, store, session, monkeypatch
):
    now = session.open + timedelta(hours=2)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
    monitor = WorkerMonitor([store])
    broker = SimpleNamespace(market_calendar=AsyncMock(side_effect=BrokerError(429)))
    await poll_once([TradingEngine(config, store)], broker, monitor)
    assert monitor.payload["market_data_status"] == "error"
    assert monitor.payload["last_calendar_success"] is None
    assert monitor.payload["last_market_data_success"] is None
    assert monitor.payload["errors"]
    assert monitor.payload["heartbeat_at"]


async def test_poll_reports_holiday_as_waiting_not_success(config, store, monkeypatch):
    now = datetime(2026, 10, 24, 16, tzinfo=UTC)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
    monitor = WorkerMonitor([store])
    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            return_value={
                "today": {"date": "2026-10-24", "regularMarket": None},
            }
        )
    )
    await poll_once([TradingEngine(config, store)], broker, monitor)
    assert monitor.payload["market_data_status"] == "closed"
    assert monitor.payload["last_calendar_success"]
    assert monitor.payload["last_market_data_success"] is None
    assert monitor.payload["errors"] == []


async def test_poll_success_requires_fresh_data_for_every_symbol(
    config, store, session, monkeypatch
):
    now = session.open + timedelta(minutes=110)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
    monitor = WorkerMonitor([store])
    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            return_value={
                "today": {
                    "date": session.day,
                    "regularMarket": {
                        "startTime": session.open.isoformat(),
                        "endTime": session.close.isoformat(),
                    },
                }
            }
        ),
        orderbook=AsyncMock(
            side_effect=lambda symbol: {
                "currency": "USD",
                "timestamp": now.isoformat(),
                "bids": [{"price": "23.9", "volume": "1000"}],
                "asks": [{"price": "23.901", "volume": "1000"}],
            }
        ),
    )

    async def fetch(broker, symbol, session, through):
        return minutes(session, symbol=symbol)

    monkeypatch.setattr("toss_trading.runner.fetch_minutes", fetch)
    engine = TradingEngine(config, store)
    await poll_once([engine], broker, monitor)
    assert monitor.payload["market_data_status"] == "success"
    assert monitor.payload["last_market_data_success"]
    # Successful HTTP responses with stale books must not count as a successful fresh collection.
    broker.orderbook.side_effect = lambda symbol: {
        "currency": "USD",
        "timestamp": (now - timedelta(minutes=10)).isoformat(),
        "bids": [{"price": "23.9", "volume": "1000"}],
        "asks": [{"price": "23.901", "volume": "1000"}],
    }
    await poll_once([engine], broker, monitor)
    assert monitor.payload["market_data_status"] == "stale"
    assert any("orderbook missing or stale" in item for item in monitor.payload["errors"])


def test_latest_decision_survives_more_than_100_other_events(store, config, session, monkeypatch):
    now = session.open + timedelta(minutes=110)
    save(store, account(session))
    engine = TradingEngine(config, store)
    engine.step(now, session, minutes(session), [quote(now, price="23.9")])
    rev, state = store.load()
    store.commit(rev, state, [{"event": "OTHER", "time": now} for _ in range(110)])

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("toss_trading.dashboard.datetime", FixedDatetime)
    settings = dashboard_settings(store)
    with TestClient(create_app(settings)) as client:
        data = client.get("/dashboard/data", auth=("admin", PASSWORD)).json()
    assert len(data["events"]) == 100
    symbol = data["entry_conditions"][0]
    assert Decimal(symbol["indicators"]["atr"]) > 0
    assert datetime.fromisoformat(symbol["decision_at"]) == now
    expected = Decimal(symbol["indicators"]["vwap"]) - config.stretch_atr * Decimal(
        symbol["indicators"]["atr"]
    )
    assert Decimal(symbol["entry_below"]) == expected
    assert symbol["checks"]["stretched_below_vwap"] is True
    assert symbol["gates"]["no_pending"] is False
    assert symbol["last_entry"]["event"] == "ORDER_SUBMITTED"


def test_live_gates_cover_cooldown_opposing_group_and_stale_decisions(default_config, session):
    config = default_config
    now = session.open + timedelta(minutes=110)
    state = account(session)
    state.session = session
    state.positions["SOXS"] = position(now, symbol="SOXS")
    state.cooldowns["SOXL"] = now + timedelta(minutes=30)
    state.marks = {"SOXL": Decimal("23.9"), "SOXS": Decimal("25")}
    state.mark_times = {"SOXL": now, "SOXS": now}
    saved = {
        "decision": {
            "time": now.isoformat(),
            "signal": "BUY_SIGNAL",
            "checks": {},
            "indicators": {"close": "23.9", "vwap": "24", "atr": ".05"},
        }
    }
    worker = {"status": "running", "market_data_status": "success"}
    result = symbol_view("SOXL", state, config, saved, worker, {"is_open": True}, now)
    assert result["status"] == "blocked"
    assert "cooldown" in result["failed_gates"]
    assert "opposing_group" in result["failed_gates"]
    assert result["decision_fresh"] is True
    later = symbol_view(
        "SOXL", state, config, saved, worker, {"is_open": True}, now + timedelta(minutes=10)
    )
    assert later["status"] == "stale"
    assert later["decision_fresh"] is False


def test_old_ledger_reads_without_monitoring_tables_and_upgrades_on_worker_start(store, config):
    with store.engine.begin() as conn:
        symbol_signals.drop(conn)
        worker_status.drop(conn)
    settings = dashboard_settings(store)
    with TestClient(create_app(settings)) as client:
        data = client.get("/dashboard/data", auth=("admin", PASSWORD)).json()
        assert data["worker"]["status"] == "unknown"
    assert not inspect(store.engine).has_table(worker_status.name)
    revision = store.load()[0]
    reopened = Store(str(store.engine.url), config)
    try:
        assert inspect(reopened.engine).has_table(worker_status.name)
        assert reopened.load()[0] == revision
    finally:
        reopened.close()


async def test_runner_once_marks_worker_stopped_and_closes_broker(store, monkeypatch):
    from toss_trading.runner import run

    settings = dashboard_settings(store)
    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            side_effect=lambda day: {
                "today": {
                    "date": day.isoformat(),
                    "regularMarket": None,
                }
            }
        ),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr("toss_trading.runner.Settings", lambda: settings)
    monkeypatch.setattr("toss_trading.runner.TossQueries", lambda _: broker)
    await run(SimpleNamespace(config=None, once=True, slack=False))
    broker.aclose.assert_awaited_once()
    with store.engine.connect() as conn:
        payload = json.loads(conn.execute(select(worker_status.c.payload)).scalar_one())
    assert payload["phase"] == "stopped"
    assert payload["stopped_at"]
    assert payload["last_calendar_success"]


def test_comparison_monitoring_is_isolated_from_primary(store, config, tmp_path):
    from test_dashboard import CONFIG

    comparison = Store(f"sqlite:///{tmp_path}/compare-monitoring.db", config)
    try:
        WorkerMonitor([comparison]).finish_poll(
            ["SOXL: orderbook unavailable"], market_data="error"
        )
        settings = dashboard_settings(
            store, compare_config_path=str(CONFIG), compare_database_url=str(comparison.engine.url)
        )
        with TestClient(create_app(settings)) as client:
            primary = client.get("/dashboard/data", auth=("admin", PASSWORD)).json()
            compare = client.get("/dashboard/data?ledger=compare", auth=("admin", PASSWORD)).json()
        assert primary["worker"]["status"] == "unknown"
        assert compare["worker"]["status"] == "running"
        assert compare["worker"]["market_data_status"] == "error"
        assert compare["worker"]["errors"] == ["SOXL: orderbook unavailable"]
    finally:
        comparison.close()
