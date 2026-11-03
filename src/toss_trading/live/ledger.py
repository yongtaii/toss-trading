"""Apply cumulative broker executions exactly once, inside the caller's transaction."""

from datetime import datetime, timedelta
from decimal import Decimal

from toss_trading.domain import ZERO, Order, OrderStatus, Position, State
from toss_trading.trading_config import TradingConfig

STATUS = {
    "PENDING": OrderStatus.SUBMITTED,
    "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid broker execution amount")
    return result


def usd_positions(holdings: dict) -> dict[str, Decimal]:
    result = {}
    for item in holdings["items"]:
        if item["currency"] != "USD":
            continue
        quantity = money(item["quantity"])
        if quantity:
            if item["symbol"] in result:
                raise ValueError("Duplicate USD holding")
            result[item["symbol"]] = quantity
    return result


def apply_snapshot(state: State, order: Order, detail: dict, config: TradingConfig) -> list[dict]:
    if (
        detail["orderId"] != order.broker_id
        or detail["symbol"] != order.symbol
        or detail["side"] != order.side
        or detail["currency"] != "USD"
        or money(detail["quantity"]) != order.quantity
    ):
        raise ValueError("Broker order identity mismatch")
    execution = detail["execution"]
    filled = money(execution["filledQuantity"])
    amount = money(execution["filledAmount"] or 0)
    fees = money(execution["commission"] or 0) + money(execution.get("tax") or 0)
    quantity, delta_amount, delta_fee = (
        filled - order.filled,
        amount - order.broker_amount,
        fees - order.broker_fees,
    )
    if filled > order.quantity or min(quantity, delta_amount, delta_fee) < 0:
        raise ValueError("Non-monotonic broker execution; reconciliation required")
    if (quantity == 0 and delta_amount != 0) or (quantity > 0 and delta_amount <= 0):
        raise ValueError("Inconsistent broker fill quantity/amount")
    if detail["status"] == "FILLED" and filled != order.quantity:
        raise ValueError("Filled status with incomplete quantity")
    records = []
    if quantity:
        at = datetime.fromisoformat(execution["filledAt"])
        if at.tzinfo is None:
            raise ValueError("Broker execution needs timezone")
        price = delta_amount / quantity
        if order.side == "BUY":
            if delta_amount + delta_fee > state.cash:
                raise ValueError("Broker execution exceeds allocated ledger cash")
            state.cash -= delta_amount + delta_fee
            p = state.positions.get(order.symbol)
            risk = order.atr * config.stop_atr
            if p:
                total = p.quantity + quantity
                p.entry = (p.entry * p.quantity + delta_amount) / total
                p.quantity = total
                p.entry_fees += delta_fee
            else:
                p = Position(
                    symbol=order.symbol,
                    quantity=quantity,
                    entry=price,
                    entry_fees=delta_fee,
                    stop=price - risk,
                    target=price + config.reward_r * risk,
                    opened=at,
                    initial_risk=risk,
                    signal_symbol=order.signal_symbol,
                    signal_direction=order.signal_direction,
                    signal_stop=order.signal_entry
                    - order.signal_direction * config.stop_atr * order.signal_atr,
                    signal_target=order.signal_entry
                    + order.signal_direction * config.reward_r * config.stop_atr * order.signal_atr,
                    signal_entry=order.signal_entry,
                    signal_risk=config.stop_atr * order.signal_atr,
                )
                state.positions[order.symbol] = p
                state.entries[order.symbol] = state.entries.get(order.symbol, 0) + 1
            order.reserved = max(ZERO, order.reserved - delta_amount - delta_fee)
            pnl = ZERO
        else:
            p = state.positions.get(order.symbol)
            if p is None or quantity > p.quantity:
                raise ValueError("Broker sell exceeds managed position")
            allocated = p.entry_fees * quantity / p.quantity
            pnl = delta_amount - p.entry * quantity - allocated - delta_fee
            state.cash += delta_amount - delta_fee
            state.realized += pnl
            state.trades.append(
                {
                    "symbol": order.symbol,
                    "opened": p.opened.isoformat(),
                    "closed": at.isoformat(),
                    "quantity": str(quantity),
                    "entry": str(p.entry),
                    "exit": str(price),
                    "pnl": str(pnl),
                    "entry_fee": str(allocated),
                    "exit_fee": str(delta_fee),
                    "slippage": "0",
                    "reason": order.reason,
                    "signal_symbol": p.signal_symbol,
                    "signal_direction": p.signal_direction,
                    "broker_id": order.broker_id,
                }
            )
            p.quantity -= quantity
            p.entry_fees -= allocated
            if not p.quantity:
                del state.positions[order.symbol]
            if order.reason == "STOP":
                state.cooldowns[order.symbol] = at + timedelta(minutes=config.cooldown_minutes)
        records.append(
            {
                "event": "BUY_FILL" if order.side == "BUY" else "SELL_FILL",
                "time": at,
                "symbol": order.symbol,
                "order_id": order.id,
                "broker_id": order.broker_id,
                "quantity": quantity,
                "price": price,
                "fee": delta_fee,
                "pnl": pnl,
                "reason": order.reason,
                "notify": True,
            }
        )
    elif delta_fee:
        # Some brokers finalize charges after the last execution.
        if delta_fee > state.cash:
            raise ValueError("Final broker charges exceed allocated cash")
        state.cash -= delta_fee
        p = state.positions.get(order.symbol)
        if order.side == "BUY" and p:
            p.entry_fees += delta_fee
        else:
            state.realized -= delta_fee
        records.append(
            {
                "event": "BROKER_FEE_ADJUSTMENT",
                "order_id": order.id,
                "fee": delta_fee,
                "notify": True,
            }
        )
    state.fees += delta_fee
    order.filled, order.broker_amount, order.broker_fees = filled, amount, fees
    order.status = STATUS.get(detail["status"], OrderStatus.UNKNOWN)
    if order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
        order.reserved = ZERO
    return records
