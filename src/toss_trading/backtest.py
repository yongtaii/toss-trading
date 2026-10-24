"""Offline one-minute CSV replay using the same strategy/risk/engine as PAPER."""

import argparse
import csv
import json
import os
import random
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from toss_trading.domain import ZERO, Bar, Quote, State
from toss_trading.engine import TradingEngine
from toss_trading.marketdata import NY, offline_session
from toss_trading.strategy import RandomEntryBaseline, Strategy, VwapCrossBaseline
from toss_trading.trading_config import TradingConfig, load_config

MIN_TRADES = 100  # Below this, report statistics carry an explicit small-sample warning.
SENSITIVITY_FACTORS = (Decimal("0.75"), Decimal("0.875"), Decimal("1.125"), Decimal("1.25"))


def read_csv(path: str | Path, config: TradingConfig) -> list[Bar]:
    with Path(path).open(newline="") as handle:
        bars = [Bar.model_validate(row) for row in csv.DictReader(handle)]
    seen = set()
    for bar in bars:
        if bar.symbol not in config.symbols:
            raise ValueError(f"CSV contains unselected symbol: {bar.symbol}")
        key = (bar.symbol, bar.end)
        if key in seen:
            raise ValueError("Duplicate symbol/timestamp in CSV")
        seen.add(key)
    return sorted(bars, key=lambda b: (b.end, b.symbol))


class MemoryStore:
    """Single-writer in-process ledger for offline replay; PAPER uses the SQL Store.

    Replaying through SQLite produced identical metrics and fills (2026-10-25 check)
    but is roughly 15x slower, which matters for repeated baseline/sensitivity runs.
    """

    def __init__(self, config: TradingConfig):
        self.revision, self.state = 0, State(cash=config.initial_cash)

    def load(self) -> tuple[int, State]:
        return self.revision, self.state

    def commit(self, revision: int, state: State, records: list[dict]) -> None:
        if revision != self.revision:
            raise RuntimeError("Revision mismatch")
        self.state, self.revision = state, revision + 1

    def record_error(self, reason: str) -> None:
        pass

    def close(self) -> None:
        pass


def round_trips(state: State) -> list[dict]:
    """Aggregate partial sell fills into one record per completed position."""
    groups: dict = {}
    for trade in state.trades:
        item = groups.setdefault(
            (trade["symbol"], trade["opened"]),
            {"symbol": trade["symbol"], "opened": trade["opened"], "pnl": ZERO, "cost": ZERO},
        )
        item.setdefault("costs", ZERO)
        item["pnl"] += Decimal(trade["pnl"])
        item["cost"] += Decimal(trade["entry"]) * Decimal(trade["quantity"])
        item["cost"] += Decimal(trade["entry_fee"])
        item["costs"] += Decimal(trade["entry_fee"]) + Decimal(trade["exit_fee"])
        item["costs"] += Decimal(trade.get("slippage", "0"))
        item["closed"] = trade["closed"]
    opened = {(p.symbol, p.opened.isoformat()) for p in state.positions.values()}
    return [value for key, value in groups.items() if key not in opened]


def summarize(completed: list[dict]) -> dict:
    wins = [x["pnl"] for x in completed if x["pnl"] > 0]
    losses = [-x["pnl"] for x in completed if x["pnl"] < 0]
    return {
        "trades": len(completed),
        "net_pnl": sum((x["pnl"] for x in completed), ZERO),
        "average_pnl": sum((x["pnl"] for x in completed), ZERO) / len(completed)
        if completed
        else ZERO,
        "win_rate": Decimal(len(wins)) / len(completed) if completed else ZERO,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
    }


def performance(
    state: State,
    curve: list[Decimal],
    initial: Decimal,
    symbols: tuple[str, ...] = (),
    config: TradingConfig | None = None,
) -> dict:
    peak, mdd = initial, ZERO
    for value in curve:
        peak = max(peak, value)
        mdd = max(mdd, (peak - value) / peak)
    completed = round_trips(state)
    wins = [x["pnl"] for x in completed if x["pnl"] > 0]
    losses = [-x["pnl"] for x in completed if x["pnl"] < 0]
    per_symbol = {}
    for symbol in sorted(set(symbols) | {x["symbol"] for x in completed}):
        selected = [x for x in completed if x["symbol"] == symbol]
        cost = sum((x["cost"] for x in selected), ZERO)
        pnl = sum((x["pnl"] for x in selected), ZERO)
        per_symbol[symbol] = {
            "net_pnl": pnl,
            "return_on_traded_capital": pnl / cost if cost else ZERO,
        }
    equity = curve[-1] if curve else initial
    result = {
        "total_return": equity / initial - 1,
        "mdd": mdd,
        "win_rate": Decimal(len(wins)) / len(completed) if completed else ZERO,
        "average_win_loss_ratio": (sum(wins) / len(wins)) / (sum(losses) / len(losses))
        if wins and losses
        else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "total_trades": len(completed),
        "average_trade_return": sum((x["pnl"] / x["cost"] for x in completed), ZERO)
        / len(completed)
        if completed
        else ZERO,
        "net_profit_after_fees": equity - initial,
        "realized_net_profit": state.realized,
        "fees": state.fees,
        "by_symbol": per_symbol,
        "unclosed_positions": sorted(state.positions),
        "unresolved_orders": [o.id for o in state.pending()],
    }
    costs = sum((x["costs"] for x in completed), ZERO)
    net = sum((x["pnl"] for x in completed), ZERO)
    # Fees are explicit; slippage is the modeled fill price versus the touch. Spread remains.
    result["costs"] = {"pnl_before_costs": net + costs, "costs": costs, "pnl_after_costs": net}
    buckets = defaultdict(list)
    for x in completed:
        local = datetime.fromisoformat(x["opened"]).astimezone(NY)
        buckets[f"{local.hour:02d}:{30 * (local.minute // 30):02d}"].append(x)
    result["by_entry_time_30m"] = {k: summarize(v) for k, v in sorted(buckets.items())}
    if config is not None:
        result["by_group"] = {
            name: summarize([x for x in completed if x["symbol"] in members])
            for name, members in config.groups.items()
        }
    if len(completed) < MIN_TRADES:
        result["sample_warning"] = (
            f"표본 부족: 완료 거래 {len(completed)}건 < {MIN_TRADES}건. "
            "승률·PF·수익률의 통계적 신뢰도가 낮습니다."
        )
    return result


def buy_and_hold(bars: list[Bar], c: TradingConfig) -> dict:
    allocation = c.initial_cash / len(c.symbols)
    total = ZERO
    results = {}
    for symbol in c.symbols:
        rows = [b for b in bars if b.symbol == symbol]
        if not rows:
            results[symbol] = None
            continue
        buy = rows[0].open * (1 + c.slippage_bps / 10000) * (1 + c.entry_cost_bps / 10000)
        sell = rows[-1].close * (1 - c.slippage_bps / 10000) * (1 - c.exit_cost_bps / 10000)
        from toss_trading.risk import floor_step

        quantity = floor_step(allocation / buy, c.quantity_step)
        pnl = quantity * (sell - buy)
        results[symbol] = {"return": pnl / allocation, "net_pnl": pnl}
        total += pnl
    return {
        "total_return": total / c.initial_cash,
        "net_profit_after_fees": total,
        "by_symbol": results,
        "allocation": "equal initial cash per selected symbol",
    }


def simulate(
    bars: list[Bar], config: TradingConfig, strategy: Strategy | None = None
) -> tuple[State, list[tuple[str, Decimal]], dict]:
    """Replay bars through the PAPER engine; returns final state and (day, equity) curve."""
    if config.mode != "PAPER":
        raise ValueError("Backtest requires PAPER")
    grouped = defaultdict(list)
    for bar in bars:
        session = offline_session(bar.end.astimezone(NY).date())
        if session and session.open < bar.end <= session.close:
            grouped[bar.end].append(bar)
    if not grouped:
        raise ValueError("No regular-session data")
    curve = []
    prior_volume = {}
    stats = {"decisions": 0, "submitted_orders": 0, "minute_coverage": defaultdict(int)}
    store = MemoryStore(config)
    engine = TradingEngine(config, store, strategy)
    for end, rows in sorted(grouped.items()):
        session = offline_session(end.astimezone(NY).date())
        # Synthetic intraminute quote path is a model, not observed tick history.
        # Low before high provides a deterministic adverse-first long-position path.
        for seconds, field in ((0, "open"), (20, "low"), (40, "high"), (60, "close")):
            now = end - timedelta(minutes=1) + timedelta(seconds=seconds, microseconds=1)
            if seconds == 60:
                now = end
            quotes = []
            for bar in rows:
                price = getattr(bar, field)
                half_spread = Decimal("0.5") / 10000  # documented synthetic 1 bp spread
                quotes.append(
                    Quote(
                        symbol=bar.symbol,
                        at=now,
                        bid=price * (1 - half_spread),
                        ask=price * (1 + half_spread),
                        bid_size=prior_volume.get(bar.symbol, ZERO) / 4,
                        ask_size=prior_volume.get(bar.symbol, ZERO) / 4,
                    )
                )
            # No fill or indicator can see later phases/candles.
            records = engine.step(now, session, rows if seconds == 60 else [], quotes)
            stats["decisions"] += sum(r["event"] == "DECISION" for r in records)
            stats["submitted_orders"] += sum(r["event"] == "ORDER_SUBMITTED" for r in records)
            curve.append((session.day, store.state.equity(config.exit_cost_bps)))
        for bar in rows:
            stats["minute_coverage"][f"{session.day}:{bar.symbol}"] += 1
            prior_volume[bar.symbol] = bar.volume
    stats["minute_coverage"] = dict(stats["minute_coverage"])
    return store.state, curve, stats


def replay(bars: list[Bar], config: TradingConfig, strategy: Strategy | None = None) -> dict:
    state, curve, stats = simulate(bars, config, strategy)
    values = [config.initial_cash] + [value for _, value in curve]
    result = performance(state, values, config.initial_cash, config.symbols, config)
    result.update(
        {
            "buy_and_hold": buy_and_hold(bars, config),
            **stats,
            "trade_log": state.trades,
            "config": config.model_dump(mode="json"),
            "assumptions": [
                "1-minute end timestamps",
                "minute typical-price VWAP",
                "synthetic 1bp spread",
                "open-low-high-close quote path",
                "next event fills; price-capped buys; partial liquidity fills",
                "no corporate action/currency/tax correction beyond input data",
            ],
        }
    )
    return result


def split_backtest(bars: list[Bar], config: TradingConfig, split_day: date) -> dict:
    train = [b for b in bars if b.end.astimezone(NY).date() < split_day]
    validation = [b for b in bars if b.end.astimezone(NY).date() >= split_day]
    if not train or not validation:
        raise ValueError("Train and validation must both contain data in separate sessions")
    return {
        "split_us_date": split_day.isoformat(),
        "parameter_search": False,
        "training": replay(train, config),
        "validation": replay(validation, config),
    }


HEADLINE = ("total_return", "profit_factor", "win_rate", "total_trades", "mdd")


def headline(result: dict) -> dict:
    return {key: result[key] for key in HEADLINE}


_WORKER_BARS: list[Bar] = []


def _init_worker(bars: list[Bar]) -> None:
    global _WORKER_BARS
    _WORKER_BARS = bars


def _run(job: tuple) -> dict:
    kind, config, schedule = job
    if kind == "vwap_only":
        strategy = VwapCrossBaseline(config)
    elif kind == "random":
        strategy = RandomEntryBaseline(config, schedule)
    else:
        strategy = None
    return headline(replay(_WORKER_BARS, config, strategy))


def run_jobs(bars: list[Bar], jobs: list[tuple], workers: int) -> list[dict]:
    if workers <= 1:
        _init_worker(bars)
        return [_run(job) for job in jobs]
    with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(bars,)) as pool:
        return list(pool.map(_run, jobs))


def entry_offsets(trade_log: list[dict]) -> list[int]:
    """Minutes after the open of each entry's decision bar (fills follow the bar close)."""
    offsets = []
    for opened in sorted({trade["opened"] for trade in trade_log}):
        at = datetime.fromisoformat(opened)
        session = offline_session(at.astimezone(NY).date())
        offsets.append(int((at - session.open).total_seconds() // 300) * 5)
    return offsets


def draw_schedule(
    offsets: list[int], days: list[str], symbols: tuple[str, ...], seed: int
) -> set[tuple[str, str, int]]:
    """Same count and time-of-day distribution as the strategy's entries; random day/symbol."""
    rng = random.Random(seed)
    lengths = {}
    for day in days:
        session = offline_session(date.fromisoformat(day))
        lengths[day] = (session.close - session.open).total_seconds() // 60
    schedule: set[tuple[str, str, int]] = set()
    for offset in offsets:
        eligible = [d for d in days if lengths[d] > offset]  # Early closes are shorter.
        for _ in range(100):  # Redraw collisions so the count matches where possible.
            key = (rng.choice(eligible), rng.choice(symbols), offset)
            if key not in schedule:
                schedule.add(key)
                break
    return schedule


def percentile(value: Decimal, sample: list[Decimal]) -> Decimal:
    below = sum(x < value for x in sample) + Decimal(sum(x == value for x in sample)) / 2
    return Decimal(below) / len(sample) * 100


def distribution(values: list[Decimal]) -> dict:
    floats = sorted(float(v) for v in values)
    return {
        "min": floats[0],
        "median": statistics.median(floats),
        "max": floats[-1],
        "mean": statistics.fmean(floats),
        "stdev": statistics.stdev(floats) if len(floats) > 1 else 0.0,
    }


def baselines(bars: list[Bar], config: TradingConfig, seeds: int, workers: int) -> dict:
    current = replay(bars, config)
    days = sorted({key.split(":")[0] for key in current["minute_coverage"]})
    offsets = entry_offsets(current["trade_log"])
    jobs = [("vwap_only", config, None)] + [
        ("random", config, draw_schedule(offsets, days, config.symbols, seed))
        for seed in range(seeds)
    ]
    results = run_jobs(bars, jobs, workers)
    randoms = results[1:]
    report = {
        "current": headline(current),
        "vwap_only": results[0],
        "buy_and_hold": current["buy_and_hold"]["total_return"],
        "random_entry": {"seeds": seeds, "target_entries": len(offsets), "runs": randoms},
    }
    if randoms:
        returns = [r["total_return"] for r in randoms]
        factors = [r["profit_factor"] for r in randoms if r["profit_factor"] is not None]
        report["random_entry"]["total_return"] = distribution(returns)
        report["current_percentile_vs_random"] = {
            "total_return": percentile(current["total_return"], returns),
            "profit_factor": percentile(current["profit_factor"], factors)
            if factors and current["profit_factor"] is not None
            else None,
        }
    report["note"] = (
        "random_entry keeps the strategy's exits, risk limits and costs and only randomizes "
        "the entry day/symbol at the same times of day. A percentile near 50 means the entry "
        "signal is indistinguishable from chance on this data."
    )
    if current["total_trades"] < MIN_TRADES:
        report["sample_warning"] = current["sample_warning"]
    return report


def neighbors(config: TradingConfig) -> list[tuple[str, object]]:
    """One-at-a-time grid around the current values of parameters that affect decisions."""
    names = ["stretch_atr", "stop_atr", "reward_r", "atr_period"]
    names += [n for n in ("rsi_entry_max", "min_volume_ratio") if getattr(config, n) is not None]
    result = []
    for name in names:
        base = getattr(config, name)
        for factor in SENSITIVITY_FACTORS:
            value = Decimal(base) * factor
            result.append((name, max(2, round(value)) if isinstance(base, int) else value))
    return result


def sensitivity(bars: list[Bar], config: TradingConfig, workers: int) -> dict:
    grid = neighbors(config)
    configs = [config] + [
        TradingConfig.model_validate({**config.model_dump(), name: value}) for name, value in grid
    ]
    results = run_jobs(bars, [("current", c, None) for c in configs], workers)
    base, rows = results[0], []
    for (name, value), metrics in zip(grid, results[1:], strict=True):
        rows.append({"parameter": name, "value": value, **metrics})
    summary = {}
    for name in dict.fromkeys(n for n, _ in grid):
        group = [r for r in rows if r["parameter"] == name]
        factors = [r["profit_factor"] for r in group if r["profit_factor"] is not None]
        summary[name] = {
            "base_value": getattr(config, name),
            "total_return": distribution([r["total_return"] for r in group]),
            "profit_factor": distribution(factors) if factors else None,
            "neighbors_with_pf_above_1": sum(f > 1 for f in factors),
            "neighbors": len(group),
        }
    return {
        "base": base,
        "grid": rows,
        "summary": summary,
        "note": "Stability check only: compare the spread of neighbors with the base. "
        "No combination is recommended; picking the best cell here would overfit.",
    }


def walk_forward(bars: list[Bar], config: TradingConfig, train: int, test: int) -> dict:
    """Fixed parameters; the train span only provides history before each test window."""
    state, curve, _ = simulate(bars, config)
    days = sorted({day for day, _ in curve})
    end_equity = {}
    for day, value in curve:
        end_equity[day] = value
    trips = round_trips(state)
    windows = []
    for start in range(train, len(days), test):
        span = days[start : start + test]
        opening = end_equity[days[start - 1]]
        peak, mdd = opening, ZERO
        for day, value in curve:
            if day in span:
                peak = max(peak, value)
                mdd = max(mdd, (peak - value) / peak)
        closed = [
            t
            for t in trips
            if datetime.fromisoformat(t["closed"]).astimezone(NY).date().isoformat() in span
        ]
        stats = summarize(closed)
        windows.append(
            {
                "first_day": span[0],
                "last_day": span[-1],
                "return": end_equity[span[-1]] / opening - 1,
                "trades": stats["trades"],
                "win_rate": stats["win_rate"],
                "profit_factor": stats["profit_factor"],
                "mdd": mdd,
            }
        )
    returns = [w["return"] for w in windows]
    return {
        "train_days": train,
        "test_days": test,
        "parameter_search": False,
        "windows": windows,
        "summary": {
            "windows": len(windows),
            "positive_windows": sum(r > 0 for r in returns),
            "return": distribution(returns) if returns else None,
            "trades": sum(w["trades"] for w in windows),
        },
    }


def table(rows: list[dict], columns: tuple[str, ...]) -> str:
    def cell(value):
        if value is None:
            return "-"
        if isinstance(value, Decimal | float):
            return f"{float(value):.4f}"
        return str(value)

    lines = [" | ".join(columns)]
    lines += [" | ".join(cell(row.get(c)) for c in columns) for row in rows]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--config", default="config/strategy.toml")
    parser.add_argument("--split", type=date.fromisoformat)
    parser.add_argument("--baselines", action="store_true")
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--sensitivity", action="store_true")
    parser.add_argument("--walk-forward", metavar="TRAIN,TEST")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--output", default="data/backtest.json")
    args = parser.parse_args()
    if not (args.split or args.baselines or args.sensitivity or args.walk_forward):
        parser.error("choose --split, --baselines, --sensitivity and/or --walk-forward")
    config = load_config(args.config)
    bars = read_csv(args.csv, config)
    report = split_backtest(bars, config, args.split) if args.split else {}
    if args.baselines:
        report["baselines"] = result = baselines(bars, config, args.seeds, args.workers)
        rows = [
            {"run": "current", **result["current"]},
            {"run": "vwap_only", **result["vwap_only"]},
        ]
        if args.seeds:
            spread = result["random_entry"]["total_return"]
            rows.append({"run": "random median", "total_return": spread["median"]})
        print(table(rows, ("run", *HEADLINE)))
        print("buy_and_hold total_return", result["buy_and_hold"])
        print("current percentile vs random:", result.get("current_percentile_vs_random"))
    if args.sensitivity:
        report["sensitivity"] = result = sensitivity(bars, config, args.workers)
        print(table(result["grid"], ("parameter", "value", *HEADLINE)))
    if args.walk_forward:
        train, test = (int(x) for x in args.walk_forward.split(","))
        report["walk_forward"] = result = walk_forward(bars, config, train, test)
        print(
            table(
                result["windows"],
                ("first_day", "last_day", "return", "trades", "win_rate", "profit_factor", "mdd"),
            )
        )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    print(f"Backtest report: {output}")


if __name__ == "__main__":
    main()
