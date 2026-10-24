import asyncio
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from toss_trading.adapters.toss import (
    BrokerError,
    ConfigurationError,
    TossQueries,
    read_only_guard,
)
from toss_trading.config import Settings


def make_broker(settings, handler, monkeypatch):
    async def no_sleep(delay):
        pass

    monkeypatch.setattr("toss_trading.adapters.toss.asyncio.sleep", no_sleep)
    return TossQueries(settings, transport=httpx.MockTransport(handler))


async def test_all_queries_and_token_cache(settings, monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/oauth2/token":
            assert req.method == "POST"
            assert parse_qs(req.content.decode()) == {
                "grant_type": ["client_credentials"],
                "client_id": ["test-client"],
                "client_secret": ["test-secret"],
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "test-token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                },
            )
        assert req.method == "GET"
        assert req.headers["Authorization"] == "Bearer test-token"
        if req.url.path not in ("/api/v1/accounts", "/api/v1/prices"):
            assert req.headers["X-Tossinvest-Account"] == "7"
        return httpx.Response(200, json={"result": {"sentinel": "185.70"}})

    broker = make_broker(settings, handler, monkeypatch)
    try:
        assert await broker.prices(["aapl", "MSFT"]) == {"sentinel": "185.70"}
        await broker.accounts()
        await broker.holdings("aapl")
        await broker.buying_power()
        await broker.orders(
            status="CLOSED",
            cursor="next=",
            limit=10,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
        )
        await broker.order("test-order_1")
        assert len(seen) == 7
        assert seen[1].url.params["symbols"] == "AAPL,MSFT"
        assert seen[4].url.params["currency"] == "USD"
        assert seen[5].url.params["from"] == "2026-01-01"
        assert seen[5].url.params["cursor"] == "next="
    finally:
        await broker.aclose()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_errors_no_retry_or_sensitive_body(settings, monkeypatch, status):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json={"error": "secret-account-data"})

    broker = make_broker(settings, handler, monkeypatch)
    try:
        with pytest.raises(BrokerError) as error:
            await broker.accounts()
        assert error.value.status == status
        assert "secret-account-data" not in str(error.value)
        assert len(seen) == 1
    finally:
        await broker.aclose()


async def test_missing_account_fails_before_network(settings, monkeypatch):
    settings.toss_account_seq = None
    broker = make_broker(settings, lambda req: pytest.fail("Unexpected I/O"), monkeypatch)
    try:
        with pytest.raises(ConfigurationError):
            await broker.holdings()
        with pytest.raises(ValueError):
            await broker.order("../orders")
        with pytest.raises(ValueError):
            await broker.prices(["AAPL,MSFT"])
    finally:
        await broker.aclose()


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/orders"),
        ("PATCH", "/api/v1/orders/abc"),
        ("DELETE", "/api/v1/orders/abc"),
        ("POST", "/api/v1/conditional-orders"),
        ("GET", "/api/v1/orders/abc/cancel"),
    ],
)
async def test_outbound_mutations_blocked(settings, monkeypatch, method, path):
    broker = make_broker(
        settings, lambda req: pytest.fail("Request reached transport"), monkeypatch
    )
    try:
        with pytest.raises(ConfigurationError):
            await broker._send(method, path)
    finally:
        await broker.aclose()


def test_foreign_origin_and_live_setting_blocked():
    with pytest.raises(ConfigurationError):
        read_only_guard(httpx.Request("POST", "https://example.com/oauth2/token"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_trading_enabled=True)


async def test_token_refresh_and_concurrent_reuse(settings, monkeypatch):
    issued = []

    def handler(req):
        if req.url.path == "/oauth2/token":
            issued.append(1)
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{len(issued)}",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                },
            )
        return httpx.Response(200, json={"result": []})

    broker = make_broker(settings, handler, monkeypatch)
    try:
        await asyncio.gather(broker.accounts(), broker.accounts())
        assert len(issued) == 1
        broker._expires = 0
        await broker.accounts()
        assert len(issued) == 2
    finally:
        await broker.aclose()


async def test_invalid_payload(settings, monkeypatch):
    broker = make_broker(settings, lambda req: httpx.Response(200, json={}), monkeypatch)
    try:
        with pytest.raises(BrokerError):
            await broker.accounts()
    finally:
        await broker.aclose()


async def test_query_response_preserves_precision_and_pagination(settings, monkeypatch):
    responses = {
        "/api/v1/accounts": [{"accountSeq": 7, "accountNo": "dummy", "accountType": "BROKERAGE"}],
        "/api/v1/holdings": {"items": [{"symbol": "AAPL", "quantity": "0.123456789"}]},
        "/api/v1/buying-power": {"currency": "USD", "cashBuyingPower": "12345.67"},
        "/api/v1/orders": {"orders": [], "nextCursor": "opaque=", "hasNext": True},
        "/api/v1/orders/example": {"orderId": "example", "status": "PARTIAL_FILLED"},
    }

    def handler(req):
        return httpx.Response(200, json={"result": responses[req.url.path]})

    broker = make_broker(settings, handler, monkeypatch)
    broker._token, broker._expires = "mock-token", float("inf")
    try:
        assert (await broker.accounts())[0]["accountSeq"] == 7
        assert (await broker.holdings())["items"][0]["quantity"] == "0.123456789"
        assert (await broker.buying_power())["cashBuyingPower"] == "12345.67"
        assert (await broker.orders(status="CLOSED"))["nextCursor"] == "opaque="
        assert (await broker.order("example"))["status"] == "PARTIAL_FILLED"
    finally:
        await broker.aclose()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_authenticated_query_failure_does_not_retry(settings, monkeypatch, status):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, headers={"Retry-After": "1"})

    broker = make_broker(settings, handler, monkeypatch)
    broker._token, broker._expires = "mock-token", float("inf")
    try:
        with pytest.raises(BrokerError) as error:
            await broker.orders()
        assert error.value.status == status
        assert len(seen) == 1
        assert seen[0].method == "GET"
    finally:
        await broker.aclose()


def test_empty_example_environment_is_loadable():
    settings = Settings(_env_file=None, toss_account_seq="", operations_api_key="")
    assert settings.toss_account_seq is None
    assert settings.operations_api_key is None


async def test_timeout(settings, monkeypatch):
    def timeout(req):
        raise httpx.ReadTimeout("sensitive-url", request=req)

    broker = make_broker(settings, timeout, monkeypatch)
    try:
        with pytest.raises(BrokerError, match="status=None"):
            await broker.accounts()
    finally:
        await broker.aclose()
