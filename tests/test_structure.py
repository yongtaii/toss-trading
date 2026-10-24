"""Direction groups, prior-session warmup, time-of-day volume, costs, and RSI."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError
from test_trading import account, order, position, quote

from toss_trading.backtest import MemoryStore
from toss_trading.domain import Bar
from toss_trading.engine import TradingEngine
from toss_trading.execution import PaperExecution
from toss_trading.marketdata import offline_session, previous_session
from toss_trading.risk import group_risk, quote_checks, rejection_reasons, size_entry
from toss_trading.strategy import Context, VwapMeanRevert, aggregate, rsi
from toss_trading.trading_config import TradingConfig


def day_minutes(session, symbol="SOXL", start="25", step="-0.01", count=None, volume=100):
    count = count or int((session.close - session.open).total_seconds() // 60)
    rows = []
    for i in range(1, count + 1):
        value = D(start) + D(i) * D(step)
        rows.append(
            Bar(
                symbol=symbol,
                end=session.open + timedelta(minutes=i),
                open=value,
                high=value + D("0.005"),
                low=value - D("0.005"),
                close=value,
                volume=volume,
            )
        )
    return rows


def cfg(**update):
    return TradingConfig.model_validate({**TradingConfig().model_dump(), **update})


# --- 1-1 direction groups -------------------------------------------------------------


def test_opposing_group_blocks_entry_unless_allowed(session):
    c = cfg()
    state = account(session)
    state.positions["SOXL"] = position(session.open, quantity="1")
    now = session.open + timedelta(hours=2)
    state.marks["SOXL"], state.mark_times["SOXL"] = D("25"), now
    _, checks = size_entry(state, "SOXS", D("0.1"), quote(now, "SOXS"), now, session, c)
    assert not checks["opposing_group"]
    assert "OPPOSING_GROUP" in rejection_reasons(checks)
    allowed = cfg(allow_opposing_groups=True)
    _, checks = size_entry(state, "SOXS", D("0.1"), quote(now, "SOXS"), now, session, allowed)
    assert checks["opposing_group"]
    # Pending opposing buys count as exposure too.
    state = account(session)
    state.orders["one"] = order(now - timedelta(seconds=5), symbol="SQQQ")
    _, checks = size_entry(state, "TQQQ", D("0.1"), quote(now, "TQQQ"), now, session, c)
    assert not checks["opposing_group"]


def test_group_risk_budget_exhausted_and_limits_quantity(session):
    c = cfg()
    now = session.open + timedelta(hours=2)
    state = account(session)
    held = position(session.open, quantity="50")  # 50 shares x $1 initial risk = $50 = 0.5%.
    state.positions["SOXL"] = held
    state.cash -= 50 * 25  # Equity stays $10,000 at the $25 mark.
    state.marks["SOXL"], state.mark_times["SOXL"] = D("25"), now
    quantity, checks = size_entry(state, "TQQQ", D("0.1"), quote(now, "TQQQ"), now, session, c)
    assert quantity == 0 and not checks["group_risk"]
    assert "GROUP_RISK_LIMIT" in rejection_reasons(checks)
    held.quantity, state.cash = D("40"), state.cash + 10 * 25  # $10 of group budget left.
    quantity, checks = size_entry(state, "TQQQ", D("0.1"), quote(now, "TQQQ"), now, session, c)
    per_share = (
        D("0.1") * c.stop_atr
        + D("26.101")
        * (1 + c.slippage_bps / 10000)
        * (c.entry_cost_bps + c.exit_cost_bps + c.slippage_bps)
        / 10000
    )
    assert (
        checks["group_risk"]
        and 0 < quantity <= (state.equity(c.exit_cost_bps) * D("0.005") - 40) / per_share
    )
    # The other group has its own budget.
    _, checks = size_entry(
        state, "SOXS", D("0.1"), quote(now, "SOXS"), now, session, cfg(allow_opposing_groups=True)
    )
    assert checks["group_risk"]


def test_group_risk_counts_partial_fills_once(session):
    c = cfg()
    state = account(session)
    state.positions["SOXL"] = position(session.open, quantity="10")  # 10 x $1.
    partial = order(session.open, symbol="SOXL", quantity="30")
    partial.filled, partial.atr = D("10"), D("0.5")
    state.orders["partial"] = partial
    # Filled shares are in the position; only the 20 unfilled shares add order risk.
    assert group_risk(state, "NASDAQ_LONG", c) == 10 * 1 + 20 * D("0.5") * c.stop_atr
    assert group_risk(state, "NASDAQ_SHORT", c) == 0
    assert group_risk(state, None, c) == 0


@pytest.mark.parametrize(
    "update",
    [
        {"groups": {"A": ["SOXL"], "B": ["SOXL"]}, "opposing_groups": []},
        {"groups": {"A": ["AAPL"]}, "opposing_groups": []},
        {"groups": {"A": ["SOXL"]}, "opposing_groups": [["A", "MISSING"]]},
    ],
)
def test_group_configuration_validation(update):
    with pytest.raises(ValidationError):
        cfg(**update)


def test_engine_logs_opposing_group_rejection(session):
    c = cfg(indicator_warmup="intraday_only", volume_mode="rolling")
    store = MemoryStore(c)
    engine = TradingEngine(c, store)
    state = store.state
    state.cash, state.reconciled, state.day = D("10000"), True, session.day
    state.session, state.day_equity = session, D("10000")
    rows = day_minutes(session, "SOXS", count=110)
    now = rows[-1].end
    state.positions["SOXL"] = position(session.open, quantity="1")
    records = engine.step(now, session, rows, [quote(now, "SOXL"), quote(now, "SOXS")])
    rejected = [r for r in records if r["event"] == "ENTRY_REJECTED"]
    assert rejected and "OPPOSING_GROUP" in rejected[0]["reasons"]


# --- 1-2 prior-session warmup ---------------------------------------------------------


def test_prior_seed_allows_entry_at_entry_after_minutes(session):
    prior_session = previous_session(date.fromisoformat(session.day))
    prior = tuple(aggregate(day_minutes(prior_session, start="25.2", step="-0.0005")))
    strategy = VwapMeanRevert(cfg())
    early = strategy.evaluate(day_minutes(session, count=10), session, Context(prior=prior))
    assert early.indicators is not None and not early.checks["after_open"]
    on_time = strategy.evaluate(day_minutes(session, count=15), session, Context(prior=prior))
    assert on_time.indicators is not None and on_time.checks["after_open"]
    assert not any(n.startswith("WARMUP_FALLBACK") for n in on_time.notes)
    # Same data without seeding is still warming up at 15 minutes.
    plain = VwapMeanRevert(cfg(indicator_warmup="intraday_only")).evaluate(
        day_minutes(session, count=15), session
    )
    assert plain.indicators is None and not plain.notes


@pytest.mark.parametrize(
    "prior,reason",
    [
        ((), "no_prior_data"),
        ("short", "insufficient_prior_data"),
        ("split", "suspected_split"),
    ],
)
def test_warmup_fallbacks_are_reported(session, prior, reason):
    prior_session = previous_session(date.fromisoformat(session.day))
    if prior == "short":
        prior = tuple(aggregate(day_minutes(prior_session, count=50)))
    elif prior == "split":
        prior = tuple(aggregate(day_minutes(prior_session, start="50", step="0")))
    decision = VwapMeanRevert(cfg()).evaluate(
        day_minutes(session, count=110), session, Context(prior=prior)
    )
    assert f"WARMUP_FALLBACK:{reason}" in decision.notes
    assert decision.indicators is not None  # Intraday-only after 105 minutes.
    assert (
        VwapMeanRevert(cfg())
        .evaluate(day_minutes(session, count=15), session, Context(prior=prior))
        .indicators
        is None
    )


def run_days(config, days, symbol="SOXL", minutes=None):
    store = MemoryStore(config)
    engine = TradingEngine(config, store)
    out = []
    for day in days:
        session = offline_session(day)
        rows = day_minutes(session, symbol, count=minutes)
        records = engine.step(rows[-1].end, session, rows, [])
        out.append((session, records))
    return store.state, out, engine


@pytest.mark.parametrize(
    "first,second",
    [
        (date(2026, 11, 25), date(2026, 11, 27)),  # After the Thanksgiving holiday.
        (date(2026, 10, 30), date(2026, 11, 2)),  # Across the DST change (UTC open shifts).
        (date(2026, 11, 27), date(2026, 11, 30)),  # After an early close (210 minutes).
    ],
)
def test_engine_carries_prior_session_across_calendar_edges(first, second):
    c = cfg()
    state, _, engine = run_days(c, [first])
    session = offline_session(second)
    rows = day_minutes(session, count=15)
    records = engine.step(rows[-1].end, session, rows, [])
    assert state.prior_day == first.isoformat()
    decision = [r for r in records if r["event"] == "DECISION"][-1]
    assert decision["indicators"] is not None
    assert not any(r["event"] == "WARMUP_FALLBACK" for r in records)


def test_missing_prior_day_or_gappy_prior_falls_back():
    c = cfg()
    # 2026-10-21 data is missing entirely: 10-22's prior (10-21) is not in the ledger.
    state, _, engine = run_days(c, [date(2026, 10, 20)])
    session = offline_session(date(2026, 10, 22))
    rows = day_minutes(session, count=15)
    records = engine.step(rows[-1].end, session, rows, [])
    assert state.prior_day == ""
    fallback = [r for r in records if r["event"] == "WARMUP_FALLBACK"]
    assert fallback and fallback[0]["reason"] == "no_prior_data"
    # A gap at minute 50 leaves only 45 contiguous minutes (9 bars) to seed from.
    store = MemoryStore(c)
    engine = TradingEngine(c, store)
    first = offline_session(date(2026, 10, 20))
    gappy = day_minutes(first)
    del gappy[49]
    engine.step(gappy[-1].end, first, gappy, [])
    session = offline_session(date(2026, 10, 21))
    rows = day_minutes(session, count=15)
    records = engine.step(rows[-1].end, session, rows, [])
    assert len(store.state.prior_bars["SOXL"]) == 9
    assert any(r.get("reason") == "insufficient_prior_data" for r in records)


def test_seed_prior_used_when_ledger_is_fresh():
    c = cfg()
    store = MemoryStore(c)
    engine = TradingEngine(c, store)
    session = offline_session(date(2026, 10, 21))
    prior = engine.needs_prior(session)
    assert prior.day == "2026-10-20"
    engine.seed_prior(prior, day_minutes(prior))
    assert engine.needs_prior(session) is None
    rows = day_minutes(session, count=15)
    records = engine.step(rows[-1].end, session, rows, [])
    # The day rollover keeps the fetched seed instead of discarding it.
    assert store.state.prior_day == "2026-10-20"
    assert [r for r in records if r["event"] == "DECISION"][-1]["indicators"] is not None
    assert (
        TradingEngine(cfg(indicator_warmup="intraday_only"), MemoryStore(c)).needs_prior(session)
        is None
    )


# --- 1-3 time-of-day volume -----------------------------------------------------------


def test_time_of_day_volume_ratio_and_insufficient_history(session):
    rows = day_minutes(session, count=110, volume=100)  # Each 5m bar: 500 volume.
    full = Context(slot_volumes=tuple([D(250)] * 20))
    decision = VwapMeanRevert(cfg(indicator_warmup="intraday_only")).evaluate(rows, session, full)
    assert decision.indicators.volume_ratio == 2 and "volume" not in decision.checks
    short = Context(slot_volumes=tuple([D(250)] * 19))
    blocked = cfg(indicator_warmup="intraday_only", min_volume_ratio="1.5")
    decision = VwapMeanRevert(blocked).evaluate(rows, session, short)
    assert "VOLUME_HISTORY_INSUFFICIENT" in decision.notes
    assert decision.indicators.volume_ratio is None and not decision.checks["volume"]
    assert decision.signal != "BUY_SIGNAL"
    rolling = cfg(indicator_warmup="intraday_only", min_volume_ratio="1", volume_fallback="rolling")
    decision = VwapMeanRevert(rolling).evaluate(rows, session, short)
    assert decision.indicators.volume_ratio == 1 and decision.checks["volume"]
    assert VwapMeanRevert(blocked).evaluate(rows, session, full).checks["volume"]


def test_slot_history_skips_early_close_and_caps_lookback():
    c = cfg(volume_lookback_days=2)
    days = [
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
        date(2026, 12, 1),
    ]
    state, _, _ = run_days(c, days)
    slots = state.slot_volumes["SOXL"]
    # 11-24, 11-25 and 11-30 are full sessions; the 11-27 early close is excluded.
    assert len(slots) == 78 and len(slots[0]) == 2
    assert 77 in slots and 78 not in slots


# --- 1-4 costs ------------------------------------------------------------------------


def test_cost_items_in_filter_risk_budget_and_sell_fees(session):
    now = session.open + timedelta(hours=2)
    q = quote(now)
    assert quote_checks(q, now, cfg(fx_spread_bps="10"))["cost"]
    assert not quote_checks(q, now, cfg(fx_spread_bps="20"))["cost"]
    assert not quote_checks(q, now, cfg(sec_fee_bps="35"))["cost"]
    c = cfg(fx_spread_bps="5", sec_fee_bps="0.3", taf_per_share="0.0002", taf_max_usd="8")
    assert c.entry_cost_bps == 15 and c.exit_cost_bps == D("15.3")
    assert c.sell_charges(D(100), D(50)) == D(100) * 50 * D("15.3") / 10000 + D("0.02")
    assert c.sell_charges(D(100000), D(1)) == D(100000) * D("15.3") / 10000 + 8
    state = account(session)
    qty, _ = size_entry(state, "SOXL", D("0.5"), q, now, session, c)
    price = q.ask * (1 + c.slippage_bps / 10000)
    per_share = (
        D("0.5") * c.stop_atr
        + c.taf_per_share
        + price * (c.entry_cost_bps + c.exit_cost_bps + c.slippage_bps) / 10000
    )
    assert qty == int(D(10000) * c.risk_fraction / per_share)


def test_paper_sell_applies_regulatory_charges_and_tracks_slippage(session):
    c = cfg(sec_fee_bps="0.3", taf_per_share="0.01", taf_max_usd="0.05")
    now = session.open + timedelta(hours=1)
    state = account(session)
    state.positions["SOXL"] = position(session.open, quantity="10")
    state.cash -= 250
    sell = order(now - timedelta(seconds=1), side="SELL", key="sell")
    state.orders["sell"] = sell
    PaperExecution(c).process(state, quote(now, price="26"), now)
    trade = state.trades[-1]
    price = D("26") * (1 - c.slippage_bps / 10000)
    assert D(trade["exit_fee"]) == D(10) * price * c.exit_cost_bps / 10000 + D("0.05")
    assert D(trade["slippage"]) == D(10) * (D("26") - price)


def test_legacy_costs_match_previous_formulas():
    legacy = cfg(fee_bps="2", slippage_bps="3")
    assert legacy.entry_cost_bps == legacy.exit_cost_bps == 2
    assert legacy.sell_charges(D(10), D(20)) == D(10) * 20 * 2 / 10000


# --- RSI ------------------------------------------------------------------------------


def test_rsi_values():
    assert rsi([D(1), D(2), D(1), D(2), D(1)], 2) == D("37.5")
    assert rsi([D(i) for i in range(20)], 14) == 100
    assert rsi([D(20 - i) for i in range(20)], 14) == 0
    assert rsi([D(5)] * 20, 14) == 50
    assert rsi([D(1)] * 14, 14) is None


def test_rsi_filter_on_off(session):
    rows = day_minutes(session, count=105)
    bounce = rows[-1].close
    for i in range(1, 6):
        value = bounce + D("0.02") * i
        rows.append(
            rows[-1].model_copy(
                update={
                    "end": rows[-1].end + timedelta(minutes=1),
                    "open": value,
                    "close": value,
                    "high": value + D("0.005"),
                    "low": value - D("0.005"),
                }
            )
        )
    base = cfg(indicator_warmup="intraday_only")
    off = VwapMeanRevert(base).evaluate(rows, session)
    assert "rsi" not in off.checks and off.signal == "BUY_SIGNAL"
    level = off.indicators.rsi
    assert 0 < level < 30
    allowed = VwapMeanRevert(cfg(indicator_warmup="intraday_only", rsi_entry_max=level))
    assert allowed.evaluate(rows, session).signal == "BUY_SIGNAL"
    blocked = VwapMeanRevert(cfg(indicator_warmup="intraday_only", rsi_entry_max=level - 1))
    decision = blocked.evaluate(rows, session)
    assert not decision.checks["rsi"] and decision.signal == "WAIT"
