from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import LEGACY_CONFIG
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_trading import account, minutes, order, position, quote, save

from toss_trading.adapters.toss import BrokerError, TossQueries
from toss_trading.api import create_app
from toss_trading.domain import OrderStatus
from toss_trading.engine import TradingEngine
from toss_trading.marketdata import NY, fetch_minutes, minute_cutoff
from toss_trading.persistence import Store
from toss_trading.runner import poll_once
from toss_trading.trading_config import TradingConfig


async def test_additional_official_read_endpoints(settings, monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"result": {"ok": True}})

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr("toss_trading.adapters.toss.asyncio.sleep", no_sleep)
    broker = TossQueries(settings, transport=httpx.MockTransport(handler))
    broker._token, broker._expires = "token", float("inf")
    try:
        await broker.candles("soxl", before="2026-10-19T15:00:00+00:00")
        await broker.orderbook("SOXS")
        await broker.market_calendar(date(2026, 10, 19))
        assert [r.url.path for r in seen] == [
            "/api/v1/candles",
            "/api/v1/orderbook",
            "/api/v1/market-calendar/US",
        ]
        assert seen[0].url.params["interval"] == "1m"
        assert seen[0].url.params["symbol"] == "SOXL"
        assert seen[0].url.params["before"] == "2026-10-19T15:00:00+00:00"
        with pytest.raises(ValueError):
            await broker.candles("SOXL", count=201)
    finally:
        await broker.aclose()


def candle(end, price="20"):
    return {
        "timestamp": end.isoformat(),
        "currency": "USD",
        "openPrice": price,
        "highPrice": price,
        "lowPrice": price,
        "closePrice": price,
        "volume": "100",
    }


async def test_inclusive_pagination_and_forming_candle_filter(session):
    one, two, three = [session.open + timedelta(minutes=i) for i in (1, 2, 3)]
    broker = SimpleNamespace(
        candles=AsyncMock(
            side_effect=[
                {"candles": [candle(three), candle(two)], "nextBefore": two.isoformat()},
                {"candles": [candle(two), candle(one)], "nextBefore": None},
            ]
        )
    )
    result = await fetch_minutes(broker, "SOXL", session, two + timedelta(seconds=30))
    assert [b.end for b in result] == [one, two]
    assert broker.candles.call_args_list[1].kwargs["before"] == two.isoformat()
    assert minute_cutoff(two) == one
    assert minute_cutoff(two + timedelta(seconds=2)) == two


async def test_pagination_stall_fails_closed(session):
    end = session.open + timedelta(minutes=10)
    broker = SimpleNamespace(
        candles=AsyncMock(
            return_value={
                "candles": [candle(end)],
                "nextBefore": end.isoformat(),
            }
        )
    )
    with pytest.raises(ValueError, match="did not advance"):
        await fetch_minutes(broker, "SOXL", session, end)


def test_paper_status_api_existing_query_compatibility(settings, config, session, store, tmp_path):
    config_path = tmp_path / "strategy.toml"
    # The ledger fixture uses the legacy settings; the API must load the same file.
    config_path.write_text(LEGACY_CONFIG.read_text())
    settings.strategy_config_path = str(config_path)
    settings.paper_database_url = SecretStr(str(store.engine.url))
    TradingEngine(config, store).step(session.open, session, [], [])
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/paper").status_code == 401
        headers = {"X-API-Key": settings.operations_api_key.get_secret_value()}
        response = client.get("/v1/paper", headers=headers)
        assert response.status_code == 200
        assert response.json()["mode"] == "PAPER"
        assert response.json()["state"]["cash"] == "10000"
        assert client.get("/v1/paper/events", headers=headers).status_code == 200
        assert client.post("/v1/orders", headers=headers).status_code == 405


def test_engine_vwap_exit_uses_completed_candle(config, session, store):
    rows = minutes(session)
    rows[-5:] = [
        bar.model_copy(
            update={"open": D("30"), "high": D("30.01"), "low": D("29.99"), "close": D("30")}
        )
        for bar in rows[-5:]
    ]
    now = rows[-1].end
    engine = TradingEngine(config, store)
    state = account(session)
    p = position(session.open)
    p.stop, p.target = D("10"), D("40")
    state.positions["SOXL"] = p
    state.cash -= 250
    save(store, state)
    engine.step(now, session, rows[:-1], [quote(now, price="30")])
    assert not store.load()[1].orders
    engine.step(now, session, rows[-1:], [quote(now, price="30")])
    assert store.load()[1].pending()[0].reason == "VWAP_EXIT"


@pytest.mark.parametrize("kind", ["stale", "missing", "disabled", "disallowed"])
def test_entry_signal_execution_gate(config, session, store, kind):
    rows = minutes(session)
    engine = TradingEngine(config, store)
    at = rows[-1].end
    now = at + timedelta(seconds=91) if kind == "stale" else at
    quotes = [] if kind == "missing" else [quote(now)]
    if kind == "disallowed":
        rows = [b.model_copy(update={"symbol": "AAPL"}) for b in rows]
    engine.step(now, session, rows, quotes, allow_entries=kind != "disabled")
    assert not store.load()[1].orders


def test_rejected_exit_can_retry_but_uncertain_exit_cannot(config, session, store):
    engine = TradingEngine(config, store)
    now = session.open + timedelta(hours=2)
    state = account(session)
    state.cash -= 250
    state.positions["SOXL"] = position(session.open)
    item = order(now - timedelta(seconds=1), side="SELL")
    item.status = OrderStatus.REJECTED
    state.orders[item.id] = item
    save(store, state)
    engine.step(now, session, [], [quote(now, price="23")])
    assert len(store.load()[1].orders) == 2
    state = store.load()[1]
    state.pending()[0].status = OrderStatus.UNKNOWN
    save(store, state)
    engine.step(
        now + timedelta(seconds=1), session, [], [quote(now + timedelta(seconds=1), price="23")]
    )
    assert len(store.load()[1].orders) == 2


def test_stop_cancels_partial_buy_remainder_before_exit(config, session, store):
    engine = TradingEngine(config, store)
    now = session.open + timedelta(hours=2)
    state = account(session)
    state.cash -= 250
    state.positions["SOXL"] = position(session.open)
    item = order(now - timedelta(seconds=1))
    item.quantity, item.filled, item.status = D(20), D(10), OrderStatus.PARTIALLY_FILLED
    state.orders[item.id] = item
    save(store, state)
    engine.step(now, session, [], [quote(now, price="23")])
    result = store.load()[1]
    assert result.orders["one"].status == OrderStatus.CANCELLED
    assert result.pending()[0].side == "SELL"
    assert result.pending()[0].quantity == 10


def test_gap_does_not_allow_entry_and_missing_calendar_does_not_fill(config, session, store):
    engine = TradingEngine(config, store)
    rows = minutes(session)
    rows.pop(50)
    now = rows[-1].end
    engine.step(now, session, rows, [quote(now)])
    assert not store.load()[1].orders
    state = store.load()[1]
    state.orders["one"] = order(now)
    save(store, state)
    engine.step(now + timedelta(seconds=1), None, [], [quote(now + timedelta(seconds=1))])
    assert not store.load()[1].positions


def test_exact_volume_ratio_and_wilder_atr(config, session):
    from toss_trading.strategy import VwapMeanRevert

    rows = minutes(session)
    for bar in rows[-5:]:
        bar.volume = D(150)
    result = VwapMeanRevert(config).evaluate(rows, session)
    assert result.indicators.volume_ratio == D("1.5")
    # Monotonic 0.01/min fall -> 0.05 close delta + 0.005 lower wick per 5m.
    assert result.indicators.atr == D("0.055")


async def test_poll_failure_suspends_entries_but_preserves_exit_loop(
    config, store, session, monkeypatch
):
    engine = TradingEngine(config, store)
    now = session.open + timedelta(hours=2)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            return_value={
                "today": {
                    "date": now.astimezone(NY).date().isoformat(),
                    "regularMarket": {
                        "startTime": (now - timedelta(hours=2)).isoformat(),
                        "endTime": (now + timedelta(hours=2)).isoformat(),
                    },
                }
            }
        ),
        candles=AsyncMock(side_effect=BrokerError(429)),
        orderbook=AsyncMock(side_effect=BrokerError(500)),
    )
    await poll_once([engine], broker)
    assert not store.load()[1].orders
    assert broker.orderbook.call_count == 4


async def test_external_readonly_reconciliation_detects_uncertain_fills(session):
    from toss_trading.reconciliation import verify_external_account

    state = account(session)
    state.orders["one"] = order(session.open)
    state.orders["one"].status = OrderStatus.UNKNOWN
    broker = SimpleNamespace(
        holdings=AsyncMock(return_value={"items": []}),
        buying_power=AsyncMock(return_value={"currency": "USD", "cashBuyingPower": "10000"}),
        orders=AsyncMock(return_value={"orders": []}),
        order=AsyncMock(return_value={"status": "FILLED", "execution": {"filledQuantity": "10"}}),
    )
    report = await verify_external_account(broker, state)
    assert not report["verified"] and not report["live_execution_enabled"]
    assert "open_order_ids" in report["mismatches"]
    assert "filled_quantity:one" in report["mismatches"]
    broker.order.assert_awaited_once_with("one")
    assert state.orders["one"].status == OrderStatus.UNKNOWN  # No blind ledger overwrite.


def test_overnight_positions_block_entries_and_are_flattened_first(config, session, store):
    engine = TradingEngine(config, store)
    old = account(session)
    old.positions["SOXL"] = position(session.open)
    old.cash -= 250
    save(store, old)
    next_session = session.model_copy(
        update={
            "day": "2026-10-20",
            "open": session.open + timedelta(days=1),
            "close": session.close + timedelta(days=1),
        }
    )
    engine.step(next_session.open, next_session, [], [quote(next_session.open, price="25")])
    state = store.load()[1]
    assert "overnight_positions" in state.alerts
    assert state.pending()[0].reason == "SESSION_FLATTEN"


async def test_calendar_failure_uses_saved_session_for_risk_only(
    config, store, session, monkeypatch
):
    engine = TradingEngine(config, store)
    now = session.open + timedelta(hours=2)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
    day = now.astimezone(NY).date().isoformat()
    from toss_trading.domain import Session

    session = Session(day=day, open=now - timedelta(hours=2), close=now + timedelta(hours=2))
    engine.step(now - timedelta(seconds=1), session, [], [])
    broker = SimpleNamespace(
        market_calendar=AsyncMock(side_effect=BrokerError(500)),
        candles=AsyncMock(side_effect=BrokerError(500)),
        orderbook=AsyncMock(side_effect=BrokerError(500)),
    )
    await poll_once([engine], broker)
    assert broker.orderbook.call_count == 4
    assert not store.load()[1].orders


async def test_one_market_fetch_feeds_separate_strategy_ledgers(
    config, store, session, monkeypatch, tmp_path
):
    compare_config = config.model_copy(update={"stretch_atr": D("2")})
    compare_store = Store(f"sqlite:///{tmp_path}/compare.db", compare_config)
    engines = [TradingEngine(config, store), TradingEngine(compare_config, compare_store)]
    now = session.open + timedelta(hours=2)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            return_value={
                "today": {
                    "date": now.astimezone(NY).date().isoformat(),
                    "regularMarket": {
                        "startTime": (now - timedelta(hours=2)).isoformat(),
                        "endTime": (now + timedelta(hours=2)).isoformat(),
                    },
                }
            }
        ),
        candles=AsyncMock(side_effect=BrokerError(429)),
        orderbook=AsyncMock(side_effect=BrokerError(500)),
    )
    try:
        await poll_once(engines, broker)
        assert broker.market_calendar.call_count == 1
        assert broker.orderbook.call_count == 4  # Shared fetch, not once per strategy.
        for ledger in (store, compare_store):
            state = ledger.load()[1]
            assert state.last_event == now and not state.orders
    finally:
        compare_store.close()


def test_compare_ledger_settings_and_api(settings, config, store, tmp_path):
    from toss_trading.config import Settings

    with pytest.raises(ValueError, match="both"):
        Settings(_env_file=None, compare_config_path="config/strategy-mean-revert.toml")
    with pytest.raises(ValueError, match="separate"):
        Settings(
            _env_file=None,
            compare_config_path="x.toml",
            compare_database_url="sqlite:///data/paper.db",
        )
    headers = {"X-API-Key": settings.operations_api_key.get_secret_value()}
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/paper?ledger=compare", headers=headers).status_code == 404
    config_path = tmp_path / "compare.toml"
    config_path.write_text('mode = "PAPER"\nstretch_atr = "2"\n')
    compare_config = TradingConfig(stretch_atr="2")
    compare = Store(f"sqlite:///{tmp_path}/compare.db", compare_config)
    settings.compare_config_path = str(config_path)
    settings.compare_database_url = SecretStr(str(compare.engine.url))
    try:
        with TestClient(create_app(settings)) as client:
            response = client.get("/v1/paper?ledger=compare", headers=headers)
            assert response.status_code == 200
            assert response.json()["state"]["cash"] == "10000"
            assert client.get("/v1/paper/events?ledger=compare", headers=headers).status_code == 200
    finally:
        compare.close()


async def test_runner_seeds_prior_session_once_and_falls_back_on_failure(
    default_config, session, monkeypatch
):
    from toss_trading.backtest import MemoryStore

    engine = TradingEngine(default_config, MemoryStore(default_config))
    now = session.open + timedelta(hours=2)
    monkeypatch.setattr("toss_trading.runner.datetime", SimpleNamespace(now=lambda tz: now))
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
        candles=AsyncMock(side_effect=BrokerError(500)),
        orderbook=AsyncMock(side_effect=BrokerError(500)),
    )
    await poll_once([engine], broker)
    first = broker.candles.call_count
    assert engine.store.state.prior_day == "2026-10-16"  # Friday before Monday 10-19.
    assert engine.store.state.prior_bars == {}  # Fetch failed: strategy falls back.
    await poll_once([engine], broker)
    assert broker.candles.call_count - first == 4  # Today's bars only; no prior refetch.
