from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from toss_trading.domain import ZERO, Bar, Order, OrderStatus, Position, Quote, Session, State
from toss_trading.execution import Execution, LiveTossExecution, PaperExecution
from toss_trading.marketdata import previous_session
from toss_trading.persistence import Store
from toss_trading.risk import quote_checks, rejection_reasons, size_entry
from toss_trading.strategy import (
    Context,
    SignalContext,
    Strategy,
    aggregate,
    create_strategy,
    required_warmup_bars,
)
from toss_trading.trading_config import TradingConfig

FULL_SESSION_SECONDS = 390 * 60
# Completed daily bars retained per signal symbol; bounds ledger growth.
DAILY_HISTORY = 120


def contiguous_prefix(minutes: list[Bar], session: Session) -> list[Bar]:
    """Minutes contiguous from the regular open, truncated to whole 5m bars."""
    result = []
    for bar in sorted(minutes, key=lambda b: b.end):
        expected = session.open.timestamp() + 60 * (len(result) + 1)
        if bar.end.timestamp() != expected or bar.end > session.close:
            break
        result.append(bar)
    return result[: len(result) - len(result) % 5]


def archive_session(state: State, old: Session | None, next_day: str, c: TradingConfig) -> None:
    """Keep the finished session's 5m bars for seeding and its slot volumes for comparison."""
    expected = previous_session(date.fromisoformat(next_day))
    if old is None or old.day != state.day:
        if expected is None or state.prior_day != expected.day:
            state.prior_day, state.prior_bars = "", {}
            state.prior_minutes = {}
        return
    full = (old.close - old.open).total_seconds() == FULL_SESSION_SECONDS
    prior, prior_minutes = {}, {}
    for symbol, minutes in state.bars.items():
        prefix = contiguous_prefix(minutes, old)
        bars = aggregate(prefix)
        if c.needs_minute_seed and prefix:
            prior_minutes[symbol] = prefix
        if bars:
            prior[symbol] = bars
        # Early-close and incomplete sessions are excluded from time-of-day volume
        # slots and from the daily history, so no partial day becomes a daily bar.
        if full and len(bars) * 300 == FULL_SESSION_SECONDS:
            slots = state.slot_volumes.setdefault(symbol, {})
            for index, bar in enumerate(bars):
                history = slots.setdefault(index, [])
                history.append(bar.volume)
                del history[: -c.volume_lookback_days]
            daily = state.daily_bars.setdefault(symbol, [])
            if not daily or daily[-1].end != old.close:
                daily.append(
                    Bar(
                        symbol=symbol,
                        end=old.close,
                        open=bars[0].open,
                        high=max(b.high for b in bars),
                        low=min(b.low for b in bars),
                        close=bars[-1].close,
                        volume=sum(b.volume for b in bars),
                    )
                )
                del daily[:-DAILY_HISTORY]
    if expected is not None and old.day == expected.day:
        state.prior_day, state.prior_bars = old.day, prior
        state.prior_minutes = prior_minutes
    elif expected is None or state.prior_day != expected.day:
        state.prior_day, state.prior_bars = "", {}
        state.prior_minutes = {}


class TradingEngine:
    def __init__(
        self,
        config: TradingConfig,
        store: Store,
        strategy: Strategy | None = None,
        execution: Execution | None = None,
    ):
        self.config, self.store = config, store
        self.prior_retry_after: datetime | None = None
        self.strategy = strategy or create_strategy(config.strategy_name, config)
        self.execution = execution or (
            PaperExecution(config) if config.mode == "PAPER" else LiveTossExecution()
        )
        revision, state = store.load()
        self.execution.reconcile(state)
        store.commit(
            revision,
            state,
            [
                {
                    "event": "RECONCILED",
                    "mode": config.mode,
                    "unresolved": not state.reconciled,
                    "notify": not state.reconciled,
                }
            ],
        )

    def step(
        self,
        now: datetime,
        session: Session | None,
        bars: list[Bar],
        quotes: list[Quote],
        *,
        allow_entries: bool = True,
        flatten: bool = False,
    ) -> list[dict]:
        if now.tzinfo is None:
            raise ValueError("Timezone-aware event time required")
        c = self.config
        rev, state = self.store.load()
        self.execution.reconcile(state)
        if state.last_event and now < state.last_event:
            raise ValueError("Out-of-order clock")
        state.last_event = now
        records = []
        if session is None:
            self.store.commit(
                rev,
                state,
                [
                    {
                        "event": "CLOSED_OR_MISSING_CALENDAR",
                        "time": now,
                        "positions_remaining": list(state.positions),
                        "notify": bool(state.positions),
                    }
                ],
            )
            return records
        finished, state.session = state.session, session
        if state.day != session.day:
            if state.day and "summary" not in state.alerts:
                records.append(
                    {
                        "event": "DAILY_SUMMARY",
                        "day": state.day,
                        "pnl": state.equity(c.exit_cost_bps) - state.day_equity,
                        "positions_remaining": list(state.positions),
                        "notify": True,
                    }
                )
            archive_session(state, finished, session.day, c)
            state.day, state.bars, state.last_bars = session.day, {}, {}
            state.entries, state.alerts = {}, []
            if state.positions:
                if not c.hold_overnight:
                    state.alerts.append("overnight_positions")
                records.append(
                    {
                        "event": "OVERNIGHT_POSITION",
                        "symbols": list(state.positions),
                        "time": now,
                        "notify": True,
                    }
                )
            state.day_equity = state.equity(c.exit_cost_bps)
            state.loss_halted = False
        regular = session.contains(now)
        all_quotes = {q.symbol: q for q in quotes if q.symbol in c.market_symbols}
        quotes_by_symbol = {s: q for s, q in all_quotes.items() if s in c.symbols}
        for symbol in c.signal_symbols:
            q = all_quotes.get(symbol)
            previous = state.signal_quotes.get(symbol)
            if (
                q
                and quote_checks(q, now, c)["fresh_quote"]
                and (previous is None or q.at > previous.at)
            ):
                state.signal_quotes[symbol] = q
        for symbol, q in quotes_by_symbol.items():
            fresh = quote_checks(q, now, c)["fresh_quote"]
            if fresh and (symbol not in state.mark_times or q.at > state.mark_times[symbol]):
                state.marks[symbol], state.mark_times[symbol] = q.bid, q.at
        pnl = state.equity(c.exit_cost_bps) - state.day_equity
        if pnl <= -state.day_equity * c.daily_loss_fraction and not state.loss_halted:
            state.loss_halted = True
            records.append({"event": "DAILY_LOSS_LIMIT", "time": now, "pnl": pnl, "notify": True})
        remaining = (session.close - now).total_seconds()
        # Tighten before any exit test so cancellation, stops and flatten agree.
        for position in state.positions.values():
            self.tighten(position, all_quotes, now)
        # Cancel remaining entry fills before stop/flatten; paper cancellation is definitive.
        for order in state.pending():
            source = c.signal_source(order.symbol)[0]
            source_quote = all_quotes.get(source)
            stale_source = source != order.symbol and (
                source_quote is None or not quote_checks(source_quote, now, c)["fresh_quote"]
            )
            position = state.positions.get(order.symbol)
            stop = position and self.price_exit(position, all_quotes, now) == "STOP"
            source_crossed = False
            if order.signal_symbol and not stale_source and source_quote:
                price = (source_quote.bid + source_quote.ask) / 2
                move = order.signal_direction * (price - order.signal_entry)
                distance = order.signal_atr * c.stop_atr
                source_crossed = move <= -distance or move >= c.reward_r * distance
            last_bar = state.last_bars.get(c.signal_source(order.symbol)[0])
            stale_bars = (
                last_bar is None or (now - last_bar).total_seconds() > c.max_bar_age_seconds
            )
            if (
                order.side == "BUY"
                and order.status != OrderStatus.UNKNOWN
                and (
                    not regular
                    or state.loss_halted
                    or not allow_entries
                    or flatten
                    or stale_bars
                    or stale_source
                    or source_crossed
                    or stop
                    or remaining <= c.no_buy_before_close_minutes * 60
                )
            ):
                self.execution.cancel(state, order)
                records.append(
                    {
                        "event": "CANCEL_REQUESTED" if c.mode == "LIVE" else "CANCELLED",
                        "order_id": order.id,
                        "time": now,
                    }
                )
        if regular:
            for q in quotes_by_symbol.values():
                records.extend(self.execution.process(state, q, now))
        # Realized fill costs may cross the daily threshold even without a new price mark.
        pnl = state.equity(c.exit_cost_bps) - state.day_equity
        if pnl <= -state.day_equity * c.daily_loss_fraction and not state.loss_halted:
            state.loss_halted = True
            records.append({"event": "DAILY_LOSS_LIMIT", "time": now, "pnl": pnl, "notify": True})
        decisions = {}
        for bar in sorted(bars, key=lambda b: (b.end, b.symbol)):
            if bar.symbol not in c.market_symbols:
                records.append(
                    {
                        "event": "REJECTED_DATA",
                        "symbol": bar.symbol,
                        "reason": "not_allowlisted",
                        "time": now,
                    }
                )
                continue
            if bar.end > now or not session.open < bar.end <= session.close:
                records.append(
                    {
                        "event": "REJECTED_DATA",
                        "symbol": bar.symbol,
                        "reason": "incomplete_or_outside_session",
                        "time": now,
                    }
                )
                continue
            previous = state.last_bars.get(bar.symbol)
            if previous and bar.end <= previous:
                continue
            state.last_bars[bar.symbol] = bar.end
            if bar.symbol not in c.signal_symbols:
                continue  # Execution candles never enter the indicator history.
            state.bars.setdefault(bar.symbol, []).append(bar)
            decision_seconds = 180 if c.needs_minute_seed else 300
            if (bar.end - session.open).total_seconds() % decision_seconds:
                continue
            for symbol in c.symbols:
                source, direction, leverage = c.signal_source(symbol)
                if source != bar.symbol:
                    continue
                history = state.bars[source]
                context = self.context(state, session, bar)
                if source == symbol:
                    decision = self.strategy.evaluate(history, session, context)
                else:
                    q = quotes_by_symbol.get(symbol)
                    decision = self.strategy.evaluate_source(
                        history,
                        session,
                        context,
                        SignalContext(direction, leverage, q.ask if q else None, symbol),
                    )
                decisions[symbol] = (decision, bar.end)
                for note in decision.notes:
                    event, _, reason = note.partition(":")
                    key = f"{event}:{symbol}"
                    if key not in state.alerts:
                        state.alerts.append(key)
                        records.append(
                            {"event": event, "symbol": symbol, "reason": reason, "time": now}
                        )
                records.append(
                    {
                        "event": "DECISION",
                        "time": bar.end,
                        "symbol": symbol,
                        "signal_symbol": source,
                        "signal_direction": direction,
                        "signal": decision.signal,
                        "checks": decision.checks,
                        "indicators": asdict(decision.indicators) if decision.indicators else None,
                        "daily_pnl": pnl,
                    }
                )
        # STOP has precedence over target, VWAP exit, then scheduled flatten.
        for symbol, position in list(state.positions.items()):
            q = quotes_by_symbol.get(symbol)
            decision = decisions.get(symbol)
            reason = self.price_exit(position, all_quotes, now)
            if flatten:
                reason = "MANUAL_FLATTEN"
            if (
                reason is None
                and decision
                and decision[0].signal in {"VWAP_EXIT", "STRUCTURE_EXIT"}
            ):
                reason = decision[0].signal
            if reason is None and regular:
                reason = self.unmonitored(state, position, now)
            if (
                reason is None
                and c.max_holding_minutes is not None
                and (now - position.opened).total_seconds() >= c.max_holding_minutes * 60
            ):
                reason = "TIME_EXIT"
            if (
                reason is None
                and not c.hold_overnight
                and (
                    remaining <= c.flatten_before_close_minutes * 60
                    or "overnight_positions" in state.alerts
                )
            ):
                reason = "SESSION_FLATTEN"
            if reason:
                if regular and q and quote_checks(q, now, c)["fresh_quote"]:
                    # Cancel unfilled buy remainder before generating a liquidation intent.
                    for order in state.pending(symbol):
                        if order.side == "BUY" and order.status != OrderStatus.UNKNOWN:
                            self.execution.cancel(state, order)
                    if not state.pending(symbol):
                        key = sha256(
                            f"{state.day}:{symbol}:SELL:{now.isoformat()}".encode()
                        ).hexdigest()
                        order = Order(
                            id=key,
                            symbol=symbol,
                            side="SELL",
                            quantity=position.quantity,
                            submitted=now,
                            limit=q.bid,
                            atr=ZERO,
                            reason=reason,
                        )
                        self.execution.submit(state, order)
                        records.append(
                            {
                                "event": "ORDER_SUBMITTED",
                                "time": now,
                                "symbol": symbol,
                                "order_id": key,
                                "side": "SELL",
                                "reason": reason,
                            }
                        )
                else:
                    records.append(
                        {
                            "event": "EXIT_BLOCKED",
                            "symbol": symbol,
                            "time": now,
                            "reason": "closed_session_or_stale_quote",
                            "notify": True,
                        }
                    )
        for symbol in c.symbols:
            if symbol not in decisions:
                continue
            decision, at = decisions[symbol]
            if decision.signal != "BUY_SIGNAL":
                continue
            q = quotes_by_symbol.get(symbol)
            reason_checks = {
                "entries_enabled": allow_entries,
                "fresh_bar": 0 <= (now - at).total_seconds() <= c.max_bar_age_seconds,
                "quote_available": q is not None,
            }
            quantity, sizing_atr = ZERO, ZERO
            source, direction, leverage = c.signal_source(symbol)
            source_quote = all_quotes.get(source)
            if source != symbol:
                reason_checks["fresh_signal_quote"] = bool(
                    source_quote and quote_checks(source_quote, now, c)["fresh_quote"]
                )
            if q and decision.indicators:
                ind = decision.indicators
                # Convert underlying ATR percentage to approximate 3x ETF dollar risk.
                sizing_atr = (
                    ind.atr
                    if source == symbol
                    else (q.ask * (1 + c.slippage_bps / 10000) * leverage * ind.atr / ind.close)
                )
                quantity, risk_checks = size_entry(state, symbol, sizing_atr, q, now, session, c)
                reason_checks.update(risk_checks)
            key = sha256(f"{state.day}:{symbol}:BUY:{at.isoformat()}".encode()).hexdigest()
            reason_checks["new_signal"] = key not in state.orders
            if quantity > 0 and all(reason_checks.values()):
                limit = q.ask * (1 + c.slippage_bps / 10000)
                self.execution.submit(
                    state,
                    Order(
                        id=key,
                        symbol=symbol,
                        side="BUY",
                        quantity=quantity,
                        submitted=now,
                        limit=limit,
                        atr=sizing_atr,
                        signal_symbol=source if source != symbol else None,
                        signal_direction=direction,
                        signal_entry=decision.indicators.close,
                        signal_atr=decision.indicators.atr,
                        reason="BUY_SIGNAL",
                        reserved=quantity * limit * (1 + c.entry_cost_bps / 10000),
                    ),
                )
                event = "ORDER_SUBMITTED"
            else:
                event = "ENTRY_REJECTED"
            records.append(
                {
                    "event": event,
                    "time": now,
                    "symbol": symbol,
                    "side": "BUY",
                    "order_id": key,
                    "quantity": quantity,
                    "checks": reason_checks,
                    "reasons": rejection_reasons(reason_checks),
                }
            )
        if (
            remaining <= c.flatten_deadline_minutes * 60
            and state.positions
            and not c.hold_overnight
        ):
            if "flatten_failed" not in state.alerts:
                state.alerts.append("flatten_failed")
                records.append(
                    {
                        "event": "FLATTEN_UNRESOLVED",
                        "time": now,
                        "symbols": list(state.positions),
                        "notify": True,
                    }
                )
        if now >= session.close and "summary" not in state.alerts:
            state.alerts.append("summary")
            records.append(
                {
                    "event": "DAILY_SUMMARY",
                    "day": state.day,
                    "pnl": pnl,
                    "positions_remaining": list(state.positions),
                    "notify": True,
                }
            )
        self.store.commit(rev, state, records)
        return records

    def unmonitored(self, state: State, position: Position, now: datetime) -> str | None:
        """Risk that cannot be observed is closed, not held until the flatten.

        The ATR stop and target live on the signal source. While its quotes are
        missing, price_exit cannot fire at all, so a sustained gap leaves the
        position without the stop it was sized for.
        """
        c = self.config
        if c.max_unmonitored_seconds is None:
            return None
        source = c.signal_source(position.symbol)[0]
        if source == position.symbol or position.signal_symbol != source:
            return None
        last = state.signal_quotes.get(source)
        age = None if last is None else (now - last.at).total_seconds()
        if age is None or age > c.max_unmonitored_seconds:
            return "RISK_UNMONITORED"
        return None

    def tighten(self, position: Position, quotes: dict[str, Quote], now: datetime) -> bool:
        """Move the stop toward the trade only. Never loosens and never passes the target."""
        c = self.config
        if c.trail_atr is None and c.breakeven_r is None:
            return False
        source, _, leverage = c.signal_source(position.symbol)
        underlying = source != position.symbol
        if underlying:
            # A position taken before these anchors existed keeps its fixed stop.
            if position.signal_symbol != source or not position.signal_risk:
                return False
            q = quotes.get(source)
            if q is None or not quote_checks(q, now, c)["fresh_quote"]:
                return False
            price = (q.bid + q.ask) / 2
            direction, entry, risk = (
                position.signal_direction,
                position.signal_entry,
                position.signal_risk,
            )
            stop, target = position.signal_stop, position.signal_target
        else:
            q = quotes.get(position.symbol)
            if q is None or not quote_checks(q, now, c)["fresh_quote"]:
                return False
            price, direction, leverage = q.bid, 1, Decimal(1)
            entry, risk = position.entry, position.initial_risk
            stop, target = position.stop, position.target
        if risk <= 0:
            return False
        moved = stop
        gain = direction * (price - entry)
        if c.trail_atr is not None and (
            c.trail_activation_r == 0 or gain >= c.trail_activation_r * risk
        ):
            # trail_atr is expressed in the same ATR units as stop_atr.
            moved = (
                max(moved, price - direction * c.trail_atr / c.stop_atr * risk)
                if direction == 1
                else min(moved, price - direction * c.trail_atr / c.stop_atr * risk)
            )
        if c.breakeven_r is not None and direction * (price - entry) >= c.breakeven_r * risk:
            # Cover the round trip so a breakeven exit is not a small loss.
            charges = entry * (c.entry_cost_bps + c.exit_cost_bps + 2 * c.slippage_bps) / 10000
            level = entry + direction * (charges / leverage + c.breakeven_lock_r * risk)
            # A configured lock must remain behind the current executable signal.
            if direction * (price - level) <= 0:
                level = stop
            moved = max(moved, level) if direction == 1 else min(moved, level)
        # The target exit owns anything at or beyond the target.
        if direction * (moved - stop) <= 0 or direction * (moved - target) >= 0:
            return False
        if underlying:
            position.signal_stop = moved
        else:
            position.stop = moved
        return True

    def price_exit(self, position: Position, quotes: dict[str, Quote], now: datetime) -> str | None:
        source = self.config.signal_source(position.symbol)[0]
        if source != position.symbol:
            # A position predating this strategy has no underlying risk anchors.
            if position.signal_symbol != source:
                return "SIGNAL_SOURCE_CHANGED"
            q = quotes.get(source)
            if q and quote_checks(q, now, self.config)["fresh_quote"]:
                price = (q.bid + q.ask) / 2
                direction = position.signal_direction
                if direction * (price - position.signal_stop) <= 0:
                    return "STOP"
                if direction * (price - position.signal_target) >= 0:
                    return "TARGET"
            return None
        q = quotes.get(position.symbol)
        if q and quote_checks(q, now, self.config)["fresh_quote"]:
            if q.bid <= position.stop:
                return "STOP"
            if q.bid >= position.target:
                return "TARGET"
        return None

    def context(self, state: State, session: Session, bar: Bar) -> Context:
        expected = previous_session(date.fromisoformat(session.day))
        fresh = expected is not None and state.prior_day == expected.day
        slot = int((bar.end - session.open).total_seconds() // 300) - 1
        return Context(
            prior=tuple(state.prior_bars.get(bar.symbol, [])) if fresh else (),
            slot_volumes=tuple(state.slot_volumes.get(bar.symbol, {}).get(slot, [])),
            daily=tuple(state.daily_bars.get(bar.symbol, [])),
            prior_minutes=tuple(state.prior_minutes.get(bar.symbol, [])) if fresh else (),
        )

    def needs_prior(self, session: Session, now: datetime | None = None) -> Session | None:
        """Prior session to fetch when the ledger cannot seed indicators by itself."""
        if self.config.indicator_warmup != "prior_session":
            return None
        expected = previous_session(date.fromisoformat(session.day))
        state = self.store.load()[1]
        if expected is None:
            return None
        if not self.config.underlying_signals:
            if state.prior_day == expected.day or state.day == expected.day:
                return None
        else:
            needed = required_warmup_bars(self.config)
            history = state.prior_bars if state.prior_day == expected.day else {}
            if self.config.needs_minute_seed:
                needed = int((expected.close - expected.open).total_seconds() // 60)
                history = state.prior_minutes if state.prior_day == expected.day else {}
            if state.day == expected.day and state.session:
                history = {
                    symbol: (
                        contiguous_prefix(minutes, state.session)
                        if self.config.needs_minute_seed
                        else aggregate(contiguous_prefix(minutes, state.session))
                    )
                    for symbol, minutes in state.bars.items()
                }
            if all(len(history.get(s, [])) >= needed for s in self.config.signal_symbols):
                return None
            if self.prior_retry_after and (now or datetime.now(UTC)) < self.prior_retry_after:
                return None
        return expected

    def seed_prior(self, prior: Session, minutes: list[Bar], now: datetime | None = None) -> None:
        """Store a fetched prior session; the strategy falls back if it is incomplete."""
        rev, state = self.store.load()
        bars, raw = {}, {}
        for symbol in self.config.signal_symbols:
            prefix = contiguous_prefix([b for b in minutes if b.symbol == symbol], prior)
            rows = aggregate(prefix)
            if self.config.needs_minute_seed and prefix:
                raw[symbol] = prefix
            if rows:
                bars[symbol] = rows
        state.prior_day, state.prior_bars = prior.day, bars
        state.prior_minutes = raw
        self.store.commit(rev, state, [{"event": "PRIOR_SESSION_SEEDED", "day": prior.day}])
        self.prior_retry_after = (now or datetime.now(UTC)) + timedelta(seconds=60)
