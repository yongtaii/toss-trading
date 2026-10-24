"""Baselines, random-entry reproducibility, sensitivity, walk-forward, reports, and loader."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from test_structure import cfg, day_minutes

from toss_trading.backtest import (
    MIN_TRADES,
    baselines,
    draw_schedule,
    entry_offsets,
    neighbors,
    percentile,
    performance,
    replay,
    sensitivity,
    walk_forward,
)
from toss_trading.dataload import load
from toss_trading.domain import State
from toss_trading.marketdata import NY, offline_session
from toss_trading.strategy import RandomEntryBaseline, VwapCrossBaseline

DAYS = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)]


def dataset(count=140):
    """Falls until minute 110, then recovers, so the strategy both enters and exits."""
    bars = []
    for day in DAYS:
        session = offline_session(day)
        for symbol, start in (("SOXL", "25"), ("SOXS", "40")):
            rows = day_minutes(session, symbol, start=start, count=count)
            for i, bar in enumerate(rows[110:], start=1):
                value = rows[109].close + D("0.08") * i
                rows[109 + i] = bar.model_copy(
                    update={
                        "open": value,
                        "close": value,
                        "high": value + D("0.005"),
                        "low": value - D("0.005"),
                    }
                )
            bars.extend(rows)
    return sorted(bars, key=lambda b: (b.end, b.symbol))


def quick_config(**update):
    return cfg(indicator_warmup="intraday_only", volume_mode="rolling", **update)


def test_random_schedule_is_seeded_and_respects_session_length():
    offsets = [100, 105, 300, 300]
    days = ["2026-11-25", "2026-11-27"]  # 11-27 closes after 210 minutes.
    first = draw_schedule(offsets, days, ("SOXL", "SOXS"), seed=7)
    assert first == draw_schedule(offsets, days, ("SOXL", "SOXS"), seed=7)
    assert first != draw_schedule(offsets, days, ("SOXL", "SOXS"), seed=8)
    assert len(first) == len(offsets)
    assert all(day == "2026-11-25" for day, _, offset in first if offset == 300)


def test_random_entry_replay_is_reproducible():
    bars, c = dataset(), quick_config()
    schedule = draw_schedule([105, 110], [d.isoformat() for d in DAYS], ("SOXL", "SOXS"), 3)
    one = replay(bars, c, RandomEntryBaseline(c, schedule))
    two = replay(bars, c, RandomEntryBaseline(c, schedule))
    assert one["trade_log"] == two["trade_log"] and one["total_return"] == two["total_return"]


def test_baselines_report_shape_and_determinism():
    bars, c = dataset(), quick_config()
    first = baselines(bars, c, seeds=2, workers=1)
    assert first == baselines(bars, c, seeds=2, workers=1)
    assert first["current"]["total_trades"] > 0
    assert set(first) >= {"current", "vwap_only", "random_entry", "buy_and_hold", "note"}
    assert len(first["random_entry"]["runs"]) == 2
    assert first["random_entry"]["target_entries"] == len(
        entry_offsets(replay(bars, c)["trade_log"])
    )
    assert 0 <= first["current_percentile_vs_random"]["total_return"] <= 100
    assert "sample_warning" in first


def test_vwap_cross_baseline_signals():
    session = offline_session(DAYS[0])
    c = quick_config()
    rows = [b for b in dataset() if b.symbol == "SOXL" and b.end <= session.close][:110]
    assert VwapCrossBaseline(c).evaluate(rows, session).signal == "VWAP_EXIT"
    rising = day_minutes(session, step="0.01", count=110)
    rising[-6] = rising[-6].model_copy(update={"close": D("20"), "low": D("20"), "open": D("20")})
    decision = VwapCrossBaseline(c).evaluate(rising, session)
    assert decision.checks["vwap_cross_up"] and decision.signal == "BUY_SIGNAL"


def test_percentile_definition():
    assert percentile(D(5), [D(1), D(2), D(3), D(4)]) == 100
    assert percentile(D(0), [D(1), D(2)]) == 0
    assert percentile(D(2), [D(1), D(2), D(3)]) == 50


def test_sensitivity_reports_spread_without_recommendation():
    bars, c = dataset(), quick_config(rsi_entry_max="40")
    grid = neighbors(c)
    names = {name for name, _ in grid}
    assert names == {"stretch_atr", "stop_atr", "reward_r", "atr_period", "rsi_entry_max"}
    assert all(isinstance(v, int) for n, v in grid if n == "atr_period")
    result = sensitivity(bars, quick_config(), workers=1)
    assert len(result["grid"]) == 16
    assert set(result["summary"]["stop_atr"]) >= {"total_return", "neighbors_with_pf_above_1"}
    assert not any("best" in key or "recommend" in key for key in result)


def test_walk_forward_windows_use_fixed_parameters():
    result = walk_forward(dataset(), quick_config(), train=1, test=1)
    assert result["parameter_search"] is False
    assert [w["first_day"] for w in result["windows"]] == ["2026-10-20", "2026-10-21"]
    assert set(result["windows"][0]) >= {"return", "trades", "win_rate", "profit_factor", "mdd"}
    assert result["summary"]["windows"] == 2


def test_report_time_buckets_groups_costs_and_sample_warning():
    c = quick_config()
    result = replay(dataset(), c)
    assert result["total_trades"] < MIN_TRADES and "표본 부족" in result["sample_warning"]
    assert set(result["by_group"]) == {"NASDAQ_LONG", "NASDAQ_SHORT"}
    assert all(len(key) == 5 and key[3:] in ("00", "30") for key in result["by_entry_time_30m"])
    costs = result["costs"]
    assert costs["pnl_before_costs"] == costs["pnl_after_costs"] + costs["costs"]
    assert costs["costs"] > 0
    empty = performance(State(cash=D(10000)), [D(10000)], D(10000))
    assert empty["costs"]["costs"] == 0 and "sample_warning" in empty


def write(tmp_path, lines, header="timestamp,open,high,low,close,volume"):
    path = tmp_path / "bars.csv"
    path.write_text("\n".join([header, *lines]) + "\n")
    return path


def test_loader_validation_report(tmp_path):
    session = offline_session(date(2026, 10, 20))
    rows = []
    for i in range(390):
        stamp = (session.open + timedelta(minutes=i)).isoformat()  # Minute start times.
        if i == 10:
            continue  # Missing minute.
        rows.append(f"{stamp},20,20.1,19.9,20,100")
    rows.append(rows[5])  # Exact duplicate.
    rows.append(rows[6].replace(",100", ",999"))  # Conflicting duplicate.
    rows.insert(3, rows[20])  # Out of order (also a duplicate of row 20's timestamp).
    pre = (session.open - timedelta(minutes=30)).isoformat()
    rows.append(f"{pre},20,20.1,19.9,20,100")  # Pre-market.
    rows.append("not-a-time,1,1,1,1,1")
    next_session = offline_session(date(2026, 10, 21))
    rows.append(f"{next_session.open.isoformat()},45,45,45,45,100")  # >40% overnight gap.
    bars, report = load(write(tmp_path, rows), symbol="soxl", timestamp_at="start")
    assert bars[0].end == session.open + timedelta(minutes=1) and bars[0].symbol == "SOXL"
    assert report["missing_minutes"]["2026-10-20"]["SOXL"] == 1
    assert report["duplicates"] == 3 and report["conflicting_duplicates"] == 1
    assert report["out_of_order"] >= 1
    assert report["outside_regular_session"] == 1
    assert len(report["invalid_rows"]) == 1
    assert report["suspected_splits"][0]["day"] == "2026-10-21"
    assert "2026-10-21" in report["incomplete_sessions"]
    assert report["warnings"]


def test_loader_naive_timezone_and_end_stamps(tmp_path):
    session = offline_session(date(2026, 11, 27))  # Early close.
    local = (session.open + timedelta(minutes=1)).astimezone(NY)
    naive = local.replace(tzinfo=None).isoformat()
    bars, report = load(
        write(
            tmp_path, [f"SQQQ,{naive},10,10,10,10,5"], "symbol,timestamp,open,high,low,close,volume"
        )
    )
    assert bars[0].end == session.open + timedelta(minutes=1)
    assert report["early_close_sessions"] == ["2026-11-27"]
    with pytest.raises(ValueError):
        load(write(tmp_path, []), symbol="SOXL", timestamp_at="middle")
