"""Offline one-minute CSV replay using the same strategy/risk/engine as PAPER."""

import argparse
import csv
import json
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from toss_trading.domain import ZERO, Bar, Quote, State
from toss_trading.engine import TradingEngine
from toss_trading.marketdata import NY, offline_session
from toss_trading.persistence import Store
from toss_trading.trading_config import TradingConfig, load_config


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


def performance(
    state: State, curve: list[Decimal], initial: Decimal, symbols: tuple[str, ...] = ()
) -> dict:
    peak, mdd = initial, ZERO
    for value in curve:
        peak = max(peak, value)
        mdd = max(mdd, (peak - value) / peak)
    # Aggregate partial sell fills into a round-trip position before statistics.
    groups = defaultdict(lambda: {"pnl": ZERO, "cost": ZERO, "symbol": ""})
    for trade in state.trades:
        item = groups[(trade["symbol"], trade["opened"])]
        item["pnl"] += Decimal(trade["pnl"])
        item["cost"] += Decimal(trade["entry"]) * Decimal(trade["quantity"])
        item["cost"] += Decimal(trade["entry_fee"])
        item["symbol"] = trade["symbol"]
    opened = {(p.symbol, p.opened.isoformat()) for p in state.positions.values()}
    completed = [value for key, value in groups.items() if key not in opened]
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
    return {
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


def buy_and_hold(bars: list[Bar], c: TradingConfig) -> dict:
    allocation = c.initial_cash / len(c.symbols)
    total = ZERO
    results = {}
    for symbol in c.symbols:
        rows = [b for b in bars if b.symbol == symbol]
        if not rows:
            results[symbol] = None
            continue
        buy = rows[0].open * (1 + c.slippage_bps / 10000) * (1 + c.fee_bps / 10000)
        sell = rows[-1].close * (1 - c.slippage_bps / 10000) * (1 - c.fee_bps / 10000)
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


def replay(bars: list[Bar], config: TradingConfig) -> dict:
    if config.mode != "PAPER":
        raise ValueError("Backtest requires PAPER")
    grouped = defaultdict(list)
    for bar in bars:
        session = offline_session(bar.end.astimezone(NY).date())
        if session and session.open < bar.end <= session.close:
            grouped[bar.end].append(bar)
    if not grouped:
        raise ValueError("No regular-session data")
    curve = [config.initial_cash]
    prior_volume = {}
    decisions, orders = 0, 0
    coverage = defaultdict(int)
    with tempfile.TemporaryDirectory() as directory:
        store = Store(f"sqlite:///{directory}/backtest.db", config)
        engine = TradingEngine(config, store)
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
                decisions += sum(r["event"] == "DECISION" for r in records)
                orders += sum(r["event"] == "ORDER_SUBMITTED" for r in records)
                _, state = store.load()
                curve.append(state.equity(config.fee_bps))
            for bar in rows:
                coverage[f"{session.day}:{bar.symbol}"] += 1
                prior_volume[bar.symbol] = bar.volume
        _, state = store.load()
        result = performance(state, curve, config.initial_cash, config.symbols)
        result.update(
            {
                "buy_and_hold": buy_and_hold(
                    [b for rows in grouped.values() for b in rows], config
                ),
                "decisions": decisions,
                "submitted_orders": orders,
                "minute_coverage": dict(coverage),
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
        store.close()
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--config", default="config/strategy.toml")
    parser.add_argument("--split", type=date.fromisoformat, required=True)
    parser.add_argument("--output", default="data/backtest.json")
    args = parser.parse_args()
    config = load_config(args.config)
    report = split_backtest(read_csv(args.csv, config), config, args.split)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    print(f"Backtest report: {output}")


if __name__ == "__main__":
    main()
