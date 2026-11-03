from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

D = Decimal
ZERO = D("0")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Bar(Model):
    """One completed minute; timestamp is exclusive interval end, in UTC/offset time."""

    symbol: str
    end: AwareDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def coherent(self):
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Inconsistent OHLC")
        if self.end.second or self.end.microsecond:
            raise ValueError("Minute bar must end on a minute boundary")
        return self


class Quote(Model):
    symbol: str
    at: AwareDatetime
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    bid_size: Decimal = Field(ge=0)
    ask_size: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def uncrossed(self):
        if self.bid > self.ask:
            raise ValueError("Crossed orderbook")
        return self


class Session(Model):
    day: str
    open: AwareDatetime
    close: AwareDatetime

    @model_validator(mode="after")
    def coherent(self):
        if self.close <= self.open:
            raise ValueError("Invalid market session")
        return self

    def contains(self, at: datetime) -> bool:
        return self.open <= at < self.close


class OrderStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


ACTIVE = {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}


class Order(Model):
    id: str
    symbol: str
    side: str
    quantity: Decimal
    filled: Decimal = ZERO
    status: OrderStatus = OrderStatus.SUBMITTED
    submitted: AwareDatetime
    last_fill: AwareDatetime | None = None
    limit: Decimal
    atr: Decimal
    reason: str
    reserved: Decimal = ZERO
    signal_symbol: str | None = None
    signal_direction: int = 1
    signal_entry: Decimal = ZERO
    signal_atr: Decimal = ZERO
    broker_id: str | None = None
    client_order_id: str | None = None
    broker_phase: str = "LOCAL"
    broker_request: dict | None = None
    broker_amount: Decimal = ZERO
    broker_fees: Decimal = ZERO
    cancel_requested: bool = False
    cancel_sent: AwareDatetime | None = None
    cancel_broker_id: str | None = None


class Position(Model):
    symbol: str
    quantity: Decimal
    entry: Decimal
    entry_fees: Decimal
    stop: Decimal
    target: Decimal
    opened: AwareDatetime
    initial_risk: Decimal
    entry_slippage: Decimal = ZERO
    signal_symbol: str | None = None
    signal_direction: int = 1
    signal_stop: Decimal = ZERO
    signal_target: Decimal = ZERO
    # Underlying entry and initial stop distance, kept so a tightened stop can
    # still be measured against the original risk. Zero on older ledger rows.
    signal_entry: Decimal = ZERO
    signal_risk: Decimal = ZERO


class State(Model):
    version: int = 1
    cash: Decimal
    positions: dict[str, Position] = Field(default_factory=dict)
    orders: dict[str, Order] = Field(default_factory=dict)
    entries: dict[str, int] = Field(default_factory=dict)
    cooldowns: dict[str, AwareDatetime] = Field(default_factory=dict)
    marks: dict[str, Decimal] = Field(default_factory=dict)
    mark_times: dict[str, AwareDatetime] = Field(default_factory=dict)
    signal_quotes: dict[str, Quote] = Field(default_factory=dict)
    day: str = ""
    session: Session | None = None
    day_equity: Decimal = ZERO
    loss_halted: bool = False
    reconciled: bool = False
    live_account_seq: int | None = None
    live_run_id: str | None = None
    live_verified_at: AwareDatetime | None = None
    live_buying_power: Decimal = ZERO
    last_event: AwareDatetime | None = None
    last_bars: dict[str, AwareDatetime] = Field(default_factory=dict)
    bars: dict[str, list[Bar]] = Field(default_factory=dict)
    # Completed prior regular session, aggregated to 5m bars, for indicator seeding.
    prior_day: str = ""
    prior_bars: dict[str, list[Bar]] = Field(default_factory=dict)
    # Original 1m bars for opt-in 3m signals; never reconstructed from 5m OHLCV.
    prior_minutes: dict[str, list[Bar]] = Field(default_factory=dict)
    # symbol -> 5m slot index -> volumes from recent full-length sessions, oldest first.
    slot_volumes: dict[str, dict[int, list[Decimal]]] = Field(default_factory=dict)
    # One bar per completed full-length regular session, oldest first, for daily rules.
    daily_bars: dict[str, list[Bar]] = Field(default_factory=dict)
    trades: list[dict] = Field(default_factory=list)
    fees: Decimal = ZERO
    realized: Decimal = ZERO
    alerts: list[str] = Field(default_factory=list)

    def equity(self, fee_bps: Decimal = ZERO) -> Decimal:
        return self.cash + sum(
            (
                p.quantity * self.marks.get(s, p.entry) * (1 - fee_bps / 10000)
                for s, p in self.positions.items()
            ),
            ZERO,
        )

    def pending(self, symbol: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.status in ACTIVE and (symbol is None or o.symbol == symbol)
        ]
