from dataclasses import asdict
from datetime import date, datetime
from hashlib import sha256

from toss_trading.domain import ZERO, Bar, Order, OrderStatus, Quote, Session, State
from toss_trading.execution import LiveTossExecution, PaperExecution
from toss_trading.marketdata import previous_session
from toss_trading.persistence import Store
from toss_trading.risk import quote_checks, rejection_reasons, size_entry
from toss_trading.strategy import Context, Strategy, aggregate, create_strategy
from toss_trading.trading_config import TradingConfig

FULL_SESSION_SECONDS = 390 * 60


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
        return
    full = (old.close - old.open).total_seconds() == FULL_SESSION_SECONDS
    prior = {}
    for symbol, minutes in state.bars.items():
        bars = aggregate(contiguous_prefix(minutes, old))
        if bars:
            prior[symbol] = bars
        # Early-close and incomplete sessions are excluded from time-of-day volume slots.
        if full and len(bars) * 300 == FULL_SESSION_SECONDS:
            slots = state.slot_volumes.setdefault(symbol, {})
            for index, bar in enumerate(bars):
                history = slots.setdefault(index, [])
                history.append(bar.volume)
                del history[: -c.volume_lookback_days]
    if expected is not None and old.day == expected.day:
        state.prior_day, state.prior_bars = old.day, prior
    elif expected is None or state.prior_day != expected.day:
        state.prior_day, state.prior_bars = "", {}


class TradingEngine:
    def __init__(self, config: TradingConfig, store: Store, strategy: Strategy | None = None):
        self.config, self.store = config, store
        self.strategy = strategy or create_strategy(config.strategy_name, config)
        self.execution = PaperExecution(config) if config.mode == "PAPER" else LiveTossExecution()
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
        quotes_by_symbol = {q.symbol: q for q in quotes if q.symbol in c.symbols}
        for symbol, q in quotes_by_symbol.items():
            fresh = quote_checks(q, now, c)["fresh_quote"]
            if fresh and (symbol not in state.mark_times or q.at > state.mark_times[symbol]):
                state.marks[symbol], state.mark_times[symbol] = q.bid, q.at
        pnl = state.equity(c.exit_cost_bps) - state.day_equity
        if pnl <= -state.day_equity * c.daily_loss_fraction and not state.loss_halted:
            state.loss_halted = True
            records.append({"event": "DAILY_LOSS_LIMIT", "time": now, "pnl": pnl, "notify": True})
        remaining = (session.close - now).total_seconds()
        # Cancel remaining entry fills before stop/flatten; paper cancellation is definitive.
        for order in state.pending():
            q = quotes_by_symbol.get(order.symbol)
            position = state.positions.get(order.symbol)
            stop = position and q and q.bid <= position.stop
            last_bar = state.last_bars.get(order.symbol)
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
                    or stale_bars
                    or stop
                    or remaining <= c.no_buy_before_close_minutes * 60
                )
            ):
                order.status, order.reserved = OrderStatus.CANCELLED, ZERO
                records.append({"event": "CANCELLED", "order_id": order.id, "time": now})
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
            if bar.symbol not in c.symbols:
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
            state.bars.setdefault(bar.symbol, []).append(bar)
            state.last_bars[bar.symbol] = bar.end
            if (bar.end - session.open).total_seconds() % 300 == 0:
                decision = self.strategy.evaluate(
                    state.bars[bar.symbol], session, self.context(state, session, bar)
                )
                decisions[bar.symbol] = (decision, bar.end)
                for note in decision.notes:
                    event, _, reason = note.partition(":")
                    key = f"{event}:{bar.symbol}"
                    if key not in state.alerts:  # Once per symbol per session.
                        state.alerts.append(key)
                        records.append(
                            {"event": event, "symbol": bar.symbol, "reason": reason, "time": now}
                        )
                records.append(
                    {
                        "event": "DECISION",
                        "time": bar.end,
                        "symbol": bar.symbol,
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
            reason = None
            if q and quote_checks(q, now, c)["fresh_quote"]:
                if q.bid <= position.stop:
                    reason = "STOP"
                elif q.bid >= position.target:
                    reason = "TARGET"
            if reason is None and decision and decision[0].signal == "VWAP_EXIT":
                reason = "VWAP_EXIT"
            if reason is None and (
                remaining <= c.flatten_before_close_minutes * 60
                or "overnight_positions" in state.alerts
            ):
                reason = "SESSION_FLATTEN"
            if reason:
                if regular and q and quote_checks(q, now, c)["fresh_quote"]:
                    # Cancel unfilled buy remainder before generating a liquidation intent.
                    for order in state.pending(symbol):
                        if order.side == "BUY" and order.status != OrderStatus.UNKNOWN:
                            order.status, order.reserved = OrderStatus.CANCELLED, ZERO
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
            quantity = ZERO
            if q and decision.indicators:
                quantity, risk_checks = size_entry(
                    state, symbol, decision.indicators.atr, q, now, session, c
                )
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
                        atr=decision.indicators.atr,
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
        if remaining <= c.flatten_deadline_minutes * 60 and state.positions:
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

    def context(self, state: State, session: Session, bar: Bar) -> Context:
        expected = previous_session(date.fromisoformat(session.day))
        fresh = expected is not None and state.prior_day == expected.day
        slot = int((bar.end - session.open).total_seconds() // 300) - 1
        return Context(
            prior=tuple(state.prior_bars.get(bar.symbol, [])) if fresh else (),
            slot_volumes=tuple(state.slot_volumes.get(bar.symbol, {}).get(slot, [])),
        )

    def needs_prior(self, session: Session) -> Session | None:
        """Prior session to fetch when the ledger cannot seed indicators by itself."""
        if self.config.indicator_warmup != "prior_session":
            return None
        expected = previous_session(date.fromisoformat(session.day))
        state = self.store.load()[1]
        if expected is None or state.prior_day == expected.day or state.day == expected.day:
            return None
        return expected

    def seed_prior(self, prior: Session, minutes: list[Bar]) -> None:
        """Store a fetched prior session; the strategy falls back if it is incomplete."""
        rev, state = self.store.load()
        bars = {}
        for symbol in self.config.symbols:
            rows = aggregate(contiguous_prefix([b for b in minutes if b.symbol == symbol], prior))
            if rows:
                bars[symbol] = rows
        state.prior_day, state.prior_bars = prior.day, bars
        self.store.commit(rev, state, [{"event": "PRIOR_SESSION_SEEDED", "day": prior.day}])
