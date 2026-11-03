"""Synchronous engine port: durable intents only; HTTP belongs to LiveBridge."""

from datetime import datetime

from toss_trading.domain import Order, OrderStatus, Quote, State
from toss_trading.execution import PaperExecution
from toss_trading.trading_config import TradingConfig


class LiveIntents:
    def __init__(self, config: TradingConfig):
        if config.mode != "LIVE":
            raise ValueError("Live intents require LIVE configuration")
        self.config = config

    def reconcile(self, state: State) -> None:
        # Never mark an account reconciled using local data alone.
        if state.cash < 0 or any(p.quantity <= 0 for p in state.positions.values()):
            raise ValueError("Invalid live ledger")
        if set(state.positions) - set(self.config.symbols) or any(
            p.symbol != symbol for symbol, p in state.positions.items()
        ):
            raise ValueError("Live ledger contains unmanaged positions")
        if any(o.status == OrderStatus.UNKNOWN for o in state.pending()):
            state.reconciled = False

    def submit(self, state: State, order: Order) -> None:
        if self.config.mode != "LIVE" or order.symbol not in self.config.symbols:
            raise ValueError("Invalid live intent")
        if order.id in state.orders or state.pending(order.symbol):
            raise ValueError("Duplicate live intent")
        if order.quantity <= 0 or order.quantity % 1 or order.side not in {"BUY", "SELL"}:
            raise ValueError("Invalid live quantity/side")
        if order.side == "SELL":
            p = state.positions.get(order.symbol)
            if p is None or order.quantity > p.quantity:
                raise ValueError("Live oversell blocked")
        state.orders[order.id] = order

    def cancel(self, state: State, order: Order) -> None:
        if order.broker_phase == "LOCAL":
            PaperExecution(self.config).cancel(state, order)
        else:
            order.cancel_requested = True  # Still active until the broker confirms.

    def process(self, state: State, quote: Quote, now: datetime) -> list[dict]:
        return []  # Real fills only come from broker snapshots.
