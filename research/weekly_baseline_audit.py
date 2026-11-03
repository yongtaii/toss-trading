"""Independently reconstruct cash, prices and daily marks for the selected swing model."""

import math
from collections import defaultdict
from datetime import date

from research import daily_data as lab


def audit_metrics(data, metrics, cost, *, signal, check_risk_budget=True):
    trades = metrics["trade_log"]
    curve = metrics["curve"]
    by_open, by_close, by_end = defaultdict(list), defaultdict(list), defaultdict(list)
    fee, friction = 0.001 * cost, 0.00055 * cost
    for i, t in enumerate(trades):
        opened, closed = date.fromisoformat(t["opened"]), date.fromisoformat(t["closed"])
        s, q = t["symbol"], t["quantity"]
        assert s in lab.TRADED and isinstance(q, int) and q > 0 and opened <= closed
        assert q * t["entry"] * (1 + fee) <= 298 + 1e-7
        assert math.isclose(t["entry"], data[opened][s]["open"] * (1 + friction), abs_tol=1e-7)
        raw = data[closed][s]["close" if t["reason"] == "END_OF_SAMPLE" else "open"]
        assert math.isclose(t["exit"], raw * (1 - friction), abs_tol=1e-7)
        charges = q * (t["entry"] + t["exit"]) * fee + max(0.01, q * t["exit"] * 0.206 / 10000)
        assert math.isclose(charges, t["fees"], abs_tol=1e-7)
        assert math.isclose(q * (t["exit"] - t["entry"]) - charges, t["pnl"], abs_tol=1e-7)
        source, actual, _ = lab.MAPPING[s]
        old = [rows[source]["close"] for d, rows in data.items() if d < opened]
        expected = signal(old)
        assert actual == expected
        by_open[opened].append(i)
        (by_end if t["reason"] == "END_OF_SAMPLE" else by_close)[closed].append(i)
    cash, peak, mdd = 1490.0, 1490.0, 0.0
    active = {}
    max_positions = 0

    def close(i):
        nonlocal cash
        assert i in active
        t = active.pop(i)
        buy_fee = t["quantity"] * t["entry"] * fee
        cash += t["quantity"] * t["exit"] - (t["fees"] - buy_fee)

    for row in curve:
        day = date.fromisoformat(row["day"])
        for i in by_close[day]:
            close(i)
        opening_equity = cash + sum(
            t["quantity"] * data[day][t["symbol"]]["open"] for t in active.values()
        )
        for i in by_open[day]:
            t = trades[i]
            assert not any(
                lab.MAPPING[v["symbol"]][2] == lab.MAPPING[t["symbol"]][2] for v in active.values()
            )
            q, px = t["quantity"], t["entry"]
            raw = data[day][t["symbol"]]["open"]
            if check_risk_budget:
                assert (
                    q * (raw * 0.06 + px * (2 * fee + 2 * friction))
                    <= opening_equity * 0.005 + 1e-7
                )
            cash -= q * px * (1 + fee)
            active[i] = t
            assert cash >= -1e-7 and len(active) <= 2
            max_positions = max(max_positions, len(active))
        if "exposure_curve" in metrics:
            exposure = next(r for r in metrics["exposure_curve"] if r["day"] == str(day))
            invested = sum(t["quantity"] * data[day][t["symbol"]]["close"] for t in active.values())
            assert math.isclose(invested, exposure["invested_usd"], abs_tol=1e-7)
            assert math.isclose(
                invested / (cash + invested), exposure["gross_allocation"], abs_tol=1e-8
            )
            assert len(active) == exposure["positions"]
        for i in by_end[day]:
            close(i)
        mark = cash
        for t in active.values():
            value = t["quantity"] * data[day][t["symbol"]]["close"] * (1 - friction)
            mark += value - value * fee - max(0.01, value * 0.206 / 10000)
        assert math.isclose(mark, row["equity"], abs_tol=1e-6)
        peak = max(peak, mark)
        mdd = max(mdd, 1 - mark / peak)
    assert not active
    assert math.isclose(cash - 1490, metrics["net_pnl"], abs_tol=1e-6)
    assert math.isclose(mdd, metrics["mdd_close"], abs_tol=1e-8)
    return {
        "trades_checked": len(trades),
        "max_positions": max_positions,
        "net_pnl_without_best_trade": metrics["net_pnl"]
        - max((t["pnl"] for t in trades), default=0),
        "end_of_sample_trade_pnl": sum(t["pnl"] for t in trades if t["reason"] == "END_OF_SAMPLE"),
        "largest_calendar_hold": max((t["hold_calendar_days"] for t in trades), default=0),
    }
