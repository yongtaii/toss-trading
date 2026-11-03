"""Live boundary and crash recovery tests; every broker operation is mocked."""

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from toss_trading.adapters.toss import BrokerError, ConfigurationError
from toss_trading.domain import Order, OrderStatus, Quote, Session
from toss_trading.engine import TradingEngine
from toss_trading.live.bridge import LiveBridge
from toss_trading.live.broker import LiveBroker, order_body
from toss_trading.live.config import LiveConfig, load_live_config
from toss_trading.live.control import AccountLock, read_control, write_control
from toss_trading.live.doctor import diagnose
from toss_trading.live.execution import LiveIntents
from toss_trading.persistence import ConcurrentUpdate, Store
from toss_trading.trading_config import TradingConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def live(tmp_path):
    value = LiveConfig(
        enabled=True,
        account_seq=7,
        capital_usd="1000",
        max_order_usd="200",
        daily_loss_usd="10",
        strategy_path=str(ROOT / "config/strategy.toml"),
        database_url=f"sqlite:///{tmp_path}/live.db",
        control_path=str(tmp_path / "control.json"),
    )
    write_control(value.control_path, "run")
    return value


def intent(now, key="buy", side="BUY", quantity="4"):
    return Order(
        id=key,
        symbol="TQQQ",
        side=side,
        quantity=quantity,
        submitted=now,
        limit="25.019",
        atr="0.15",
        reason="BUY_SIGNAL" if side == "BUY" else "STOP",
        reserved=D(quantity) * D("25.019") * D("1.001") if side == "BUY" else 0,
        signal_symbol="QQQ",
        signal_entry="100",
        signal_atr="0.2",
    )


def snapshot(order, broker_id="broker1", filled="0", amount=None, fees=None, status="PENDING"):
    return {
        "orderId": broker_id,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": str(order.quantity),
        "currency": "USD",
        "status": status,
        "orderedAt": order.submitted.isoformat(),
        "orderType": "LIMIT" if order.side == "BUY" else "MARKET",
        "execution": {
            "filledQuantity": filled,
            "filledAmount": amount,
            "commission": fees,
            "tax": None,
            "filledAt": datetime.now(UTC).isoformat() if D(filled) else None,
        },
    }


class Broker:
    armed = True

    def __init__(self):
        self.details = {}
        self.create_calls = 0
        self.cancel_calls = 0
        self.raise_after_send = False
        self.crash_after_send = False
        self.external_orders = []
        self.external_positions = {}
        self.observe = None

    async def accounts(self):
        return [{"accountSeq": 7}]

    async def buying_power(self):
        return {"currency": "USD", "cashBuyingPower": "1000"}

    async def holdings(self):
        quantities = dict(self.external_positions)
        for detail in self.details.values():
            sign = 1 if detail["side"] == "BUY" else -1
            symbol = detail["symbol"]
            quantities[symbol] = quantities.get(symbol, D(0)) + sign * D(
                detail["execution"]["filledQuantity"]
            )
        return {
            "items": [
                {"symbol": s, "quantity": str(q), "currency": "USD"}
                for s, q in quantities.items()
                if q
            ]
        }

    async def orders(self, **kwargs):
        return {
            "orders": [
                copy.deepcopy(d)
                for d in self.details.values()
                if d["status"] in {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL"}
            ]
            + self.external_orders,
            "hasNext": False,
            "nextCursor": None,
        }

    async def order(self, broker_id):
        return copy.deepcopy(self.details[broker_id])

    async def create_order(self, order, client_id):
        self.create_calls += 1
        if self.observe:
            self.observe(order)
        broker_id = f"broker{self.create_calls}"
        self.details[broker_id] = snapshot(order, broker_id)
        if self.crash_after_send:
            raise SystemExit("simulated crash after the broker accepted the order")
        if self.raise_after_send:
            raise BrokerError()
        return {"orderId": broker_id, "clientOrderId": client_id}

    async def cancel_order(self, broker_id):
        self.cancel_calls += 1
        return {"orderId": f"cancel{self.cancel_calls}"}


@pytest.fixture
def setup_live(live):
    # Isolate broker/recovery mechanics from the intentionally blocked strategy integration.
    config = TradingConfig().model_copy(
        update={
            "mode": "LIVE",
            "initial_cash": live.capital_usd,
            "max_order_usd": live.max_order_usd,
            "daily_loss_fraction": live.daily_loss_usd / live.capital_usd,
        }
    )
    store = Store(live.database_url, config)
    now = datetime.now(UTC)
    rev, state = store.load()
    state.session = Session(
        day=now.date().isoformat(), open=now - timedelta(hours=2), close=now + timedelta(hours=2)
    )
    state.day, state.day_equity = state.session.day, state.cash
    state.marks["TQQQ"], state.mark_times["TQQQ"] = D("25"), now
    state.signal_quotes["QQQ"] = Quote(
        symbol="QQQ",
        at=now,
        bid="100",
        ask="100.001",
        bid_size=100,
        ask_size=100,
    )
    store.commit(rev, state, [])
    broker = Broker()
    bridge = LiveBridge(store, broker, live, config)
    yield store, broker, bridge, config
    store.close()


def add_intent(store, order):
    rev, state = store.load()
    state.orders[order.id] = order
    state.mark_times["TQQQ"] = datetime.now(UTC)
    store.commit(rev, state, [])


async def test_durable_before_send_and_reconcile_restart(setup_live, live):
    store, broker, bridge, config = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    add_intent(store, order)

    def persisted_before_io(sent):
        saved = store.load()[1].orders[sent.id]
        assert saved.broker_phase == "SENDING" and saved.status == OrderStatus.UNKNOWN
        assert saved.broker_request == order_body(saved, saved.client_order_id)

    broker.observe = persisted_before_io
    await bridge.flush()
    assert broker.create_calls == 1
    assert store.load()[1].orders[order.id].broker_id == "broker1"
    broker.details["broker1"] = snapshot(
        order, filled="2", amount="50", fees="0.05", status="PARTIAL_FILLED"
    )
    restarted = LiveBridge(store, broker, live, config)
    assert await restarted.reconcile()
    first = store.load()[1]
    assert first.positions["TQQQ"].quantity == 2
    assert first.cash == D("949.95")
    assert first.positions["TQQQ"].signal_stop == D("99.6")
    assert await restarted.reconcile()
    assert store.load()[1].cash == first.cash
    assert store.load()[1].positions["TQQQ"].quantity == 2
    broker.details["broker1"] = snapshot(
        order, filled="4", amount="101", fees="0.101", status="FILLED"
    )
    assert await restarted.reconcile()
    state = store.load()[1]
    assert state.cash == D("898.899")
    assert state.positions["TQQQ"].entry == D("25.25")
    assert state.positions["TQQQ"].signal_stop == D("99.6")
    assert state.fees == D("0.101")


@pytest.mark.parametrize("crash", [False, True])
async def test_unknown_send_is_never_retried_and_requires_mapping(setup_live, live, crash):
    store, broker, bridge, config = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    add_intent(store, order)
    broker.raise_after_send, broker.crash_after_send = not crash, crash
    if crash:
        with pytest.raises(SystemExit):
            await bridge.flush()
    else:
        await bridge.flush()
    assert store.load()[1].orders[order.id].status == OrderStatus.UNKNOWN
    restarted = LiveBridge(store, broker, live, config)
    assert not await restarted.reconcile()
    await restarted.flush()
    assert broker.create_calls == 1
    await restarted.attach(order.id, "broker1")
    assert await restarted.reconcile()
    assert store.load()[1].orders[order.id].broker_id == "broker1"
    await restarted.flush()
    assert broker.create_calls == 1


async def test_failed_intent_commit_never_reaches_broker(setup_live, monkeypatch):
    store, broker, bridge, _ = setup_live
    assert await bridge.reconcile()
    add_intent(store, intent(datetime.now(UTC)))
    commit = store.commit

    def fail_before_send(rev, state, records):
        if any(o.broker_phase == "SENDING" for o in state.orders.values()):
            raise ConcurrentUpdate("simulated competing writer")
        commit(rev, state, records)

    monkeypatch.setattr(store, "commit", fail_before_send)
    with pytest.raises(ConcurrentUpdate):
        await bridge.flush()
    assert broker.create_calls == 0


async def test_attach_cannot_map_an_old_matching_order(setup_live):
    store, broker, bridge, _ = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    add_intent(store, order)
    broker.raise_after_send = True
    await bridge.flush()
    broker.details["broker1"]["orderedAt"] = (order.submitted - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="order time"):
        await bridge.attach(order.id, "broker1")
    assert store.load()[1].orders[order.id].broker_id is None


async def test_cancel_waits_for_confirmation_and_includes_racing_partial_fill(setup_live):
    store, broker, bridge, config = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    add_intent(store, order)
    await bridge.flush()
    broker.details["broker1"] = snapshot(
        order, filled="2", amount="50", fees="0.05", status="PARTIAL_FILLED"
    )
    assert await bridge.reconcile()
    engine = TradingEngine(config, store, execution=LiveIntents(config))

    def flatten():
        now = datetime.now(UTC)
        quotes = [
            Quote(symbol=s, at=now, bid=p, ask=p + D("0.001"), bid_size=100, ask_size=100)
            for s, p in (("TQQQ", D(25)), ("QQQ", D(100)))
        ]
        engine.step(now, store.load()[1].session, [], quotes, allow_entries=False, flatten=True)

    flatten()
    assert not any(o.side == "SELL" for o in store.load()[1].orders.values())
    await bridge.flush()
    assert broker.cancel_calls == 1
    assert store.load()[1].orders[order.id].cancel_broker_id == "cancel1"
    assert await bridge.reconcile()
    flatten()
    await bridge.flush()
    assert broker.cancel_calls == 1
    assert broker.create_calls == 1
    broker.details["broker1"] = snapshot(
        order, filled="3", amount="75", fees="0.075", status="CANCELED"
    )
    assert await bridge.reconcile()
    flatten()
    sell = next(o for o in store.load()[1].pending() if o.side == "SELL")
    assert sell.quantity == 3 and sell.reason == "MANUAL_FLATTEN"
    await bridge.flush()
    broker.details["broker2"] = snapshot(
        sell, "broker2", filled="3", amount="78", fees="0.078", status="FILLED"
    )
    assert await bridge.reconcile()
    state = store.load()[1]
    assert not state.positions and not state.pending()
    assert state.realized == D("2.847")
    assert state.cash == D("1002.847")
    assert state.trades[-1]["signal_symbol"] == "QQQ"


@pytest.mark.parametrize(
    "kind", ["manual_position", "manual_order", "partial_snapshot", "wrong_identity"]
)
async def test_account_mismatch_blocks_new_orders(setup_live, kind):
    store, broker, bridge, _ = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    if kind == "manual_position":
        broker.external_positions["AAPL"] = D(1)
    elif kind == "manual_order":
        broker.external_orders = [{"orderId": "outside"}]
    elif kind == "partial_snapshot":
        broker.orders = AsyncMock(return_value={"orders": [], "hasNext": True})
    else:
        add_intent(store, order)
        await bridge.flush()
        broker.details["broker1"]["symbol"] = "SQQQ"
    assert not await bridge.reconcile()
    assert not store.load()[1].reconciled


@pytest.mark.parametrize(
    "kind",
    [
        "paused",
        "stale",
        "daily_limit",
        "cash",
        "disabled",
        "source_missing",
        "source_stop",
        "source_stale",
        "risk_remaining",
        "allocated_cash",
    ],
)
async def test_entry_checks_again_before_transmission(setup_live, live, kind):
    store, broker, bridge, _ = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    if kind == "stale":
        order.submitted -= timedelta(minutes=1)
    if kind == "paused":
        write_control(live.control_path, "pause")
    if kind == "disabled":
        broker.armed = False
    add_intent(store, order)
    rev, state = store.load()
    if kind == "daily_limit":
        state.day_equity = state.cash + 20
    if kind == "cash":
        state.live_buying_power = D(1)
    if kind == "source_missing":
        state.signal_quotes = {}
    if kind == "source_stop":
        state.signal_quotes["QQQ"].bid = D(99)
        state.signal_quotes["QQQ"].ask = D("99.001")
    if kind == "source_stale":
        state.signal_quotes["QQQ"].at -= timedelta(minutes=1)
    if kind == "risk_remaining":
        state.day_equity = state.cash + D("9.9")
    if kind == "allocated_cash":
        state.cash = D(50)
    store.commit(rev, state, [])
    await bridge.flush()
    assert broker.create_calls == 0


async def test_live_transport_scope_headers_and_precision(settings, live, monkeypatch):
    seen = []

    async def no_sleep(delay):
        pass

    monkeypatch.setattr("toss_trading.adapters.toss.asyncio.sleep", no_sleep)

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"result": {"orderId": "operation-new-id"}})
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"result": {"orderId": "broker1", "clientOrderId": body["clientOrderId"]}}
        )

    broker = LiveBroker(settings, live, armed=True, transport=httpx.MockTransport(handler))
    broker._token, broker._expires = "mock-token", float("inf")
    try:
        order = intent(datetime.now(UTC))
        await broker.create_order(order, "idempotency-1")
        assert json.loads(seen[-1].content)["price"] == "25.01"
        assert seen[-1].headers["X-Tossinvest-Account"] == "7"
        await broker.create_order(intent(datetime.now(UTC), side="SELL"), "sell-1")
        assert "price" not in json.loads(seen[-1].content)
        assert json.loads(seen[-1].content)["orderType"] == "MARKET"
        assert (await broker.cancel_order("broker1"))["orderId"] == "operation-new-id"
        for method, path in (
            ("POST", "/api/v1/conditional-orders"),
            ("DELETE", "/api/v1/orders/broker1"),
        ):
            with pytest.raises(ConfigurationError):
                await broker._send(method, path)
        with pytest.raises(ConfigurationError):
            await broker._guard_live(httpx.Request("POST", "https://example.com/api/v1/orders"))
    finally:
        await broker.aclose()


async def test_unarmed_live_client_never_sends_mutation(settings, live):
    broker = LiveBroker(
        settings, live, transport=httpx.MockTransport(lambda r: pytest.fail("network"))
    )
    try:
        with pytest.raises(ConfigurationError):
            await broker.create_order(intent(datetime.now(UTC)), "key")
    finally:
        await broker.aclose()


def test_live_config_and_control_fail_closed(live, tmp_path):
    with pytest.raises(ValueError, match="offline only"):
        live.strategy()
    assert read_control(tmp_path / "missing") == "pause"
    Path(live.control_path).write_text("broken")
    assert read_control(live.control_path) == "pause"
    with pytest.raises(ValidationError):
        LiveConfig(account_seq=7, capital_usd=100, max_order_usd=200, daily_loss_usd=10)
    with AccountLock(987654321):
        with pytest.raises(RuntimeError):
            with AccountLock(987654321):
                pass


def test_account_can_be_unset_only_while_live_is_disabled(settings, live):
    values = {**live.model_dump(), "account_seq": None, "enabled": False}
    unbound = LiveConfig.model_validate(values)
    with pytest.raises(ValueError, match="offline only"):
        unbound.strategy()
    with pytest.raises(ValidationError, match="account must be selected"):
        LiveConfig.model_validate({**values, "enabled": True})
    settings.toss_account_seq = None
    with pytest.raises(ConfigurationError, match="account does not match"):
        LiveBroker(settings, unbound, armed=True)


def test_live_file_resolves_account_from_environment_without_overriding_explicit_account(
    tmp_path,
    monkeypatch,
    settings,
):
    path = tmp_path / "live.toml"
    contents = """enabled = false
capital_usd = "1490"
max_order_usd = "298"
daily_loss_usd = "14.90"
"""
    path.write_text(contents)
    assert load_live_config(path).account_seq is None
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "7")
    assert load_live_config(path).account_seq == 7
    path.write_text(contents + "account_seq = 8\n")
    explicit = load_live_config(path)
    assert explicit.account_seq == 8
    with pytest.raises(ConfigurationError, match="account does not match"):
        LiveBroker(settings, explicit, armed=True)


async def test_diagnostic_connection_failure_is_read_only(settings, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("sensitive details must not be logged")

    monkeypatch.setattr(httpx.AsyncClient, "get", unavailable)
    monkeypatch.setattr(
        "toss_trading.live.doctor.load_config",
        lambda: SimpleNamespace(underlying_signals=True, strategy_name="research_only"),
    )
    report = await diagnose(settings)
    assert report["connection_error"] == "ConnectError" and not report["ready_for_live"]
    assert "sensitive details" not in json.dumps(report)


async def test_actual_broker_fees_and_rejected_partial_fill_are_accounted(setup_live, live):
    store, broker, bridge, config = setup_live
    assert await bridge.reconcile()
    order = intent(datetime.now(UTC))
    add_intent(store, order)
    await bridge.flush()
    broker.details["broker1"] = snapshot(
        order, filled="2", amount="50", fees="0.05", status="REJECTED"
    )
    assert await bridge.reconcile()
    assert store.load()[1].positions["TQQQ"].quantity == 2
    assert not store.load()[1].pending()
    # A restart reads terminal orders as well, including finalized commission.
    broker.details["broker1"]["execution"]["commission"] = "0.10"
    assert await LiveBridge(store, broker, live, config).reconcile()
    state = store.load()[1]
    assert state.cash == D("949.90") and state.fees == D("0.10")
    assert state.positions["TQQQ"].entry_fees == D("0.10")


async def test_acknowledged_tlt_is_preserved_outside_strategy_equity_and_orders(setup_live, live):
    store, broker, _, config = setup_live
    protected = LiveConfig.model_validate(
        {**live.model_dump(), "protected_positions": {"TLT": "0.007468"}}
    )
    broker.external_positions["TLT"] = D("0.007468")
    bridge = LiveBridge(store, broker, protected, config)
    assert await bridge.reconcile()
    state = store.load()[1]
    assert state.positions == {} and state.equity() == 1000
    assert state.live_run_id.endswith(bridge.protected_binding())
    order = intent(datetime.now(UTC))
    add_intent(store, order)
    await bridge.flush()
    broker.details["broker1"] = snapshot(
        order, filled="4", amount="100", fees="0.1", status="FILLED"
    )
    assert await bridge.reconcile()
    assert set(store.load()[1].positions) == {"TQQQ"}
    assert broker.external_positions["TLT"] == D("0.007468")


@pytest.mark.parametrize(
    "changed", [{"TLT": D("0.007469")}, {}, {"TLT": D("0.007468"), "AAPL": D(1)}]
)
async def test_protected_holdings_are_still_checked_for_unexpected_changes(
    setup_live, live, changed
):
    store, broker, _, config = setup_live
    protected = LiveConfig.model_validate(
        {**live.model_dump(), "protected_positions": {"TLT": "0.007468"}}
    )
    broker.external_positions["TLT"] = D("0.007468")
    bridge = LiveBridge(store, broker, protected, config)
    assert await bridge.reconcile()
    broker.external_positions = changed
    assert not await bridge.reconcile()
    assert not store.load()[1].reconciled and broker.create_calls == 0


def test_protected_holdings_cannot_include_trade_symbols_or_invalid_quantities(live):
    for position in (
        {"TQQQ": 1},
        {"SQQQ": 1},
        {"SOXL": 1},
        {"SOXS": 1},
        {"TLT": 0},
        {"TLT": -1},
        {"TLT": "NaN"},
    ):
        with pytest.raises(ValidationError):
            LiveConfig.model_validate({**live.model_dump(), "protected_positions": position})


async def test_bound_protected_holdings_cannot_be_silently_reconfigured(setup_live, live):
    store, broker, _, config = setup_live
    protected = LiveConfig.model_validate(
        {**live.model_dump(), "protected_positions": {"TLT": "0.007468"}}
    )
    broker.external_positions["TLT"] = D("0.007468")
    assert await LiveBridge(store, broker, protected, config).reconcile()
    bound_identity = store.load()[1].live_run_id
    changed = LiveConfig.model_validate(
        {**live.model_dump(), "protected_positions": {"TLT": "0.007469"}}
    )
    broker.external_positions["TLT"] = D("0.007469")
    assert not await LiveBridge(store, broker, changed, config).reconcile()
    assert store.load()[1].live_run_id == bound_identity
