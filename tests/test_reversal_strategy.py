"""Rebound confirmation and cost/risk hurdles use completed candles only."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_structure import cfg

from toss_trading.domain import Bar
from toss_trading.strategy import Decision, Indicators, VwapReversal, create_strategy


def candles(session, close="90"):
    bars = []
    for minute in range(1, 11):
        current = minute > 5
        bars.append(
            Bar(
                symbol="SOXL",
                end=session.open + timedelta(minutes=minute),
                open="88.5" if current else "89",
                high=max(D(close), D("89")) if current else D("89"),
                low=min(D(close), D("88")),
                close=close if current else "88.5",
                volume=100,
            )
        )
    return bars


def base(**update):
    ind = Indicators(
        close=D("90"),
        vwap=D("100"),
        ema_fast=D("91"),
        ema_slow=D("92"),
        volume_ratio=D("1"),
        atr=D("2"),
        volume=D("100"),
        volume_average=D("100"),
    )
    return Decision(
        "WAIT",
        {"after_open": True, "before_close": True, "atr_positive": True},
        replace(ind, **update),
    )


def test_falling_price_is_blocked_despite_being_oversold(session):
    config = cfg(strategy_name="vwap_reversal")
    strategy = create_strategy(config.strategy_name, config)
    assert isinstance(strategy, VwapReversal)
    result = strategy.decide(candles(session, close="87"), session, base(close=D("87")))
    assert result.checks["stretched_below_vwap"]
    assert not result.checks["confirmed_reversal"]
    assert result.signal == "WAIT"


def test_rebound_with_room_for_costs_and_stop_is_allowed(session):
    result = VwapReversal(cfg()).decide(candles(session), session, base())
    assert result.signal == "BUY_SIGNAL"
    assert all(result.checks.values())


def test_small_recovery_does_not_pay_round_trip_costs(session):
    result = VwapReversal(cfg()).decide(
        candles(session), session, base(vwap=D("90.5"), atr=D("0.1"))
    )
    assert result.checks["confirmed_reversal"]
    assert result.checks["stretched_below_vwap"]
    assert not result.checks["recovery_covers_costs"]
    assert result.signal == "WAIT"


def test_costs_fit_but_net_reward_does_not_cover_stop(session):
    result = VwapReversal(cfg()).decide(candles(session), session, base(vwap=D("94.2")))
    assert result.checks["recovery_covers_costs"]
    assert not result.checks["net_reward_covers_stop"]
    assert result.signal == "WAIT"


def test_profit_target_caps_expected_recovery(session):
    result = VwapReversal(cfg(reward_r="0.1")).decide(candles(session), session, base())
    assert not result.checks["recovery_covers_costs"]
    assert result.signal == "WAIT"


def test_exit_is_not_blocked_by_entry_filters(session):
    result = VwapReversal(cfg()).decide(
        candles(session, close="87"), session, base(close=D("87"), vwap=D("86"))
    )
    assert not result.checks["confirmed_reversal"]
    assert result.signal == "VWAP_EXIT"


@pytest.mark.parametrize("check", ["after_open", "before_close", "rsi", "volume"])
def test_existing_entry_gates_remain_required(session, check):
    decision = base()
    blocked = replace(decision, checks={**decision.checks, check: False})
    assert VwapReversal(cfg()).decide(candles(session), session, blocked).signal == "WAIT"
