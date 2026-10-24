from datetime import datetime, timedelta
from typing import Protocol

from toss_trading.domain import ZERO, Order, OrderStatus, Position, Quote, State
from toss_trading.risk import floor_step, quote_checks
from toss_trading.trading_config import TradingConfig


class Execution(Protocol):
    def reconcile(self, state: State) -> None: ...
    def submit(self, state: State, order: Order) -> None: ...
    def process(self, state: State, quote: Quote, now: datetime) -> list[dict]: ...


class PaperExecution:
    """Only mutates a local transactional ledger; never calls a broker."""

    def __init__(self, config: TradingConfig):
        self.config = config

    def reconcile(self, state: State) -> None:
        if state.cash < 0 or any(p.quantity <= 0 for p in state.positions.values()):
            raise ValueError("Corrupt paper account")
        if any(s not in self.config.symbols for s in state.positions):
            raise ValueError("Position outside current allowlist; manual reconciliation required")
        if any(o.filled < 0 or o.filled > o.quantity for o in state.orders.values()):
            raise ValueError("Corrupt order fills")
        state.reconciled = not any(o.status == OrderStatus.UNKNOWN for o in state.pending())

    def submit(self, state: State, order: Order) -> None:
        if order.symbol not in self.config.symbols or self.config.mode != "PAPER":
            raise ValueError("Execution mode or symbol not allowed")
        if order.id in state.orders or state.pending(order.symbol):
            raise ValueError("Duplicate or unresolved order")
        if order.quantity <= 0 or order.side not in ("BUY", "SELL"):
            raise ValueError("Invalid order")
        if order.side == "SELL":
            if order.symbol not in state.positions:
                raise ValueError("No position")
            if order.quantity > state.positions[order.symbol].quantity:
                raise ValueError("Oversell blocked")
        state.orders[order.id] = order

    def process(self, state: State, quote: Quote, now: datetime) -> list[dict]:
        c = self.config
        records = []
        if not quote_checks(quote, now, c)["fresh_quote"]:
            return records
        for order in state.pending(quote.symbol):
            if order.status == OrderStatus.UNKNOWN:
                continue  # Query/reconcile only. Never assume a timeout means rejection.
            if quote.at <= order.submitted or order.last_fill and quote.at <= order.last_fill:
                continue
            buy = order.side == "BUY"
            if buy and not all(quote_checks(quote, now, c).values()):
                continue
            price = (quote.ask if buy else quote.bid) * (
                1 + (c.slippage_bps if buy else -c.slippage_bps) / 10000
            )
            if buy and price > order.limit:
                continue
            size = quote.ask_size if buy else quote.bid_size
            quantity = min(
                order.quantity - order.filled, floor_step(size * c.participation, c.quantity_step)
            )
            if quantity <= 0:
                continue
            fee = quantity * price * c.fee_bps / 10000
            if buy:
                total = quantity * price + fee
                other_reserved = sum(
                    (o.reserved for o in state.pending() if o.side == "BUY" and o.id != order.id),
                    ZERO,
                )
                if total > state.cash - other_reserved:
                    order.status, order.reserved = OrderStatus.REJECTED, ZERO
                    records.append(
                        {
                            "event": "ORDER_FAILED",
                            "order_id": order.id,
                            "status": order.status,
                            "reason": "insufficient_cash",
                            "time": now,
                            "notify": True,
                        }
                    )
                    continue
                state.cash -= total
                previous = state.positions.get(order.symbol)
                risk = order.atr * c.stop_atr
                if previous:
                    new_qty = previous.quantity + quantity
                    previous.entry = (
                        previous.entry * previous.quantity + price * quantity
                    ) / new_qty
                    previous.quantity = new_qty
                    previous.entry_fees += fee
                    previous.stop, previous.target = (
                        previous.entry - risk,
                        previous.entry + c.reward_r * risk,
                    )
                else:
                    state.positions[order.symbol] = Position(
                        symbol=order.symbol,
                        quantity=quantity,
                        entry=price,
                        entry_fees=fee,
                        stop=price - risk,
                        target=price + c.reward_r * risk,
                        opened=now,
                        initial_risk=risk,
                    )
                    state.entries[order.symbol] = state.entries.get(order.symbol, 0) + 1
                order.reserved = max(ZERO, order.reserved - total)
                pnl = ZERO
            else:
                position = state.positions[order.symbol]
                quantity = min(quantity, position.quantity)
                fee = quantity * price * c.fee_bps / 10000
                allocated_fee = position.entry_fees * quantity / position.quantity
                pnl = (price - position.entry) * quantity - fee - allocated_fee
                state.cash += quantity * price - fee
                state.realized += pnl
                state.trades.append(
                    {
                        "symbol": order.symbol,
                        "opened": position.opened.isoformat(),
                        "closed": now.isoformat(),
                        "quantity": str(quantity),
                        "entry": str(position.entry),
                        "exit": str(price),
                        "pnl": str(pnl),
                        "entry_fee": str(allocated_fee),
                        "exit_fee": str(fee),
                        "reason": order.reason,
                    }
                )
                position.quantity -= quantity
                position.entry_fees -= allocated_fee
                if position.quantity == 0:
                    del state.positions[order.symbol]
                if order.reason == "STOP":
                    state.cooldowns[order.symbol] = now + timedelta(minutes=c.cooldown_minutes)
            state.fees += fee
            order.filled += quantity
            order.last_fill = quote.at
            order.status = (
                OrderStatus.FILLED
                if order.filled == order.quantity
                else OrderStatus.PARTIALLY_FILLED
            )
            if order.status == OrderStatus.FILLED:
                order.reserved = ZERO
            records.append(
                {
                    "event": "BUY_FILL" if buy else "SELL_FILL",
                    "time": now,
                    "symbol": order.symbol,
                    "order_id": order.id,
                    "status": order.status,
                    "quantity": quantity,
                    "price": price,
                    "fee": fee,
                    "pnl": pnl,
                    "daily_pnl": state.equity(c.fee_bps) - state.day_equity,
                    "reason": order.reason,
                    "notify": True,
                }
            )
            if order.reason == "STOP" and not buy:
                records.append(
                    {
                        "event": "STOP",
                        "time": now,
                        "symbol": order.symbol,
                        "order_id": order.id,
                        "notify": True,
                    }
                )
        return records


class LiveTossExecution:
    """Deliberately unavailable until independently verified against a live account.

    The strategy and Execution port are shared; this adapter has no order HTTP client.
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "LIVE is blocked: broker reconciliation and execution certification required"
        )
