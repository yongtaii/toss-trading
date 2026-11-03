import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toss_trading.config import Settings
from toss_trading.trading_config import TradingConfig, load_config

WEEKLY_STRATEGY = "weekly_baseline"


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    account_seq: int | None = Field(default=None, gt=0)
    capital_usd: Decimal = Field(gt=0)
    max_order_usd: Decimal = Field(gt=0)
    daily_loss_usd: Decimal = Field(gt=0)
    weekly_target_allocation: Decimal | None = Field(default=None, gt=0, le=1)
    strategy_path: str = "config/strategy.toml"
    database_url: str = "sqlite:///data/live.db"
    control_path: str = "data/live-control.json"
    max_verification_age_seconds: int = Field(default=30, ge=1, le=60)
    max_entry_age_seconds: int = Field(default=10, ge=1, le=30)
    # Explicitly acknowledged holdings outside this strategy; never traded or valued by it.
    protected_positions: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def limits(self):
        for symbol, quantity in self.protected_positions.items():
            if symbol in {"SOXL", "SOXS", "TQQQ", "SQQQ"}:
                raise ValueError("Protected holdings cannot overlap the strategy's trade symbols")
            if not symbol or symbol != symbol.upper() or not quantity.is_finite() or quantity <= 0:
                raise ValueError(
                    "Protected holdings need uppercase symbols and positive quantities"
                )
        if self.enabled and self.account_seq is None:
            raise ValueError("An account must be selected before enabling live execution")
        if self.max_order_usd > self.capital_usd or self.daily_loss_usd >= self.capital_usd:
            raise ValueError("Live limits must fit the allocated capital")
        return self

    def strategy(self) -> TradingConfig:
        config = load_config(self.strategy_path)
        if config.strategy_name != WEEKLY_STRATEGY or config.mode != "LIVE":
            raise ValueError("Existing research configuration is offline only; select weekly LIVE")
        return TradingConfig.model_validate(
            {
                **config.model_dump(),
                "symbols": ("SOXL", "TQQQ"),
                "underlying_signals": True,
                "groups": {},
                "opposing_groups": (),
                "initial_cash": self.capital_usd,
                "max_order_usd": min(self.max_order_usd, Decimal("298")),
                "max_positions": 2,
                "quantity_step": Decimal(1),
                "risk_fraction": Decimal("0.005"),
                "stop_atr": Decimal(1),
                "reward_r": Decimal(3),
                "hold_overnight": True,
            }
        )


def load_live_config(path: str | Path) -> LiveConfig:
    with Path(path).open("rb") as handle:
        values = tomllib.load(handle)
    if "account_seq" not in values:
        values["account_seq"] = Settings().toss_account_seq
    return LiveConfig.model_validate(values)
