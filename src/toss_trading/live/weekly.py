"""Weekly SMA20/200 execution using completed daily bars and durable LIVE intents."""

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache

from toss_trading.domain import Order, Session
from toss_trading.live.control import read_control
from toss_trading.live.execution import LiveIntents
from toss_trading.live.ledger import money
from toss_trading.marketdata import (
    NY,
    broker_session,
    fetch_interval,
    parse_quote,
    previous_session,
)
from toss_trading.risk import quote_checks

SOURCES = {"SOXL": "SOXX", "TQQQ": "QQQ"}
STOP = Decimal("0.06")
TARGET = Decimal("0.18")
MAX_SESSIONS = 20
OPEN_WINDOW = timedelta(minutes=5)


def entry_window(session: Session, now: datetime) -> bool:
    return session.contains(now) and now < session.open + OPEN_WINDOW


@lru_cache(maxsize=16)
def first_week_day(day: date) -> date:
    first = day
    while previous := previous_session(first):
        earlier = date.fromisoformat(previous.day)
        if earlier.isocalendar()[:2] != day.isocalendar()[:2]:
            return first
        first = earlier
    raise ValueError("Missing previous session")


def bullish(closes):
    if len(closes) < 200:
        raise ValueError("Weekly strategy requires 200 completed daily closes")
    slow = sum(closes[-200:]) / 200
    return closes[-1] > slow and sum(closes[-20:]) / 20 > slow


@lru_cache(maxsize=16)
def completed_days(before: date, count: int) -> tuple[date, ...]:
    days = []
    for _ in range(count):
        session = previous_session(before)
        if session is None:
            raise ValueError("Missing preceding regular session")
        before = date.fromisoformat(session.day)
        days.append(before)
    return tuple(reversed(days))


async def daily_closes(broker, symbol, days):
    bars = await fetch_interval(broker, symbol, days[0], days[-1], "1d", adjusted=False)
    closes = {}
    for bar in bars:
        day = bar.end.astimezone(NY).date()
        if day in closes:
            raise ValueError("Duplicate daily candle date")
        closes[day] = bar.close
    if set(days) != closes.keys():
        raise ValueError("Incomplete daily history")
    return [closes[day] for day in days]


def position_exit(position, completed, closes, *, basis_day=None):
    opened = position.opened.astimezone(NY).date()
    if len(completed) >= MAX_SESSIONS and opened <= completed[-MAX_SESSIONS]:
        return "TIME"
    for day, close in zip(completed, closes, strict=True):
        if day >= (basis_day or opened):
            if close <= position.entry * (1 - STOP):
                return "CLOSE_STOP_NEXT_OPEN"
            if close >= position.entry * (1 + TARGET):
                return "CLOSE_TARGET_NEXT_OPEN"
    return None


def opening_order(state, quote, config, now, day, *, desired=None, remaining_risk=None):
    price = quote.ask.quantize(
        Decimal(".01") if quote.ask >= 1 else Decimal(".0001"), rounding=ROUND_DOWN
    )
    if price != quote.ask or not all(quote_checks(quote, now, config).values()):
        return None
    unit = price * (1 + config.entry_cost_bps / 10000)
    risk = (
        price
        * (STOP + (config.entry_cost_bps + config.exit_cost_bps + 2 * config.slippage_bps) / 10000)
        + config.taf_per_share
    )
    equity = state.equity()
    reserved = sum(o.reserved for o in state.pending() if o.side == "BUY")
    requested = (
        equity * config.risk_fraction / risk
        if desired is None
        else min(desired, remaining_risk / risk)
    )
    quantity = min(
        requested,
        config.max_order_usd / unit,
        (min(state.cash, state.live_buying_power) - reserved) / unit,
        quote.ask_size,
    ).to_integral_value(rounding=ROUND_DOWN)
    if quantity < 1:
        return None
    return Order(
        id=f"weekly:{day}:{quote.symbol}:BUY",
        symbol=quote.symbol,
        side="BUY",
        quantity=quantity,
        submitted=now,
        limit=price,
        atr=price * STOP,
        reason="WEEKLY_ENTRY",
        reserved=quantity * unit,
    )


class WeeklyLive:
    def __init__(self, bridge):
        self.bridge = bridge
        self.config = bridge.config
        self.intents = LiveIntents(self.config)
        self.calendar_day = None
        self.session = None
        self.daily_cache = {}
        self.fees_covered = None

    async def closes(self, symbol, days):
        key = symbol, days[0], days[-1]
        if key not in self.daily_cache:
            self.daily_cache[key] = await daily_closes(self.bridge.broker, symbol, days)
        return self.daily_cache[key]

    async def poll(self):
        bridge = self.bridge
        now = datetime.now(UTC)
        day = now.astimezone(NY).date()
        if day != self.calendar_day:
            self.session = broker_session(await bridge.broker.market_calendar(day), day)
            self.calendar_day = day
            self.daily_cache.clear()
            self.fees_covered = None
        session = self.session
        if session is None or not session.contains(now):
            return False

        first = first_week_day(day)
        first_of_week = first == day
        action = read_control(bridge.live.control_path)
        _, snapshot = bridge.load()
        eligible = {}
        due = {}
        if action != "flatten":
            if snapshot.positions:
                completed = completed_days(day, MAX_SESSIONS)
                for symbol, position in snapshot.positions.items():
                    closes = []
                    if position.opened.astimezone(NY).date() > completed[0]:
                        closes = await self.closes(symbol, completed)
                    basis_day = None
                    if bridge.live.weekly_target_allocation is not None:
                        basis_day = max(
                            (
                                order.last_fill.astimezone(NY).date()
                                for order in snapshot.orders.values()
                                if order.side == "BUY"
                                and order.symbol == symbol
                                and order.last_fill
                                and order.last_fill >= position.opened
                            ),
                            default=None,
                        )
                    reason = position_exit(position, completed, closes, basis_day=basis_day)
                    if reason:
                        due[symbol] = reason
            can_enter = first_of_week and action == "run" and entry_window(session, now)
            if snapshot.positions.keys() - due.keys() or can_enter:
                days = completed_days(first, 200)
                for symbol, source in SOURCES.items():
                    if symbol not in due and (can_enter or symbol in snapshot.positions):
                        eligible[symbol] = bullish(await self.closes(source, days))
                for symbol in snapshot.positions:
                    if symbol in eligible and not eligible[symbol]:
                        due[symbol] = "SIGNAL"
            if can_enter and any(eligible.values()) and self.fees_covered is None:
                commissions = await bridge.broker.commissions()
                rates = [
                    money(item["commissionRate"])
                    for item in commissions
                    if item["marketCountry"] == "US"
                ]
                self.fees_covered = bool(rates) and max(rates) * 10000 <= self.config.fee_bps

        if not await bridge.reconcile():
            return True
        rev, state = bridge.load()
        quotes = {
            symbol: parse_quote(symbol, await bridge.broker.orderbook(symbol))
            for symbol in self.config.symbols
        }
        now = datetime.now(UTC)
        if not session.contains(now):
            return False
        action = read_control(bridge.live.control_path)
        fresh = {
            s: q
            for s, q in quotes.items()
            if session.contains(q.at)
            and 0 <= (now - q.at).total_seconds() <= self.config.max_quote_age_seconds
        }
        if state.day != session.day:
            state.day, state.loss_halted = session.day, False
            state.day_equity = state.equity(self.config.exit_cost_bps)
        state.session, state.last_event = session, now
        for symbol, quote in fresh.items():
            state.marks[symbol], state.mark_times[symbol] = quote.bid, quote.at
        if (
            state.equity(self.config.exit_cost_bps) - state.day_equity
            <= -bridge.live.daily_loss_usd
        ):
            state.loss_halted = True

        entries_open = (
            action == "run"
            and first_of_week
            and entry_window(session, now)
            and self.fees_covered is True
        )
        for order in state.pending():
            if (order.side == "BUY" or order.reason == "WEEKLY_TRIM") and (
                not entries_open or state.loss_halted
            ):
                self.intents.cancel(state, order)
        if action == "flatten":
            due = dict.fromkeys(state.positions, "MANUAL_FLATTEN")

        records = []
        for symbol, reason in due.items():
            position = state.positions.get(symbol)
            key = f"weekly:{day}:{symbol}:SELL"
            if (
                position is None
                or symbol not in fresh
                or state.pending(symbol)
                or key in state.orders
            ):
                continue
            order = Order(
                id=key,
                symbol=symbol,
                side="SELL",
                quantity=position.quantity,
                submitted=now,
                limit=fresh[symbol].bid,
                atr=position.initial_risk,
                reason=reason,
            )
            self.intents.submit(state, order)
            records.append({"event": "WEEKLY_INTENT", "order_id": key, "reason": reason})

        if (
            entries_open
            and not state.loss_halted
            and len(fresh) == len(quotes)
            and not (due.keys() & state.positions.keys())
            and not any(o.side == "SELL" for o in state.pending())
        ):
            allocation = bridge.live.weekly_target_allocation
            targets = {}
            if allocation is not None and not state.pending():
                selected = [symbol for symbol, permitted in eligible.items() if permitted]
                for symbol in selected:
                    unit = fresh[symbol].ask * (1 + self.config.entry_cost_bps / 10000)
                    targets[symbol] = (
                        state.equity() * allocation / len(selected) / unit
                    ).to_integral_value(rounding=ROUND_DOWN)
                for symbol, target in targets.items():
                    position = state.positions.get(symbol)
                    key = f"weekly:{day}:{symbol}:TRIM"
                    if position is None or position.quantity <= target or key in state.orders:
                        continue
                    excess = position.quantity - target
                    self.intents.submit(
                        state,
                        Order(
                            id=key,
                            symbol=symbol,
                            side="SELL",
                            quantity=excess,
                            submitted=now,
                            limit=fresh[symbol].bid,
                            atr=position.initial_risk,
                            reason="WEEKLY_TRIM",
                        ),
                    )
                    records.append(
                        {"event": "WEEKLY_INTENT", "order_id": key, "reason": "WEEKLY_TRIM"}
                    )
            for symbol, permitted in eligible.items():
                key = f"weekly:{day}:{symbol}:BUY"
                if not permitted or key in state.orders:
                    continue
                if allocation is None:
                    if symbol in state.positions or state.pending(symbol):
                        continue
                    order = opening_order(state, fresh[symbol], self.config, now, day)
                else:
                    if state.pending() or symbol not in targets:
                        continue
                    position = state.positions.get(symbol)
                    desired = targets[symbol] - (position.quantity if position else 0)
                    pnl = state.equity(self.config.exit_cost_bps) - state.day_equity
                    order = opening_order(
                        state,
                        fresh[symbol],
                        self.config,
                        now,
                        day,
                        desired=desired,
                        remaining_risk=max(Decimal(0), bridge.live.daily_loss_usd + pnl),
                    )
                if order is not None:
                    self.intents.submit(state, order)
                    records.append(
                        {"event": "WEEKLY_INTENT", "order_id": key, "reason": order.reason}
                    )
                    break  # Reconcile and refresh quotes before submitting the other symbol.
        bridge.store.commit(rev, state, records)
        await bridge.flush()
        return True
