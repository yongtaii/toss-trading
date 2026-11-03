"""Retained weekly timing and fail-closed runtime boundaries."""

from datetime import date

import pytest
from test_compare_weekly_allocations import world

from research import weekly_baseline
from toss_trading.backtest import simulate as minute_replay
from toss_trading.strategy import create_strategy
from toss_trading.trading_config import TradingConfig


def test_holiday_week_starts_tuesday_and_midweek_start_waits():
    data = world()
    holiday = weekly_baseline.simulate(data, date(2026, 9, 8), date(2026, 9, 15))
    assert {t["opened"] for t in holiday["trade_log"]} == {"2026-09-08"}
    midweek = weekly_baseline.simulate(data, date(2026, 9, 9), date(2026, 9, 15))
    assert {t["opened"] for t in midweek["trade_log"]} == {"2026-09-14"}


def test_gap_stop_executes_at_next_open_and_no_midweek_reentry():
    data = world()
    for symbol in ("SOXL", "TQQQ"):
        data[date(2026, 9, 8)][symbol]["close"] = 45.0
        data[date(2026, 9, 9)][symbol]["open"] = 30.0
    result = weekly_baseline.simulate(data, date(2026, 9, 8), date(2026, 9, 15))
    stopped = [t for t in result["trade_log"] if t["reason"] == "CLOSE_STOP_NEXT_OPEN"]
    assert len(stopped) == 2
    assert all(t["closed"] == "2026-09-09" for t in stopped)
    assert all(t["exit"] == pytest.approx(30 * 0.99945) for t in stopped)
    assert {t["opened"] for t in result["trade_log"]} == {"2026-09-08", "2026-09-14"}


def test_no_bullish_signal_stays_in_cash():
    data = world()
    for rows in data.values():
        for source in ("QQQ", "SOXX"):
            rows[source] = {"open": 100.0, "close": 100.0}
    result = weekly_baseline.simulate(data, date(2026, 9, 8), date(2026, 9, 15))
    assert result["trades"] == 0
    assert result["total_return"] == result["mdd_close"] == 0
    assert result["average_allocation"] == 0


@pytest.mark.parametrize("name", ["vwap_reversal", "prior_day_breakout", "daily_breakout"])
def test_removed_strategy_cannot_be_instantiated(name):
    with pytest.raises(ValueError, match="Removed strategy"):
        create_strategy(name, TradingConfig())


def test_minute_replay_cannot_silently_report_zero_returns_for_disabled_strategy():
    with pytest.raises(ValueError, match="No runtime strategy"):
        minute_replay([], TradingConfig())
