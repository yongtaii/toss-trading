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


NO_CONTEXT = Context()


class Strategy(Protocol):
    def evaluate(
        self, minutes: list[Bar], session: Session, context: Context = NO_CONTEXT
    ) -> Decision: ...


class VwapIndicators:
    """Shared indicator math. Pure: no HTTP, credentials, LLM, or order access."""

    def __init__(self, config: TradingConfig):
        self.config = config

    def evaluate(
        self, minutes: list[Bar], session: Session, context: Context = NO_CONTEXT
    ) -> Decision:
        base = self.indicators(minutes, session, context)
        if base.indicators is None:
            return base
        return self.decide(minutes, session, base)

    def decide(self, minutes: list[Bar], session: Session, base: Decision) -> Decision:
        raise NotImplementedError

    def indicators(self, minutes: list[Bar], session: Session, context: Context) -> Decision:
        c = self.config
        # Require a contiguous prefix from the regular open: missing VWAP data fails closed.
        valid = bool(minutes) and minutes[0].end.timestamp() == session.open.timestamp() + 60
        valid = valid and all(
            (b.end - a.end).total_seconds() == 60
            for a, b in zip(minutes, minutes[1:], strict=False)
        )
        valid = valid and all(session.open < b.end <= session.close for b in minutes)
        if not valid or len(minutes) % 5:
            return Decision("WAIT", {"complete_contiguous_session": False}, None)
        today = aggregate(minutes)
        needed = max(c.ema_slow, c.atr_period + 1)
        if c.volume_mode == "rolling":
            needed = max(needed, c.volume_period + 1)
        if c.rsi_entry_max is not None:
            needed = max(needed, c.rsi_period + 1)
        notes = []
        seed: list[Bar] = []
        if c.indicator_warmup == "prior_session":
            prior = list(context.prior)
            if not prior:
                notes.append("WARMUP_FALLBACK:no_prior_data")
            elif len(prior) < needed:
                notes.append("WARMUP_FALLBACK:insufficient_prior_data")
            elif abs(minutes[0].open / prior[-1].close - 1) > c.split_gap_fraction:
                # No corporate-action feed: an extreme overnight gap may be a split.
                notes.append("WARMUP_FALLBACK:suspected_split")
            else:
                seed = prior
        bars = seed + today
        if len(bars) < needed or sum(b.volume for b in minutes) <= 0:
            return Decision("WAIT", {"warmup": False}, None, tuple(notes))
        closes = [b.close for b in bars]
        average_volume = None
        if c.volume_mode == "time_of_day":
            history = context.slot_volumes[-c.volume_lookback_days :]
            if len(history) >= c.volume_lookback_days:
                average_volume = sum(history) / len(history)
            else:
                notes.append("VOLUME_HISTORY_INSUFFICIENT")
        if average_volume is None and (
            c.volume_mode == "rolling" or c.volume_fallback == "rolling"
        ):
            if len(bars) > c.volume_period:
                previous = bars[-c.volume_period - 1 : -1]
                average_volume = sum(b.volume for b in previous) / c.volume_period
            if c.volume_mode == "rolling" and not average_volume:
                return Decision("WAIT", {"volume_available": False}, None, tuple(notes))
        # Minute OHLCV typical-price approximation, not exact trade-tick VWAP.
        vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in minutes)
        vwap /= sum(b.volume for b in minutes)
        ranges = [
            max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
            for a, b in zip(bars, bars[1:], strict=False)
        ]
        atr = sum(ranges[: c.atr_period]) / c.atr_period
        for tr in ranges[c.atr_period :]:
            atr = (atr * (c.atr_period - 1) + tr) / c.atr_period
        ind = Indicators(
            close=closes[-1],
            vwap=vwap,
            ema_fast=ema(closes, c.ema_fast),
            ema_slow=ema(closes, c.ema_slow),
            volume_ratio=bars[-1].volume / average_volume if average_volume else None,
            atr=atr,
            volume=bars[-1].volume,
            volume_average=average_volume,
            rsi=rsi(closes, c.rsi_period),
        )
        checks = {
            "after_open": (minutes[-1].end - session.open).total_seconds()
            >= c.entry_after_minutes * 60,
            "before_close": (session.close - minutes[-1].end).total_seconds()
            >= max(c.entry_before_close_minutes, c.no_buy_before_close_minutes) * 60,
            "atr_positive": ind.atr > 0,
        }
        if c.min_volume_ratio is not None:
            checks["volume"] = ind.volume_ratio is not None and (
                ind.volume_ratio >= c.min_volume_ratio
            )
        if c.rsi_entry_max is not None:
            checks["rsi"] = ind.rsi is not None and ind.rsi <= c.rsi_entry_max
        return Decision("WAIT", checks, ind, tuple(notes))


class VwapMeanRevert(VwapIndicators):
    """Long when stretched below session VWAP; exit when VWAP is reclaimed.

    Backtested 2026-07-24..10-23: profitable Jul-Sep, lost in October validation.
    """

    def decide(self, minutes: list[Bar], session: Session, base: Decision) -> Decision:
        ind = base.indicators
        checks = {
            "stretched_below_vwap": ind.close < ind.vwap - self.config.stretch_atr * ind.atr,
            **base.checks,
        }
        if ind.close >= ind.vwap:
            return Decision("VWAP_EXIT", checks, ind, base.notes)
        signal = "BUY_SIGNAL" if all(checks.values()) else "WAIT"
        return Decision(signal, checks, ind, base.notes)


class VwapReversal(VwapMeanRevert):
    """Buy a confirmed rebound below VWAP only when recovery can pay for the risk.

    Uses completed bars only. Risk/fees are unchanged; VWAP is a prospective exit,
    not a guarantee that price will recover. The original strategy remains available.
    """

    def decide(self, minutes: list[Bar], session: Session, base: Decision) -> Decision:
        current = super().decide(minutes, session, base)
        ind, c = base.indicators, self.config
        candles = aggregate(minutes[-10:])
        reversal = len(candles) == 2 and (
            candles[-1].close > candles[-2].high and candles[-1].close > candles[-1].open
        )
        # Include both directions of slippage/charges and a conservative full spread.
        # TAF per-share is an upper bound when the per-order cap would apply.
        costs = (
            ind.close
            * (c.entry_cost_bps + c.exit_cost_bps + 2 * c.slippage_bps + c.max_spread_bps)
            / 10000
            + c.taf_per_share
        )
        stop_distance = c.stop_atr * ind.atr
        recovery = min(ind.vwap - ind.close, c.reward_r * stop_distance)
        checks = {
            **current.checks,
            "confirmed_reversal": reversal,
            "recovery_covers_costs": recovery >= costs * c.min_reversion_cost_multiple,
            "net_reward_covers_stop": recovery - costs >= stop_distance,
        }
        if current.signal == "VWAP_EXIT":
            return Decision("VWAP_EXIT", checks, ind, current.notes)
        return Decision(
            "BUY_SIGNAL" if all(checks.values()) else "WAIT", checks, ind, current.notes
        )


class VwapCrossBaseline(VwapIndicators):
    """Baseline: buy the first close back above VWAP; exit when close drops below it."""

    def decide(self, minutes: list[Bar], session: Session, base: Decision) -> Decision:
        ind = base.indicators
        earlier = minutes[:-5]
        crossed = False
        if earlier and sum(b.volume for b in earlier) > 0:
            prior_vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in earlier)
            prior_vwap /= sum(b.volume for b in earlier)
            crossed = earlier[-1].close <= prior_vwap
        checks = {"vwap_cross_up": ind.close > ind.vwap and crossed, **base.checks}
        if ind.close < ind.vwap:
            return Decision("VWAP_EXIT", checks, ind, base.notes)
        signal = "BUY_SIGNAL" if all(checks.values()) else "WAIT"
        return Decision(signal, checks, ind, base.notes)


class RandomEntryBaseline(VwapMeanRevert):
    """Baseline: enter at pre-drawn (day, symbol, minute) slots; keep the current exits."""

    def __init__(self, config: TradingConfig, schedule: set[tuple[str, str, int]]):
        super().__init__(config)
        self.schedule = schedule

    def decide(self, minutes: list[Bar], session: Session, base: Decision) -> Decision:
        current = super().decide(minutes, session, base)
        offset = int((minutes[-1].end - session.open).total_seconds() // 60)
        drawn = (session.day, minutes[-1].symbol, offset) in self.schedule
        checks = {"drawn": drawn, **base.checks}
        if drawn and all(checks.values()):
            return Decision("BUY_SIGNAL", checks, current.indicators, current.notes)
        signal = "VWAP_EXIT" if current.signal == "VWAP_EXIT" else "WAIT"
        return Decision(signal, checks, current.indicators, current.notes)


def create_strategy(name: str, config: TradingConfig) -> Strategy:
    # Explicit registry; never discovers or installs arbitrary remote packages.
    registry = {
        "vwap_mean_revert": VwapMeanRevert,
        "vwap_reversal": VwapReversal,
        "vwap_only": VwapCrossBaseline,
    }
    return registry[name](config)
