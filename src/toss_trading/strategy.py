from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from toss_trading.domain import Bar, Session
from toss_trading.trading_config import TradingConfig


@dataclass(frozen=True)
class Indicators:
    close: Decimal
    vwap: Decimal
    ema_fast: Decimal
    ema_slow: Decimal
    volume_ratio: Decimal | None
    atr: Decimal
    volume: Decimal
    volume_average: Decimal | None
    rsi: Decimal | None = None


@dataclass(frozen=True)
class Context:
    """Data from before today's session, supplied by the engine's ledger."""

    prior: tuple[Bar, ...] = ()  # Prior regular session as completed 5m bars.
    slot_volumes: tuple[Decimal, ...] = ()  # Same 5m slot on past full sessions, oldest first.
    daily: tuple[Bar, ...] = ()  # One bar per completed full session, oldest first.
    prior_minutes: tuple[Bar, ...] = ()  # Original prior-session 1m bars for 3m rules.


@dataclass(frozen=True)
class Decision:
    signal: str
    checks: dict[str, bool]
    indicators: Indicators | None
    # Fallbacks taken while computing indicators, e.g. "WARMUP_FALLBACK:no_prior_data".
    notes: tuple[str, ...] = field(default=())


def ema(values: list[Decimal], period: int) -> Decimal:
    value = sum(values[:period]) / period
    alpha = Decimal(2) / (period + 1)
    for current in values[period:]:
        value += alpha * (current - value)
    return value


def rsi(values: list[Decimal], period: int) -> Decimal | None:
    """Wilder RSI; None until period + 1 closes exist."""
    if len(values) <= period:
        return None
    deltas = [b - a for a, b in zip(values, values[1:], strict=False)]
    gain = sum(max(d, Decimal(0)) for d in deltas[:period]) / period
    loss = sum(max(-d, Decimal(0)) for d in deltas[:period]) / period
    for d in deltas[period:]:
        gain = (gain * (period - 1) + max(d, Decimal(0))) / period
        loss = (loss * (period - 1) + max(-d, Decimal(0))) / period
    if loss == 0:
        return Decimal(100) if gain else Decimal(50)
    return 100 - 100 / (1 + gain / loss)


def aggregate(minutes: list[Bar]) -> list[Bar]:
    result = []
    for start in range(0, len(minutes) - 4, 5):
        group = minutes[start : start + 5]
        result.append(
            Bar(
                symbol=group[0].symbol,
                end=group[-1].end,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(b.volume for b in group),
            )
        )
    return result


@dataclass(frozen=True)
class SignalContext:
    direction: int = 1
    leverage: Decimal = Decimal(1)
    execution_price: Decimal | None = None
    trade_symbol: str | None = None


NO_SIGNAL = SignalContext()
NO_CONTEXT = Context()


def required_warmup_bars(c: TradingConfig) -> int:
    needed = max(c.ema_slow, c.atr_period + 1)
    if c.volume_mode == "rolling":
        needed = max(needed, c.volume_period + 1)
    if c.rsi_entry_max is not None:
        needed = max(needed, c.rsi_period + 1)
    return needed


class Strategy(Protocol):
    def evaluate(
        self, minutes: list[Bar], session: Session, context: Context = NO_CONTEXT
    ) -> Decision: ...

    def evaluate_source(
        self,
        minutes: list[Bar],
        session: Session,
        context: Context = NO_CONTEXT,
        signal: SignalContext = NO_SIGNAL,
    ) -> Decision: ...


class ResearchOnly:
    """Non-trading placeholder for common ledger and dashboard infrastructure."""

    def evaluate(self, minutes, session, context=NO_CONTEXT):
        return Decision("WAIT", {"research_only": False}, None, ("OFFLINE_RESEARCH_ONLY",))

    def evaluate_source(self, minutes, session, context=NO_CONTEXT, signal=NO_SIGNAL):
        return self.evaluate(minutes, session, context)


def create_strategy(name: str, config: TradingConfig) -> Strategy:
    if name != "research_only":
        raise ValueError(f"Removed strategy: {name}. Use research.compare_weekly_allocations.")
    return ResearchOnly()
