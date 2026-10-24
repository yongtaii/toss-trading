from datetime import date, timedelta
from decimal import Decimal as D

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from toss_trading.backtest import performance, read_csv, replay, split_backtest
from toss_trading.domain import Bar, Order, OrderStatus, Position, Quote, State
from toss_trading.engine import TradingEngine
from toss_trading.execution import LiveTossExecution, PaperExecution
from toss_trading.marketdata import broker_session, offline_session, parse_candle, parse_quote
from toss_trading.notifications import dispatch
from toss_trading.persistence import ConcurrentUpdate, Store, events, outbox
from toss_trading.risk import quote_checks, size_entry
from toss_trading.strategy import VwapBreakout, aggregate, ema
from toss_trading.trading_config import TradingConfig


def minutes(session, count=110, symbol="SOXL", descending=False):
    result = []
    for i in range(1, count + 1):
        value = D("25") + D(i) * (D("-0.01") if descending else D("0.01"))
        result.append(
            Bar(
                symbol=symbol,
                end=session.open + timedelta(minutes=i),
                open=value,
                high=value + D("0.005"),
                low=value - D("0.005"),
                close=value,
                volume=1000 if i > count - 5 else 100,
            )
        )
    return result


def quote(now, symbol="SOXL", price="26.10", size="10000"):
    return Quote(
        symbol=symbol,
        at=now,
        bid=D(price),
        ask=D(price) + D("0.001"),
        bid_size=D(size),
        ask_size=D(size),
    )


def position(now, symbol="SOXL", quantity="10"):
    return Position(
        symbol=symbol,
        quantity=D(quantity),
        entry=D("25"),
        entry_fees=D("0.05"),
        stop=D("24"),
        target=D("27"),
        opened=now,
        initial_risk=D("1"),
    )


def order(now, symbol="SOXL", side="BUY", key="one", quantity="10"):
    return Order(
        id=key,
        symbol=symbol,
        side=side,
        quantity=D(quantity),
        submitted=now,
        limit=D("27"),
        atr=D("0.5"),
        reason="BUY_SIGNAL" if side == "BUY" else "STOP",
        reserved=D(quantity) * D("27.01") if side == "BUY" else D(0),
    )


def account(session):
    return State(cash=D("10000"), reconciled=True, day=session.day, day_equity=D("10000"))


def save(store, state):
    rev, _ = store.load()
    store.commit(rev, state, [])


def test_default_user_symbols_and_configuration(config):
    assert config.symbols == ("SOXL", "SOXS", "TQQQ", "SQQQ")
    assert config.mode == "PAPER"
    for change in (
        {"risk_fraction": "0.006"},
        {"daily_loss_fraction": "0.02"},
        {"symbol_fraction": "0.21"},
        {"max_positions": 4},
        {"cooldown_minutes": 29},
        {"max_entries": 4},
        {"ema_fast": 22},
        {"symbols": ["SOXL", "SOXL"]},
        {"symbols": ["soxl"]},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            TradingConfig(**change)
    with pytest.raises(RuntimeError, match="LIVE is blocked"):
        LiveTossExecution()


def test_indicators_and_buy_signal(config, session):
    rows = minutes(session)
    decision = VwapBreakout(config).evaluate(rows, session)
    assert decision.signal == "BUY_SIGNAL"
    assert all(decision.checks.values())
    ind = decision.indicators
    assert ind.volume_ratio == 10  # Current volume excluded from prior 20-bar denominator.
    assert ind.close > ind.ema_fast > ind.ema_slow
    assert ind.close > ind.vwap
    assert ind.breakout_high == rows[-6].high
    assert ind.atr > 0
    assert len(aggregate(rows)) == 22
    assert ema([D(1), D(2), D(3), D(4)], 3) == 3
    expected = sum((b.high + b.low + b.close) / 3 * b.volume for b in rows)
    expected /= sum(b.volume for b in rows)
    assert ind.vwap == expected


@pytest.mark.parametrize("kind", ["volume", "breakout", "gap", "warmup", "incomplete", "zero"])
def test_technical_conditions_reject(config, session, kind):
    rows = minutes(session)
    if kind == "volume":
        rows = [b.model_copy(update={"volume": D(100)}) for b in rows]
    elif kind == "breakout":
        rows[-6] = rows[-6].model_copy(update={"high": D(30)})
    elif kind == "gap":
        rows.pop(40)
    elif kind == "warmup":
        rows = rows[:15]
    elif kind == "incomplete":
        rows = rows[:-1]
    else:
        rows = [b.model_copy(update={"volume": D(0)}) for b in rows]
    assert VwapBreakout(config).evaluate(rows, session).signal != "BUY_SIGNAL"


def test_trend_exit_and_session_vwap_reset(config, session):
    decision = VwapBreakout(config).evaluate(minutes(session, descending=True), session)
    assert decision.signal == "TREND_EXIT"
    assert not decision.checks["above_vwap"]
    assert not decision.checks["above_ema"]
    assert not decision.checks["ema_uptrend"]
    next_day = session.model_copy(
        update={
            "open": session.open + timedelta(days=1),
            "close": session.close + timedelta(days=1),
        }
    )
    assert VwapBreakout(config).evaluate(minutes(session), next_day).indicators is None


@pytest.mark.parametrize("offset,allowed", [(14, False), (15, True), (360, True), (361, False)])
def test_entry_session_boundaries(config, session, offset, allowed):
    now = session.open + timedelta(minutes=offset)
    quantity, checks = size_entry(
        account(session), "SOXL", D("0.2"), quote(now), now, session, config
    )
    assert checks["entry_window"] is allowed
    assert (quantity > 0) is allowed


@pytest.mark.parametrize(
    "rule",
    [
        "held",
        "pending",
        "halted",
        "loss",
        "slots",
        "count",
        "cooldown",
        "unreconciled",
        "unselected",
        "cash",
        "stale_marks",
    ],
)
def test_risk_guards(config, session, rule):
    now = session.open + timedelta(minutes=120)
    state = account(session)
    symbol = "SOXL"
    if rule == "held":
        state.positions[symbol] = position(now)
    elif rule == "pending":
        state.orders["one"] = order(now)
    elif rule == "halted":
        state.loss_halted = True
    elif rule == "loss":
        state.cash = D("9899")
    elif rule == "slots":
        for s in ("SOXS", "TQQQ", "SQQQ"):
            state.orders[s] = order(now, s, key=s)
    elif rule == "count":
        state.entries[symbol] = 3
    elif rule == "cooldown":
        state.cooldowns[symbol] = now + timedelta(minutes=1)
    elif rule == "unreconciled":
        state.reconciled = False
    elif rule == "unselected":
        symbol = "AAPL"
    elif rule == "cash":
        state.cash = D("0.01")
        state.day_equity = D("0.01")
    else:
        state.positions["SOXS"] = position(now, "SOXS")
    quantity, _ = size_entry(state, symbol, D("0.2"), quote(now, symbol), now, session, config)
    assert quantity == 0


def test_sizing_risk_cash_weight_cost_and_cooldown_expiry(config, session):
    now = session.open + timedelta(minutes=120)
    state = account(session)
    state.cooldowns["SOXL"] = now
    q = quote(now)
    qty, checks = size_entry(state, "SOXL", D("0.8"), q, now, session, config)
    price = q.ask * (1 + config.slippage_bps / 10000)
    cost = price * (1 + config.fee_bps / 10000)
    risk = D("0.8") * config.stop_atr + price * (2 * config.fee_bps + config.slippage_bps) / 10000
    assert qty == int(qty) and qty > 0 and all(checks.values())
    assert qty * risk <= state.day_equity * config.risk_fraction
    assert qty * cost <= min(
        state.cash, state.day_equity * config.symbol_fraction, config.max_order_usd
    )
    state.orders["other"] = order(now, "SOXS", key="other", quantity="365")
    smaller, _ = size_entry(state, "SOXL", D("0.8"), q, now, session, config)
    assert smaller < qty


@pytest.mark.parametrize("case", ["stale", "future", "spread", "cost"])
def test_quote_quality(config, session, case):
    now = session.open + timedelta(hours=2)
    q = quote(now)
    if case == "stale":
        q.at = now - timedelta(seconds=11)
    elif case == "future":
        q.at = now + timedelta(seconds=1)
    elif case == "spread":
        q.ask = q.bid + 1
    else:
        config = TradingConfig(max_cost_bps="1")
    assert not all(quote_checks(q, now, config).values())


def test_partial_fill_no_same_event_fill_and_protective_levels(config, session):
    now = session.open + timedelta(hours=2)
    state = account(session)
    execution = PaperExecution(config)
    item = order(now)
    execution.submit(state, item)
    assert execution.process(state, quote(now), now) == []
    later = now + timedelta(seconds=1)
    q = quote(later, size="20")
    execution.process(state, q, later)
    assert item.status == OrderStatus.PARTIALLY_FILLED and item.filled == 2
    assert state.entries["SOXL"] == 1
    assert execution.process(state, q, later) == []
    p = state.positions["SOXL"]
    assert p.entry - p.stop == config.stop_atr * item.atr
    assert p.target - p.entry == config.reward_r * config.stop_atr * item.atr
    execution.process(state, quote(later + timedelta(seconds=1)), later + timedelta(seconds=1))
    assert item.status == OrderStatus.FILLED and item.reserved == 0
    assert state.entries["SOXL"] == 1


def test_rejected_unknown_duplicate_and_oversell(config, session):
    now = session.open + timedelta(hours=2)
    execution = PaperExecution(config)
    state = account(session)
    item = order(now)
    execution.submit(state, item)
    with pytest.raises(ValueError, match="Duplicate"):
        execution.submit(state, order(now, key="two"))
    item.status = OrderStatus.UNKNOWN
    execution.reconcile(state)
    assert not state.reconciled
    assert (
        execution.process(state, quote(now + timedelta(seconds=1)), now + timedelta(seconds=1))
        == []
    )
    item.status, state.cash = OrderStatus.SUBMITTED, D(0)
    records = execution.process(
        state, quote(now + timedelta(seconds=1)), now + timedelta(seconds=1)
    )
    assert item.status == OrderStatus.REJECTED
    assert records[0]["event"] == "ORDER_FAILED"
    state.positions["SOXL"] = position(now)
    with pytest.raises(ValueError, match="Oversell"):
        execution.submit(state, order(now, side="SELL", key="three", quantity="11"))
    with pytest.raises(ValueError, match="symbol"):
        execution.submit(state, order(now, symbol="AAPL", key="four"))


def test_stop_partial_fills_fees_and_cooldown(config, session):
    now = session.open + timedelta(hours=2)
    state = account(session)
    state.positions["SOXL"] = position(now)
    execution = PaperExecution(config)
    item = order(now, side="SELL")
    execution.submit(state, item)
    later = now + timedelta(seconds=1)
    records = execution.process(state, quote(later, price="23", size="20"), later)
    assert state.positions["SOXL"].quantity == 8
    assert item.status == OrderStatus.PARTIALLY_FILLED
    assert state.cooldowns["SOXL"] == later + timedelta(minutes=30)
    assert state.realized < -4
    assert any(r["event"] == "STOP" for r in records)
    execution.process(
        state, quote(later + timedelta(seconds=1), price="23"), later + timedelta(seconds=1)
    )
    assert "SOXL" not in state.positions
    assert item.status == OrderStatus.FILLED
    assert sum(D(t["pnl"]) for t in state.trades) == state.realized


def test_engine_idempotency_restart_and_future_bars(config, session, store):
    engine = TradingEngine(config, store)
    rows = minutes(session)
    now = rows[-1].end
    records = engine.step(now, session, rows, [quote(now)])
    assert any(r["event"] == "ORDER_SUBMITTED" for r in records)
    _, state = store.load()
    assert len(state.orders) == 1 and not state.positions
    engine = TradingEngine(config, store)
    engine.step(now, session, rows, [quote(now)])
    assert len(store.load()[1].orders) == 1
    later = now + timedelta(seconds=1)
    engine.step(later, session, [], [quote(later)])
    assert store.load()[1].positions["SOXL"].quantity > 0
    records = engine.step(
        later, session, [rows[-1].model_copy(update={"end": now + timedelta(minutes=5)})], []
    )
    assert records[0]["event"] == "REJECTED_DATA"
    with pytest.raises(ValueError, match="Out-of-order"):
        engine.step(now, session, [], [])


@pytest.mark.parametrize(
    "reason,price", [("STOP", "23"), ("TARGET", "28"), ("SESSION_FLATTEN", "25")]
)
def test_engine_exit_priority_and_no_duplicate(config, session, store, reason, price):
    engine = TradingEngine(config, store)
    now = session.close - timedelta(minutes=14)
    state = account(session)
    state.positions["SOXL"] = position(session.open)
    state.cash -= 250
    save(store, state)
    engine.step(now, session, [], [quote(now, price=price)])
    pending = store.load()[1].pending()
    assert len(pending) == 1 and pending[0].reason == reason
    engine.step(now, session, [], [quote(now, price=price)])
    assert len(store.load()[1].orders) == 1
    engine.step(
        now + timedelta(seconds=1), session, [], [quote(now + timedelta(seconds=1), price=price)]
    )
    assert not store.load()[1].positions


def test_loss_latch_keeps_exits_and_unknown_not_retried(config, session, store):
    engine = TradingEngine(config, store)
    now = session.open + timedelta(hours=2)
    state = account(session)
    state.cash = D("9600")
    state.positions["SOXL"] = position(session.open)
    save(store, state)
    records = engine.step(now, session, [], [quote(now, price="23")])
    assert store.load()[1].loss_halted
    assert any(r["event"] == "DAILY_LOSS_LIMIT" for r in records)
    assert store.load()[1].pending()[0].side == "SELL"
    state = store.load()[1]
    state.pending()[0].status = OrderStatus.UNKNOWN
    save(store, state)
    engine = TradingEngine(config, store)
    engine.step(
        now + timedelta(seconds=1), session, [], [quote(now + timedelta(seconds=1), price="23")]
    )
    assert len(store.load()[1].orders) == 1
    assert not store.load()[1].reconciled


def test_flatten_alert_stale_prices_and_no_after_hours_fill(config, session, store):
    engine = TradingEngine(config, store)
    state = account(session)
    state.positions["SOXL"] = position(session.open)
    state.cash -= 250
    save(store, state)
    now = session.close - timedelta(minutes=5)
    records = engine.step(now, session, [], [quote(now - timedelta(seconds=11))])
    assert not store.load()[1].orders
    assert any(r["event"] == "FLATTEN_UNRESOLVED" for r in records)
    records = engine.step(session.close, session, [], [quote(session.close)])
    assert store.load()[1].positions
    assert any(r["event"] == "DAILY_SUMMARY" for r in records)


def test_multi_symbol_slots_and_cash_reservations(config, session, store):
    engine = TradingEngine(config, store)
    rows = [b for symbol in config.symbols for b in minutes(session, symbol=symbol)]
    now = max(b.end for b in rows)
    engine.step(now, session, rows, [quote(now, s) for s in config.symbols])
    state = store.load()[1]
    assert len(state.pending()) == 3
    assert sum(o.reserved for o in state.pending()) <= state.cash
    assert not state.positions


def test_optimistic_concurrency_rolls_back_events_and_config_change(config, store):
    rev, state = store.load()
    store.commit(rev, state, [{"event": "first"}])
    with pytest.raises(ConcurrentUpdate):
        store.commit(rev, state, [{"event": "duplicate"}])
    with store.engine.connect() as conn:
        assert len(conn.execute(select(events)).all()) == 1
    with pytest.raises(ValueError, match="Configuration differs"):
        Store(str(store.engine.url), TradingConfig(volume_ratio="2"))


def test_calendar_holiday_dst_early_close_and_official_parse():
    assert offline_session(date(2026, 7, 3)) is None
    assert offline_session(date(2026, 10, 24)) is None
    assert offline_session(date(2026, 3, 6)).open.hour == 14
    assert offline_session(date(2026, 3, 9)).open.hour == 13
    early = offline_session(date(2026, 11, 27))
    assert (early.close - early.open).total_seconds() == 210 * 60
    assert (
        broker_session({"today": {"date": "2026-07-03", "regularMarket": None}}, date(2026, 7, 3))
        is None
    )
    with pytest.raises(ValueError):
        broker_session({"today": {"date": "2026-07-03"}}, date(2026, 7, 4))


def test_parse_real_shapes_and_invalid_market_data(session):
    q = parse_quote(
        "SOXL",
        {
            "timestamp": session.open.isoformat(),
            "currency": "USD",
            "bids": [{"price": "20", "volume": "50"}],
            "asks": [{"price": "20.02", "volume": "60"}, {"price": "20.01", "volume": "70"}],
        },
    )
    assert q.ask == D("20.01")
    b = parse_candle(
        "SOXL",
        {
            "timestamp": (session.open + timedelta(minutes=1)).isoformat(),
            "currency": "USD",
            "openPrice": "20",
            "highPrice": "21",
            "lowPrice": "19",
            "closePrice": "20.5",
            "volume": "100",
        },
    )
    assert b.end == session.open + timedelta(minutes=1)
    with pytest.raises(ValidationError):
        Quote(symbol="SOXL", at=session.open, bid=21, ask=20, bid_size=1, ask_size=1)
    with pytest.raises(ValueError):
        parse_quote("SOXL", {"currency": "USD", "timestamp": None})


async def test_slack_outbox_delivery_and_no_duplicate(store):
    rev, state = store.load()
    store.commit(rev, state, [{"event": "STOP", "notify": True}])
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, text="ok")

    url = "https://hooks.slack.com/services/test/test/test"
    assert await dispatch(store, url, transport=httpx.MockTransport(handler)) == 1
    assert await dispatch(store, url, transport=httpx.MockTransport(handler)) == 0
    assert len(seen) == 1
    with pytest.raises(ValueError):
        await dispatch(store, "https://example.com/services/test")


async def test_slack_timeout_unknown_does_not_resend(store):
    rev, state = store.load()
    store.commit(rev, state, [{"event": "API_ERROR", "notify": True}])

    def handler(req):
        raise httpx.ReadTimeout("secret", request=req)

    assert (
        await dispatch(
            store, "https://hooks.slack.com/services/a/b/c", transport=httpx.MockTransport(handler)
        )
        == 0
    )
    with store.engine.connect() as conn:
        assert conn.execute(select(outbox.c.status)).scalar_one() == "UNKNOWN"


def test_backtest_same_engine_no_signal_close_fill_and_metrics(config, session):
    rows = minutes(session, count=120)
    # Preserve a breakout on bar 22, then let the subsequent minute supply fills.
    for i, bar in enumerate(rows):
        bar.volume = D(1000 if 105 <= i < 110 else 100)
    report = replay(rows, config)
    assert report["submitted_orders"] >= 1
    assert "buy_and_hold" in report
    assert report["minute_coverage"]["2026-10-19:SOXL"] == 120
    assert "unclosed_positions" in report
    # Known equity curve: 10% MDD, 5% total return.
    metrics = performance(State(cash=D(10500)), [D(10000), D(9000), D(10500)], D(10000))
    assert metrics["mdd"] == D("0.1") and metrics["total_return"] == D("0.05")
    assert metrics["profit_factor"] is None


def test_train_validation_and_csv_reject_duplicates(config, session, tmp_path):
    with pytest.raises(ValueError, match="Train and validation"):
        split_backtest(minutes(session), config, date(2026, 10, 20))
    path = tmp_path / "bars.csv"
    path.write_text(
        "symbol,end,open,high,low,close,volume\nAAPL,2026-10-19T13:31:00Z,20,21,19,20,100\n"
    )
    with pytest.raises(ValueError, match="unselected"):
        read_csv(path, config)
    row = "SOXL,2026-10-19T13:31:00Z,20,21,19,20,100\n"
    path.write_text("symbol,end,open,high,low,close,volume\n" + row + row)
    with pytest.raises(ValueError, match="Duplicate"):
        read_csv(path, config)
