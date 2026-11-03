"""High allocations: cash-funded fills, partial cost basis and unchanged daily timing."""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research import compare_weekly_allocations as study  # noqa: E402
from research import daily_data as lab  # noqa: E402


def world():
    rows = {}
    day = date(2025, 7, 1)
    index = 0
    while day <= date(2026, 9, 21):
        if lab.offline_session(day):
            rows[day] = {s: {"open": 50.0, "close": 50.0} for s in lab.MARKET}
            for s in ("QQQ", "SOXX"):
                rows[day][s] = {"open": 100 + index * 0.1, "close": 100.05 + index * 0.1}
            index += 1
        day += timedelta(days=1)
    return rows


def test_100_percent_target_never_borrows_or_uses_fractional_shares():
    for cost in (1, 2):
        m = study.simulate(world(), date(2026, 9, 8), date(2026, 9, 21), 1.0, cost)
        assert m["fills"]
        assert all(f["cash_after"] >= -1e-7 and isinstance(f["quantity"], int) for f in m["fills"])
        assert m["maximum_allocation"] < 1
        assert sum(r["pnl"] for r in m["realizations"]) == pytest.approx(m["net_pnl"])


def test_one_eligible_asset_gets_entire_target_budget():
    rows = world()
    for values in rows.values():
        values["SOXX"] = {"open": 100.0, "close": 100.0}
    m = study.simulate(rows, date(2026, 9, 8), date(2026, 9, 8), 0.9)
    buys = [f for f in m["fills"] if f["side"] == "BUY"]
    assert len(buys) == 1 and buys[0]["symbol"] == "TQQQ"
    assert buys[0]["quantity"] == 26


def test_rebalance_preserves_age_and_pro_rata_cost_basis():
    rows = world()
    rows[date(2026, 9, 14)]["SOXL"] = {"open": 60.0, "close": 60.0}
    rows[date(2026, 9, 14)]["TQQQ"] = {"open": 48.0, "close": 48.0}
    m = study.simulate(rows, date(2026, 9, 8), date(2026, 9, 14), 0.9)
    assert any(f["reason"] == "REBALANCE_TRIM" for f in m["fills"])
    assert any(f["reason"] == "REBALANCE_ADD" for f in m["fills"])
    assert all(c["opened"] == "2026-09-08" and c["hold_sessions"] == 5 for c in m["cycles"])
    t = next(r for r in m["realizations"] if r["symbol"] == "TQQQ")
    assert t["average_entry_fill"] == pytest.approx((13 * 50 + 48) / 14 * 1.00055)
    assert sum(r["pnl"] for r in m["realizations"]) == pytest.approx(
        sum(c["pnl"] for c in m["cycles"])
    )


def test_stop_uses_adverse_next_open_and_waits_for_weekly_reentry():
    rows = world()
    for s in ("SOXL", "TQQQ"):
        rows[date(2026, 9, 8)][s]["close"] = 45.0
        rows[date(2026, 9, 9)][s]["open"] = 30.0
    m = study.simulate(rows, date(2026, 9, 8), date(2026, 9, 15), 0.95)
    stopped = [f for f in m["fills"] if f["reason"] == "CLOSE_STOP_NEXT_OPEN"]
    assert stopped and all(f["day"] == "2026-09-09" for f in stopped)
    assert all(f["price"] == pytest.approx(30 * 0.99945) for f in stopped)
    assert {f["day"] for f in m["fills"] if f["side"] == "BUY"} == {"2026-09-08", "2026-09-14"}


def test_future_prices_cannot_change_earlier_fills():
    rows = world()
    before = study.simulate(rows, date(2026, 9, 8), date(2026, 9, 21), 0.99)
    for day, values in rows.items():
        if day >= date(2026, 9, 15):
            for s in lab.MARKET:
                values[s] = {"open": 1000.0, "close": 1000.0}
    after = study.simulate(rows, date(2026, 9, 8), date(2026, 9, 21), 0.99)
    assert [f for f in before["fills"] if f["day"] < "2026-09-15"] == [
        f for f in after["fills"] if f["day"] < "2026-09-15"
    ]
