"""Persist before HTTP, reconcile cumulative executions, never retry unknown writes."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from toss_trading.adapters.toss import BrokerError, ConfigurationError
from toss_trading.domain import ACTIVE, ZERO, OrderStatus
from toss_trading.live.broker import LiveBroker, order_body
from toss_trading.live.config import WEEKLY_STRATEGY, LiveConfig
from toss_trading.live.control import read_control
from toss_trading.live.ledger import apply_snapshot, money, usd_positions
from toss_trading.live.weekly import entry_window, first_week_day
from toss_trading.marketdata import NY
from toss_trading.persistence import Store
from toss_trading.trading_config import TradingConfig


class LiveBridge:
    def __init__(self, store: Store, broker: LiveBroker, live: LiveConfig, config: TradingConfig):
        self.store, self.broker, self.live, self.config = store, broker, live, config
        self.terminal_checked: set[str] = set()

    def protected_binding(self) -> str:
        if not self.live.protected_positions:
            return ""
        payload = json.dumps(
            {s: str(q.normalize()) for s, q in sorted(self.live.protected_positions.items())},
            sort_keys=True,
        )
        return ":protected:" + sha256(payload.encode()).hexdigest()[:24]

    def load(self):
        revision, state = self.store.load()
        return revision, state.model_copy(deep=True)

    def block(self, reason: str) -> None:
        rev, state = self.load()
        state.reconciled, state.live_verified_at = False, None
        records = (
            []
            if reason == "verification_in_progress"
            else [
                {
                    "event": "LIVE_RECONCILIATION_BLOCKED",
                    "time": datetime.now(UTC).isoformat(),
                    "reason": reason,
                    "notify": True,
                }
            ]
        )
        self.store.commit(rev, state, records)

    async def reconcile(self) -> bool:
        self.block("verification_in_progress")
        try:
            accounts = await self.broker.accounts()
            if self.live.account_seq not in {a["accountSeq"] for a in accounts}:
                raise ValueError("Configured account unavailable")
            rev, state = self.load()
            if state.live_account_seq not in (None, self.live.account_seq):
                raise ValueError("Ledger belongs to a different account")
            if set(state.positions) - set(self.config.symbols):
                raise ValueError("Managed ledger contains an out-of-strategy position")
            if state.live_account_seq is not None:
                # Keep the existing State schema compatible with rollback of PAPER releases.
                # This suffix binds the acknowledged holdings to the persisted live run identity.
                binding = self.protected_binding()
                if (binding and not (state.live_run_id or "").endswith(binding)) or (
                    not binding and ":protected:" in (state.live_run_id or "")
                ):
                    raise ValueError("Protected holdings differ from the bound live identity")
            records = []
            terminal_checked = set()
            for order in state.orders.values():
                if order.broker_phase in {"SENDING", "UNKNOWN"} and not order.broker_id:
                    order.status, order.broker_phase = OrderStatus.UNKNOWN, "UNKNOWN"
                if order.broker_id and (
                    order.status in ACTIVE or order.id not in self.terminal_checked
                ):
                    detail = await self.broker.order(order.broker_id)
                    records.extend(apply_snapshot(state, order, detail, self.config))
                    if order.status not in ACTIVE:
                        terminal_checked.add(order.id)
            # Commit executions even if later account validation finds a mismatch.
            self.store.commit(rev, state, records)
            self.terminal_checked.update(terminal_checked)
            holdings = await self.broker.holdings()
            power = await self.broker.buying_power()
            opened = await self.broker.orders(status="OPEN")
            if opened.get("hasNext") or opened.get("nextCursor"):
                raise ValueError("Incomplete OPEN order snapshot")
            actual = usd_positions(holdings)
            rev, state = self.load()
            expected = {
                **self.live.protected_positions,
                **{s: p.quantity for s, p in state.positions.items()},
            }
            if actual != expected:
                raise ValueError("Broker positions differ from managed positions")
            owned = {o.broker_id for o in state.orders.values() if o.broker_id}
            operations = {o.cancel_broker_id for o in state.orders.values() if o.cancel_broker_id}
            actual_ids = {o["orderId"] for o in opened["orders"]}
            if actual_ids - owned - operations:
                raise ValueError("Unmanaged open orders exist")
            active_ids = {o.broker_id for o in state.pending() if o.broker_id}
            if active_ids - actual_ids:
                raise ValueError("Order status changed during account snapshot")
            if any(o.status == OrderStatus.UNKNOWN for o in state.pending()):
                raise ValueError("Unknown order requires explicit broker-ID reconciliation")
            if power["currency"] != "USD":
                raise ValueError("USD buying power required")
            available = money(power["cashBuyingPower"])
            if state.live_account_seq is None and available < self.live.capital_usd:
                raise ValueError("Insufficient cash for allocated capital")
            if state.live_account_seq is None:
                state.live_run_id = uuid4().hex + self.protected_binding()
            state.live_account_seq = self.live.account_seq
            state.live_run_id = state.live_run_id or uuid4().hex
            state.live_buying_power = available
            state.live_verified_at = datetime.now(UTC)
            state.reconciled = True
            self.store.commit(rev, state, [{"event": "LIVE_ACCOUNT_VERIFIED"}])
            return True
        except (BrokerError, ConfigurationError, ValueError, KeyError, TypeError, ArithmeticError):
            self.block("account_or_order_verification_failed")
            return False

    async def attach(self, local_id: str, broker_id: str) -> None:
        """Operator-provided mapping; never guess an order from ticker/size alone."""
        detail = await self.broker.order(broker_id)
        rev, state = self.load()
        order = state.orders[local_id]
        if order.broker_id or order.broker_phase not in {"SENDING", "UNKNOWN"}:
            raise ValueError("Only unresolved order identities can be attached")
        if any(o.broker_id == broker_id for o in state.orders.values()):
            raise ValueError("Broker order already owned")
        ordered_at = datetime.fromisoformat(detail["orderedAt"])
        if ordered_at.tzinfo is None or not (
            -30 <= (ordered_at - order.submitted).total_seconds() <= 120
        ):
            raise ValueError("Broker order time does not match the unresolved request")
        order.broker_id = broker_id
        records = apply_snapshot(state, order, detail, self.config)
        order.broker_phase = "ACKNOWLEDGED"
        state.reconciled, state.live_verified_at = False, None
        self.store.commit(
            rev, state, records + [{"event": "LIVE_ORDER_ATTACHED", "order_id": local_id}]
        )

    async def flush(self) -> None:
        # Reload after every commit; never send a stale in-memory batch.
        ids = [o.id for o in self.store.load()[1].pending()]
        for key in ids:
            rev, state = self.load()
            order = state.orders[key]
            now = datetime.now(UTC)
            if order.cancel_requested and order.broker_id:
                if order.cancel_sent:
                    continue  # A cancellation timeout is not proof that it failed.
                order.cancel_sent = now
                self.store.commit(rev, state, [{"event": "LIVE_CANCEL_SENDING", "order_id": key}])
                try:
                    result = await self.broker.cancel_order(order.broker_id)
                except (BrokerError, ConfigurationError, ValueError):
                    self.block("cancellation_response_unknown")
                    continue
                rev, state = self.load()
                state.orders[key].cancel_broker_id = result["orderId"]
                self.store.commit(rev, state, [{"event": "LIVE_CANCEL_ACK", "order_id": key}])
                continue
            if order.broker_phase != "LOCAL" or order.status not in ACTIVE:
                continue
            if not self.live.enabled or not self.broker.armed:
                continue
            # Both entries and exits require a fresh verified account, session and quote.
            age = (now - state.live_verified_at).total_seconds() if state.live_verified_at else -1
            valid = state.reconciled and 0 <= age <= self.live.max_verification_age_seconds
            valid = valid and state.session is not None and state.session.contains(now)
            mark_at = state.mark_times.get(order.symbol)
            valid = (
                valid
                and mark_at is not None
                and (0 <= (now - mark_at).total_seconds() <= self.config.max_quote_age_seconds)
            )
            if not valid:
                # Keep liquidation intent; discard delayed entries instead of replaying them.
                if order.side == "BUY":
                    order.status, order.reserved = OrderStatus.CANCELLED, ZERO
                    self.store.commit(
                        rev, state, [{"event": "LIVE_ENTRY_EXPIRED", "order_id": key}]
                    )
                continue
            if order.side == "BUY":
                age = (now - order.submitted).total_seconds()
                source = state.signal_quotes.get(order.signal_symbol)
                source_fresh = source is not None and (
                    0 <= (now - source.at).total_seconds() <= self.config.max_quote_age_seconds
                )
                source_valid = False
                weekly = self.config.strategy_name == WEEKLY_STRATEGY
                if weekly:
                    source_valid = state.session is not None and entry_window(state.session, now)
                    day = now.astimezone(NY).date()
                    source_valid = (
                        source_valid
                        and first_week_day(day) == day
                        and order.reason == "WEEKLY_ENTRY"
                        and order.id == f"weekly:{day}:{order.symbol}:BUY"
                    )
                elif source_fresh:
                    move = order.signal_direction * (
                        (source.bid + source.ask) / 2 - order.signal_entry
                    )
                    distance = order.signal_atr * self.config.stop_atr
                    source_valid = -distance < move < self.config.reward_r * distance
                required = order.quantity * order.limit * (1 + self.config.entry_cost_bps / 10000)
                pnl = state.equity(self.config.exit_cost_bps) - state.day_equity
                risk = order.quantity * (
                    order.atr * self.config.stop_atr
                    + self.config.taf_per_share
                    + order.limit
                    * (
                        self.config.entry_cost_bps
                        + self.config.exit_cost_bps
                        + self.config.slippage_bps
                    )
                    / 10000
                )
                allocated = sum(p.quantity * p.entry for p in state.positions.values())
                allocated += sum(
                    (o.quantity - o.filled) * o.limit for o in state.pending() if o.side == "BUY"
                )
                other_reserved = sum(
                    o.reserved for o in state.pending() if o.side == "BUY" and o.id != key
                )
                if (
                    read_control(self.live.control_path) != "run"
                    or not source_valid
                    or not 0 <= age <= self.live.max_entry_age_seconds
                    or state.loss_halted
                    or pnl <= -self.live.daily_loss_usd
                    or risk > max(ZERO, self.live.daily_loss_usd + pnl)
                    or required > state.live_buying_power
                    or required > state.cash - other_reserved
                    or order.quantity * order.limit > self.live.max_order_usd
                    or (weekly and required > self.config.max_order_usd)
                    or allocated > self.live.capital_usd
                ):
                    order.status, order.reserved = OrderStatus.CANCELLED, ZERO
                    self.store.commit(
                        rev, state, [{"event": "LIVE_ENTRY_BLOCKED", "order_id": key}]
                    )
                    continue
            if order.reason == "WEEKLY_TRIM" and (
                read_control(self.live.control_path) != "run"
                or state.loss_halted
                or not entry_window(state.session, now)
                or first_week_day(now.astimezone(NY).date()) != now.astimezone(NY).date()
            ):
                order.status = OrderStatus.CANCELLED
                self.store.commit(rev, state, [{"event": "LIVE_TRIM_EXPIRED", "order_id": key}])
                continue
            order.client_order_id = sha256(f"{state.live_run_id}:{key}".encode()).hexdigest()[:32]
            order.broker_request = order_body(order, order.client_order_id)
            order.broker_phase, order.status = "SENDING", OrderStatus.UNKNOWN
            state.reconciled = False
            self.store.commit(rev, state, [{"event": "LIVE_ORDER_SENDING", "order_id": key}])
            try:
                result = await self.broker.create_order(order, order.client_order_id)
            except (BrokerError, ConfigurationError, ValueError):
                rev, state = self.load()
                state.orders[key].broker_phase = "UNKNOWN"
                self.store.commit(
                    rev, state, [{"event": "LIVE_ORDER_UNKNOWN", "order_id": key, "notify": True}]
                )
                continue
            rev, state = self.load()
            order = state.orders[key]
            order.broker_id, order.broker_phase = result["orderId"], "ACKNOWLEDGED"
            order.status = OrderStatus.SUBMITTED
            self.store.commit(rev, state, [{"event": "LIVE_ORDER_ACK", "order_id": key}])
            # Reverify before another order can consume cash/position capacity.
            await self.reconcile()
