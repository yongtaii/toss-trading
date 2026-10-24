import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    strategy_name: str = Field(default="vwap_mean_revert", min_length=1)
    symbols: tuple[str, ...] = ("SOXL", "SOXS", "TQQQ", "SQQQ")
    initial_cash: Decimal = Field(default=Decimal("10000"), gt=0)
    ema_fast: int = Field(default=9, ge=2)
    ema_slow: int = Field(default=21, ge=3)
    volume_period: int = Field(default=20, ge=1)
    atr_period: int = Field(default=14, ge=2)
    # Entry: close below session VWAP minus this many 5-minute ATRs.
    stretch_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    stop_atr: Decimal = Field(default=Decimal("2"), gt=0)
    reward_r: Decimal = Field(default=Decimal("3"), gt=0)
    risk_fraction: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.005"))
    daily_loss_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.01"))
    symbol_fraction: Decimal = Field(default=Decimal("0.20"), gt=0, le=Decimal("0.20"))
    max_order_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_positions: int = Field(default=3, ge=1, le=3)
    cooldown_minutes: int = Field(default=30, ge=30)
    max_entries: int = Field(default=3, ge=1, le=3)
    entry_after_minutes: int = Field(default=15, ge=15)
    entry_before_close_minutes: int = Field(default=30, ge=30)
    no_buy_before_close_minutes: int = Field(default=15, ge=15)
    flatten_before_close_minutes: int = Field(default=15, ge=5)
    flatten_deadline_minutes: int = Field(default=5, ge=5)
    max_quote_age_seconds: int = Field(default=10, ge=1)
    max_bar_age_seconds: int = Field(default=90, ge=1)
    max_spread_bps: Decimal = Field(default=Decimal("20"), gt=0)
    max_cost_bps: Decimal = Field(default=Decimal("30"), gt=0)
    fee_bps: Decimal = Field(default=Decimal("2"), ge=0)
    slippage_bps: Decimal = Field(default=Decimal("3"), ge=0)
    quantity_step: Decimal = Field(default=Decimal("1"), gt=0)
    participation: Decimal = Field(default=Decimal("0.1"), gt=0, le=1)
    paper_poll_seconds: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def validate_rules(self):
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Provide unique explicitly selected symbols")
        if any(
            not s.isascii() or not s.replace("-", "").replace(".", "").isalnum() or s != s.upper()
            for s in self.symbols
        ):
            raise ValueError("Use uppercase ticker symbols")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("Fast EMA must be shorter than slow EMA")
        if self.flatten_before_close_minutes < self.flatten_deadline_minutes:
            raise ValueError("Flatten must start before its deadline")
        return self


def load_config(path: str | Path = "config/strategy.toml") -> TradingConfig:
    with Path(path).open("rb") as handle:
        return TradingConfig.model_validate(tomllib.load(handle))
