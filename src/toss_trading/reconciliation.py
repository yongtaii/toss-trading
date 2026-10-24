"""Read-only external account verification for eventual LIVE certification.

Never apply a real account snapshot to a PAPER ledger: their balances are independent.
"""

from decimal import Decimal

from toss_trading.domain import ACTIVE, OrderStatus, State
from toss_trading.ports import BrokerQueries

STATUS_MAP = {
    "PENDING": OrderStatus.SUBMITTED,
    "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


async def verify_external_account(queries: BrokerQueries, expected: State) -> dict:
    """Return mismatches; exceptions never produce a successful verification."""
    holdings = await queries.holdings()
    power = await queries.buying_power()
    orders = await queries.orders(status="OPEN")
    actual_positions = {
        item["symbol"]: Decimal(item["quantity"])
        for item in holdings["items"]
        if item["currency"] == "USD" and Decimal(item["quantity"]) != 0
    }
    expected_positions = {symbol: p.quantity for symbol, p in expected.positions.items()}
    mismatches = []
    if actual_positions != expected_positions:
        mismatches.append("positions")
    if power["currency"] != "USD" or Decimal(power["cashBuyingPower"]) != expected.cash:
        mismatches.append("available_cash")
    actual_ids = {item["orderId"] for item in orders["orders"]}
    expected_ids = {o.id for o in expected.pending()}
    if actual_ids != expected_ids:
        mismatches.append("open_order_ids")
    statuses = {}
    # Query EVERY unresolved order, including orders absent from the OPEN collection.
    for order in expected.pending():
        detail = await queries.order(order.id)
        normalized = STATUS_MAP.get(detail["status"], OrderStatus.UNKNOWN)
        statuses[order.id] = normalized
        if normalized != order.status or normalized not in ACTIVE:
            mismatches.append(f"order_state:{order.id}")
        if Decimal(detail["execution"]["filledQuantity"]) != order.filled:
            mismatches.append(f"filled_quantity:{order.id}")
    return {
        "verified": not mismatches,
        "mismatches": mismatches,
        "order_statuses": statuses,
        "live_execution_enabled": False,
    }
