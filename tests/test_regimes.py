"""Regime direction, inverse exposure, calendar handling and independent replay."""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from toss_trading.backtest import read_csv, replay
from toss_trading.marketdata import NY, offline_session
from toss_trading.regimes import RegimeConfig, Scenario, run_suite, synthetic_bars, write_csv
from toss_trading.trading_config import load_config

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "config/strategy.toml"


def suite(**updates):
    return RegimeConfig.model_validate(
        {
            "start": "2026-11-25",
            "sessions": 2,
            "exposure": {"SOXL": 1, "SOXS": -1, "TQQQ": 1, "SQQQ": -1},
            "scenarios": {
                "bull": {"daily_return": "0.006"},
                "sideways": {"daily_return": "0"},
                "bear": {"daily_return": "-0.006"},
            },
            **updates,
        }
    )


@pytest.mark.parametrize("regime,sign", [("bull", 1), ("sideways", 0), ("bear", -1)])
def test_regime_paths_inverse_exposure_and_session_calendar(regime, sign):
    config, settings = load_config(STRATEGY), suite()
    bars = synthetic_bars(config, settings, settings.scenarios[regime])
    assert bars == synthetic_bars(config, settings, settings.scenarios[regime])
    days = {b.end.astimezone(NY).date() for b in bars}
    assert days == {date(2026, 11, 25), date(2026, 11, 27)}  # Thanksgiving skipped.
    assert len(bars) == (390 + 210) * 4  # Early close respected.
    for symbol in config.symbols:
        rows = [b for b in bars if b.symbol == symbol]
        change = rows[-1].close - rows[0].open
        assert (change > 0) - (change < 0) == sign * settings.exposure[symbol]
        assert all(a.close == b.open for a, b in zip(rows, rows[1:], strict=False))
        moves = [b.close - b.open for b in rows]
        assert min(moves) < 0 < max(moves)  # Pullbacks and rebounds in all regimes.
        assert all(b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in rows)
        assert all(b.end <= offline_session(b.end.astimezone(NY).date()).close for b in rows)


def test_invalid_regime_configuration_fails():
    with pytest.raises(ValidationError, match="exactly"):
        suite(scenarios={"bull": {"daily_return": "0.01"}})
    with pytest.raises(ValueError, match="exposure"):
        synthetic_bars(
            load_config(STRATEGY), suite(exposure={"SOXL": 1}), Scenario(daily_return="0")
        )


def test_suite_csv_and_synthetic_use_independent_identical_strategy(tmp_path):
    config, settings = load_config(STRATEGY), suite()
    source = tmp_path / "history.csv"
    write_csv(source, synthetic_bars(config, settings, settings.scenarios["bull"]))
    manifest = tmp_path / "suite.toml"
    manifest.write_text("""start = "2026-11-25"
sessions = 2
[exposure]
SOXL = 1
SOXS = -1
TQQQ = 1
SQQQ = -1
[scenarios.bull]
daily_return = "0.006"
csv = "history.csv"
[scenarios.sideways]
daily_return = "0"
[scenarios.bear]
daily_return = "-0.006"
""")
    output = tmp_path / "reports"
    report = run_suite(STRATEGY, manifest, output)
    assert list(report["regimes"]) == ["bull", "sideways", "bear"]
    assert report["regimes"]["bull"]["scenario"]["source"] == "historical_csv"
    expected = replay(read_csv(source, config), config)
    for name, result in report["regimes"].items():
        assert result["config"] == expected["config"]
        assert result["unclosed_positions"] == []
        assert result["unresolved_orders"] == []
        assert (output / f"{name}.json").exists()
    assert report["regimes"]["bull"]["trade_log"] == expected["trade_log"]
    assert report["regimes"]["bull"]["total_return"] == expected["total_return"]
    saved = json.loads((output / "summary.json").read_text())
    assert len(saved["comparison"]) == 3
    assert read_csv(output / "bear.csv", config)
