import httpx
import pytest
from fastapi.testclient import TestClient

from toss_trading.adapters.toss import TossQueries
from toss_trading.api import create_app
from toss_trading.config import Settings


def test_auth_read_queries_and_no_order_submission(settings):
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                },
            )
        return httpx.Response(
            200,
            json={
                "result": [
                    {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD", "timestamp": None},
                ]
            },
        )

    broker = TossQueries(settings, transport=httpx.MockTransport(handler))
    # Lifespan owns only adapters it creates. Close this injected adapter in the same loop.
    with TestClient(create_app(settings, broker)) as client:
        assert client.get("/health").json()["live_trading_enabled"] is False
        assert client.get("/v1/accounts").status_code == 401
        assert not seen
        headers = {"X-API-Key": settings.operations_api_key.get_secret_value()}
        response = client.get("/v1/prices?symbols=AAPL", headers=headers)
        assert response.json()["result"][0]["lastPrice"] == "185.70"
        assert response.headers["Cache-Control"] == "no-store"
        assert client.post("/v1/orders", headers=headers, json={}).status_code == 405
        assert client.get("/v1/orders?status=BAD", headers=headers).status_code == 422
        client.portal.call(broker.aclose)
    assert len(seen) == 2


def test_no_credentials_health_and_fail_closed_auth():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/accounts").status_code == 503


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_upstream_errors_are_sanitized(settings, status):
    broker = TossQueries(
        settings,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json={"secret": "account-and-token"}),
        ),
    )
    with TestClient(create_app(settings, broker)) as client:
        response = client.get(
            "/v1/accounts",
            headers={
                "X-API-Key": settings.operations_api_key.get_secret_value(),
            },
        )
        assert response.status_code == 502
        assert response.json()["upstream_status"] == status
        assert "account-and-token" not in response.text
        client.portal.call(broker.aclose)
