import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    strategy_name: str = Field(default="research_only", min_length=1)
    symbols: tuple[str, ...] = ("SOXL", "SOXS", "TQQQ", "SQQQ")
    # False is retained only for reproducing historical ETF-signal reports.
    underlying_signals: bool = True
    initial_cash: Decimal = Field(default=Decimal("10000"), gt=0)
    ema_fast: int = Field(default=9, ge=2)
    ema_slow: int = Field(default=21, ge=3)
    volume_period: int = Field(default=20, ge=1)
    atr_period: int = Field(default=14, ge=2)
    # Entry: close below session VWAP minus this many 5-minute ATRs.
    stretch_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    # Reversal entries require enough expected gross recovery to cover round-trip costs.
    min_reversion_cost_multiple: Decimal = Field(default=Decimal("2"), ge=1)
    # Optional net VWAP/target reward divided by stop loss including round-trip costs.
    # None preserves the historical reversal strategy for comparison and existing ledgers.
    min_net_reversion_r: Decimal | None = Field(default=None, ge=1)
    # Shorter completed candles confirm turns earlier; indicators stay on 5m bars.
    reversal_confirmation_minutes: Literal[1, 5] = 5
    # Market-structure research strategies; inert for the existing strategies.
    structure_lookback_bars: int = Field(default=6, ge=3, le=24)
    opening_range_minutes: Literal[15, 30] = 15
    signal_timeframe_minutes: Literal[3, 5, 15, 30] = 15
    # Used by the opt-in active breakout research strategies only.
    breakout_reference: Literal["prior_session", "intraday_channel"] = "prior_session"
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
    # Optional exit tightening, measured on the signal source and never loosened.
    # trail_atr keeps the stop this many ATR behind the best price reached;
    # breakeven_r lifts it to entry plus the round trip once that many R is
    # reached. None leaves the entry stop fixed, which is the previous behaviour.
    trail_atr: Decimal | None = Field(default=None, gt=0)
    trail_activation_r: Decimal = Field(default=Decimal("0"), ge=0)
    breakeven_r: Decimal | None = Field(default=None, gt=0)
    # Lock this much initial risk as well as estimated round-trip charges.
    breakeven_lock_r: Decimal = Field(default=Decimal("0"), ge=0)
    # Liquidate when the signal source has not produced a usable quote for this
    # long: without it the ATR stop cannot be observed at all. None keeps the
    # previous behaviour of holding an unmonitored position until the flatten.
    max_unmonitored_seconds: int | None = Field(default=None, ge=1)
    # Elapsed wall-clock minutes since the first fill; evaluated on each event.
    max_holding_minutes: int | None = Field(default=None, ge=1)
    # Carry positions across sessions instead of flattening before the close. The
    # stop and target stay on the signal source, so an overnight gap can exceed
    # the intended loss. False keeps the intraday-only behaviour.
    hold_overnight: bool = False
    # Completed full sessions a daily rule may look back over.
    daily_lookback_days: int = Field(default=20, ge=2)
    risk_fraction: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.005"))
    daily_loss_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.01"))
    symbol_fraction: Decimal = Field(default=Decimal("0.20"), gt=0, le=Decimal("0.20"))
    max_order_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_positions: int = Field(default=3, ge=1, le=3)
    cooldown_minutes: int = Field(default=30, ge=30)
    max_entries: int = Field(default=3, ge=1, le=3)
    entry_after_minutes: int = Field(default=15, ge=5)
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
    # Per-sale floor for the SEC fee. At the order sizes used here the floor is
    # what actually gets charged, so it is applied to realised fills; the
    # pre-trade estimates keep the proportional rate only.
    sec_fee_min_usd: Decimal = Field(default=Decimal("0"), ge=0)
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
        if self.signal_timeframe_minutes in (3, 5) and self.strategy_name not in {
            "active_prior_breakout",
            "active_cost_breakout",
        }:
            raise ValueError("3m/5m signal timeframes require an active breakout strategy")
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
    def needs_minute_seed(self) -> bool:
        return self.signal_timeframe_minutes == 3 and self.strategy_name in {
            "active_prior_breakout",
            "active_cost_breakout",
        }

    @property
    def entry_cost_bps(self) -> Decimal:
        return self.fee_bps + self.fx_spread_bps

    def signal_source(self, symbol: str) -> tuple[str, int, Decimal]:
        if self.underlying_signals:
            sources = {
                "TQQQ": ("QQQ", 1),
                "SQQQ": ("QQQ", -1),
                "SOXL": ("SOXX", 1),
                "SOXS": ("SOXX", -1),
            }
            if symbol in sources:
                source, direction = sources[symbol]
                return source, direction, Decimal(3)
        return symbol, 1, Decimal(1)

    @property
    def signal_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.signal_source(s)[0] for s in self.symbols))

    @property
    def market_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.symbols, *self.signal_symbols)))

    @property
    def exit_cost_bps(self) -> Decimal:
        return self.fee_bps + self.fx_spread_bps + self.sec_fee_bps

    def sell_charges(self, quantity: Decimal, price: Decimal) -> Decimal:
        taf = quantity * self.taf_per_share
        if self.taf_max_usd:
            taf = min(taf, self.taf_max_usd)
        proceeds = quantity * price
        regulatory = proceeds * self.sec_fee_bps / 10000
        if self.sec_fee_bps and self.sec_fee_min_usd:
            regulatory = max(regulatory, self.sec_fee_min_usd)
        other = self.exit_cost_bps - self.sec_fee_bps
        return proceeds * other / 10000 + regulatory + taf

    def group_of(self, symbol: str) -> str | None:
        return next((name for name, group in self.groups.items() if symbol in group), None)

    def opposing(self, group: str | None) -> set[str]:
        return {b if a == group else a for a, b in self.opposing_groups if group in (a, b)}


def load_config(path: str | Path = "config/strategy.toml") -> TradingConfig:
    with Path(path).open("rb") as handle:
        return TradingConfig.model_validate(tomllib.load(handle))
