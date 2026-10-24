from dataclasses import dataclass
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
    volume_ratio: Decimal
    atr: Decimal
    breakout_high: Decimal
    volume: Decimal
    volume_average: Decimal


@dataclass(frozen=True)
class Decision:
    signal: str
    checks: dict[str, bool]
    indicators: Indicators | None


def ema(values: list[Decimal], period: int) -> Decimal:
    value = sum(values[:period]) / period
    alpha = Decimal(2) / (period + 1)
    for current in values[period:]:
        value += alpha * (current - value)
    return value


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


class Strategy(Protocol):
    def evaluate(self, minutes: list[Bar], session: Session) -> Decision: ...


class VwapBreakout:
    """Pure completed-candle strategy. No HTTP, credentials, LLM, or order access."""

    def __init__(self, config: TradingConfig):
        self.config = config

    def evaluate(self, minutes: list[Bar], session: Session) -> Decision:
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
        bars = aggregate(minutes)
        needed = max(c.ema_slow, c.volume_period + 1, c.atr_period + 1, c.breakout_bars + 1)
        if len(bars) < needed or sum(b.volume for b in minutes) <= 0:
            return Decision("WAIT", {"warmup": False}, None)
        closes = [b.close for b in bars]
        previous = bars[-c.volume_period - 1 : -1]
        average_volume = sum(b.volume for b in previous) / c.volume_period
        if average_volume <= 0:
            return Decision("WAIT", {"volume_available": False}, None)
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
            closes[-1],
            vwap,
            ema(closes, c.ema_fast),
            ema(closes, c.ema_slow),
            bars[-1].volume / average_volume,
            atr,
            max(b.high for b in bars[-c.breakout_bars - 1 : -1]),
            bars[-1].volume,
            average_volume,
        )
        checks = {
            "above_vwap": ind.close > ind.vwap,
            "ema_uptrend": ind.ema_fast > ind.ema_slow,
            "above_ema": ind.close > ind.ema_fast,
            "volume": ind.volume_ratio >= c.volume_ratio,
            "breakout": ind.close > ind.breakout_high,
            "after_open": (minutes[-1].end - session.open).total_seconds()
            >= c.entry_after_minutes * 60,
            "before_close": (session.close - minutes[-1].end).total_seconds()
            >= max(c.entry_before_close_minutes, c.no_buy_before_close_minutes) * 60,
            "atr_positive": ind.atr > 0,
        }
        signal = "BUY_SIGNAL" if all(checks.values()) else "WAIT"
        if ind.close < ind.vwap and ind.close < ind.ema_fast:
            signal = "TREND_EXIT"
        return Decision(signal, checks, ind)


def create_strategy(name: str, config: TradingConfig) -> Strategy:
    # Explicit registry; never discovers or installs arbitrary remote packages.
    registry = {"vwap_breakout": VwapBreakout}
    return registry[name](config)
