"""Common minute replay utilities for explicitly injected strategies, without a strategy CLI."""

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from toss_trading.domain import ZERO, Bar, Quote, State
from toss_trading.engine import TradingEngine
from toss_trading.marketdata import NY, offline_session, previous_session
from toss_trading.strategy import Strategy
from toss_trading.trading_config import TradingConfig

MIN_TRADES = 100  # Below this, report statistics carry an explicit small-sample warning.


def read_csv(path: str | Path, config: TradingConfig) -> list[Bar]:
    with Path(path).open(newline="") as handle:
        bars = [Bar.model_validate(row) for row in csv.DictReader(handle)]
    seen = set()
    for bar in bars:
        if bar.symbol not in config.market_symbols:
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


QUOTE_PATHS = {
    "adverse_first": ((0, "open"), (20, "low"), (40, "high"), (60, "close")),
    "favorable_first": ((0, "open"), (20, "high"), (40, "low"), (60, "close")),
}


def simulate(
    bars: list[Bar],
    config: TradingConfig,
    strategy: Strategy | None = None,
    *,
    prior_bars: list[Bar] | None = None,
    quote_path: str = "adverse_first",
) -> tuple[State, list[tuple[str, Decimal]], dict]:
    """Replay bars through the PAPER engine; returns final state and (day, equity) curve."""
    if strategy is None:
        raise ValueError(
            "No runtime strategy is configured; use research.compare_weekly_allocations"
        )
    if quote_path not in QUOTE_PATHS:
        raise ValueError(f"Unknown quote path: {quote_path}")
    if config.mode != "PAPER":
        raise ValueError("Backtest requires PAPER")
    missing = set(config.market_symbols) - {b.symbol for b in bars}
    if config.underlying_signals and missing:
        raise ValueError(f"Missing execution/underlying candles: {sorted(missing)}")
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
    if prior_bars:
        first = min(grouped).astimezone(NY).date()
        prior = previous_session(first)
        if prior is None or any(not prior.open < b.end <= prior.close for b in prior_bars):
            raise ValueError("Warmup must only contain the previous completed regular session")
        engine.seed_prior(prior, prior_bars, prior.close)
    for end, rows in sorted(grouped.items()):
        session = offline_session(end.astimezone(NY).date())
        # Synthetic intraminute quote path is a model, not observed tick history.
        # Inverse ETFs traverse the mirrored extreme so that underlying stops cannot
        # observe a low while an inverse ETF also sees a low.
        for seconds, field in QUOTE_PATHS[quote_path]:
            now = end - timedelta(minutes=1) + timedelta(seconds=seconds, microseconds=1)
            if seconds == 60:
                now = end
            quotes = []
            for bar in rows:
                _, direction, _ = config.signal_source(bar.symbol)
                quote_field = field
                if direction == -1 and field in {"low", "high"}:
                    quote_field = "high" if field == "low" else "low"
                price = getattr(bar, quote_field)
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


def replay(
    bars: list[Bar],
    config: TradingConfig,
    strategy: Strategy | None = None,
    *,
    prior_bars: list[Bar] | None = None,
) -> dict:
    state, curve, stats = simulate(bars, config, strategy, prior_bars=prior_bars)
    values = [config.initial_cash] + [value for _, value in curve]
    result = performance(state, values, config.initial_cash, config.symbols, config)
    result.update(
        {
            **stats,
            "trade_log": state.trades,
            "config": config.model_dump(mode="json"),
            "assumptions": [
                "1-minute end timestamps",
                "prior completed session supplied for indicator-only warmup"
                if prior_bars
                else "no supplied initial indicator warmup",
                "minute typical-price VWAP",
                "synthetic 1bp spread",
                "adverse_first synthetic quote path: underlying low before high, "
                "inverse extremes mirrored",
                "next event fills; price-capped buys; partial liquidity fills",
                "mapped signals: underlying OHLCV only; inverse direction mirrored"
                if config.underlying_signals
                else "legacy: traded ETF signals",
                "underlying quote stop/target; 3x underlying ATR percentage for sizing"
                if config.underlying_signals
                else "legacy: traded ETF ATR stop/target",
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


def main():
    raise SystemExit(
        "Minute strategy CLI was removed. Use python -m research.compare_weekly_allocations."
    )


if __name__ == "__main__":
    main()
