"""Retained small-account weekly SMA20/200 strategy; no alternate signal families."""

import math
from datetime import date

from research import daily_data as lab
from research.daily_data import ROOT, merge, read_daily

PERIODS = {
    "bear2022": (date(2022, 6, 1), date(2022, 12, 30)),
    "year2023": (date(2023, 1, 3), date(2023, 12, 29)),
    "year2025": (date(2025, 1, 2), date(2025, 10, 31)),
    "year2026": (date(2026, 1, 2), date(2026, 10, 30)),
    "recent2026": (date(2026, 8, 3), date(2026, 10, 30)),
}
INITIAL = 1490.0
ORDER_CAP = 298.0
RISK = 0.005
STOP = 0.06
REWARD = 3
HORIZON = 20


def week(day):
    return day.isocalendar()[:2]


def wanted(history):
    result = {}
    for source, values in history.items():
        if len(values) < 200:
            result[source] = 0
        else:
            slow = sum(values[-200:]) / 200
            result[source] = int(values[-1] > slow and sum(values[-20:]) / 20 > slow)
    return result


def simulate(data, start, end, cost=1):
    days = sorted(data)
    evaluation = [d for d in days if start <= d <= end]
    if not evaluation:
        raise ValueError("Empty evaluation period")
    evaluation_set = set(evaluation)
    fee, friction = 0.001 * cost, 0.00055 * cost
    cash, peak, mdd = INITIAL, INITIAL, 0.0
    positions, trades, curve, exposures = {}, [], [], []
    history = {s: [] for s in ("QQQ", "SOXX")}
    pending, weekly_signal = {}, {}
    previous_day = None

    def close(symbol, raw, day, reason):
        nonlocal cash
        p = positions.pop(symbol)
        price = raw * (1 - friction)
        charges = price * p["qty"] * fee + max(0.01, price * p["qty"] * 0.206 / 10000)
        received = price * p["qty"] - charges
        cash += received
        trades.append(
            {
                "symbol": symbol,
                "opened": str(p["day"]),
                "closed": str(day),
                "entry": p["entry"],
                "exit": price,
                "quantity": p["qty"],
                "fees": p["fee"] + charges,
                "pnl": received - p["spent"],
                "reason": reason,
                "hold_sessions": p["age"],
                "hold_calendar_days": (day - p["day"]).days,
            }
        )

    for day in days:
        rows = data[day]
        first_of_week = previous_day is None or week(day) != week(previous_day)
        if first_of_week:
            weekly_signal = wanted(history)
        if day in evaluation_set:
            desired = weekly_signal
            for symbol in list(positions):
                p = positions[symbol]
                source, actual, _ = lab.MAPPING[symbol]
                reason = pending.get(symbol)
                if p["age"] >= HORIZON:
                    reason = "TIME"
                elif desired[source] != actual:
                    reason = reason or "SIGNAL"
                if reason:
                    close(symbol, rows[symbol]["open"], day, reason)
            pending = {}
            equity_open = cash + sum(p["qty"] * rows[s]["open"] for s, p in positions.items())
            allow_entry = first_of_week
            if allow_entry:
                for source, group in (("SOXX", "SEMI"), ("QQQ", "NASDAQ")):
                    direction = desired[source]
                    if not direction or any(lab.MAPPING[s][2] == group for s in positions):
                        continue
                    symbol = next(
                        s for s in lab.TRADED if lab.MAPPING[s][:2] == (source, direction)
                    )
                    raw = rows[symbol]["open"]
                    price = raw * (1 + friction)
                    distance = raw * STOP
                    limits = [ORDER_CAP / (price * (1 + fee)), cash / (price * (1 + fee))]
                    limits.insert(
                        0, equity_open * RISK / (distance + price * (2 * fee + 2 * friction))
                    )
                    qty = math.floor(min(limits))
                    if qty < 1:
                        continue
                    spent = qty * price * (1 + fee)
                    cash -= spent
                    positions[symbol] = {
                        "qty": qty,
                        "entry": price,
                        "raw": raw,
                        "spent": spent,
                        "fee": qty * price * fee,
                        "day": day,
                        "age": 0,
                    }
            for symbol, p in positions.items():
                p["age"] += 1
                move = rows[symbol]["close"] / p["raw"] - 1
                if move <= -STOP:
                    pending[symbol] = "CLOSE_STOP_NEXT_OPEN"
                elif move >= REWARD * STOP:
                    pending[symbol] = "CLOSE_TARGET_NEXT_OPEN"
            invested = sum(p["qty"] * rows[s]["close"] for s, p in positions.items())
            exposures.append(
                {
                    "day": str(day),
                    "gross_allocation": invested / (cash + invested),
                    "invested_usd": invested,
                    "positions": len(positions),
                }
            )
            if day == evaluation[-1]:
                for symbol in list(positions):
                    close(symbol, rows[symbol]["close"], day, "END_OF_SAMPLE")
            mark = cash + sum(
                p["qty"] * rows[s]["close"] * (1 - friction)
                - (
                    p["qty"] * rows[s]["close"] * (1 - friction) * fee
                    + max(0.01, p["qty"] * rows[s]["close"] * (1 - friction) * 0.206 / 10000)
                )
                for s, p in positions.items()
            )
            peak = max(peak, mark)
            mdd = max(mdd, 1 - mark / peak)
            curve.append({"day": str(day), "equity": mark})
            assert cash >= -1e-7 and len(positions) <= 2
        for source in history:
            history[source].append(rows[source]["close"])
        previous_day = day
    assert not positions
    assert math.isclose(sum(t["pnl"] for t in trades), cash - INITIAL, abs_tol=1e-6)
    prior_equity, month_start, current_month = INITIAL, INITIAL, None
    months = []
    for row in curve:
        month = row["day"][:7]
        if current_month is not None and month != current_month:
            months.append({"month": current_month, "return": prior_equity / month_start - 1})
            month_start = prior_equity
        current_month, prior_equity = month, row["equity"]
    months.append({"month": current_month, "return": prior_equity / month_start - 1})
    invested_days = sum(e["positions"] > 0 for e in exposures)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] < 0]
    return {
        "total_return": cash / INITIAL - 1,
        "net_pnl": cash - INITIAL,
        "trades": len(trades),
        "trades_per_day": len(trades) / len(evaluation),
        "sessions": len(evaluation),
        "mdd_close": mdd,
        "mean_hold_sessions": sum(t["hold_sessions"] for t in trades) / len(trades)
        if trades
        else None,
        "trade_log": trades,
        "curve": curve,
        "exposure_curve": exposures,
        "average_allocation": sum(e["gross_allocation"] for e in exposures) / len(exposures),
        "maximum_allocation": max(e["gross_allocation"] for e in exposures),
        "fraction_days_invested": invested_days / len(evaluation),
        "fees": sum(t["fees"] for t in trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "monthly": months,
        "worst_month_return": min(m["return"] for m in months),
    }


def datasets(directory=None):
    directory = directory or ROOT / "data/history"
    filenames = (
        "2021-04-01_2022-12-30-raw-1d.csv",
        "2022-11-01_2023-12-29-raw-1d.csv",
        "2024-03-08_2025-10-31-raw-1d.csv",
        "2025-05-01_2026-10-30-raw-1d.csv",
    )
    paths = [directory / f for f in filenames]
    old = merge(read_daily(paths[0])[0], read_daily(paths[1])[0])
    current = merge(read_daily(paths[2])[0], read_daily(paths[3])[0])
    return {p: old if p in {"bear2022", "year2023"} else current for p in PERIODS}, paths
