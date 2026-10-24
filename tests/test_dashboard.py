from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toss_trading.api import create_app
from toss_trading.config import Settings
from toss_trading.domain import Order, Position
from toss_trading.persistence import Store

PASSWORD = "dashboard-test-password-only"
CONFIG = Path(__file__).parents[1] / "config" / "strategy-legacy.toml"


def dashboard_settings(store, **kwargs):
    return Settings(
        _env_file=None,
        dashboard_password=PASSWORD,
        paper_database_url=str(store.engine.url),
        strategy_config_path=str(CONFIG),
        **kwargs,
    )


@pytest.mark.parametrize("path", ["/dashboard/", "/dashboard/data", "/dashboard/app.js"])
def test_dashboard_requires_separate_credentials(store, path):
    settings = dashboard_settings(store, operations_api_key="operations-key-32-characters-long")
    with TestClient(create_app(settings)) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, auth=("admin", "wrong-password")).status_code == 401
        assert (
            client.get(path, headers={"x-api-key": "operations-key-32-characters-long"}).status_code
            == 401
        )
        response = client.get(path, auth=("admin", PASSWORD))
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert PASSWORD not in response.text
        assert client.get("/v1/accounts", auth=("admin", PASSWORD)).status_code == 401


def test_dashboard_metrics_pending_orders_and_no_broker_calls(store):
    now = datetime.now(UTC)
    rev, state = store.load()
    state.cash = Decimal("8999")
    state.last_event = now
    state.marks = {"SOXL": Decimal("110")}
    state.mark_times = {"SOXL": now}
    state.positions["SOXL"] = Position(
        symbol="SOXL",
        quantity=10,
        entry=100,
        entry_fees=1,
        stop=90,
        target=130,
        opened=now,
        initial_risk=100,
    )
    order = Order(
        id="pending",
        symbol="TQQQ",
        side="BUY",
        quantity=2,
        submitted=now,
        limit=50,
        atr=2,
        reason="ENTRY",
        reserved=101,
    )
    state.orders[order.id] = order
    store.commit(rev, state, [{"event": "ENTRY_REJECTED", "reason": "OPPOSING_GROUP", "time": now}])
    with TestClient(create_app(dashboard_settings(store))) as client:
        data = client.get("/dashboard/data", auth=("admin", PASSWORD)).json()
        assert data["summary"]["equity"] == "10099"
        assert data["summary"]["unrealized"] == "99"
        assert data["summary"]["total_pnl"] == "99"
        assert data["summary"]["reserved"] == "101"
        assert data["positions"][0]["unrealized"] == "99"
        assert data["engine_status"] == "recent"
        assert data["orders"][0]["id"] == "pending"
        assert data["events"][0]["reason"] == "OPPOSING_GROUP"
        assert "bars" not in data
        assert data["compare_available"] is False
        assert (
            client.get("/dashboard/data?ledger=compare", auth=("admin", PASSWORD)).status_code
            == 404
        )
        assert client.get("/dashboard/data?ledger=bad", auth=("admin", PASSWORD)).status_code == 422
    # Read requests must not mutate the ledger.
    assert store.load()[0] == rev + 1


def test_dashboard_uninitialized_and_delayed_state(store):
    settings = dashboard_settings(store)
    with TestClient(create_app(settings)) as client:
        assert (
            client.get("/dashboard/data", auth=("admin", PASSWORD)).json()["engine_status"]
            == "unknown"
        )
        rev, state = store.load()
        state.last_event = datetime.now(UTC) - timedelta(hours=1)
        store.commit(rev, state, [])
        assert (
            client.get("/dashboard/data", auth=("admin", PASSWORD)).json()["engine_status"]
            == "delayed"
        )


def test_dashboard_compare_uses_separate_ledger(store, tmp_path, config):
    url = f"sqlite:///{tmp_path}/compare.db"
    compare = Store(url, config)
    try:
        rev, state = compare.load()
        state.cash = Decimal("12000")
        compare.commit(rev, state, [])
        settings = dashboard_settings(
            store, compare_config_path=str(CONFIG), compare_database_url=url
        )
        with TestClient(create_app(settings)) as client:
            assert (
                client.get("/dashboard/data", auth=("admin", PASSWORD)).json()["summary"]["cash"]
                == "10000"
            )
            data = client.get("/dashboard/data?ledger=compare", auth=("admin", PASSWORD)).json()
            assert data["summary"]["cash"] == "12000"
            assert data["compare_available"] is True
    finally:
        compare.close()


def test_dashboard_missing_ledger_fails_without_creating_tables(tmp_path):
    settings = Settings(
        _env_file=None,
        dashboard_password=PASSWORD,
        paper_database_url=f"sqlite:///{tmp_path}/missing.db",
        strategy_config_path=str(CONFIG),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/dashboard/data", auth=("admin", PASSWORD))
        assert response.status_code == 503
        assert str(tmp_path) not in response.text


def test_dashboard_configuration_mismatch_is_sanitized(store):
    settings = dashboard_settings(store)
    settings.strategy_config_path = str(CONFIG.with_name("strategy.toml"))
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard/data", auth=("admin", PASSWORD)).status_code == 503


def test_dashboard_password_missing_is_disabled(store):
    settings = dashboard_settings(store)
    settings.dashboard_password = None
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard/", auth=("admin", PASSWORD)).status_code == 503
