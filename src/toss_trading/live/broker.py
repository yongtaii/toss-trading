"""Official v1.2.24 order transport. No automatic mutation retries."""

import re
from decimal import ROUND_DOWN, Decimal

import httpx

from toss_trading.adapters.toss import (
    ORDER_ID,
    BrokerError,
    ConfigurationError,
    TossQueries,
    read_only_guard,
)
from toss_trading.config import Settings
from toss_trading.domain import Order
from toss_trading.live.config import LiveConfig


def order_body(order: Order, client_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", client_id):
        raise ValueError("Invalid client order ID")
    if order.side not in {"BUY", "SELL"}:
        raise ValueError("Invalid order side")
    if not order.quantity.is_finite() or order.quantity <= 0 or order.quantity % 1:
        raise ValueError("Live orders require positive whole shares")
    body = {
        "clientOrderId": client_id,
        "symbol": order.symbol,
        "side": order.side,
        "orderType": "LIMIT" if order.side == "BUY" else "MARKET",
        "quantity": format(order.quantity, "f"),
        "timeInForce": "DAY",
    }
    if order.side == "BUY":
        tick = Decimal("0.01") if order.limit >= 1 else Decimal("0.0001")
        price = order.limit.quantize(tick, rounding=ROUND_DOWN)
        if not price.is_finite() or price <= 0:
            raise ValueError("Invalid buy price")
        body["price"] = format(price, "f")
    return body


class LiveBroker(TossQueries):
    def __init__(
        self,
        settings: Settings,
        live: LiveConfig,
        *,
        armed: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if live.account_seq is None or settings.toss_account_seq != live.account_seq:
            raise ConfigurationError("Live account does not match TOSS_ACCOUNT_SEQ")
        super().__init__(settings, transport=transport)
        self.live, self.armed = live, armed
        self._http.event_hooks["request"] = [self._guard_live]

    async def _guard_live(self, request: httpx.Request) -> None:
        path = request.url.path
        if request.method != "POST" or path == "/oauth2/token":
            read_only_guard(request)
            return
        if not self.live.enabled or not self.armed:
            raise ConfigurationError("Live writes are not armed")
        if (
            request.url.scheme != "https"
            or request.url.host != "openapi.tossinvest.com"
            or request.url.port not in (None, 443)
        ):
            raise ConfigurationError("Invalid live API origin")
        allowed = path == "/api/v1/orders" or bool(
            re.fullmatch(
                r"/api/v1/orders/[A-Za-z0-9_-]{1,256}/cancel",
                path,
            )
        )
        if not allowed or request.headers.get("X-Tossinvest-Account") != str(self.live.account_seq):
            raise ConfigurationError("Live endpoint or account is not allowed")

    async def _post(self, path: str, body: dict) -> dict:
        if not self.armed or not self.live.enabled:
            raise ConfigurationError("Live writes are not armed")
        response = await self._send(
            "POST",
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {await self._access_token()}",
                "X-Tossinvest-Account": str(self.live.account_seq),
            },
        )
        try:
            result = response.json()["result"]
            if not isinstance(result, dict) or not ORDER_ID.fullmatch(result["orderId"]):
                raise ValueError
            return result
        except (KeyError, ValueError, TypeError):
            raise BrokerError() from None

    async def create_order(self, order: Order, client_id: str) -> dict:
        if order.symbol not in {"SOXL", "SOXS", "TQQQ", "SQQQ"}:
            raise ConfigurationError("Symbol not allowed for live execution")
        body = order_body(order, client_id)
        if (
            order.side == "BUY"
            and Decimal(body["price"]) * order.quantity > self.live.max_order_usd
        ):
            raise ConfigurationError("Live per-order cap exceeded")
        result = await self._post("/api/v1/orders", body)
        if result.get("clientOrderId") != client_id:
            raise BrokerError()
        return result

    async def cancel_order(self, order_id: str) -> dict:
        if not ORDER_ID.fullmatch(order_id):
            raise ValueError("Invalid broker order ID")
        return await self._post(f"/api/v1/orders/{order_id}/cancel", {})
