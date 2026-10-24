import asyncio
import re
from datetime import date
from time import monotonic
from typing import Any, Literal

import httpx

from toss_trading.config import Settings

BASE_URL = "https://openapi.tossinvest.com"
SYMBOL = re.compile(r"[A-Za-z0-9.\-]{1,32}")
ORDER_ID = re.compile(r"[A-Za-z0-9_\-]{1,256}")
READ_PATHS = frozenset(
    {
        "/api/v1/prices",
        "/api/v1/accounts",
        "/api/v1/holdings",
        "/api/v1/buying-power",
        "/api/v1/orders",
        "/api/v1/candles",
        "/api/v1/orderbook",
        "/api/v1/market-calendar/US",
    }
)


class BrokerError(Exception):
    """Sanitized error: never include broker bodies, headers, or credentials."""

    def __init__(self, status: int | None = None):
        self.status = status
        super().__init__(f"Broker request failed (status={status})")


class ConfigurationError(Exception):
    pass


def symbol_value(value: str) -> str:
    if not SYMBOL.fullmatch(value):
        raise ValueError("Invalid symbol")
    return value.upper()


def read_only_guard(request: httpx.Request) -> None:
    """Final outbound boundary: token POST is the only permitted mutation verb."""
    path = request.url.path
    allowed = (
        request.method == "POST"
        and path == "/oauth2/token"
        or request.method == "GET"
        and (
            path in READ_PATHS
            or path.startswith("/api/v1/orders/")
            and ORDER_ID.fullmatch(path.removeprefix("/api/v1/orders/"))
        )
    )
    if request.url.scheme != "https" or request.url.host != "openapi.tossinvest.com":
        raise ConfigurationError("Only the official HTTPS API origin is permitted")
    if request.url.port not in (None, 443) or not allowed:
        raise ConfigurationError("Outbound request blocked by read-only policy")


async def _guard(request: httpx.Request) -> None:
    read_only_guard(request)


class TossQueries:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            event_hooks={"request": [_guard]},
        )
        self._token: str | None = None
        self._expires = 0.0
        self._token_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        # Conservative shared pacing, including ACCOUNT's 1 TPS limit.
        async with self._request_lock:
            await asyncio.sleep(max(0, 1.05 - (monotonic() - self._last_request)))
            self._last_request = monotonic()
            try:
                response = await self._http.request(method, path, **kwargs)
            except httpx.RequestError:
                raise BrokerError() from None
        if not response.is_success:
            raise BrokerError(response.status_code)
        return response

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and monotonic() < self._expires:
                return self._token
            cid, secret = self.settings.toss_client_id, self.settings.toss_client_secret
            if not cid or not secret or not cid.get_secret_value() or not secret.get_secret_value():
                raise ConfigurationError("Toss credentials are required")
            response = await self._send(
                "POST",
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": cid.get_secret_value(),
                    "client_secret": secret.get_secret_value(),
                },
            )
            try:
                payload = response.json()
                token, ttl = payload["access_token"], int(payload["expires_in"])
                if not isinstance(token, str) or not token or ttl <= 0:
                    raise ValueError
                if payload["token_type"] != "Bearer":
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BrokerError() from None
            self._token = token
            self._expires = monotonic() + max(0, ttl - 60)
            return token

    async def _get(self, path: str, *, account: bool = False, **params: Any) -> Any:
        headers = {}
        if account:
            if self.settings.toss_account_seq is None:
                raise ConfigurationError("TOSS_ACCOUNT_SEQ (accountSeq) is required")
            headers["X-Tossinvest-Account"] = str(self.settings.toss_account_seq)
        headers["Authorization"] = f"Bearer {await self._access_token()}"
        response = await self._send(
            "GET",
            path,
            headers=headers,
            params={key: value for key, value in params.items() if value is not None},
        )
        try:
            body = response.json()
            return body["result"]
        except (ValueError, KeyError, TypeError):
            raise BrokerError() from None

    async def prices(self, symbols: list[str]) -> Any:
        if not 1 <= len(symbols) <= 200:
            raise ValueError("Provide 1 to 200 symbols")
        return await self._get("/api/v1/prices", symbols=",".join(map(symbol_value, symbols)))

    async def accounts(self) -> Any:
        return await self._get("/api/v1/accounts")

    async def candles(self, symbol: str, *, count: int = 200, before: str | None = None) -> Any:
        if not 1 <= count <= 200:
            raise ValueError("Candle count must be 1 to 200")
        return await self._get(
            "/api/v1/candles",
            symbol=symbol_value(symbol),
            interval="1m",
            count=count,
            before=before,
            adjusted="true",
        )

    async def orderbook(self, symbol: str) -> Any:
        return await self._get("/api/v1/orderbook", symbol=symbol_value(symbol))

    async def market_calendar(self, day: date) -> Any:
        return await self._get("/api/v1/market-calendar/US", date=day.isoformat())

    async def holdings(self, symbol: str | None = None) -> Any:
        return await self._get(
            "/api/v1/holdings",
            account=True,
            symbol=symbol_value(symbol) if symbol else None,
        )

    async def buying_power(self) -> Any:
        return await self._get("/api/v1/buying-power", account=True, currency="USD")

    async def orders(
        self,
        *,
        status: Literal["OPEN", "CLOSED"] = "OPEN",
        symbol: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> Any:
        if status not in ("OPEN", "CLOSED") or not 1 <= limit <= 100:
            raise ValueError("Invalid order filter")
        if from_date and to_date and from_date > to_date:
            raise ValueError("from_date must precede to_date")
        return await self._get(
            "/api/v1/orders",
            account=True,
            **{
                "status": status,
                "symbol": symbol_value(symbol) if symbol else None,
                "cursor": cursor,
                "limit": limit,
                "from": from_date,
                "to": to_date,
            },
        )

    async def order(self, order_id: str) -> Any:
        if not ORDER_ID.fullmatch(order_id):
            raise ValueError("Invalid order ID")
        return await self._get(f"/api/v1/orders/{order_id}", account=True)
