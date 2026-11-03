"""Offline high-allocation weekly portfolios with explicit partial-fill cost basis.

The signal and close-based exits are unchanged. Unlike the legacy entry-budget model,
target allocation is restored weekly. Averaged entry basis changes on additions and
the original position age is retained. No broker or LIVE settings are accessed.
"""

import argparse
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

from research import daily_data as lab
from research import weekly_baseline as original

DEFAULT_POLICY = "legacy_weekly"
TARGETS = (0.90, 0.95, 0.99, 1.0)
INITIAL = 1490.0
ASSETS = ("SOXL", "TQQQ")


def simulate(data, start, end, allocation, cost=1):
    if not 0 < allocation <= 1:
        raise ValueError("Allocation must be in (0,1]")
    if allocation not in TARGETS:
        raise ValueError("Only retained targets 90/95/99/100 percent are supported")
    days = sorted(data)
    evaluation = [d for d in days if start <= d <= end]
    if not evaluation:
        raise ValueError("No evaluation days")
    selected_days = set(evaluation)
    fee, friction = 0.001 * cost, 0.00055 * cost
    cash, peak, mdd = INITIAL, INITIAL, 0.0
    positions, pending, weekly_signal = {}, {}, {}
    history = {s: [] for s in ("QQQ", "SOXX")}
    previous = None
    next_cycle = 0
    fills, realizations, cycles, curve, exposures = [], [], [], [], []
    clipped = 0

    def buy(symbol, quantity, raw, day, reason):
        nonlocal cash, next_cycle
        if not quantity:
            return
        price = raw * (1 + friction)
        charge = quantity * price * fee
        spent = quantity * price + charge
        assert spent <= cash + 1e-7
        cash -= spent
        if symbol not in positions:
            next_cycle += 1
            positions[symbol] = {
                "cycle": next_cycle,
                "quantity": 0,
                "raw_average": 0.0,
                "cost_basis": 0.0,
                "fee_basis": 0.0,
                "age": 0,
                "opened": day,
                "realized": 0.0,
                "total_bought": 0,
                "total_sold": 0,
            }
        p = positions[symbol]
        old = p["quantity"]
        p["raw_average"] = (old * p["raw_average"] + quantity * raw) / (old + quantity)
        p["quantity"] += quantity
        p["cost_basis"] += spent
        p["fee_basis"] += charge
        p["total_bought"] += quantity
        fills.append(
            {
                "cycle": p["cycle"],
                "day": str(day),
                "phase": "open",
                "symbol": symbol,
                "side": "BUY",
                "quantity": quantity,
                "raw": raw,
                "price": price,
                "fee": charge,
                "cash_after": cash,
                "reason": reason,
            }
        )

    def sell(symbol, quantity, raw, day, reason, phase="open"):
        nonlocal cash
        p = positions[symbol]
        assert 0 < quantity <= p["quantity"]
        old = p["quantity"]
        basis = p["cost_basis"] * quantity / old
        entry_fee = p["fee_basis"] * quantity / old
        price = raw * (1 - friction)
        charge = quantity * price * fee + max(0.01, quantity * price * 0.206 / 10000)
        received = quantity * price - charge
        cash += received
        pnl = received - basis
        p["cost_basis"] -= basis
        p["fee_basis"] -= entry_fee
        p["quantity"] -= quantity
        p["realized"] += pnl
        p["total_sold"] += quantity
        fills.append(
            {
                "cycle": p["cycle"],
                "day": str(day),
                "phase": phase,
                "symbol": symbol,
                "side": "SELL",
                "quantity": quantity,
                "raw": raw,
                "price": price,
                "fee": charge,
                "cash_after": cash,
                "reason": reason,
            }
        )
        realizations.append(
            {
                "cycle": p["cycle"],
                "symbol": symbol,
                "opened": str(p["opened"]),
                "closed": str(day),
                "quantity": quantity,
                "basis": basis,
                "entry_fee": entry_fee,
                "exit_fee": charge,
                "average_entry_fill": (basis - entry_fee) / quantity,
                "exit": price,
                "pnl": pnl,
                "reason": reason,
            }
        )
        if not p["quantity"]:
            assert p["total_bought"] == p["total_sold"]
            cycles.append(
                {
                    "cycle": p["cycle"],
                    "symbol": symbol,
                    "opened": str(p["opened"]),
                    "closed": str(day),
                    "pnl": p["realized"],
                    "hold_sessions": p["age"],
                    "total_bought": p["total_bought"],
                    "reason": reason,
                }
            )
            del positions[symbol]

    def equity(rows, field):
        return cash + sum(
            p["quantity"] * rows[s][field] * (1 - friction)
            - (
                p["quantity"] * rows[s][field] * (1 - friction) * fee
                + max(0.01, p["quantity"] * rows[s][field] * (1 - friction) * 0.206 / 10000)
            )
            for s, p in positions.items()
        )

    for day in days:
        rows = data[day]
        first_of_week = previous is None or original.week(day) != original.week(previous)
        if first_of_week:
            weekly_signal = original.wanted(history)
        if day in selected_days:
            for s in list(positions):
                p = positions[s]
                source = lab.MAPPING[s][0]
                reason = pending.get(s)
                if p["age"] >= original.HORIZON:
                    reason = "TIME"
                elif not weekly_signal[source]:
                    reason = reason or "SIGNAL"
                if reason:
                    sell(s, p["quantity"], rows[s]["open"], day, reason)
            pending = {}
            if first_of_week:
                eligible = [s for s in ASSETS if weekly_signal[lab.MAPPING[s][0]]]
                opening_equity = cash + sum(
                    p["quantity"] * rows[s]["open"] for s, p in positions.items()
                )
                targets = {
                    s: math.floor(
                        opening_equity
                        * allocation
                        / len(eligible)
                        / (rows[s]["open"] * (1 + friction) * (1 + fee))
                    )
                    for s in eligible
                }
                # Freeze targets, release net sale proceeds, then fund buys with cash.
                for s in list(positions):
                    excess = positions[s]["quantity"] - targets.get(s, 0)
                    if excess > 0:
                        sell(s, excess, rows[s]["open"], day, "REBALANCE_TRIM")
                for s in eligible:
                    current = positions.get(s, {}).get("quantity", 0)
                    needed = targets[s] - current
                    affordable = math.floor(
                        max(0, cash) / (rows[s]["open"] * (1 + friction) * (1 + fee))
                    )
                    quantity = min(max(0, needed), affordable)
                    if needed > affordable:
                        clipped += 1
                    buy(s, quantity, rows[s]["open"], day, "REBALANCE_ADD" if current else "ENTRY")
            for s, p in positions.items():
                p["age"] += 1
                move = rows[s]["close"] / p["raw_average"] - 1
                if move <= -original.STOP:
                    pending[s] = "CLOSE_STOP_NEXT_OPEN"
                elif move >= original.REWARD * original.STOP:
                    pending[s] = "CLOSE_TARGET_NEXT_OPEN"
            invested = sum(p["quantity"] * rows[s]["close"] for s, p in positions.items())
            exposures.append(
                {
                    "day": str(day),
                    "gross_allocation": invested / (cash + invested),
                    "invested_usd": invested,
                    "positions": len(positions),
                }
            )
            if day == evaluation[-1]:
                for s in list(positions):
                    sell(
                        s, positions[s]["quantity"], rows[s]["close"], day, "END_OF_SAMPLE", "close"
                    )
            mark = equity(rows, "close")
            peak = max(peak, mark)
            mdd = max(mdd, 1 - mark / peak)
            curve.append({"day": str(day), "equity": mark, "cash": cash})
            assert cash >= -1e-7 and len(positions) <= 2
        for source in history:
            history[source].append(rows[source]["close"])
        previous = day
    assert not positions
    assert math.isclose(sum(r["pnl"] for r in realizations), cash - INITIAL, abs_tol=1e-6)
    assert math.isclose(sum(c["pnl"] for c in cycles), cash - INITIAL, abs_tol=1e-6)
    return {
        "total_return": cash / INITIAL - 1,
        "net_pnl": cash - INITIAL,
        "sessions": len(evaluation),
        "mdd_close": mdd,
        "trades": len(cycles),
        "order_fills": len(fills),
        "partial_sale_events": sum(r["reason"] == "REBALANCE_TRIM" for r in realizations),
        "fees": sum(f["fee"] for f in fills),
        "cash_clipped_buy_orders": clipped,
        "mean_hold_sessions": sum(c["hold_sessions"] for c in cycles) / len(cycles)
        if cycles
        else None,
        "average_allocation": sum(e["gross_allocation"] for e in exposures) / len(exposures),
        "maximum_allocation": max(e["gross_allocation"] for e in exposures),
        "fraction_days_invested": sum(e["positions"] > 0 for e in exposures) / len(exposures),
        "fills": fills,
        "realizations": realizations,
        "cycles": cycles,
        "curve": curve,
        "exposure_curve": exposures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=lab.ROOT / "data/weekly-allocation-comparison-20261103-v1",
    )
    parser.add_argument("--history-dir", type=Path, default=lab.ROOT / "data/history")
    args = parser.parse_args()
    out = args.output.resolve()
    data, paths = original.datasets(args.history_dir.resolve())
    out.mkdir(parents=True, exist_ok=False)
    sources = [
        Path(__file__),
        Path(original.__file__),
        Path(lab.__file__),
    ]
    plan = {
        "created_utc": datetime.now(UTC).isoformat(),
        "default_policy": DEFAULT_POLICY,
        "targets": TARGETS,
        "initial_cash": INITIAL,
        "replays": (len(TARGETS) + 1) * len(original.PERIODS) * 2,
        "periods": {p: [str(a), str(b)] for p, (a, b) in original.PERIODS.items()},
        "signal": "Same weekly daily-SMA20/200 eligibility; no signal stays in cash",
        "allocation": (
            "Total target equally split among eligible assets; one eligible gets all target"
        ),
        "rebalance": "First actual weekly session; sell before buy; cash-funded whole shares only",
        "basis": "Weighted-average raw entry on additions; pro-rata basis on trims; age not reset",
        "limits_removed": (
            "0.5% risk sizing and $298 entry cap removed in target-allocation variants"
        ),
        "exits": (
            "Same6%/18% close triggers, next open; max20 sessions; weekly first-session entries"
        ),
        "notional_caution": (
            "Target is not a daily ceiling; drift, no-signal and integer residuals remain"
        ),
        "cost": (
            "10bp fee+5bp slip+0.5bp half spread each side; sellSEC0.206bp/min$0.01; stress2x exSEC"
        ),
        "excluded": "Dividends,FX,interest,tax; no actual orders or LIVE changes",
        "historical_reuse": True,
        "independent_validation": False,
        "source_hashes": {str(p): lab.digest(p) for p in paths},
        "code_hashes": {str(p): lab.digest(p) for p in sources},
    }
    lab.save(out / "plan.json", plan)
    frozen = out / "frozen-source"
    frozen.mkdir()
    for source in sources:
        shutil.copyfile(source, frozen / source.name)
    results = {}
    for period, (start, end) in original.PERIODS.items():
        results[period] = {}
        for policy in (DEFAULT_POLICY, *(f"target{round(x * 100)}" for x in TARGETS)):
            window = {}
            for cost in (1, 2):
                if policy == DEFAULT_POLICY:
                    m = original.simulate(data[period], start, end, cost)
                else:
                    m = simulate(data[period], start, end, int(policy[6:]) / 100, cost)
                window[str(cost)] = m
            results[period][policy] = window
        print(
            period,
            [
                (
                    p,
                    round(w["1"]["total_return"] * 100, 2),
                    round(w["1"]["mdd_close"] * 100, 2),
                    round(w["1"]["average_allocation"] * 100, 1),
                )
                for p, w in results[period].items()
            ],
            flush=True,
        )
    lab.save(out / "summary.json", {"plan": plan, "results": results})


if __name__ == "__main__":
    main()
