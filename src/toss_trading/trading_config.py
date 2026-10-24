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
    # Optional oversold filter: enter only when Wilder RSI <= this value. None disables it.
    rsi_period: int = Field(default=14, ge=2)
    rsi_entry_max: Decimal | None = Field(default=None, gt=0, lt=100)
    # EMA/ATR/RSI seed: prior regular session 5m bars, or today's bars only. VWAP always resets.
    indicator_warmup: Literal["prior_session", "intraday_only"] = "prior_session"
    # Skip the prior-session seed when the overnight gap suggests an unadjusted split.
    split_gap_fraction: Decimal = Field(default=Decimal("0.4"), gt=0)
    # Volume ratio: vs the prior volume_period bars, or vs the same 5m slot on past sessions.
    volume_mode: Literal["rolling", "time_of_day"] = "time_of_day"
    volume_lookback_days: int = Field(default=20, ge=1)
    # Without enough slot history: "block" fails the volume check, "rolling" falls back.
    volume_fallback: Literal["block", "rolling"] = "block"
    # Optional entry filter on the volume ratio. None disables it (ratio is logged only).
    min_volume_ratio: Decimal | None = Field(default=None, gt=0)
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
    # Conservative placeholders, not confirmed broker terms; set from your own fee schedule.
    fee_bps: Decimal = Field(default=Decimal("10"), ge=0)
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0)
    fx_spread_bps: Decimal = Field(default=Decimal("0"), ge=0)  # One-way FX conversion cost.
    # Sell-side regulatory charges (SEC fee on proceeds, FINRA TAF per share, capped).
    sec_fee_bps: Decimal = Field(default=Decimal("0"), ge=0)
    taf_per_share: Decimal = Field(default=Decimal("0"), ge=0)
    taf_max_usd: Decimal = Field(default=Decimal("0"), ge=0)  # 0 means no cap.
    quantity_step: Decimal = Field(default=Decimal("1"), gt=0)
    participation: Decimal = Field(default=Decimal("0.1"), gt=0, le=1)
    paper_poll_seconds: int = Field(default=5, ge=1)
    # Direction groups: correlated symbols share one risk budget; opposing groups never mix.
    groups: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: {"NASDAQ_LONG": ("SOXL", "TQQQ"), "NASDAQ_SHORT": ("SOXS", "SQQQ")}
    )
    opposing_groups: tuple[tuple[str, str], ...] = (("NASDAQ_LONG", "NASDAQ_SHORT"),)
    allow_opposing_groups: bool = False
    max_group_risk_fraction: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.01"))

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
        members = [s for group in self.groups.values() for s in group]
        if len(members) != len(set(members)) or not set(members) <= set(self.symbols):
            raise ValueError("Each grouped symbol must be selected and belong to one group")
        if any(g not in self.groups for pair in self.opposing_groups for g in pair):
            raise ValueError("Opposing groups must be defined in groups")
        return self

    @property
    def entry_cost_bps(self) -> Decimal:
        return self.fee_bps + self.fx_spread_bps

    @property
    def exit_cost_bps(self) -> Decimal:
        return self.fee_bps + self.fx_spread_bps + self.sec_fee_bps

    def sell_charges(self, quantity: Decimal, price: Decimal) -> Decimal:
        taf = quantity * self.taf_per_share
        if self.taf_max_usd:
            taf = min(taf, self.taf_max_usd)
        return quantity * price * self.exit_cost_bps / 10000 + taf

    def group_of(self, symbol: str) -> str | None:
        return next((name for name, group in self.groups.items() if symbol in group), None)

    def opposing(self, group: str | None) -> set[str]:
        return {b if a == group else a for a, b in self.opposing_groups if group in (a, b)}


def load_config(path: str | Path = "config/strategy.toml") -> TradingConfig:
    with Path(path).open("rb") as handle:
        return TradingConfig.model_validate(tomllib.load(handle))
