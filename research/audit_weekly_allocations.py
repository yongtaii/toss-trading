"""Independent fill/basis/exposure reconstruction of target-weight weekly portfolios."""

import argparse
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

from research import compare_weekly_allocations as model
from research.weekly_baseline_audit import audit_metrics


def signal_current(closes):
    if len(closes) < 200:
        return 0
    slow = sum(closes[-200:]) / 200
    return int(closes[-1] > slow and sum(closes[-20:]) / 20 > slow)


def audit_target(data, metrics, allocation, cost):
    days = sorted(data)
    index = {d: i for i, d in enumerate(days)}
    fee, friction = 0.001 * cost, 0.00055 * cost
    cash, peak, mdd = model.INITIAL, model.INITIAL, 0.0
    positions = {}
    fills_by_day = defaultdict(lambda: defaultdict(list))
    for f in metrics["fills"]:
        fills_by_day[date.fromisoformat(f["day"])][f["phase"]].append(f)
    realization_index = cycle_index = 0
    observed_cycles = set()

    def apply(f, day):
        nonlocal cash, realization_index, cycle_index
        s, q = f["symbol"], f["quantity"]
        assert s in model.ASSETS and isinstance(q, int) and q > 0
        assert f["phase"] in ("open", "close") and f["side"] in ("BUY", "SELL")
        raw = data[day][s][f["phase"]]
        assert math.isclose(raw, f["raw"], abs_tol=1e-8)
        sign = 1 if f["side"] == "BUY" else -1
        price = raw * (1 + sign * friction)
        assert math.isclose(price, f["price"], abs_tol=1e-8)
        charge = q * price * fee + (max(0.01, q * price * 0.206 / 10000) if sign < 0 else 0)
        assert math.isclose(charge, f["fee"], abs_tol=1e-8)
        if sign > 0:
            assert f["phase"] == "open"
            assert model.original.week(day) != model.original.week(days[index[day] - 1])
            source = model.lab.MAPPING[s][0]
            old = [data[d][source]["close"] for d in days[: index[day]]]
            assert signal_current(old) == 1
            cash -= q * price + charge
            if s not in positions:
                assert f["cycle"] not in observed_cycles
                observed_cycles.add(f["cycle"])
                positions[s] = {
                    "cycle": f["cycle"],
                    "q": 0,
                    "raw_average": 0.0,
                    "basis": 0.0,
                    "fee_basis": 0.0,
                    "opened": day,
                    "pnl": 0.0,
                    "bought": 0,
                    "sold": 0,
                }
            p = positions[s]
            assert f["cycle"] == p["cycle"]
            p["raw_average"] = (p["raw_average"] * p["q"] + q * raw) / (p["q"] + q)
            p["q"] += q
            p["basis"] += q * price + charge
            p["fee_basis"] += charge
            p["bought"] += q
        else:
            p = positions[s]
            assert p["q"] >= q and p["cycle"] == f["cycle"]
            reason = f["reason"]
            if reason == "END_OF_SAMPLE":
                assert f["phase"] == "close" and str(day) == metrics["curve"][-1]["day"]
            else:
                assert f["phase"] == "open"
            if reason == "TIME":
                assert index[day] - index[p["opened"]] == model.original.HORIZON
            elif reason == "SIGNAL":
                assert model.original.week(day) != model.original.week(days[index[day] - 1])
                source = model.lab.MAPPING[s][0]
                assert signal_current([data[d][source]["close"] for d in days[: index[day]]]) == 0
            elif reason in ("CLOSE_STOP_NEXT_OPEN", "CLOSE_TARGET_NEXT_OPEN"):
                move = data[days[index[day] - 1]][s]["close"] / p["raw_average"] - 1
                assert move <= -0.06 if reason == "CLOSE_STOP_NEXT_OPEN" else move >= 0.18
            removed = p["basis"] * q / p["q"]
            buy_fee = p["fee_basis"] * q / p["q"]
            pnl = q * price - charge - removed
            r = metrics["realizations"][realization_index]
            realization_index += 1
            assert r["cycle"] == p["cycle"] and r["quantity"] == q
            assert math.isclose(r["basis"], removed, abs_tol=1e-7)
            assert math.isclose(r["entry_fee"], buy_fee, abs_tol=1e-7)
            assert math.isclose(r["exit_fee"], charge, abs_tol=1e-7)
            assert math.isclose(r["pnl"], pnl, abs_tol=1e-7)
            assert math.isclose(r["average_entry_fill"], (removed - buy_fee) / q, abs_tol=1e-7)
            p["q"] -= q
            p["basis"] -= removed
            p["fee_basis"] -= buy_fee
            p["pnl"] += pnl
            p["sold"] += q
            cash += q * price - charge
            if not p["q"]:
                c = metrics["cycles"][cycle_index]
                cycle_index += 1
                assert c["cycle"] == p["cycle"] and p["bought"] == p["sold"] == c["total_bought"]
                assert math.isclose(c["pnl"], p["pnl"], abs_tol=1e-7)
                held = index[day] - index[p["opened"]] + int(f["phase"] == "close")
                assert held == c["hold_sessions"] <= model.original.HORIZON
                del positions[s]
        assert cash >= -1e-7 and len(positions) <= 2
        assert math.isclose(cash, f["cash_after"], abs_tol=1e-6)

    for row, exposure in zip(metrics["curve"], metrics["exposure_curve"], strict=True):
        day = date.fromisoformat(row["day"])
        assert str(day) == exposure["day"]
        opening = fills_by_day[day]["open"]
        rebalance_reasons = {"ENTRY", "REBALANCE_ADD", "REBALANCE_TRIM"}
        boundary = next(
            (i for i, f in enumerate(opening) if f["reason"] in rebalance_reasons), len(opening)
        )
        for f in opening[:boundary]:
            apply(f, day)
        weekly = model.original.week(day) != model.original.week(days[index[day] - 1])
        assert weekly or boundary == len(opening)
        if weekly:
            eligible = []
            for s in model.ASSETS:
                source = model.lab.MAPPING[s][0]
                if signal_current([data[d][source]["close"] for d in days[: index[day]]]):
                    eligible.append(s)
            gross_open = cash + sum(p["q"] * data[day][s]["open"] for s, p in positions.items())
            targets = {
                s: math.floor(
                    gross_open
                    * allocation
                    / len(eligible)
                    / (data[day][s]["open"] * (1 + friction) * (1 + fee))
                )
                for s in eligible
            }
            for f in opening[boundary:]:
                s = f["symbol"]
                assert f["reason"] in rebalance_reasons
                if f["side"] == "SELL":
                    assert f["quantity"] == positions[s]["q"] - targets.get(s, 0) > 0
                else:
                    need = targets[s] - positions.get(s, {}).get("q", 0)
                    affordable = math.floor(
                        max(0, cash) / (data[day][s]["open"] * (1 + friction) * (1 + fee))
                    )
                    assert f["quantity"] == min(max(0, need), affordable) > 0
                apply(f, day)
            for s in model.ASSETS:
                q = positions.get(s, {}).get("q", 0)
                assert q <= targets.get(s, 0)
                if q < targets.get(s, 0):
                    assert cash < data[day][s]["open"] * (1 + friction) * (1 + fee) + 1e-7
        invested = sum(p["q"] * data[day][s]["close"] for s, p in positions.items())
        assert math.isclose(exposure["invested_usd"], invested, abs_tol=1e-6)
        assert math.isclose(
            exposure["gross_allocation"], invested / (cash + invested), abs_tol=1e-8
        )
        assert exposure["positions"] == len(positions) and exposure["gross_allocation"] <= 1 + 1e-8
        for f in fills_by_day[day]["close"]:
            apply(f, day)
        mark = cash
        for s, p in positions.items():
            value = p["q"] * data[day][s]["close"] * (1 - friction)
            mark += value - value * fee - max(0.01, value * 0.206 / 10000)
        assert math.isclose(mark, row["equity"], abs_tol=1e-6)
        assert math.isclose(cash, row["cash"], abs_tol=1e-6)
        peak = max(peak, mark)
        mdd = max(mdd, 1 - mark / peak)
    assert not positions
    assert realization_index == len(metrics["realizations"])
    assert cycle_index == len(metrics["cycles"]) == metrics["trades"]
    assert math.isclose(cash - model.INITIAL, metrics["net_pnl"], abs_tol=1e-6)
    assert math.isclose(mdd, metrics["mdd_close"], abs_tol=1e-8)
    assert math.isclose(sum(f["fee"] for f in metrics["fills"]), metrics["fees"], abs_tol=1e-6)
    return {
        "fills": len(metrics["fills"]),
        "realizations": realization_index,
        "cycles": cycle_index,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    folder = parser.parse_args().folder
    summary = json.loads((folder / "summary.json").read_text())
    plan = summary["plan"]
    for path, digest in plan["source_hashes"].items():
        assert model.lab.digest(Path(path)) == digest
    for path, digest in plan["code_hashes"].items():
        assert model.lab.digest(folder / "frozen-source" / Path(path).name) == digest
    data, _ = model.original.datasets(Path(next(iter(plan["source_hashes"]))).parent)
    checks = {}
    for period, policies in summary["results"].items():
        for policy, window in policies.items():
            for cost, m in window.items():
                key = f"{period}:{policy}:cost{cost}"
                if policy == "legacy_weekly":
                    checks[key] = audit_metrics(data[period], m, int(cost), signal=signal_current)
                else:
                    checks[key] = audit_target(data[period], m, int(policy[6:]) / 100, int(cost))
    output = {
        "replays": len(checks),
        "target_fill_rows": sum(v.get("fills", 0) for v in checks.values()),
        "target_realization_rows": sum(v.get("realizations", 0) for v in checks.values()),
        "checks": checks,
        "audit_code_sha256": model.lab.digest(Path(__file__)),
    }
    model.lab.save(folder / "audit.json", output)
    print("Audited", len(checks), "replays and", output["target_fill_rows"], "target fill rows")


if __name__ == "__main__":
    main()
