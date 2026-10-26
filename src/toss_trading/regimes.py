"""Run independent bull, sideways and bear backtests with one unchanged strategy."""

import argparse
import csv
import json
import tomllib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toss_trading.backtest import HEADLINE, headline, read_csv, replay, table
from toss_trading.domain import Bar
from toss_trading.marketdata import NY, offline_session
from toss_trading.trading_config import TradingConfig, load_config

REGIMES = ("bull", "sideways", "bear")
LABELS = {"bull": "상승장", "sideways": "횡보장", "bear": "하락장"}


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_return: Decimal = Field(ge=Decimal("-0.1"), le=Decimal("0.1"))
    intraday_swing: Decimal = Field(default=Decimal("0.012"), gt=0, le=Decimal("0.1"))
    csv: str | None = None


class RegimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date = date(2026, 9, 1)
    sessions: int = Field(default=22, ge=2, le=252)
    # Explicit exposure avoids guessing whether an arbitrary ticker is inverse.
    exposure: dict[str, Literal[-1, 1]]
    scenarios: dict[str, Scenario]

    @model_validator(mode="after")
    def validate_scenarios(self):
        if set(self.scenarios) != set(REGIMES):
            raise ValueError("Provide exactly bull, sideways and bear scenarios")
        if self.scenarios["bull"].daily_return <= 0:
            raise ValueError("bull daily_return must be positive")
        if self.scenarios["bear"].daily_return >= 0:
            raise ValueError("bear daily_return must be negative")
        if self.scenarios["sideways"].daily_return != 0:
            raise ValueError("sideways daily_return must be zero")
        return self


def synthetic_bars(config: TradingConfig, suite: RegimeConfig, scenario: Scenario) -> list[Bar]:
    """Deterministic OHLCV with pullbacks/rebounds and opposite inverse ETF exposure.

    Session endpoints follow the configured drift; triangular intraday waves return
    to zero at the close. This is an engine exercise, not a leveraged ETF price model.
    """
    if set(suite.exposure) != set(config.symbols):
        raise ValueError("exposure must specify every selected symbol exactly once")
    prices = {symbol: Decimal(20 + 10 * i) for i, symbol in enumerate(config.symbols)}
    bars = []
    day, count = suite.start, 0
    while count < suite.sessions:
        session = offline_session(day)
        day += timedelta(days=1)
        if session is None:
            continue
        minutes = int((session.close - session.open).total_seconds() // 60)
        for minute in range(1, minutes + 1):
            progress = Decimal(minute) / minutes
            # Three full cycles: dips and rebounds in every market regime.
            phase = (progress * 3) % 1
            wave = (
                4 * phase
                if phase <= Decimal("0.25")
                else (2 - 4 * phase if phase <= Decimal("0.75") else 4 * phase - 4)
            )
            for symbol in config.symbols:
                base = prices[symbol]
                direction = suite.exposure[symbol]
                close = base * (
                    1
                    + direction
                    * (scenario.daily_return * progress + scenario.intraday_swing * wave)
                )
                previous = bars[-len(config.symbols)].close if minute > 1 else base
                spread = base * Decimal("0.0005")
                bars.append(
                    Bar(
                        symbol=symbol,
                        end=session.open + timedelta(minutes=minute),
                        open=previous,
                        high=max(previous, close) + spread,
                        low=min(previous, close) - spread,
                        close=close,
                        volume=Decimal(10000 if minute % 130 < 15 else 3000),
                    )
                )
        for symbol in config.symbols:
            prices[symbol] *= 1 + suite.exposure[symbol] * scenario.daily_return
        count += 1
    return bars


def write_csv(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ("symbol", "end", "open", "high", "low", "close", "volume")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for bar in bars:
            writer.writerow(bar.model_dump(mode="json", include=set(fields)))


def run_suite(strategy_path: Path, suite_path: Path, output: Path) -> dict:
    config = load_config(strategy_path)
    suite = RegimeConfig.model_validate(tomllib.loads(suite_path.read_text()))
    if config.mode != "PAPER":
        raise ValueError("Backtest requires PAPER")
    output.mkdir(parents=True, exist_ok=True)
    results, comparison = {}, []
    for name in REGIMES:
        scenario = suite.scenarios[name]
        if scenario.csv:
            path = (suite_path.parent / scenario.csv).resolve()
            bars = read_csv(path, config)
            source = "historical_csv"
        else:
            bars = synthetic_bars(config, suite, scenario)
            path = output / f"{name}.csv"
            write_csv(path, bars)
            source = "synthetic"
        present = {b.symbol for b in bars}
        if present != set(config.symbols):
            raise ValueError(f"{name}: data must contain all selected symbols")
        result = replay(bars, config)  # New ledger and identical parameters for each regime.
        result["scenario"] = {
            "regime": name,
            "label": LABELS[name],
            "source": source,
            "csv": str(path),
            "first_bar": bars[0].end.isoformat(),
            "last_bar": bars[-1].end.isoformat(),
            "sessions": len({b.end.astimezone(NY).date() for b in bars}),
            "parameters": scenario.model_dump(mode="json"),
        }
        results[name] = result
        comparison.append(
            {
                "regime": name,
                **headline(result),
                "buy_and_hold_return": result["buy_and_hold"]["total_return"],
                "net_profit_after_fees": result["net_profit_after_fees"],
                "unclosed_positions": result["unclosed_positions"],
                "unresolved_orders": result["unresolved_orders"],
                "sample_warning": result.get("sample_warning"),
            }
        )
        (output / f"{name}.json").write_text(
            json.dumps(result, indent=2, default=str, ensure_ascii=False)
        )
        print(f"{LABELS[name]} 완료 ({source}, {result['total_trades']} trades)", flush=True)
    report = {
        "strategy_config": str(strategy_path),
        "suite_config": str(suite_path),
        "independent_initial_cash": config.initial_cash,
        "note": "각 장세는 동일 전략·비용과 독립 원장으로 실행합니다. 합성 데이터는 실제 시장 "
        "성과가 아닙니다. CSV의 장세는 사용자가 지정하며 자동 판별하지 않습니다. "
        "롱·인버스 균등 Buy & Hold는 서로 상쇄될 수 있으므로 종목별 결과도 확인하세요.",
        "comparison": comparison,
        "regimes": results,
    }
    (output / "summary.json").write_text(
        json.dumps(report, indent=2, default=str, ensure_ascii=False)
    )
    print(table(comparison, ("regime", *HEADLINE, "buy_and_hold_return")))
    print(f"Regime report: {output / 'summary.json'}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/strategy.toml"))
    parser.add_argument("--suite", type=Path, default=Path("config/backtest-regimes.toml"))
    parser.add_argument("--output", type=Path, default=Path("data/regimes"))
    args = parser.parse_args()
    run_suite(args.config, args.suite, args.output)


if __name__ == "__main__":
    main()
